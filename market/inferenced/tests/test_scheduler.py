import threading
import time

import pytest

from market.inferenced.scheduler import (
    DeadlineExceededError,
    FairScheduler,
    QueueFullError,
    ScheduledJob,
)


def test_scheduler_serializes_driver_calls_from_multiple_clients():
    scheduler = FairScheduler()
    active = 0
    peak = 0
    lock = threading.Lock()

    def run(value):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.02)
        with lock:
            active -= 1
        return value

    jobs = [
        scheduler.submit(
            ScheduledJob(
                client_id=f"client-{index % 3}",
                priority=50,
                deadline=time.monotonic() + 2,
                execute=lambda index=index: run(index),
            )
        )
        for index in range(9)
    ]
    assert [job.result(2) for job in jobs] == list(range(9))
    assert peak == 1
    scheduler.close()


def test_expired_job_never_enters_driver():
    scheduler = FairScheduler()
    called = False

    def execute():
        nonlocal called
        called = True

    job = scheduler.submit(
        ScheduledJob(
            client_id="expired",
            priority=100,
            deadline=time.monotonic() - 1,
            execute=execute,
        )
    )
    with pytest.raises(DeadlineExceededError):
        job.result(1)
    assert called is False
    scheduler.close()


def test_per_client_queue_is_bounded_while_model_is_rate_limited():
    scheduler = FairScheduler(max_pending_per_client=1)
    future = time.monotonic() + 2
    first = scheduler.submit(
        ScheduledJob(
            client_id="one",
            priority=50,
            deadline=future + 1,
            ready_at=lambda: future,
            execute=lambda: None,
        )
    )
    with pytest.raises(QueueFullError):
        scheduler.submit(
            ScheduledJob(
                client_id="one",
                priority=50,
                deadline=future + 1,
                execute=lambda: None,
            )
        )
    scheduler.cancel_client("one")
    with pytest.raises(RuntimeError, match="disconnected"):
        first.result(1)
    scheduler.close()


def test_multiple_model_connections_share_one_app_fairness_queue():
    scheduler = FairScheduler(max_pending_per_client=1)
    future = time.monotonic() + 2
    first = scheduler.submit(
        ScheduledJob(
            client_id="model-connection-a",
            fairness_id="app:instance:7",
            priority=50,
            deadline=future + 1,
            ready_at=lambda: future,
            execute=lambda: "a",
        )
    )
    with pytest.raises(QueueFullError, match="app:instance:7"):
        scheduler.submit(
            ScheduledJob(
                client_id="model-connection-b",
                fairness_id="app:instance:7",
                priority=50,
                deadline=future + 1,
                execute=lambda: "b",
            )
        )
    assert scheduler.cancel_client("model-connection-a") == 1
    with pytest.raises(RuntimeError, match="disconnected"):
        first.result(1)
    scheduler.close()
