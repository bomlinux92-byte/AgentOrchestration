import asyncio
import pytest
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
        import asyncio
        task = asyncio.run(self.scheduler.dequeue())
        assert task is not None
        assert task["type"] == "test"

    def test_enqueue_multiple_priorities(self):
        self.scheduler.enqueue({"type": "low"}, priority=1)
        self.scheduler.enqueue({"type": "high"}, priority=10)
        import asyncio
        task = asyncio.run(self.scheduler.dequeue())
        assert task["type"] == "high"

    def test_complete_task(self):
        self.scheduler.enqueue({"type": "test"})
        import asyncio
        task = asyncio.run(self.scheduler.dequeue())
        assert self.scheduler.complete(task["id"])

    def test_fail_task_with_retry(self):
        self.scheduler.enqueue({"type": "test"})
        import asyncio
        task = asyncio.run(self.scheduler.dequeue())
        assert self.scheduler.fail(task["id"])

    def test_audit_log_records_dequeue(self):
        self.scheduler.enqueue({"type": "test", "payload": {}})
        import asyncio
        task = asyncio.run(self.scheduler.dequeue())
        entries = self.scheduler.audit_log.get_entries(task["id"])
        assert len(entries) >= 1
        assert any(e["event"] == "dequeue_allowed" for e in entries)

    def test_audit_log_records_complete(self):
        self.scheduler.enqueue({"type": "test"})
        import asyncio
        task = asyncio.run(self.scheduler.dequeue())
        self.scheduler.complete(task["id"])
        entries = self.scheduler.audit_log.get_entries(task["id"])
        assert any(e["event"] == "task_completed" for e in entries)

    def test_audit_log_records_fail_and_retry(self):
        self.scheduler.enqueue({"type": "test"})
        import asyncio
        task = asyncio.run(self.scheduler.dequeue())
        self.scheduler.fail(task["id"])
        entries = self.scheduler.audit_log.get_entries(task["id"])
        assert any(e["event"] == "task_failed" for e in entries)
        assert any(e["event"] == "task_requeued" for e in entries)

    def test_audit_log_records_task_dropped_after_max_retries(self):
        scheduler = TaskScheduler()
        scheduler._max_retries = 1
        scheduler.enqueue({"type": "test"})
        import asyncio
        task = asyncio.run(scheduler.dequeue())
        scheduler.fail(task["id"])
        entries = scheduler.audit_log.get_entries(task["id"])
        assert any(e["event"] == "task_dropped" for e in entries)

    def test_audit_log_records_dequeue_rejected_empty_queue(self):
        import asyncio
        asyncio.run(self.scheduler.dequeue())
        entries = self.scheduler.audit_log.get_entries()
        assert any(e["event"] == "dequeue_rejected" for e in entries)

    def test_audit_log_get_entries_by_task_id(self):
        self.scheduler.enqueue({"type": "test"})
        import asyncio
        task = asyncio.run(self.scheduler.dequeue())
        task_id = task["id"]
        entries = self.scheduler.audit_log.get_entries(task_id=task_id)
        assert all(e["task_id"] == task_id for e in entries)

    def test_audit_log_clear(self):
        self.scheduler.enqueue({"type": "test"})
        import asyncio
        asyncio.run(self.scheduler.dequeue())
        assert len(self.scheduler.audit_log.get_entries()) > 0
        self.scheduler.audit_log.clear()
        assert len(self.scheduler.audit_log.get_entries()) == 0

    def test_stale_duplicate_transition_rejected_via_audit(self):
        """Verify stale task completion attempts are logged and rejected."""
        self.scheduler.enqueue({"type": "test"})
        import asyncio
        task = asyncio.run(self.scheduler.dequeue())
        assert self.scheduler.complete(task["id"]) is True
        assert self.scheduler.complete(task["id"]) is False
        failed_entries = self.scheduler.audit_log.get_entries(task["id"])
        assert any(e["event"] == "task_complete_failed" for e in failed_entries)

    def test_state_transition_queued_to_in_flight(self):
        task_id = self.scheduler.enqueue({"type": "test"})
        assert self.scheduler._task_states[task_id] == TaskState.QUEUED
        task = asyncio.run(self.scheduler.dequeue())
        assert task is not None
        assert self.scheduler._task_states[task["id"]] == TaskState.IN_FLIGHT

    def test_state_transition_in_flight_to_completed(self):
        self.scheduler.enqueue({"type": "test"})
        task = asyncio.run(self.scheduler.dequeue())
        assert self.scheduler.complete(task["id"])
        assert self.scheduler._task_states[task["id"]] == TaskState.COMPLETED

    def test_state_transition_in_flight_to_failed_after_max_retries(self):
        scheduler = TaskScheduler()
        scheduler._max_retries = 1
        scheduler.enqueue({"type": "test"})
        task = asyncio.run(scheduler.dequeue())
        scheduler.fail(task["id"])
        assert scheduler._task_states[task["id"]] == TaskState.FAILED

    def test_double_complete_rejected(self):
        self.scheduler.enqueue({"type": "test"})
        task = asyncio.run(self.scheduler.dequeue())
        assert self.scheduler.complete(task["id"]) is True
        assert self.scheduler.complete(task["id"]) is False

    def test_complete_unknown_task_rejected(self):
        assert self.scheduler.complete("nonexistent") is False

    def test_fail_unknown_task_rejected(self):
        assert self.scheduler.fail("nonexistent") is False

    def test_reclaim_stale_tasks(self):
        task_id = self.scheduler.enqueue({"type": "test"})
        task = asyncio.run(self.scheduler.dequeue())
        assert len(self.scheduler._in_flight) == 1
        reclaimed = self.scheduler.reclaim_stale(max_age_seconds=0.0)
        assert reclaimed == 1
        assert len(self.scheduler._in_flight) == 0
        assert self.scheduler._task_states[task_id] == TaskState.FAILED

    def test_reclaim_does_not_affect_fresh_tasks(self):
        self.scheduler.enqueue({"type": "test"})
        task = asyncio.run(self.scheduler.dequeue())
        reclaimed = self.scheduler.reclaim_stale(max_age_seconds=3600.0)
        assert reclaimed == 0
        assert len(self.scheduler._in_flight) == 1

    def test_retry_is_bounded(self):
        scheduler = TaskScheduler()
        scheduler._max_retries = 2
        scheduler.enqueue({"type": "test"})
        task = asyncio.run(scheduler.dequeue())

        assert scheduler.fail(task["id"]) is True
        task2 = asyncio.run(scheduler.dequeue())
        assert task2 is not None

        assert scheduler.fail(task2["id"]) is False
        assert scheduler._task_states[task2["id"]] == TaskState.FAILED

    def test_fail_and_requeue_cycles_through_states(self):
        self.scheduler.enqueue({"type": "test"})
        task = asyncio.run(self.scheduler.dequeue())
        assert self.scheduler._task_states[task["id"]] == TaskState.IN_FLIGHT

        self.scheduler.fail(task["id"])
        assert self.scheduler._task_states[task["id"]] == TaskState.QUEUED

        task2 = asyncio.run(self.scheduler.dequeue())
        assert task2 is not None
        assert self.scheduler._task_states[task2["id"]] == TaskState.IN_FLIGHT

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

# 2019-11-11T10:43:52 update

# 2020-01-17T13:40:02 update

# 2020-02-07T14:06:34 update

# 2020-04-03T08:53:40 update

# 2020-04-06T19:36:29 update

# 2020-05-12T11:51:05 update

# 2020-08-17T08:37:15 update

# 2020-09-15T10:39:38 update

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

# 2021-12-16T12:05:30 update

# 2022-05-07T14:05:12 update

# 2022-07-18T20:52:29 update

# 2022-07-31T18:42:26 update

# 2022-09-09T13:10:08 update

# 2023-01-04T15:16:57 update

# 2023-01-17T14:49:04 update

# 2023-02-15T13:51:30 update

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
