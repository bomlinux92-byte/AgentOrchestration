"""Tests for batch cancel endpoint with workspace scope enforcement."""

import pytest
from fastapi.testclient import TestClient

from src.api.server import create_app
from src.orchestrator.scheduler import TaskScheduler


class TestBatchCancelEndpoint:
    """Test suite for batch cancel endpoint."""

    def setup_method(self):
        self.app = create_app()
        self.client = TestClient(self.app)
        self.auth_header = {"Authorization": "Bearer test-token"}

    def test_batch_cancel_without_auth_returns_401(self):
        """Request without auth header should return 401."""
        response = self.client.post(
            "/api/v2/tasks/batch-cancel",
            json={"task_ids": ["task-123"], "workspace_id": "ws-1"},
        )
        assert response.status_code == 401

    def test_batch_cancel_empty_task_ids_returns_400(self):
        """Malformed request with empty task_ids should return 400."""
        response = self.client.post(
            "/api/v2/tasks/batch-cancel",
            json={"task_ids": [], "workspace_id": "ws-1", "role": "operator"},
            headers=self.auth_header,
        )
        assert response.status_code == 400
        assert "task_ids cannot be empty" in response.json()["detail"]

    def test_batch_cancel_exceeds_max_returns_400(self):
        """Request exceeding max task_ids should return 400."""
        response = self.client.post(
            "/api/v2/tasks/batch-cancel",
            json={
                "task_ids": [f"task-{i}" for i in range(101)],
                "workspace_id": "ws-1",
                "role": "operator"
            },
            headers=self.auth_header,
        )
        assert response.status_code == 400
        assert "exceeds maximum" in response.json()["detail"]

    def test_batch_cancel_invalid_role_returns_403(self):
        """Request with invalid role should return 403 before any lookup."""
        response = self.client.post(
            "/api/v2/tasks/batch-cancel",
            json={"task_ids": ["task-123"], "workspace_id": "ws-1", "role": "guest"},
            headers=self.auth_header,
        )
        assert response.status_code == 403
        assert "Insufficient permissions" in response.json()["detail"]

    def test_batch_cancel_cross_workspace_returns_forbidden(self):
        """Request to cancel task in different workspace should fail with forbidden."""
        from src.api.routes import scheduler as route_scheduler
        import time

        # Enqueue a task with a different workspace
        route_scheduler._scheduled["task-cross"] = time.time() + 3600
        route_scheduler._scheduled_data["task-cross"] = {
            "id": "task-cross",
            "workspace_id": "ws-other",
            "name": "cross-workspace-task"
        }

        response = self.client.post(
            "/api/v2/tasks/batch-cancel",
            json={"task_ids": ["task-cross"], "workspace_id": "ws-1", "role": "operator"},
            headers=self.auth_header,
        )
        assert response.status_code == 200
        data = response.json()
        assert "task-cross" in data["failed"]
        assert data["failed"]["task-cross"] == "forbidden"

    def test_batch_cancel_nonexistent_task_returns_not_found(self):
        """Request for non-existent task should return not_found in response."""
        response = self.client.post(
            "/api/v2/tasks/batch-cancel",
            json={"task_ids": ["nonexistent-task"], "workspace_id": "ws-1", "role": "operator"},
            headers=self.auth_header,
        )
        assert response.status_code == 200
        data = response.json()
        assert "nonexistent-task" in data["failed"]
        assert data["failed"]["nonexistent-task"] == "not_found"

    def test_batch_cancel_valid_task_succeeds(self):
        """Cancel task with matching workspace should succeed."""
        from src.api.routes import scheduler as route_scheduler
        import time

        # Set up task with matching workspace
        route_scheduler._scheduled["task-valid"] = time.time() + 3600
        route_scheduler._scheduled_data["task-valid"] = {
            "id": "task-valid",
            "workspace_id": "ws-1",
            "name": "valid-task"
        }

        response = self.client.post(
            "/api/v2/tasks/batch-cancel",
            json={"task_ids": ["task-valid"], "workspace_id": "ws-1", "role": "operator"},
            headers=self.auth_header,
        )
        assert response.status_code == 200
        data = response.json()
        assert "task-valid" in data["cancelled"]


