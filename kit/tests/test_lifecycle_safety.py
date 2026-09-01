"""Failure-injection tests for transactional App lifecycle cleanup."""
from __future__ import annotations

import pytest

import kit.app as kit_app
import kit.config as kit_config
from kit.errors import AdapterError


class LifecycleAbort(BaseException):
    """Control-flow failure whose identity must survive cleanup."""


class CountingModel:
    def __init__(self) -> None:
        self.release_calls = 0

    def release(self) -> None:
        self.release_calls += 1


class CountingSource:
    def __init__(self) -> None:
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


class CountingSink:
    def __init__(self) -> None:
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


class TransactionApp(kit_app.App):
    id = "transaction-test"
    owns_loop = True

    def __init__(self) -> None:
        super().__init__()
        self.finish_calls = 0

    def run(self) -> None:
        pass

    def finish(self) -> None:
        self.finish_calls += 1
        super().finish()


def _manifest(model_count: int = 1) -> dict:
    return {
        "id": "transaction-test",
        "models": [
            {"id": f"model-{index}", "file": f"models/{index}.rknn"}
            for index in range(model_count)
        ],
    }


def _start(app: TransactionApp, manifest: dict, **kwargs):
    return app.start(
        None,
        app_dir="/installed/transaction-test",
        manifest=manifest,
        config={},
        verbose=False,
        **kwargs,
    )


def test_second_model_failure_releases_first_and_finish_is_idempotent():
    error = LifecycleAbort("second model failed")
    first = CountingModel()
    app = TransactionApp()
    calls = 0

    def load_model(_path):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise error
        return first

    app._load_model = load_model
    with pytest.raises(LifecycleAbort) as caught:
        _start(app, _manifest(2))

    assert caught.value is error
    assert app.finish_calls == 1
    assert first.release_calls == 1
    assert len(app.models) == 0

    app.finish()
    assert app.finish_calls == 2
    assert first.release_calls == 1


def test_setup_failure_model_release_counts_are_exact():
    error = LifecycleAbort("setup failed")
    loaded = [CountingModel(), CountingModel()]
    queue = list(loaded)
    app = TransactionApp()
    app._load_model = lambda _path: queue.pop(0)
    app.setup = lambda _config: (_ for _ in ()).throw(error)

    with pytest.raises(LifecycleAbort) as caught:
        _start(app, _manifest(2))

    assert caught.value is error
    assert [model.release_calls for model in loaded] == [1, 1]
    app.finish()
    assert [model.release_calls for model in loaded] == [1, 1]


def test_source_open_failure_rolls_back_models(monkeypatch):
    error = LifecycleAbort("source open failed")
    model = CountingModel()
    app = TransactionApp()
    app._load_model = lambda _path: model
    monkeypatch.setattr(kit_app, "open_frame_source",
                        lambda **_kwargs: (_ for _ in ()).throw(error))

    with pytest.raises(LifecycleAbort) as caught:
        _start(app, _manifest())

    assert caught.value is error
    assert app.finish_calls == 1
    assert model.release_calls == 1


def test_failure_after_source_and_owned_sink_closes_everything_once(monkeypatch):
    error = LifecycleAbort("handler install failed")
    model = CountingModel()
    source = CountingSource()
    sink = CountingSink()

    class HandlerFailureApp(TransactionApp):
        def _install_reload_handler(self) -> None:
            raise error

    app = HandlerFailureApp()
    app._load_model = lambda _path: model
    monkeypatch.setattr(kit_app, "open_frame_source", lambda **_kwargs: source)
    monkeypatch.setattr(kit_app, "open_result_sink", lambda *_args, **_kwargs: sink)

    with pytest.raises(LifecycleAbort) as caught:
        _start(app, _manifest(), sink=None)

    assert caught.value is error
    assert app.finish_calls == 1
    assert model.release_calls == 1
    assert source.close_calls == 1
    assert sink.close_calls == 1

    app.finish()
    assert model.release_calls == 1
    assert source.close_calls == 1
    assert sink.close_calls == 1


def test_prepare_runtime_failure_is_inside_start_transaction(monkeypatch):
    error = LifecycleAbort("app-owned runtime preparation failed")
    model = CountingModel()
    source = CountingSource()
    sink = CountingSink()

    class PrepareFailureApp(TransactionApp):
        def prepare_runtime(self) -> None:
            raise error

    app = PrepareFailureApp()
    app._load_model = lambda _path: model
    monkeypatch.setattr(kit_app, "open_frame_source", lambda **_kwargs: source)
    monkeypatch.setattr(kit_app, "open_result_sink", lambda *_args, **_kwargs: sink)

    with pytest.raises(LifecycleAbort) as caught:
        _start(app, _manifest(), sink=None)

    assert caught.value is error
    assert app.finish_calls == 1
    assert model.release_calls == 1
    assert source.close_calls == 1
    assert sink.close_calls == 1

    app.finish()
    assert model.release_calls == 1
    assert source.close_calls == 1
    assert sink.close_calls == 1


class RunAppPhaseProbe(kit_app.App):
    id = "run-app-phase-probe"
    owns_loop = True
    needs_model = False
    needs_frames = False

    def __init__(self, phase: str, error: BaseException,
                 finish_error: BaseException | None = None) -> None:
        super().__init__()
        self.phase = phase
        self.error = error
        self.finish_error = finish_error
        self.start_calls = 0
        self.run_calls = 0
        self.finish_calls = 0

    def start(self, *_args, **_kwargs):
        self.start_calls += 1
        if self.phase == "start":
            raise self.error
        return self

    def run(self) -> None:
        self.run_calls += 1
        if self.phase == "run":
            raise self.error

    def finish(self) -> None:
        self.finish_calls += 1
        self._lifecycle_cleaned = True
        if self.finish_error is not None:
            raise self.finish_error


