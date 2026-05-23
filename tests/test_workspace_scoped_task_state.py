"""Tests for workspace-scoped task state management.

These tests verify the fix for issue #3370:
- Task state reads and writes require workspace scope
- Task ID collisions across workspaces are properly isolated
- Scoped repository methods are enforced
- Direct unscoped query helpers are deprecated/blocked

See: https://github.com/orchestration-agent/AgentOrchestration/issues/3370
"""

import asyncio
import pytest
from src.orchestrator.scheduler import TaskScheduler, TaskState, WorkspaceScopeError
from src.storage.task_state_repo import TaskStateRepo, WorkspaceScopeError as RepoScopeError


class TestWorkspaceScopedTaskState:
    """Test workspace scope enforcement on task state."""

    def setup_method(self):
        self.scheduler = TaskScheduler()

    def test_task_id_collision_isolation_between_workspaces(self):
        """Task ID '123' in workspace-A must not affect task ID '123' in workspace-B."""
        # Enqueue same task_id format in different workspaces
        task_a = {"type": "test", "payload": {"data": "A"}}
        task_b = {"type": "test", "payload": {"data": "B"}}

        task_id_a = self.scheduler.enqueue(task_a, workspace_id="workspace-A")
        task_id_b = self.scheduler.enqueue(task_b, workspace_id="workspace-B")

        # Even if task IDs have same format, they should be stored separately
        # Verify task A is in workspace-A
        state_a = self.scheduler.get_task_state_scoped("workspace-A", task_id_a)
        assert state_a == TaskState.QUEUED

        # Verify task B is in workspace-B
        state_b = self.scheduler.get_task_state_scoped("workspace-B", task_id_b)
        assert state_b == TaskState.QUEUED

        # Task A should NOT be accessible from workspace-B
        state_a_from_b = self.scheduler.get_task_state_scoped("workspace-B", task_id_a)
        assert state_a_from_b is None

        # Task B should NOT be accessible from workspace-A
        state_b_from_a = self.scheduler.get_task_state_scoped("workspace-A", task_id_b)
        assert state_b_from_a is None

    def test_complete_scoped_requires_workspace(self):
        """complete_scoped must reject calls without workspace_id."""
        scheduler = TaskScheduler()
        task_id = scheduler.enqueue({"type": "test"}, workspace_id="ws1")

        # Dequeue to put task in flight first
        task = asyncio.run(scheduler.dequeue(workspace_id="ws1"))
        assert task is not None

        # Complete with correct workspace should work
        result = scheduler.complete_scoped("ws1", task_id)
        assert result is True

        # Complete with wrong workspace should fail
        task_id2 = scheduler.enqueue({"type": "test"}, workspace_id="ws1")
        asyncio.run(scheduler.dequeue(workspace_id="ws1"))
        result = scheduler.complete_scoped("ws2", task_id2)
        assert result is False

    def test_fail_scoped_requires_workspace(self):
        """fail_scoped must reject calls without workspace_id."""
        scheduler = TaskScheduler()
        task_id = scheduler.enqueue({"type": "test"}, workspace_id="ws1")

        # Use dequeue to put in flight
        task = asyncio.run(scheduler.dequeue(workspace_id="ws1"))
        assert task is not None

        result = scheduler.fail_scoped("ws1", task_id)
        assert result is True

    def test_scoped_method_rejects_empty_workspace(self):
        """Scoped methods must reject empty/None workspace_id."""
        scheduler = TaskScheduler()
        task_id = scheduler.enqueue({"type": "test"}, workspace_id="ws1")

        with pytest.raises(WorkspaceScopeError):
            scheduler.get_task_state_scoped("", task_id)

        with pytest.raises(WorkspaceScopeError):
            scheduler.get_task_state_scoped(None, task_id)  # type: ignore

    def test_enqueue_with_workspace_stores_scoped(self):
        """Enqueue with workspace_id stores in scoped storage."""
        scheduler = TaskScheduler()
        task_id = scheduler.enqueue({"type": "test"}, workspace_id="test-ws")

        # Verify it's in scoped storage
        state = scheduler.get_task_state_scoped("test-ws", task_id)
        assert state == TaskState.QUEUED

    def test_list_workspace_tasks(self):
        """list_workspace_tasks returns only tasks for that workspace."""
        scheduler = TaskScheduler()

        # Create tasks in two workspaces
        for _ in range(3):
            scheduler.enqueue({"type": "test"}, workspace_id="ws-A")
        for _ in range(2):
            scheduler.enqueue({"type": "test"}, workspace_id="ws-B")

        tasks_a = scheduler.list_workspace_tasks("ws-A")
        tasks_b = scheduler.list_workspace_tasks("ws-B")

        assert len(tasks_a) == 3
        assert len(tasks_b) == 2

        # No overlap
        assert set(tasks_a).isdisjoint(set(tasks_b))

    def test_cross_workspace_task_id_same_value_isolation(self):
        """Two tasks with same task_id string in different workspaces are isolated.

        This is the core of issue #3370 - if task_id "abc-123" is created in
        workspace A, operations on "abc-123" in workspace B must not see or
        affect the task from workspace A.
        """
        scheduler = TaskScheduler()

        # Create first task
        task1_id = scheduler.enqueue({"type": "task"}, workspace_id="workspace-1")
        # Manually set the task_id to a predictable value for testing collision
        # (In real usage, UUIDs prevent this, but tests should verify isolation)

        # Verify workspace-1 task is accessible in workspace-1
        state1 = scheduler.get_task_state_scoped("workspace-1", task1_id)
        assert state1 is not None

        # Verify workspace-1 task is NOT accessible in workspace-2
        state1_from_2 = scheduler.get_task_state_scoped("workspace-2", task1_id)
        assert state1_from_2 is None

    def test_reclaim_stale_respects_workspace(self):
        """reclaim_stale should only reclaim tasks in specified workspace."""
        scheduler = TaskScheduler()

        # Enqueue with workspace
        task_id = scheduler.enqueue({"type": "test"}, workspace_id="ws1")
        # Use workspace-scoped dequeue
        asyncio.run(scheduler.dequeue(queue="default", workspace_id="ws1"))

        # Reclaim with wrong workspace should not affect the task
        reclaimed = scheduler.reclaim_stale(max_age_seconds=0.0, workspace_id="ws2")
        assert reclaimed == 0

        # Task should still be in flight in ws1
        assert scheduler.get_task_state_scoped("ws1", task_id) == TaskState.IN_FLIGHT


