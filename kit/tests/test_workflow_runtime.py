"""Host tests for the deterministic workflow runtime contract."""

from __future__ import annotations

import logging
import threading

import pytest

import kit
from kit.errors import ConfigurationError
from kit.workflow import (
    CancellationToken,
    Pipeline,
    ResourceKind,
    Stage,
    StageError,
    WorkflowCancelled,
    WorkflowCleanupError,
    WorkflowCloseReport,
    WorkflowClosedError,
    WorkflowContext,
    WorkflowError,
    WorkflowResourceError,
    WorkflowTimeout,
)


pytestmark = pytest.mark.host


def test_common_workflow_primitives_are_available_from_package_root() -> None:
    assert kit.Pipeline is Pipeline
    assert kit.Stage is Stage
    assert kit.WorkflowContext is WorkflowContext
    assert kit.WorkflowCloseReport is WorkflowCloseReport
    assert not hasattr(kit, "CloseReport")


class _RecordHandler(logging.Handler):
    """Collect log records without relying on the kit's logger module name."""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _test_logger(name: str) -> tuple[logging.Logger, _RecordHandler]:
    logger = logging.Logger(name, level=logging.DEBUG)
    handler = _RecordHandler()
    logger.addHandler(handler)
    return logger, handler


def test_pipeline_composition_is_lazy_ordered_and_context_aware() -> None:
    seen: list[tuple[int, int | None, str]] = []

    def record(value: int, *, context: WorkflowContext) -> int:
        seen.append((value, context.item_index, context.metadata["app_id"]))
        return value + 1

    pipeline = Stage("double", lambda value: value * 2) | Stage("record", record)
    pipeline = pipeline | Stage("string-length", lambda value: len(str(value)))
    context = WorkflowContext(metadata={"app_id": "detector"})

    inputs = iter((2, 5))
    results = pipeline.run(inputs, context)
    assert seen == []
    assert next(results) == 1
    assert seen == [(4, 0, "detector")]
    assert list(results) == [2]
    assert seen == [(4, 0, "detector"), (10, 1, "detector")]
    assert [stage.name for stage in pipeline.stages] == [
        "double",
        "record",
        "string-length",
    ]


def test_optional_or_variadic_callable_arguments_are_not_hijacked_for_context() -> None:
    optional = Stage("optional", lambda value, offset=3: value + offset)

    def variadic(*values: int) -> tuple[int, ...]:
        return values

    assert Pipeline((optional,)).run_one(4) == 7
    assert Pipeline((Stage("variadic", variadic),)).run_one(4) == (4,)


def test_run_one_uses_explicit_resources_and_fails_closed() -> None:
    camera = object()

    def use_resources(value: str, context: WorkflowContext) -> str:
        assert context.require_resource(ResourceKind.CAMERA) is camera
        assert context.get_resource("npu") == "npu-session"
        return value.upper()

    stage = Stage(
        "infer",
        use_resources,
        requires=frozenset({ResourceKind.CAMERA, ResourceKind.NPU}),
    )
    pipeline = Pipeline((stage,))

    with pytest.raises(WorkflowResourceError) as caught:
        pipeline.run_one(
            "frame",
            WorkflowContext(resources={ResourceKind.CAMERA: camera}),
            item_index=7,
        )

    error = caught.value
    assert error.operation == "workflow.stage.resources"
    assert error.details["stage"] == "infer"
    assert error.details["stage_index"] == 0
    assert error.details["item_index"] == 7
    assert error.details["required_resources"] == ["camera", "npu"]
    assert error.details["missing_resources"] == ["npu"]

    context = WorkflowContext(
        resources={ResourceKind.CAMERA: camera, "npu": "npu-session"}
    )
    assert pipeline.run_one("frame", context) == "FRAME"


def test_stage_error_retains_cause_context_and_structured_log() -> None:
    logger, handler = _test_logger("workflow-stage-error-test")
    backend_error = RuntimeError("backend exploded")

    def fail(_value: object) -> object:
        raise backend_error

    pipeline = Pipeline((Stage("decode", fail),), logger=logger)

    with pytest.raises(StageError) as caught:
        pipeline.run_one({"frame": 3}, item_index=4)

    error = caught.value
    assert error.__cause__ is backend_error
    assert error.operation == "workflow.stage.execute"
    assert error.details["stage"] == "decode"
    assert error.details["stage_index"] == 0
    assert error.details["item_index"] == 4
    assert error.details["item_type"] == "dict"
    assert error.details["cause_type"] == "RuntimeError"
    assert error.details["elapsed_ms"] >= 0.0
    assert len(handler.records) == 1
    record = handler.records[0]
    assert record.levelno == logging.ERROR
    assert record.event == "workflow_error"
    assert record.operation == "workflow.stage.execute"
    assert record.error_code == "workflow_stage_failed"
    assert record.stage == "decode"
    assert record.item_index == 4


