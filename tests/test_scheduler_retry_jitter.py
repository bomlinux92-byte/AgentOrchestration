"""Regression tests for issue #1546 — queue runtime retry/idempotency bugs.

Tests three bug fixes:
1. Retries use jitter (exponential backoff with random noise), not immediate enqueue.
2. Scheduled tasks persist (task_dict, deadline) pairs — deadline expiry carries payload.
3. Terminal outcomes are recorded and block duplicate state mutations (idempotency guard).
"""

import asyncio
import random
import sys
import time

sys.path.insert(0, "src")

from orchestrator.scheduler import TaskScheduler


class TestRetryJitter:
    """Verify retry delays use jitter rather than immediate re-enqueue."""

    def test_fail_reenqueues_with_delay_not_immediately(self):
        """
        When fail() is called, the task should be scheduled with a delay,
        not immediately pushed back onto the queue.
        """
        sched = TaskScheduler()
        task_id = sched.enqueue({"type": "test"}, queue="default", priority=1)

        # Dequeue so it's in-flight.
        async def get_task():
            return await sched.dequeue(timeout=0.1)

        task = asyncio.run(get_task())
        assert task is not None
        assert task["id"] == task_id

        # fail() should not put the task back onto the queue synchronously.
        was_reenqueued = sched.fail(task_id)
        assert was_reenqueued is True

        # The task must NOT be immediately on the queue (that's the bug).
        immediate_task = sched._queues["default"].pop()
        assert immediate_task is None, "Task was immediately re-enqueued — jitter not applied"

        # The task should be in _scheduled with a future deadline.
        assert task_id in sched._scheduled
        task_payload, deadline = sched._scheduled[task_id]
        assert task_payload["id"] == task_id
        assert deadline > time.time(), "Deadline must be in the future"

    def test_fail_applies_exponential_backoff_jitter(self):
        """
        Verify jitter delay grows exponentially with retry attempt.

        We test the deterministic base by patching random.uniform to always
        return 0 (giving zero jitter), so the delay is purely the exponential base.
        """
        sched = TaskScheduler()

        original_uniform = random.uniform
        try:
            random.uniform = lambda a, b: 0.0  # Zero out jitter → pure exponential.
            delays = []
            for attempt in range(sched.max_retries):
                delay = sched._jitter_delay(attempt)
                delays.append(delay)
            # With jitter removed, delays follow exact exponential growth.
            for i in range(1, len(delays)):
                assert delays[i] > delays[i - 1], f"Exponential growth expected: {delays}"
        finally:
            random.uniform = original_uniform

    def test_fail_returns_false_when_max_retries_exceeded(self):
        """Once max_retries is exhausted, fail() returns False and task has terminal outcome."""
        sched = TaskScheduler()
        sched._max_retries = 1

        task_id = sched.enqueue({"type": "test"})

        async def get():
            return await sched.dequeue(timeout=0.1)

        task = asyncio.run(get())  # in _in_flight

        # First failure: budget exhausted → terminal failed, not re-scheduled.
        result = sched.fail(task["id"])
        assert result is False
        assert sched.get_outcome(task["id"]) == "failed"
        assert task["id"] not in sched._in_flight


class TestScheduledTaskPayload:
    """Verify scheduled tasks store task payload, not just a timestamp."""

    def test_schedule_stores_task_payload(self):
        """schedule() must persist the full task dict, not just the deadline."""
        sched = TaskScheduler()
        task = {"type": "scheduled", "priority": 5}
        task_id = sched.schedule(task, delay=10.0)

        assert task_id in sched._scheduled
        stored_task, deadline = sched._scheduled[task_id]

        # The stored task must be the full task dict.
        assert stored_task["type"] == "scheduled"
        assert stored_task["priority"] == 5
        assert stored_task["id"] == task_id

    def test_dequeue_moves_scheduled_task_to_queue(self):
        """Expired scheduled tasks must be moved to the queue with correct payload."""
        sched = TaskScheduler()
        task = {"type": "deferred"}
        task_id = sched.schedule(task, delay=0.01)

        # Wait for expiration.
        time.sleep(0.05)

        async def drain():
            return await sched.dequeue(timeout=0.1)

        dequeued = asyncio.run(drain())

        assert dequeued is not None
        assert dequeued["id"] == task_id
        assert dequeued["type"] == "deferred"
        # Task must no longer be in scheduled.
        assert task_id not in sched._scheduled

    def test_dequeue_does_not_restore_task_lost_bug(self):
        """
        Before the fix, _scheduled[task_id] stored just time.time() + delay.
        When expired, that float was popped and passed to enqueue(), which
        assigned a NEW id.  The original task was lost.

        This test verifies the fix: the original task payload survives expiry.
        """
        sched = TaskScheduler()
        original_task = {"type": "original_payload", "priority": 99}
        task_id = sched.schedule(original_task, delay=0.01)

        time.sleep(0.05)

        async def get():
            return await sched.dequeue(timeout=0.1)

        restored = asyncio.run(get())

        assert restored is not None
        assert restored["id"] == task_id
        assert restored["type"] == "original_payload"
        assert restored["priority"] == 99