class TestCancelTasksBatchService:
    """Test suite for cancel_tasks_batch service function."""

    def test_cancel_tasks_batch_valid_inputs(self):
        """Valid inputs should result in successful cancellation."""
        scheduler = TaskScheduler()
        task = {"id": "task-1", "workspace_id": "ws-1"}
        scheduler._in_flight["task-1"] = task

        from src.api.routes import cancel_tasks_batch
        cancelled, failed = cancel_tasks_batch(["task-1"], "ws-1", "operator")
        assert "task-1" in cancelled
        assert "task-1" not in failed

    def test_cancel_tasks_batch_forbidden_different_workspace(self):
        """Task in different workspace should return forbidden error."""
        scheduler = TaskScheduler()
        task = {"id": "task-2", "workspace_id": "ws-other"}
        scheduler._in_flight["task-2"] = task

        from src.api.routes import cancel_tasks_batch
        cancelled, failed = cancel_tasks_batch(["task-2"], "ws-1", "operator")
        assert "task-2" not in cancelled
        assert failed["task-2"] == "forbidden"

    def test_cancel_tasks_batch_not_found(self):
        """Non-existent task should return not_found error."""
        from src.api.routes import cancel_tasks_batch
        cancelled, failed = cancel_tasks_batch(["nonexistent"], "ws-1", "operator")
        assert "nonexistent" not in cancelled
        assert failed["nonexistent"] == "not_found"


class TestCancelTaskScheduler:
    """Test suite for TaskScheduler.cancel_task method."""

    def test_cancel_task_in_flight_with_matching_workspace(self):
        """Cancel in-flight task with matching workspace should succeed."""
        scheduler = TaskScheduler()
        scheduler._in_flight["task-1"] = {"id": "task-1", "workspace_id": "ws-1"}

        success, error_code = scheduler.cancel_task("task-1", "ws-1")
        assert success is True
        assert error_code == ""
        assert "task-1" not in scheduler._in_flight

    def test_cancel_task_in_flight_cross_workspace_forbidden(self):
        """Cancel in-flight task with different workspace should return forbidden."""
        scheduler = TaskScheduler()
        scheduler._in_flight["task-1"] = {"id": "task-1", "workspace_id": "ws-other"}

        success, error_code = scheduler.cancel_task("task-1", "ws-1")
        assert success is False
        assert error_code == "forbidden"
        assert "task-1" in scheduler._in_flight  # Task should remain

    def test_cancel_task_scheduled_with_matching_workspace(self):
        """Cancel scheduled task with matching workspace should succeed."""
        import time
        scheduler = TaskScheduler()
        scheduler._scheduled["task-1"] = time.time() + 3600
        scheduler._scheduled_data["task-1"] = {"id": "task-1", "workspace_id": "ws-1"}

        success, error_code = scheduler.cancel_task("task-1", "ws-1")
        assert success is True
        assert error_code == ""
        assert "task-1" not in scheduler._scheduled
        assert "task-1" not in scheduler._scheduled_data

    def test_cancel_task_scheduled_cross_workspace_forbidden(self):
        """Cancel scheduled task with different workspace should return forbidden."""
        import time
        scheduler = TaskScheduler()
        scheduler._scheduled["task-1"] = time.time() + 3600
        scheduler._scheduled_data["task-1"] = {"id": "task-1", "workspace_id": "ws-other"}

        success, error_code = scheduler.cancel_task("task-1", "ws-1")
        assert success is False
        assert error_code == "forbidden"
        assert "task-1" in scheduler._scheduled
        assert "task-1" in scheduler._scheduled_data

    def test_cancel_task_not_found(self):
        """Cancel non-existent task should return not_found."""
        scheduler = TaskScheduler()

        success, error_code = scheduler.cancel_task("nonexistent", "ws-1")
        assert success is False
        assert error_code == "not_found"

    def test_cancel_task_invalid_input_empty_task_id(self):
        """Cancel with empty task_id should return invalid_input."""
        scheduler = TaskScheduler()

        success, error_code = scheduler.cancel_task("", "ws-1")
        assert success is False
        assert error_code == "invalid_input"

    def test_cancel_task_invalid_input_empty_workspace_id(self):
        """Cancel with empty workspace_id should return invalid_input."""
        scheduler = TaskScheduler()

        success, error_code = scheduler.cancel_task("task-1", "")
        assert success is False
        assert error_code == "invalid_input"

    def test_cancel_task_invalid_input_none_workspace_id(self):
        """Cancel with None workspace_id should return invalid_input."""
        scheduler = TaskScheduler()

        success, error_code = scheduler.cancel_task("task-1", None)
        assert success is False
        assert error_code == "invalid_input"