def _patch_run_app_infrastructure(monkeypatch, sink: CountingSink,
                                  ready_error: BaseException | None = None):
    monkeypatch.setattr(kit_config, "app_dir_of", lambda _app: "/fake/app")
    monkeypatch.setattr(kit_config, "load_manifest", lambda *_a, **_k: {})
    monkeypatch.setattr(kit_config, "effective_config", lambda *_a, **_k: {})
    monkeypatch.setattr(kit_app, "open_result_sink", lambda *_a, **_k: sink)
    monkeypatch.setattr(kit_app, "_maybe_open_mqtt_sink",
                        lambda *_a, **_k: None)

    from kit.adapters import output_sink
    monkeypatch.setattr(output_sink, "assemble_output_sink",
                        lambda *_a, **_k: (None, False))

    if ready_error is None:
        monkeypatch.setattr(kit_app, "_signal_ready", lambda: None)
    else:
        monkeypatch.setattr(
            kit_app,
            "_signal_ready",
            lambda: (_ for _ in ()).throw(ready_error),
        )


@pytest.mark.parametrize("phase", ["start", "ready", "run"])
def test_run_app_phase_failure_finishes_once_and_preserves_error(
    monkeypatch, phase
):
    error = LifecycleAbort(f"{phase} failed")
    sink = CountingSink()
    ready_error = error if phase == "ready" else None
    _patch_run_app_infrastructure(monkeypatch, sink, ready_error)
    app = RunAppPhaseProbe(phase, error)

    with pytest.raises(LifecycleAbort) as caught:
        kit_app.run_app(app, ["--sink", "stdout", "--quiet"])

    assert caught.value is error
    assert app.start_calls == 1
    assert app.run_calls == (1 if phase == "run" else 0)
    assert app.finish_calls == 1
    assert sink.close_calls == 1


def test_run_app_cleanup_failure_does_not_replace_run_failure(monkeypatch):
    error = LifecycleAbort("run failed")
    cleanup_error = SystemExit(91)
    sink = CountingSink()
    _patch_run_app_infrastructure(monkeypatch, sink)
    app = RunAppPhaseProbe("run", error, finish_error=cleanup_error)

    with pytest.raises(LifecycleAbort) as caught:
        kit_app.run_app(app, ["--sink", "stdout", "--quiet"])

    assert caught.value is error
    assert app.finish_calls == 1
    assert sink.close_calls == 1


def test_run_app_never_signals_ready_when_prepare_runtime_fails(monkeypatch):
    error = LifecycleAbort("audio runtime prepare failed")
    sink = CountingSink()
    _patch_run_app_infrastructure(monkeypatch, sink)
    ready_calls = []
    monkeypatch.setattr(kit_app, "_signal_ready",
                        lambda: ready_calls.append(True))

    class PrepareProbe(kit_app.App):
        id = "prepare-ready-probe"
        owns_loop = True
        needs_model = False
        needs_frames = False

        def prepare_runtime(self) -> None:
            raise error

        def run(self) -> None:
            raise AssertionError("run must not start after prepare failure")

    with pytest.raises(LifecycleAbort) as caught:
        kit_app.run_app(PrepareProbe(), ["--sink", "stdout", "--quiet"])

    assert caught.value is error
    assert ready_calls == []
    assert sink.close_calls == 1


def test_run_app_output_assembly_failure_closes_primary_sink(monkeypatch):
    error = LifecycleAbort("output assembly failed")
    sink = CountingSink()
    _patch_run_app_infrastructure(monkeypatch, sink)
    from kit.adapters import output_sink
    monkeypatch.setattr(
        output_sink,
        "assemble_output_sink",
        lambda *_a, **_k: (_ for _ in ()).throw(error),
    )
    app = RunAppPhaseProbe("run", LifecycleAbort("must not run"))

    with pytest.raises(LifecycleAbort) as caught:
        kit_app.run_app(app, ["--sink", "stdout", "--quiet"])

    assert caught.value is error
    assert app.start_calls == 0
    assert app.finish_calls == 0
    assert sink.close_calls == 1


def test_finish_attempts_every_resource_and_reports_cleanup_failures(monkeypatch):
    class FailingModel(CountingModel):
        def release(self):
            super().release()
            raise RuntimeError("model release failed")

    class FailingSource(CountingSource):
        def close(self):
            super().close()
            raise RuntimeError("source close failed")

    class FailingSink(CountingSink):
        def close(self):
            super().close()
            raise RuntimeError("sink close failed")

    model = FailingModel()
    source = FailingSource()
    sink = FailingSink()
    app = TransactionApp()
    app._load_model = lambda _path: model
    monkeypatch.setattr(kit_app, "open_frame_source", lambda **_kwargs: source)
    monkeypatch.setattr(kit_app, "open_result_sink",
                        lambda *_args, **_kwargs: sink)
    _start(app, _manifest(), sink=None)

    with pytest.raises(AdapterError) as caught:
        app.finish()

    assert caught.value.operation == "app.finish"
    assert [item["resource"] for item in caught.value.details["failures"]] == [
        "frame_source", "model:model-0", "result_sink",
    ]
    assert source.close_calls == 1
    assert model.release_calls == 1
    assert sink.close_calls == 1

    # Detach-before-close keeps cleanup idempotent even when all callbacks fail.
    app.finish()
    assert source.close_calls == 1
    assert model.release_calls == 1
    assert sink.close_calls == 1
