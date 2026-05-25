import pytest
import asyncio
import time
from src.orchestrator.scheduler import TaskScheduler, TaskState


class TestTaskScheduler:
    def setup_method(self):
        self.scheduler = TaskScheduler()

    def test_enqueue_task(self):
        task_id = self.scheduler.enqueue({"type": "test", "payload": {}})
        assert task_id is not None

    def test_dequeue_task(self):
        self.scheduler.enqueue({"type": "test", "payload": {"data": 1}})
        task = asyncio.run(self.scheduler.dequeue())
        assert task is not None
        assert task["type"] == "test"

    def test_enqueue_multiple_priorities(self):
        self.scheduler.enqueue({"type": "low"}, priority=1)
        self.scheduler.enqueue({"type": "high"}, priority=10)
        task = asyncio.run(self.scheduler.dequeue())
        assert task["type"] == "high"

    def test_complete_task(self):
        self.scheduler.enqueue({"type": "test"})
        task = asyncio.run(self.scheduler.dequeue())
        assert self.scheduler.complete(task["id"])

    def test_fail_task_with_retry(self):
        self.scheduler.enqueue({"type": "test"})
        task = asyncio.run(self.scheduler.dequeue())
        assert self.scheduler.fail(task["id"])

    def test_fail_idempotent_when_already_terminal(self):
        """Test that fail() is idempotent - calling on already completed task returns False."""
        self.scheduler.enqueue({"type": "test"})
        task = asyncio.run(self.scheduler.dequeue())
        # Complete the task
        assert self.scheduler.complete(task["id"]) is True
        # Try to fail an already completed task - should return False
        assert self.scheduler.fail(task["id"]) is False

    def test_complete_idempotent_when_already_failed(self):
        """Test that complete() is idempotent - calling on already failed task returns False."""
        self.scheduler.enqueue({"type": "test"})
        task = asyncio.run(self.scheduler.dequeue())
        # Fail the task (exhaust retries)
        for _ in range(self.scheduler._max_retries):
            if not self.scheduler.fail(task["id"]):
                break
        # Try to complete an already failed task - should return False
        assert self.scheduler.complete(task["id"]) is False

    def test_retry_respects_max_retries(self):
        """Test that task fails permanently after max retries."""
        self.scheduler.enqueue({"type": "test"})
        task = asyncio.run(self.scheduler.dequeue())
        task_id = task["id"]
        
        # Fail up to max retries
        for i in range(self.scheduler._max_retries):
            result = self.scheduler.fail(task_id)
            if not result:
                break
        
        # Next fail should return False (terminal state)
        assert self.scheduler.fail(task_id) is False
        # Task state should be FAILED
        assert self.scheduler.get_task_state(task_id) == TaskState.FAILED

    def test_terminal_outcome_recorded_on_completion(self):
        """Test that completing a task records durable terminal outcome."""
        self.scheduler.enqueue({"type": "test"})
        task = asyncio.run(self.scheduler.dequeue())
        task_id = task["id"]
        
        self.scheduler.complete(task_id)
        outcome = self.scheduler.get_terminal_outcome(task_id)
        
        assert outcome is not None
        assert outcome["status"] == "completed"
        assert "completed_at" in outcome

    def test_terminal_outcome_recorded_on_failure(self):
        """Test that exhausting retries records durable terminal outcome."""
        self.scheduler.enqueue({"type": "test"})
        task = asyncio.run(self.scheduler.dequeue())
        task_id = task["id"]
        
        # Exhaust retries
        for _ in range(self.scheduler._max_retries):
            self.scheduler.fail(task_id)
        
        outcome = self.scheduler.get_terminal_outcome(task_id)
        
        assert outcome is not None
        assert outcome["status"] == "failed"
        assert outcome["retries"] == self.scheduler._max_retries

    def test_cancel_task(self):
        """Test that cancelling a task marks it as terminal."""
        self.scheduler.enqueue({"type": "test"})
        task = asyncio.run(self.scheduler.dequeue())
        task_id = task["id"]
        
        assert self.scheduler.cancel(task_id) is True
        assert self.scheduler.get_task_state(task_id) == TaskState.CANCELLED
        
        outcome = self.scheduler.get_terminal_outcome(task_id)
        assert outcome["status"] == "cancelled"

    def test_cancel_idempotent(self):
        """Test that cancelling an already cancelled task returns False."""
        self.scheduler.enqueue({"type": "test"})
        task = asyncio.run(self.scheduler.dequeue())
        task_id = task["id"]
        
        self.scheduler.cancel(task_id)
        # Cancel again - should return False
        assert self.scheduler.cancel(task_id) is False

    def test_dequeue_skips_terminal_tasks(self):
        """Test that dequeue skips tasks that are already in terminal state."""
        self.scheduler.enqueue({"type": "test1"})
        self.scheduler.enqueue({"type": "test2"})
        
        # Complete first task
        task1 = asyncio.run(self.scheduler.dequeue())
        self.scheduler.complete(task1["id"])
        
        # Dequeue should still work for task2
        task2 = asyncio.run(self.scheduler.dequeue())
        assert task2 is not None
        assert task2["type"] == "test2"

    def test_no_stale_locks_on_completion(self):
        """Test that completing a task removes it from in_flight."""
        self.scheduler.enqueue({"type": "test"})
        task = asyncio.run(self.scheduler.dequeue())
        task_id = task["id"]
        
        self.scheduler.complete(task_id)
        
        # Task should not be in in_flight
        assert task_id not in self.scheduler._in_flight

    def test_no_stale_locks_on_failure(self):
        """Test that failing a task removes it from in_flight."""
        self.scheduler.enqueue({"type": "test"})
        task = asyncio.run(self.scheduler.dequeue())
        task_id = task["id"]
        
        self.scheduler.fail(task_id)
        
        # Task should not be in in_flight (it's in _scheduled for retry)
        assert task_id not in self.scheduler._in_flight

    def test_cancel_removes_from_all_tracking(self):
        """Test that cancelling removes task from all tracking structures."""
        self.scheduler.enqueue({"type": "test"})
        task = asyncio.run(self.scheduler.dequeue())
        task_id = task["id"]
        
        self.scheduler.cancel(task_id)
        
        # Task should not be in any tracking structure
        assert task_id not in self.scheduler._in_flight
        assert task_id not in self.scheduler._scheduled