class TestIdempotencyGuard:
    """Verify terminal outcomes prevent duplicate state mutations."""

    def test_complete_idempotent(self):
        """Calling complete() twice on the same task is a no-op."""
        sched = TaskScheduler()
        task_id = sched.enqueue({"type": "test"})
        async def get():
            return await sched.dequeue(timeout=0.1)

        task = asyncio.run(get())
        assert sched.complete(task["id"]) is True
        assert sched.get_outcome(task["id"]) == "completed"
        # Second call must return False.
        assert sched.complete(task["id"]) is False

    def test_complete_after_fail_is_rejected(self):
        """Once a task has been failed (terminal), it cannot be completed."""
        sched = TaskScheduler()
        task_id = sched.enqueue({"type": "test"})
        async def get():
            return await sched.dequeue(timeout=0.1)

        task = asyncio.run(get())  # task is now in _in_flight

        # Exhaust retry budget → terminal "failed" outcome.
        sched._max_retries = 1
        sched.fail(task["id"])   # Retry 1: budget exhausted → terminal failed
        # Task is no longer in _in_flight (already popped by fail).

        # Now try to complete the same task — must be rejected.
        result = sched.complete(task["id"])
        assert result is False
        assert sched.get_outcome(task["id"]) == "failed"

    def test_fail_after_complete_is_rejected(self):
        """Once completed, a task cannot be failed."""
        sched = TaskScheduler()
        task_id = sched.enqueue({"type": "test"})
        async def get():
            return await sched.dequeue(timeout=0.1)

        task = asyncio.run(get())
        sched.complete(task["id"])

        result = sched.fail(task["id"])
        assert result is False
        assert sched.get_outcome(task["id"]) == "completed"

    def test_cancel_idempotent(self):
        """cancel() is idempotent — calling it twice is a no-op."""
        sched = TaskScheduler()
        task_id = sched.enqueue({"type": "test"})
        assert sched.cancel(task_id) is True
        assert sched.cancel(task_id) is True  # Already cancelled
        assert sched.get_outcome(task_id) == "cancelled"

    def test_cancel_completed_task_returns_true(self):
        """Cancelling an already-completed task returns True but doesn't change outcome."""
        sched = TaskScheduler()
        task_id = sched.enqueue({"type": "test"})
        async def get():
            return await sched.dequeue(timeout=0.1)

        task = asyncio.run(get())
        sched.complete(task["id"])

        result = sched.cancel(task_id)
        assert result is True
        assert sched.get_outcome(task_id) == "completed"  # Unchanged

    def test_cancel_removes_from_in_flight(self):
        """cancel() removes the task from in_flight immediately."""
        sched = TaskScheduler()
        task_id = sched.enqueue({"type": "test"})
        async def get():
            return await sched.dequeue(timeout=0.1)

        task = asyncio.run(get())
        assert task["id"] in sched._in_flight
        sched.cancel(task["id"])
        assert task["id"] not in sched._in_flight

    def test_set_outcome_rejects_conflicting_outcome(self):
        """Setting a different outcome on an already-outcomed task raises."""
        sched = TaskScheduler()
        task_id = sched.enqueue({"type": "test"})
        async def get():
            return await sched.dequeue(timeout=0.1)

        task = asyncio.run(get())
        sched.complete(task["id"])

        # Try to set a different outcome.
        try:
            sched._set_outcome(task_id, "failed")
            assert False, "Should have raised ValueError"
        except ValueError as e:
            assert "already has outcome" in str(e)

    def test_no_orphaned_locks_after_fail_exhausted(self):
        """
        When retries are exhausted, the task must not remain in _in_flight.
        """
        sched = TaskScheduler()
        sched._max_retries = 1
        task_id = sched.enqueue({"type": "test"})
        async def get():
            return await sched.dequeue(timeout=0.1)

        task = asyncio.run(get())
        assert task_id in sched._in_flight
        sched.fail(task_id)  # Retry 1
        sched.fail(task_id)  # Retry 2 — exhausted

        assert task_id not in sched._in_flight
        assert sched.get_outcome(task_id) == "failed"


if __name__ == "__main__":
    import pytest

    print("Running issue #1546 regression tests...\n")

    tests = TestRetryJitter()
    tests.test_fail_reenqueues_with_delay_not_immediately()
    print("PASS: fail reenqueues with delay, not immediately")
    tests.test_fail_applies_exponential_backoff_jitter()
    print("PASS: fail applies exponential backoff jitter")
    tests.test_fail_returns_false_when_max_retries_exceeded()
    print("PASS: fail returns False when max retries exceeded")

    tests2 = TestScheduledTaskPayload()
    tests2.test_schedule_stores_task_payload()
    print("PASS: schedule stores task payload")
    tests2.test_dequeue_moves_scheduled_task_to_queue()
    print("PASS: dequeue moves scheduled task to queue")
    tests2.test_dequeue_does_not_restore_task_lost_bug()
    print("PASS: scheduled task payload survives expiry (lost-task bug fixed)")

    tests3 = TestIdempotencyGuard()
    tests3.test_complete_idempotent()
    print("PASS: complete is idempotent")
    tests3.test_complete_after_fail_is_rejected()
    print("PASS: complete after fail is rejected")
    tests3.test_fail_after_complete_is_rejected()
    print("PASS: fail after complete is rejected")
    tests3.test_cancel_idempotent()
    print("PASS: cancel is idempotent")
    tests3.test_cancel_completed_task_returns_true()
    print("PASS: cancel completed task returns True")
    tests3.test_cancel_removes_from_in_flight()
    print("PASS: cancel removes from in_flight")
    tests3.test_set_outcome_rejects_conflicting_outcome()
    print("PASS: set_outcome rejects conflicting outcome")
    tests3.test_no_orphaned_locks_after_fail_exhausted()
    print("PASS: no orphaned locks after fail exhausted")

    print("\nAll regression tests passed!")