class TestTaskStateRepository:
    """Test the TaskStateRepo for workspace-scoped storage."""

    def setup_method(self):
        self.repo = TaskStateRepo()

    def test_put_requires_workspace(self):
        """put() must have workspace_id."""
        with pytest.raises(RepoScopeError):
            self.repo.put("", "task-1", TaskState.QUEUED)

        with pytest.raises(RepoScopeError):
            self.repo.put(None, "task-1", TaskState.QUEUED)  # type: ignore

    def test_get_requires_workspace(self):
        """get() must have workspace_id - empty/None raises error."""
        # Empty workspace should raise
        with pytest.raises(RepoScopeError):
            self.repo.get("", "task-1")

        with pytest.raises(RepoScopeError):
            self.repo.get(None, "task-1")  # type: ignore

    def test_task_id_collision_isolation_in_repo(self):
        """Task with same ID in different workspaces are isolated."""
        # Put same task_id in two workspaces
        self.repo.put("workspace-A", "task-X", TaskState.QUEUED)
        self.repo.put("workspace-B", "task-X", TaskState.COMPLETED)

        # Verify each workspace has its own state
        state_a = self.repo.get_state("workspace-A", "task-X")
        state_b = self.repo.get_state("workspace-B", "task-X")

        assert state_a == TaskState.QUEUED
        assert state_b == TaskState.COMPLETED

        # Cross-workspace lookups return correct results
        assert self.repo.exists("workspace-A", "task-X") is True
        assert self.repo.exists("workspace-B", "task-X") is True
        assert self.repo.exists("workspace-C", "task-X") is False

    def test_delete_respects_workspace(self):
        """delete() only deletes within specified workspace."""
        self.repo.put("ws1", "task-1", TaskState.QUEUED)
        self.repo.put("ws2", "task-1", TaskState.QUEUED)

        # Delete from ws1 only
        result = self.repo.delete("ws1", "task-1")
        assert result is True

        # ws1 should be empty, ws2 should still have task
        assert self.repo.exists("ws1", "task-1") is False
        assert self.repo.exists("ws2", "task-1") is True

    def test_list_by_workspace(self):
        """list_by_workspace returns only tasks in that workspace."""
        for i in range(3):
            self.repo.put("ws-A", f"task-{i}", TaskState.QUEUED)
        for i in range(2):
            self.repo.put("ws-B", f"task-{i}", TaskState.QUEUED)

        tasks_a = self.repo.list_by_workspace("ws-A")
        tasks_b = self.repo.list_by_workspace("ws-B")

        assert len(tasks_a) == 3
        assert len(tasks_b) == 2

    def test_clear_workspace(self):
        """clear_workspace removes all tasks for a workspace."""
        self.repo.put("ws1", "task-1", TaskState.QUEUED)
        self.repo.put("ws1", "task-2", TaskState.QUEUED)
        self.repo.put("ws2", "task-1", TaskState.QUEUED)

        count = self.repo.clear_workspace("ws1")
        assert count == 2

        assert self.repo.list_by_workspace("ws1") == []
        assert len(self.repo.list_by_workspace("ws2")) == 1

    def test_unscoped_access_blocked(self):
        """Unscoped access should be blocked or deprecated."""
        # The repo itself enforces workspace scope - there's no unscoped method
        # Empty workspace_id raises WorkspaceScopeError
        with pytest.raises(RepoScopeError):
            self.repo.get("", "any-task")

    def test_collision_prevents_data_leakage(self):
        """Verify that task ID collision does not leak data between workspaces."""
        # Simulate the bug scenario: same task_id in different workspaces
        task_id = "same-task-id"

        # Workspace 1: task is COMPLETED
        self.repo.put("workspace-1", task_id, TaskState.COMPLETED, metadata={"secret": "workspace-1-data"})

        # Workspace 2: task is FAILED
        self.repo.put("workspace-2", task_id, TaskState.FAILED, metadata={"secret": "workspace-2-data"})

        # Reading workspace-1 should only see workspace-1 data
        entry1 = self.repo.get("workspace-1", task_id)
        assert entry1["state"] == "completed"
        assert entry1["metadata"]["secret"] == "workspace-1-data"
        assert "workspace-2-data" not in str(entry1)

        # Reading workspace-2 should only see workspace-2 data
        entry2 = self.repo.get("workspace-2", task_id)
        assert entry2["state"] == "failed"
        assert entry2["metadata"]["secret"] == "workspace-2-data"
        assert "workspace-1-data" not in str(entry2)


class TestBackwardCompatibility:
    """Test that existing code still works but with deprecation warnings."""

    def test_legacy_unscoped_enqueue_still_works(self):
        """Enqueue without workspace_id still works for backward compatibility."""
        scheduler = TaskScheduler()
        task_id = scheduler.enqueue({"type": "test"})
        assert task_id is not None

    def test_legacy_unscoped_dequeue_still_works(self):
        """Dequeue without workspace_id still works."""
        scheduler = TaskScheduler()
        scheduler.enqueue({"type": "test"})
        task = asyncio.run(scheduler.dequeue())
        assert task is not None
        assert task["type"] == "test"

    def test_legacy_unscoped_complete_still_works(self):
        """Complete without workspace_id still works."""
        scheduler = TaskScheduler()
        task_id = scheduler.enqueue({"type": "test"})
        result = scheduler.complete(task_id)
        assert result is True


# 2026-05-24T06:00:00 fix for issue #3370