def test_successful_items_do_not_emit_per_frame_logs() -> None:
    logger, handler = _test_logger("workflow-success-test")
    pipeline = Pipeline((Stage("identity", lambda value: value),), logger=logger)

    assert list(pipeline.run(range(5))) == list(range(5))
    assert handler.records == []


def test_cancellation_is_checked_before_and_after_stages() -> None:
    token = CancellationToken()
    token.cancel("application stopping")
    pipeline = Pipeline((Stage("unreachable", lambda value: value),))

    with pytest.raises(WorkflowCancelled) as caught:
        pipeline.run_one("frame", WorkflowContext(cancellation=token), item_index=2)
    assert caught.value.details["stage"] is None
    assert caught.value.details["item_index"] == 2
    assert caught.value.details["reason"] == "application stopping"

    token = CancellationToken()

    def request_stop(value: str, context: WorkflowContext) -> str:
        assert context.cancellation.cancel("model unloaded") is True
        assert context.cancellation.cancel("ignored") is False
        return value

    pipeline = Pipeline((Stage("request-stop", request_stop),))
    with pytest.raises(WorkflowCancelled) as caught:
        pipeline.run_one("frame", WorkflowContext(cancellation=token), item_index=9)
    assert caught.value.details["stage"] == "request-stop"
    assert caught.value.details["item_index"] == 9
    assert caught.value.details["reason"] == "model unloaded"


def test_first_nonempty_cancellation_reason_wins() -> None:
    token = CancellationToken()
    assert token.cancel() is True
    assert token.cancel("shutdown") is False
    assert token.cancel("ignored") is False
    assert token.reason == "shutdown"


def test_relative_deadline_is_enforced_at_stage_boundaries() -> None:
    now = [100.0]

    def clock() -> float:
        return now[0]

    def slow_stage(value: str, context: WorkflowContext) -> str:
        assert context.deadline == pytest.approx(100.1)
        now[0] += 0.2
        return value

    pipeline = Pipeline((Stage("slow", slow_stage),), clock=clock)
    with pytest.raises(WorkflowTimeout) as caught:
        pipeline.run_one("frame", timeout=0.1, item_index=6)

    error = caught.value
    assert error.operation == "workflow.run"
    assert error.details["stage"] == "slow"
    assert error.details["item_index"] == 6
    assert error.details["deadline"] == pytest.approx(100.1)
    assert error.details["elapsed_ms"] == pytest.approx(200.0)


def test_earliest_context_or_explicit_deadline_wins() -> None:
    now = [10.0]
    observed: list[float | None] = []

    def clock() -> float:
        return now[0]

    def inspect_deadline(value: int, context: WorkflowContext) -> int:
        observed.append(context.deadline)
        return value

    pipeline = Pipeline((Stage("inspect", inspect_deadline),), clock=clock)
    context = WorkflowContext(deadline=20.0)
    assert pipeline.run_one(1, context, timeout=5.0, deadline=30.0) == 1
    assert observed == [15.0]


def test_base_exception_from_stage_is_not_wrapped_or_swallowed() -> None:
    class Abort(BaseException):
        pass

    signal = Abort("stop process")

    def abort(_value: object) -> object:
        raise signal

    pipeline = Pipeline((Stage("abort", abort),))
    with pytest.raises(Abort) as caught:
        pipeline.run_one(object())
    assert caught.value is signal


def test_close_is_reverse_order_idempotent_and_reports_ordinary_failures() -> None:
    events: list[str] = []

    class Closeable:
        def __init__(self, name: str, error: Exception | None = None) -> None:
            self.name = name
            self.error = error

        def __call__(self, value: object) -> object:
            return value

        def close(self) -> None:
            events.append(self.name)
            if self.error is not None:
                raise self.error

    pipeline = Pipeline(
        (
            Stage("first", Closeable("first")),
            Stage("no-close", lambda value: value),
            Stage("broken", Closeable("broken", RuntimeError("close failed"))),
            Stage("last", Closeable("last")),
        )
    )

    report = pipeline.close()
    assert events == ["last", "broken", "first"]
    assert report.attempted == 3
    assert report.closed == 2
    assert report.skipped == 1
    assert report.ok is False
    assert len(report.failures) == 1
    assert report.failures[0].stage == "broken"
    assert isinstance(report.failures[0].error, RuntimeError)
    assert report.errors == ("broken: RuntimeError: close failed",)

    repeated = pipeline.close()
    assert repeated.already_closed is True
    assert repeated.errors == report.errors
    assert events == ["last", "broken", "first"]
    with pytest.raises(WorkflowClosedError):
        pipeline.run_one("frame")


def test_close_continues_cleanup_then_reraises_base_exception() -> None:
    class Abort(BaseException):
        pass

    events: list[str] = []
    signal = Abort("shutdown now")

    def abort_close() -> None:
        events.append("abort")
        raise signal

    def finish_close() -> None:
        events.append("finish")

    pipeline = Pipeline(
        (
            Stage("finish", lambda value: value, closer=finish_close),
            Stage("abort", lambda value: value, closer=abort_close),
        )
    )

    with pytest.raises(Abort) as caught:
        pipeline.close()
    assert caught.value is signal
    assert events == ["abort", "finish"]
    report = pipeline.close()
    assert report.already_closed is True
    assert report.attempted == 2
    assert report.closed == 1