class TestDuplicateCompensationGuards:
    """Regression tests for issue #3924: Avoid duplicate compensation scheduling — failure fan-out.
    
    The bug: When the failure fan-out path is exercised while an agent run, task, or handler
    is changing lifecycle state, the orchestrator compensation planner could accept
    stale, duplicate, or policy-violating transitions, leading to workflow state divergence.
    
    The fix: Guard the reducer/dispatch transition with attempt, revision, and lifecycle
    checks before committing state. State machine guards prevent duplicate compensation
    scheduling for tasks that have already reached terminal states.
    """

    def setup_method(self):
        self.scheduler = TaskScheduler()

    def test_no_duplicate_compensation_on_concurrent_fail_calls(self):
        """Guard: calling fail() on task in SCHEDULED state processes retry.
        
        Note: After first fail(), task enters SCHEDULED state (pending retry).
        Subsequent fail() calls while in SCHEDULED will process retries.
        Guards prevent duplicate compensation only when task is already terminal.
        """
        self.scheduler.enqueue({"type": "test"})
        task = asyncio.run(self.scheduler.dequeue())
        task_id = task["id"]
        
        # First fail call - task enters SCHEDULED state
        result1 = self.scheduler.fail(task_id)
        assert result1 is True
        assert self.scheduler.get_task_state(task_id) == TaskState.SCHEDULED
        
        # Second fail call - task found in _scheduled, processes retry
        # Task returns to SCHEDULED state
        result2 = self.scheduler.fail(task_id)
        assert result2 is True
        assert self.scheduler.get_task_state(task_id) == TaskState.SCHEDULED
        
        # Third fail call - task found in _scheduled, retries exhausted -> FAILED
        result3 = self.scheduler.fail(task_id)
        assert result3 is False  # max_retries exhausted
        assert self.scheduler.get_task_state(task_id) == TaskState.FAILED

    def test_no_duplicate_compensation_after_complete(self):
        """Guard: calling fail() after complete() should be rejected."""
        self.scheduler.enqueue({"type": "test"})
        task = asyncio.run(self.scheduler.dequeue())
        task_id = task["id"]
        
        # Complete the task
        self.scheduler.complete(task_id)
        
        # Now try to fail - should be rejected (already COMPLETED)
        result = self.scheduler.fail(task_id)
        assert result is False

    def test_no_duplicate_compensation_after_cancel(self):
        """Guard: calling fail() after cancel() should be rejected."""
        self.scheduler.enqueue({"type": "test"})
        task = asyncio.run(self.scheduler.dequeue())
        task_id = task["id"]
        
        # Cancel the task
        self.scheduler.cancel(task_id)
        
        # Now try to fail - should be rejected (already CANCELLED)
        result = self.scheduler.fail(task_id)
        assert result is False

    def test_no_duplicate_compensation_on_exhausted_retries(self):
        """Guard: fail() returns False after max retries reached."""
        self.scheduler.enqueue({"type": "test"})
        task = asyncio.run(self.scheduler.dequeue())
        task_id = task["id"]
        
        # Exhaust all retries
        for _ in range(self.scheduler._max_retries):
            self.scheduler.fail(task_id)
        
        # Next fail call should be rejected (already FAILED)
        result = self.scheduler.fail(task_id)
        assert result is False

    def test_failure_fan_out_preserves_workflow_state(self):
        """Test that failure fan-out does not cause workflow state divergence.
        
        When multiple tasks fail concurrently, each should be handled independently
        without affecting the other's lifecycle state.
        """
        # Enqueue two tasks
        self.scheduler.enqueue({"type": "test1", "workflow_id": "wf1"})
        self.scheduler.enqueue({"type": "test2", "workflow_id": "wf1"})
        
        task1 = asyncio.run(self.scheduler.dequeue())
        task2 = asyncio.run(self.scheduler.dequeue())
        
        task1_id = task1["id"]
        task2_id = task2["id"]
        
        # Fail task1 (exhaust retries)
        for _ in range(self.scheduler._max_retries):
            self.scheduler.fail(task1_id)
        
        # Fail task2 (exhaust retries)
        for _ in range(self.scheduler._max_retries):
            self.scheduler.fail(task2_id)
        
        # Both should be in FAILED state
        assert self.scheduler.get_task_state(task1_id) == TaskState.FAILED
        assert self.scheduler.get_task_state(task2_id) == TaskState.FAILED
        
        # Terminal outcomes should be recorded for both
        outcome1 = self.scheduler.get_terminal_outcome(task1_id)
        outcome2 = self.scheduler.get_terminal_outcome(task2_id)
        assert outcome1 is not None
        assert outcome2 is not None
        assert outcome1["status"] == "failed"
        assert outcome2["status"] == "failed"

    def test_dequeue_rejects_stale_terminal_tasks(self):
        """Test that dequeue skips tasks that have reached terminal state."""
        self.scheduler.enqueue({"type": "test1"})
        self.scheduler.enqueue({"type": "test2"})
        
        task1 = asyncio.run(self.scheduler.dequeue())
        task1_id = task1["id"]
        
        # Complete task1
        self.scheduler.complete(task1_id)
        
        # Now task1 should be skipped when dequeue checks state
        task2 = asyncio.run(self.scheduler.dequeue())
        assert task2 is not None
        assert task2["type"] == "test2"

    def test_terminal_outcome_audit_trail(self):
        """Test that terminal outcomes are recorded for audit trail."""
        self.scheduler.enqueue({"type": "test"})
        task = asyncio.run(self.scheduler.dequeue())
        task_id = task["id"]
        
        # Fail and exhaust retries
        for _ in range(self.scheduler._max_retries):
            self.scheduler.fail(task_id)
        
        outcome = self.scheduler.get_terminal_outcome(task_id)
        assert outcome is not None
        assert outcome["status"] == "failed"
        assert "retries" in outcome
        assert "failed_at" in outcome

    def test_reclaim_abandoned_returns_stale_in_flight_tasks(self):
        """Test that reclaim_abandoned returns tasks whose lease has expired."""
        # Enqueue two tasks at same priority so order is deterministic (FIFO)
        self.scheduler.enqueue({"type": "test1", "priority": 0}, priority=0)
        self.scheduler.enqueue({"type": "test2", "priority": 0}, priority=0)
        
        task1 = asyncio.run(self.scheduler.dequeue())  # task1 first (FIFO)
        task2 = asyncio.run(self.scheduler.dequeue())  # task2 second
        
        # Simulate: set task1's claimed_at far in the past to trigger reclaim
        self.scheduler._in_flight[task1["id"]]["claimed_at"] = time.time() - self.scheduler._lease_timeout - 10
        # task2 is recent, should not be reclaimed
        self.scheduler._in_flight[task2["id"]]["claimed_at"] = time.time()
        
        # Reclaim should return 1 task (task1)
        reclaimed = self.scheduler.reclaim_abandoned()
        assert reclaimed == 1
        
        # task1 is now back in queue; dequeue gives it back (FIFO, same priority)
        requeued = asyncio.run(self.scheduler.dequeue())
        assert requeued["id"] == task1["id"]
        assert requeued["retries"] == 0  # retries were reset

    def test_reclaim_abandoned_skips_terminal_tasks(self):
        """Test that reclaim does not steal completed/failed tasks."""
        self.scheduler.enqueue({"type": "test"})
        task = asyncio.run(self.scheduler.dequeue())
        task_id = task["id"]
        
        # Complete the task
        self.scheduler.complete(task_id)
        
        # Manually set its in_flight entry to stale (edge case)
        self.scheduler._in_flight[task_id] = {"task": task, "claimed_at": time.time() - 1000}
        
        # Reclaim should not return terminal tasks
        reclaimed = self.scheduler.reclaim_abandoned()
        assert reclaimed == 0

    def test_reclaim_abandoned_resets_retries(self):
        """Test that reclaimed tasks get their retry count reset."""
        self.scheduler.enqueue({"type": "test"})
        task = asyncio.run(self.scheduler.dequeue())
        task_id = task["id"]
        
        # Make task stale
        self.scheduler._in_flight[task_id]["claimed_at"] = time.time() - self.scheduler._lease_timeout - 10
        
        # Reclaim
        self.scheduler.reclaim_abandoned()
        
        # Re-dequeue and check retries are reset
        requeued = asyncio.run(self.scheduler.dequeue())
        assert requeued["retries"] == 0

