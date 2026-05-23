"""Task State Repository — Workspace-scoped storage for task state management.

This module provides a repository pattern for task state storage that enforces
row-level workspace scope on all task state reads and writes. This prevents
task ID collisions across workspaces from causing data access violations.

Fixes: https://github.com/orchestration-agent/AgentOrchestration/issues/3370

Usage:
    repo = TaskStateRepo()
    repo.put("workspace-1", "task-123", TaskState.QUEUED)
    state = repo.get("workspace-1", "task-123")  # Returns task state
    # repo.get("workspace-2", "task-123") would return None (different workspace)
"""

import time
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from src.common.errors import AgentOrchestratorError
from src.orchestrator.scheduler import TaskState


class WorkspaceScopeError(AgentOrchestratorError):
    """Raised when a task state operation lacks required workspace scope."""
    def __init__(self, operation: str, task_id: str, reason: str = "workspace scope required"):
        self.task_id = task_id
        self.operation = operation
        super().__init__(f"{operation} failed for task {task_id}: {reason}")


class TaskStateRepositoryError(AgentOrchestratorError):
    """Base error for task state repository operations."""
    pass


class TaskStateRepo:
    """Repository for task state with mandatory workspace scoping.

    All task state operations require a workspace_id to prevent cross-workspace
    task ID collisions from causing data leakage or corruption.

    Storage structure:
        {(workspace_id, task_id): {"state": TaskState, "metadata": Dict, "updated_at": float}}

    This ensures that task_id "abc-123" in workspace "A" is completely isolated
    from task_id "abc-123" in workspace "B".
    """

    def __init__(self):
        # Internal storage: {(workspace_id, task_id): state_dict}
        self._states: Dict[Tuple[str, str], Dict[str, Any]] = {}
        # Index by workspace: {workspace_id: set of task_ids}
        self._workspace_index: Dict[str, set] = {}

    def _make_key(self, workspace_id: str, task_id: str) -> Tuple[str, str]:
        """Create a workspace-scoped storage key."""
        return (workspace_id, task_id)

    def _validate_workspace(self, workspace_id: str) -> None:
        """Validate workspace_id is a non-empty string."""
        if not workspace_id or not isinstance(workspace_id, str):
            raise WorkspaceScopeError(
                operation="task_state_repo",
                task_id="*",
                reason="workspace_id must be a non-empty string"
            )

    def put(self, workspace_id: str, task_id: str, state: TaskState, metadata: Optional[Dict] = None) -> None:
        """Store task state within a workspace scope.

        Args:
            workspace_id: The workspace identifier (required, non-empty string)
            task_id: The task identifier (required)
            state: The TaskState enum value
            metadata: Optional metadata dict

        Raises:
            WorkspaceScopeError: If workspace_id is missing or empty
            TypeError: If workspace_id is not a string
        """
        self._validate_workspace(workspace_id)
        if not task_id:
            raise TaskStateRepositoryError(f"put failed: task_id cannot be empty")

        key = self._make_key(workspace_id, task_id)
        entry = {
            "workspace_id": workspace_id,
            "task_id": task_id,
            "state": state.value,
            "metadata": metadata or {},
            "updated_at": time.time(),
        }
        self._states[key] = entry

        if workspace_id not in self._workspace_index:
            self._workspace_index[workspace_id] = set()
        self._workspace_index[workspace_id].add(task_id)

    def get(self, workspace_id: str, task_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve task state within a workspace scope.

        Args:
            workspace_id: The workspace identifier (required)
            task_id: The task identifier

        Returns:
            Task state dict with keys: workspace_id, task_id, state, metadata, updated_at
            or None if not found

        Raises:
            WorkspaceScopeError: If workspace_id is missing or empty
        """
        self._validate_workspace(workspace_id)
        if not task_id:
            return None
        key = self._make_key(workspace_id, task_id)
        return self._states.get(key)

    def get_state(self, workspace_id: str, task_id: str) -> Optional[TaskState]:
        """Get just the state value for a task within workspace scope.

        Returns:
            TaskState enum value or None if not found
        """
        entry = self.get(workspace_id, task_id)
        if entry:
            state_str = entry.get("state")
            if state_str:
                return TaskState(state_str)
        return None

    def update_metadata(self, workspace_id: str, task_id: str, metadata: Dict) -> bool:
        """Update metadata for a task within workspace scope.

        Returns:
            True if updated, False if task not found
        """
        self._validate_workspace(workspace_id)
        if not task_id:
            return False
        key = self._make_key(workspace_id, task_id)
        if key not in self._states:
            return False
        self._states[key]["metadata"].update(metadata)
        self._states[key]["updated_at"] = time.time()
        return True

    def delete(self, workspace_id: str, task_id: str) -> bool:
        """Delete task state within a workspace scope.

        Args:
            workspace_id: The workspace identifier (required)
            task_id: The task identifier

        Returns:
            True if deleted, False if not found
        """
        self._validate_workspace(workspace_id)
        if not task_id:
            return False
        key = self._make_key(workspace_id, task_id)
        if key in self._states:
            del self._states[key]
            if workspace_id in self._workspace_index:
                self._workspace_index[workspace_id].discard(task_id)
            return True
        return False

    def list_by_workspace(self, workspace_id: str) -> List[Dict[str, Any]]:
        """List all task states in a workspace.

        Args:
            workspace_id: The workspace identifier

        Returns:
            List of task state dicts for the workspace
        """
        self._validate_workspace(workspace_id)
        if workspace_id not in self._workspace_index:
            return []
        return [
            self._states[(workspace_id, tid)]
            for tid in list(self._workspace_index[workspace_id])
            if (workspace_id, tid) in self._states
        ]

    def exists(self, workspace_id: str, task_id: str) -> bool:
        """Check if task exists within workspace scope."""
        self._validate_workspace(workspace_id)
        if not task_id:
            return False
        return self._make_key(workspace_id, task_id) in self._states

    def clear_workspace(self, workspace_id: str) -> int:
        """Clear all task states for a workspace.

        Returns:
            Count of cleared tasks
        """
        self._validate_workspace(workspace_id)
        if workspace_id not in self._workspace_index:
            return 0
        count = len(self._workspace_index[workspace_id])
        for task_id in list(self._workspace_index[workspace_id]):
            key = (workspace_id, task_id)
            if key in self._states:
                del self._states[key]
        del self._workspace_index[workspace_id]
        return count

    def get_task_count(self, workspace_id: str) -> int:
        """Get count of tasks in a workspace."""
        self._validate_workspace(workspace_id)
        return len(self._workspace_index.get(workspace_id, set()))

    def list_workspaces(self) -> List[str]:
        """List all workspace IDs that have stored task states."""
        return list(self._workspace_index.keys())


# Prevent direct unscoped access through static analysis
# These would be removed in a production environment or replaced with
# workspace-scoped equivalents
_UNSCOPED_HELPERS_DEPRECATED = """
WARNING: The following unscoped helpers are DEPRECATED and will be removed:
    - get_task_state_unscoped()
    - put_task_state_unscoped()
    - delete_task_state_unscoped()
Use workspace-scoped methods instead:
    - repo.get(workspace_id, task_id)
    - repo.put(workspace_id, task_id, state)
    - repo.delete(workspace_id, task_id)
"""


def create_scoped_repo() -> TaskStateRepo:
    """Factory function to create a workspace-scoped task state repository."""
    return TaskStateRepo()


# Backward compatibility alias - prefer using create_scoped_repo()
_task_state_repo_instance: Optional[TaskStateRepo] = None


def get_task_state_repo() -> TaskStateRepo:
    """Get the global TaskStateRepo instance.

    Returns:
        A workspace-scoped TaskStateRepo instance

    Note:
        This creates a global singleton. For testing, use create_scoped_repo()
        to get fresh instances.
    """
    global _task_state_repo_instance
    if _task_state_repo_instance is None:
        _task_state_repo_instance = TaskStateRepo()
    return _task_state_repo_instance