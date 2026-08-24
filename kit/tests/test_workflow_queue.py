"""Host tests for bounded workflow input queues and backpressure."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

import pytest

from kit.errors import ConfigurationError
from kit.workflow import (
    DropPolicy,
    InputQueue,
    PutStatus,
    QueueClosedError,
    QueueTimeoutError,
    WorkflowBackpressureError,
)


pytestmark = pytest.mark.host


def _wait_until(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            pytest.fail("timed out waiting for worker state")
        threading.Event().wait(0.005)


def test_block_policy_timeout_is_typed_and_counted_exactly() -> None:
    queue: InputQueue[str] = InputQueue(1, DropPolicy.BLOCK)
    assert queue.put("first").status is PutStatus.ENQUEUED

    with pytest.raises(WorkflowBackpressureError) as caught:
        queue.put("second", timeout=0)

    error = caught.value
    assert error.operation == "workflow.queue.put"
    assert error.retryable is True
    assert error.details["capacity"] == 1
    assert error.details["size"] == 1
    assert error.details["policy"] == "block"
    assert error.details["timeout"] == 0.0
    stats = queue.stats()
    assert stats.put_attempts == 2
    assert stats.enqueued == 1
    assert stats.put_waits == 1
    assert stats.put_timeouts == 1
    assert stats.size == 1
    assert stats.high_watermark == 1


def test_empty_get_timeout_is_typed_and_counted_exactly() -> None:
    queue: InputQueue[str] = InputQueue(2)
    with pytest.raises(QueueTimeoutError) as caught:
        queue.get_nowait()

    assert caught.value.operation == "workflow.queue.get"
    assert caught.value.retryable is True
    assert caught.value.details["timeout"] == 0.0
    stats = queue.stats()
    assert stats.get_attempts == 1
    assert stats.get_waits == 1
    assert stats.get_timeouts == 1
    assert stats.dequeued == 0


def test_blocked_put_succeeds_when_consumer_releases_capacity() -> None:
    queue: InputQueue[str] = InputQueue(1)
    queue.put("first")
    result = []
    errors: list[BaseException] = []

    def producer() -> None:
        try:
            result.append(queue.put("second", timeout=1.0))
        except BaseException as exc:  # captured only to make test failures visible
            errors.append(exc)

    worker = threading.Thread(target=producer, name="workflow-blocked-producer")
    worker.start()
    _wait_until(lambda: queue.stats().put_waits == 1)
    assert queue.get(timeout=1.0) == "first"
    worker.join(timeout=2.0)

    assert not worker.is_alive()
    assert errors == []
    assert len(result) == 1
    assert result[0].status is PutStatus.ENQUEUED
    assert result[0].accepted is True
    assert result[0].dropped is False
    assert result[0].waited_seconds >= 0.0
    assert queue.get_nowait() == "second"
    stats = queue.stats()
    assert stats.put_attempts == 2
    assert stats.enqueued == 2
    assert stats.dequeued == 2
    assert stats.put_waits == 1
    assert stats.put_timeouts == 0


def test_drop_oldest_returns_evicted_item_and_preserves_fifo() -> None:
    queue: InputQueue[int] = InputQueue(2, DropPolicy.DROP_OLDEST)
    queue.put(1)
    queue.put(2)
    result = queue.put(3)

    assert result.status is PutStatus.DROPPED_OLDEST
    assert result.accepted is True
    assert result.dropped is True
    assert result.dropped_item == 1
    assert result.size_after == 2
    assert queue.get_nowait() == 2
    assert queue.get_nowait() == 3
    stats = queue.stats()
    assert stats.put_attempts == 3
    assert stats.enqueued == 3
    assert stats.dequeued == 2
    assert stats.dropped_oldest == 1
    assert stats.dropped_newest == 0
    assert stats.dropped == 1
    assert stats.size == 0
    assert stats.enqueued == stats.dequeued + stats.size + stats.dropped_oldest


def test_drop_newest_returns_rejected_item_and_preserves_fifo() -> None:
    queue: InputQueue[int] = InputQueue(2, "drop_newest")
    queue.put(1)
    queue.put(2)
    result = queue.put(3)

    assert result.status is PutStatus.DROPPED_NEWEST
    assert result.accepted is False
    assert result.dropped is True
    assert result.dropped_item == 3
    assert result.size_after == 2
    assert queue.get_nowait() == 1
    assert queue.get_nowait() == 2
    stats = queue.stats()
    assert stats.put_attempts == 3
    assert stats.enqueued == 2
    assert stats.dequeued == 2
    assert stats.dropped_oldest == 0
    assert stats.dropped_newest == 1
    assert stats.dropped == 1
    assert stats.size == 0


def test_close_allows_drain_then_rejects_get_and_put() -> None:
    queue: InputQueue[str] = InputQueue(2)
    queue.put("a")
    queue.put("b")
    close_stats = queue.close()
    assert close_stats.closed is True
    assert close_stats.size == 2
    assert queue.get() == "a"
    assert queue.get() == "b"

    with pytest.raises(QueueClosedError) as get_error:
        queue.get()
    with pytest.raises(QueueClosedError) as put_error:
        queue.put("c")
    assert get_error.value.operation == "workflow.queue.get"
    assert put_error.value.operation == "workflow.queue.put"

    stats = queue.stats()
    assert stats.closed is True
    assert stats.put_attempts == 3
    assert stats.get_attempts == 3
    assert stats.enqueued == 2
    assert stats.dequeued == 2
    assert stats.closed_puts == 1
    assert stats.closed_gets == 1
    assert stats.size == 0


@pytest.mark.parametrize("waiter", ["producer", "consumer"])
def test_close_wakes_blocked_waiters_with_typed_error(waiter: str) -> None:
    queue: InputQueue[str] = InputQueue(1)
    if waiter == "producer":
        queue.put("occupied")
    caught: list[BaseException] = []

    def wait() -> None:
        try:
            if waiter == "producer":
                queue.put("blocked")
            else:
                queue.get()
        except BaseException as exc:  # captured only to make assertion possible
            caught.append(exc)

    worker = threading.Thread(target=wait, name=f"workflow-{waiter}-waiter")
    worker.start()
    if waiter == "producer":
        _wait_until(lambda: queue.stats().put_waits == 1)
    else:
        _wait_until(lambda: queue.stats().get_waits == 1)
    queue.close()
    worker.join(timeout=2.0)

    assert not worker.is_alive()
    assert len(caught) == 1
    assert isinstance(caught[0], QueueClosedError)
    stats = queue.stats()
    if waiter == "producer":
        assert stats.closed_puts == 1
    else:
        assert stats.closed_gets == 1


def test_close_remains_an_admission_barrier_when_space_is_freed() -> None:
    """A blocked put must not slip through a simultaneous drain-and-close."""

    queue: InputQueue[str] = InputQueue(1)
    queue.put("occupied")
    caught: list[BaseException] = []

    def producer() -> None:
        try:
            queue.put("must-not-enter")
        except BaseException as exc:
            caught.append(exc)

    worker = threading.Thread(target=producer)
    worker.start()
    _wait_until(lambda: queue.stats().put_waits == 1)

    # Holding the condition makes the consumer-like removal and close one
    # atomic state transition from the producer's point of view.  This targets
    # the wakeup race without timing assumptions.
    with queue._condition:  # noqa: SLF001 - white-box concurrency regression
        assert queue._items.popleft() == "occupied"  # noqa: SLF001
        queue._dequeued += 1  # noqa: SLF001
        queue._closed = True  # noqa: SLF001
        queue._condition.notify_all()  # noqa: SLF001

    worker.join(timeout=2.0)
    assert not worker.is_alive()
    assert len(caught) == 1
    assert isinstance(caught[0], QueueClosedError)
    assert queue.empty()
    stats = queue.stats()
    assert stats.closed is True
    assert stats.closed_puts == 1
    assert stats.enqueued == 1
    assert stats.dequeued == 1


def test_iteration_drains_a_closed_queue_in_order() -> None:
    queue: InputQueue[int] = InputQueue(3)
    for value in range(3):
        queue.put(value)
    queue.close()

    assert list(queue) == [0, 1, 2]
    stats = queue.stats()
    assert stats.dequeued == 3
    # Iteration performs one final get to observe the closed/drained state.
    assert stats.closed_gets == 1
    assert stats.get_attempts == 4


def test_multiple_producers_and_consumer_preserve_all_blocked_items() -> None:
    producer_count = 3
    items_per_producer = 50
    total = producer_count * items_per_producer
    queue: InputQueue[tuple[int, int]] = InputQueue(8, DropPolicy.BLOCK)
    start = threading.Barrier(producer_count + 2)
    received: list[tuple[int, int]] = []
    errors: list[BaseException] = []
    errors_lock = threading.Lock()

    def report_error(exc: BaseException) -> None:
        with errors_lock:
            errors.append(exc)

    def producer(producer_id: int) -> None:
        try:
            start.wait(timeout=3.0)
            for sequence in range(items_per_producer):
                result = queue.put((producer_id, sequence), timeout=3.0)
                assert result.status is PutStatus.ENQUEUED
        except BaseException as exc:
            report_error(exc)

    def consumer() -> None:
        try:
            start.wait(timeout=3.0)
            for _ in range(total):
                received.append(queue.get(timeout=3.0))
        except BaseException as exc:
            report_error(exc)

    threads = [
        threading.Thread(target=producer, args=(producer_id,))
        for producer_id in range(producer_count)
    ]
    threads.append(threading.Thread(target=consumer))
    for thread in threads:
        thread.start()
    start.wait(timeout=3.0)
    for thread in threads:
        thread.join(timeout=10.0)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert len(received) == total
    assert set(received) == {
        (producer_id, sequence)
        for producer_id in range(producer_count)
        for sequence in range(items_per_producer)
    }
    stats = queue.stats()
    assert stats.put_attempts == total
    assert stats.get_attempts == total
    assert stats.enqueued == total
    assert stats.dequeued == total
    assert stats.size == 0
    assert stats.dropped == 0
    assert 1 <= stats.high_watermark <= stats.capacity
    assert stats.put_timeouts == 0
    assert stats.get_timeouts == 0


@pytest.mark.parametrize(
    ("factory", "field"),
    [
        (lambda: InputQueue(0), "capacity"),
        (lambda: InputQueue(True), "capacity"),
        (lambda: InputQueue(1, "unknown"), "policy"),
        (lambda: InputQueue(1).get(timeout=-1), "timeout"),
        (lambda: InputQueue(1).put("item", timeout=float("inf")), "timeout"),
    ],
)
def test_invalid_queue_configuration_is_typed(factory, field: str) -> None:
    with pytest.raises(ConfigurationError) as caught:
        factory()
    assert caught.value.operation == "workflow.queue.configure"
    assert caught.value.details["field"] == field