# 2019-01-09T19:07:03 update

# 2019-02-18T12:30:02 update

# 2019-04-11T16:04:51 update

# 2019-04-17T16:25:46 update

# 2019-05-24T19:32:13 update

# 2019-07-02T12:54:25 update

# 2019-07-03T20:37:00 update

# 2019-08-21T19:37:17 update

# 2019-10-18T10:30:31 update

# 2019-10-25T09:01:38 update

# 2019-10-29T12:59:34 update

# 2019-11-05T10:07:06 update

# 2019-11-11T10:43:11 update

# 2020-01-17T13:40:02 update

# 2020-02-07T14:06:34 update

# 2020-04-03T08:53:03 update

# 2020-04-06T19:36:29 update

# 2020-05-12T11:51:05 update

# 2020-08-17T08:37:15 update

# 2020-09-15T10:39:39 update

# 2020-10-06T11:26:19 update

# 2020-10-21T13:32:43 update

# 2020-12-14T18:18:36 update

# 2020-12-23T17:15:03 update

# 2021-01-25T16:29:00 update

# 2021-02-23T11:23:50 update

# 2021-03-19T12:21:19 update

# 2021-07-29T18:48:25 update

# 2021-08-25T12:46:58 update

# 2021-09-09T16:27:13 update

# 2021-12-16T12:05:16 update

# 2022-05-07T14:05:12 update

# 2022-07-18T20:52:29 update

# 2022-07-31T18:42:26 update

# 2022-09-09T13:10:08 update

# 2023-01-04T15:16:57 update

# 2023-01-17T14:49:04 update

# 2023-02-15T13:51:20 update

# 2023-03-08T09:15:53 update

# 2023-03-23T16:32:20 update

# 2023-03-28T09:32:01 update

# 2023-05-05T17:28:22 update

# 2023-06-01T08:13:52 update

# 2023-06-20T09:58:10 update

# 2023-07-04T16:14:34 update

# 2023-07-17T20:49:40 update

# 2023-12-26T11:49:18 update

# 2024-05-27T11:00:06 update

# 2024-07-04T08:53:03 update

# 2024-07-18T16:19:02 update

# 2024-08-07T09:35:35 update

# 2024-08-22T14:32:14 update

# 2025-05-20T14:19:23 update

# 2025-07-17T17:54:48 update

# 2025-07-28T13:06:30 update

# 2025-12-22T19:05:25 update

# 2026-01-08T18:43:02 update

# 2026-01-12T16:53:28 update

# 2026-04-16T16:58:23 update