def test_close_waits_for_active_stage_before_releasing_its_resource() -> None:
    invoked = threading.Event()
    allow_finish = threading.Event()
    closed = threading.Event()
    results: list[str] = []

    def blocking(value: str) -> str:
        invoked.set()
        assert not closed.is_set()
        assert allow_finish.wait(2.0)
        assert not closed.is_set()
        return value

    pipeline = Pipeline((Stage("blocking", blocking, closer=closed.set),))
    runner = threading.Thread(target=lambda: results.append(pipeline.run_one("ok")))
    closer = threading.Thread(target=pipeline.close)
    runner.start()
    assert invoked.wait(1.0)
    closer.start()
    assert not closed.wait(0.05)
    allow_finish.set()
    runner.join(2.0)
    closer.join(2.0)
    assert results == ["ok"]
    assert closed.is_set()


def test_stage_cannot_deadlock_by_closing_its_own_pipeline() -> None:
    holder: dict[str, Pipeline] = {}

    def close_self(value: str) -> str:
        with pytest.raises(WorkflowError) as caught:
            holder["pipeline"].close()
        assert caught.value.code == "reentrant_close"
        return value

    pipeline = Pipeline((Stage("self-close", close_self),))
    holder["pipeline"] = pipeline
    assert pipeline.run_one("ok") == "ok"
    assert pipeline.close().ok


def test_stage_has_one_pipeline_owner_and_composition_transfers_it() -> None:
    events: list[str] = []
    first = Stage("first", lambda value: value, closer=lambda: events.append("first"))
    original = Pipeline((first,))
    with pytest.raises(ConfigurationError):
        Pipeline((first,))

    combined = original | Stage(
        "second", lambda value: value, closer=lambda: events.append("second")
    )
    with pytest.raises(WorkflowClosedError):
        original.run_one("frame")
    assert original.close().already_closed is True
    assert combined.run_one("frame") == "frame"
    assert combined.close().ok
    assert events == ["second", "first"]


def test_failed_stage_composition_does_not_strand_its_lifetime() -> None:
    events: list[str] = []
    stage = Stage("only", lambda value: value, closer=lambda: events.append("close"))
    with pytest.raises(TypeError):
        _ = stage | object()
    assert stage.close() is True
    assert events == ["close"]

    left = Stage("duplicate", lambda value: value)
    right = Stage("duplicate", lambda value: value)
    with pytest.raises(ConfigurationError):
        _ = left | right
    # Validation is transactional; both stages remain directly closable.
    assert left.close() is False
    assert right.close() is False


def test_context_manager_surfaces_cleanup_failure_without_masking_body() -> None:
    broken = Stage(
        "broken",
        lambda value: value,
        closer=lambda: (_ for _ in ()).throw(RuntimeError("close failed")),
    )
    pipeline = Pipeline((broken,))
    with pytest.raises(WorkflowCleanupError) as caught:
        with pipeline:
            assert pipeline.run_one(1) == 1
    assert caught.value.details["errors"] == ["broken: RuntimeError: close failed"]
    assert pipeline.close_report is not None

    body_error = ValueError("body failed")
    pipeline = Pipeline(
        (
            Stage(
                "broken-again",
                lambda value: value,
                closer=lambda: (_ for _ in ()).throw(RuntimeError("cleanup")),
            ),
        )
    )
    with pytest.raises(ValueError) as preserved:
        with pipeline:
            raise body_error
    assert preserved.value is body_error


def test_workflow_error_details_never_call_or_store_item_repr() -> None:
    class SecretItem:
        def __repr__(self) -> str:
            raise AssertionError("repr must not be called")

    pipeline = Pipeline(
        (Stage("failure", lambda _value: (_ for _ in ()).throw(ValueError("bad"))),)
    )
    with pytest.raises(StageError) as caught:
        pipeline.run_one(SecretItem())
    assert caught.value.details["item_type"] == "SecretItem"
    assert "item" not in caught.value.details


@pytest.mark.parametrize(
    ("factory", "field"),
    [
        (lambda: Stage("", lambda value: value), "name"),
        (lambda: Stage("duplicate", lambda value: value) | Stage("duplicate", int), "stages"),
        (lambda: WorkflowContext(metadata={1: "not-a-string-key"}), "metadata"),
        (lambda: WorkflowContext(deadline=float("nan")), "deadline"),
        (lambda: Pipeline().run_one(1, item_index=-1), "item_index"),
    ],
)
def test_invalid_workflow_configuration_is_typed(factory, field: str) -> None:
    with pytest.raises(ConfigurationError) as caught:
        factory()
    assert caught.value.operation == "workflow.configure"
    assert caught.value.details["field"] == field
