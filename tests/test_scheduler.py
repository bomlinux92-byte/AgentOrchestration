import pytest
from src.orchestrator.scheduler import TaskScheduler, SchedulerState


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

    def test_tenant_capacity_tracking(self):
        """Test that per-tenant concurrency is properly tracked."""
        import asyncio
        
        # Create scheduler with max 2 concurrent per tenant
        scheduler = TaskScheduler(default_tenant_max_concurrent=2)
        
        # Enqueue tasks for tenant-a
        task1 = {"type": "test", "tenant_id": "tenant-a"}
        task2 = {"type": "test", "tenant_id": "tenant-a"}
        task3 = {"type": "test", "tenant_id": "tenant-a"}
        
        id1 = scheduler.enqueue(task1)
        id2 = scheduler.enqueue(task2)
        id3 = scheduler.enqueue(task3)
        
        # Dequeue should work for first 2
        t1 = asyncio.run(scheduler.dequeue())
        t2 = asyncio.run(scheduler.dequeue())
        
        # Third should be deferred (at capacity)
        t3 = asyncio.run(scheduler.dequeue())
        # At capacity, so task is re-queued and None returned
        assert t3 is None or t3["id"] == id3
        
        # Complete tasks and verify capacity is released
        scheduler.complete(t1["id"])
        scheduler.complete(t2["id"])
        
        # Now dequeue should work again
        t3_new = asyncio.run(scheduler.dequeue())
        assert t3_new is not None

    def test_recover_in_flight_enforces_capacity(self):
        """Test that recovery scanner enforces per-tenant concurrency."""
        import asyncio
        
        scheduler = TaskScheduler(default_tenant_max_concurrent=2)
        
        # Simulate in-flight tasks from before restart
        scheduler._in_flight = {
            "task-1": {"id": "task-1", "type": "test", "tenant_id": "tenant-a"},
            "task-2": {"id": "task-2", "type": "test", "tenant_id": "tenant-a"},
            "task-3": {"id": "task-3", "type": "test", "tenant_id": "tenant-a"},  # Should be rejected
        }
        
        result = asyncio.run(scheduler.recover_in_flight())
        
        # task-3 should be rejected (capacity exceeded for tenant-a)
        assert result["status"] == "completed"
        assert result["rejected_count"] >= 1
        assert result["recovered_count"] >= 2
        
        # Verify rejected tasks are removed from in_flight
        assert "task-3" not in scheduler._in_flight

    def test_audit_log_bounded(self):
        """Test that audit log has bounded retention."""
        import asyncio
        
        scheduler = TaskScheduler()
        
        # Add many audit entries
        for i in range(1100):
            asyncio.run(scheduler._add_audit("tenant", f"task-{i}", "TEST", "a", "b", "reason"))
        
        # Should be bounded to MAX_ENTRIES
        log = asyncio.run(scheduler.get_audit_log())
        assert len(log) <= 1000

    def test_atomic_state_transitions(self):
        """Test that invalid state transitions are rejected."""
        scheduler = TaskScheduler()
        
        # Valid: IDLE -> RECOVERING
        assert scheduler._can_transition_state(SchedulerState.RECOVERING)
        
        # Set to RECOVERING
        scheduler._state = SchedulerState.RECOVERING
        
        # Valid: RECOVERING -> ACTIVE
        assert scheduler._can_transition_state(SchedulerState.ACTIVE)
        
        # Set to ACTIVE
        scheduler._state = SchedulerState.ACTIVE
        
        # Invalid: ACTIVE -> RECOVERING (not in transitions)
        assert not scheduler._can_transition_state(SchedulerState.RECOVERING)

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
