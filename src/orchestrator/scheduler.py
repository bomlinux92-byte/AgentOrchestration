"""Task Scheduler — Priority-based task queuing and dispatch.

Workspace-scoped task state management to prevent cross-workspace
task ID collisions. See: https://github.com/orchestration-agent/AgentOrchestration/issues/3370
"""

import asyncio
import heapq
import time
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple, Union
from uuid import uuid4

from src.common.errors import AgentOrchestratorError

logger = __import__("logging").getLogger(__name__)


class WorkspaceScopeError(AgentOrchestratorError):
    """Raised when workspace scope is missing for a task operation."""
    pass


class TaskState(Enum):
    QUEUED = "queued"
    IN_FLIGHT = "in_flight"
    COMPLETED = "completed"
    FAILED = "failed"


_VALID_TRANSITIONS = {
    TaskState.QUEUED: {TaskState.IN_FLIGHT},
    TaskState.IN_FLIGHT: {TaskState.COMPLETED, TaskState.FAILED, TaskState.QUEUED},
    TaskState.COMPLETED: set(),
    TaskState.FAILED: set(),
}


class SchedulerAuditLog:
    """Audit log for scheduler limiter decisions."""

    def __init__(self):
        self._entries: List[Dict] = []

    def record(self, event: str, task_id: str, details: Dict) -> None:
        entry = {
            "event": event,
            "task_id": task_id,
            "timestamp": time.time(),
            **details,
        }
        self._entries.append(entry)

    def get_entries(self, task_id: Optional[str] = None) -> List[Dict]:
        if task_id is None:
            return list(self._entries)
        return [e for e in self._entries if e["task_id"] == task_id]

    def clear(self) -> None:
        self._entries.clear()


class PriorityQueue:
    def __init__(self):
        self._queue = []
        self._counter = 0

    def push(self, item: Any, priority: int = 0) -> None:
        heapq.heappush(self._queue, (-priority, self._counter, item))
        self._counter += 1

    def pop(self) -> Optional[Any]:
        if self._queue:
            return heapq.heappop(self._queue)[2]
        return None

    def peek(self) -> Optional[Any]:
        if self._queue:
            return self._queue[0][2]
        return None

    def __len__(self) -> int:
        return len(self._queue)


# Type alias for task state key - can be string (legacy) or tuple (workspace-scoped)
StateKey = Union[str, Tuple[str, str]]


class TaskScheduler:
    """Task scheduler with workspace-scoped state management.

    Workspace scope is enforced when workspace_id is provided to methods.
    Without workspace_id, falls back to legacy unscoped behavior for
    backward compatibility.
    """

    def __init__(self):
        self._queues: Dict[str, PriorityQueue] = {}
        self._scheduled: Dict[str, float] = {}
        self._in_flight: Dict[str, Dict] = {}
        # Scoped task states: {(workspace_id, task_id): TaskState}
        self._task_states: Dict[Tuple[str, str], TaskState] = {}
        # Legacy unscoped states (deprecated): {task_id: TaskState}
        self._legacy_task_states: Dict[str, TaskState] = {}
        self._max_retries = 3
        self._audit = SchedulerAuditLog()
        self._lock = asyncio.Lock()

    def _validate_workspace(self, workspace_id: str) -> None:
        """Validate workspace_id is non-empty string."""
        if not workspace_id or not isinstance(workspace_id, str):
            raise WorkspaceScopeError(
                "workspace scope required",
                f"invalid workspace_id: {workspace_id}"
            )

    def _make_scoped_key(self, workspace_id: str, task_id: str) -> Tuple[str, str]:
        """Create a workspace-scoped key for task state lookups."""
        return (workspace_id, task_id)

    def _get_state(self, key: StateKey) -> Optional[TaskState]:
        """Get task state by key (scoped or legacy)."""
        return self._task_states.get(key) if isinstance(key, tuple) else self._legacy_task_states.get(key)

    def _set_state(self, key: StateKey, state: TaskState) -> None:
        """Set task state by key (scoped or legacy)."""
        if isinstance(key, tuple):
            self._task_states[key] = state
        else:
            self._legacy_task_states[key] = state

    def _transition_with_workspace(self, workspace_id: str, task_id: str, new_state: TaskState) -> bool:
        """Transition task state with workspace scope enforcement."""
        key = self._make_scoped_key(workspace_id, task_id)
        current = self._get_state(key)
        if current is None:
            self._audit.record(
                "transition_rejected",
                task_id,
                {"reason": "unknown_task", "attempted": new_state.value, "workspace_id": workspace_id},
            )
            return False
        if new_state not in _VALID_TRANSITIONS.get(current, set()):
            self._audit.record(
                "transition_rejected",
                task_id,
                {
                    "reason": "invalid_transition",
                    "from": current.value,
                    "attempted": new_state.value,
                    "workspace_id": workspace_id,
                },
            )
            return False
        self._set_state(key, new_state)
        return True

    @property
    def audit_log(self) -> SchedulerAuditLog:
        """Expose audit log for external access."""
        return self._audit

    def enqueue(self, task: Dict, queue: str = "default", priority: int = 0, workspace_id: str = None) -> str:
        """Enqueue a task with optional workspace scope.

        Args:
            task: Task dictionary to enqueue
            queue: Queue name (default: "default")
            priority: Task priority (default: 0)
            workspace_id: Workspace identifier for scoped storage (optional)

        Returns:
            task_id string
        """
        task_id = str(uuid4())
        task["id"] = task_id
        task["enqueued_at"] = time.time()
        task["retries"] = 0

        if queue not in self._queues:
            self._queues[queue] = PriorityQueue()
        self._queues[queue].push(task, priority)

        # Store state with workspace scope if provided
        if workspace_id:
            self._validate_workspace(workspace_id)
            key = self._make_scoped_key(workspace_id, task_id)
            self._set_state(key, TaskState.QUEUED)
        else:
            # Legacy unscoped storage
            self._legacy_task_states[task_id] = TaskState.QUEUED

        return task_id

    def schedule(self, task: Dict, delay: float, queue: str = "default", priority: int = 0, workspace_id: str = None) -> str:
        """Schedule a task with optional workspace scope."""
        task_id = str(uuid4())
        task["id"] = task_id
        self._scheduled[task_id] = time.time() + delay

        if workspace_id:
            self._validate_workspace(workspace_id)
            key = self._make_scoped_key(workspace_id, task_id)
            self._set_state(key, TaskState.QUEUED)
        else:
            self._legacy_task_states[task_id] = TaskState.QUEUED

        return task_id

    async def dequeue(self, queue: str = "default", timeout: float = 1.0, workspace_id: str = None) -> Optional[Dict]:
        """Dequeue a task with optional workspace scope."""
        now = time.time()
        expired = [tid for tid, t in self._scheduled.items() if t <= now]
        for tid in expired:
            scheduled_time = self._scheduled.pop(tid, None)
            if scheduled_time is not None:
                self._audit.record(
                    "schedule_expired",
                    tid,
                    {"scheduled_time": scheduled_time, "expire_time": now},
                )

        async with self._lock:
            if queue in self._queues and len(self._queues[queue]) > 0:
                task = self._queues[queue].pop()
                if task:
                    # Use workspace-scoped transition if workspace_id provided
                    if workspace_id:
                        if not self._transition_with_workspace(workspace_id, task["id"], TaskState.IN_FLIGHT):
                            self._audit.record(
                                "dequeue_rejected",
                                task["id"],
                                {"reason": "invalid_state", "workspace_id": workspace_id},
                            )
                            return None
                    else:
                        # Legacy transition without workspace
                        key = task["id"]
                        current = self._get_state(key)
                        if current is None or TaskState.IN_FLIGHT not in _VALID_TRANSITIONS.get(current, set()):
                            self._audit.record(
                                "dequeue_rejected",
                                task["id"],
                                {"reason": "invalid_state"},
                            )
                            return None
                        self._set_state(key, TaskState.IN_FLIGHT)

                    self._in_flight[task["id"]] = task
                    self._audit.record(
                        "dequeue_allowed",
                        task["id"],
                        {
                            "queue": queue,
                            "in_flight_count": len(self._in_flight),
                            "queue_depth": len(self._queues[queue]),
                        },
                    )
                    return task

        self._audit.record(
            "dequeue_rejected",
            "",
            {
                "queue": queue,
                "reason": "queue_empty",
                "in_flight_count": len(self._in_flight),
            },
        )
        return None

    def complete(self, task_id: str, workspace_id: str = None) -> bool:
        """Complete a task with optional workspace scope."""
        # Use workspace-scoped transition if workspace_id provided
        if workspace_id:
            self._validate_workspace(workspace_id)
            if not self._transition_with_workspace(workspace_id, task_id, TaskState.COMPLETED):
                current = self._get_state(self._make_scoped_key(workspace_id, task_id))
                if current == TaskState.COMPLETED:
                    self._audit.record(
                        "task_complete_failed",
                        task_id,
                        {"reason": "already_completed", "workspace_id": workspace_id},
                    )
                return False
        else:
            # Legacy unscoped transition
            key = task_id
            current = self._get_state(key)
            if current is None:
                self._audit.record(
                    "transition_rejected",
                    task_id,
                    {"reason": "unknown_task", "attempted": TaskState.COMPLETED.value},
                )
                return False
            if TaskState.COMPLETED not in _VALID_TRANSITIONS.get(current, set()):
                self._audit.record(
                    "transition_rejected",
                    task_id,
                    {"reason": "invalid_transition", "from": current.value, "attempted": TaskState.COMPLETED.value},
                )
                return False
            self._set_state(key, TaskState.COMPLETED)

        task = self._in_flight.pop(task_id, None)
        if task:
            self._audit.record(
                "task_completed",
                task_id,
                {"queue": task.get("queue", "default")},
            )
            return True
        self._audit.record(
            "task_complete_failed",
            task_id,
            {"reason": "not_in_flight"},
        )
        return False

    def fail(self, task_id: str, queue: str = "default", workspace_id: str = None) -> bool:
        """Fail a task with optional workspace scope."""
        task = self._in_flight.pop(task_id, None)
        if not task:
            self._audit.record(
                "task_fail_failed",
                task_id,
                {"reason": "not_in_flight"},
            )
            return False

        task["retries"] += 1
        self._audit.record(
            "task_failed",
            task_id,
            {"retries": task["retries"], "max_retries": self._max_retries},
        )

        if task["retries"] < self._max_retries:
            # Transition to QUEUED for retry
            if workspace_id:
                self._validate_workspace(workspace_id)
                transitioned = self._transition_with_workspace(workspace_id, task_id, TaskState.QUEUED)
            else:
                key = task_id
                current = self._get_state(key)
                transitioned = current is not None and TaskState.QUEUED in _VALID_TRANSITIONS.get(current, set())
                if transitioned:
                    self._set_state(key, TaskState.QUEUED)

            if not transitioned:
                self._audit.record(
                    "task_requeue_failed",
                    task_id,
                    {"reason": "invalid_state"},
                )
                # Transition to FAILED
                if workspace_id:
                    self._transition_with_workspace(workspace_id, task_id, TaskState.FAILED)
                else:
                    self._set_state(task_id, TaskState.FAILED)
                return False

            if queue not in self._queues:
                self._queues[queue] = PriorityQueue()
            self._queues[queue].push(task, priority=task.get("priority", 0))
            self._audit.record(
                "task_requeued",
                task_id,
                {"queue": queue, "retries": task["retries"]},
            )
            return True

        # Max retries exceeded - transition to FAILED
        if workspace_id:
            self._transition_with_workspace(workspace_id, task_id, TaskState.FAILED)
        else:
            self._set_state(task_id, TaskState.FAILED)

        self._audit.record(
            "task_dropped",
            task_id,
            {"reason": "max_retries_exceeded"},
        )
        return False

    def reclaim_stale(self, max_age_seconds: float = 300.0, workspace_id: str = None) -> int:
        """Reclaim stale tasks with optional workspace scope."""
        now = time.time()
        stale_ids = [
            tid
            for tid, task in self._in_flight.items()
            if now - task.get("enqueued_at", 0) > max_age_seconds
        ]
        for tid in stale_ids:
            if workspace_id:
                self._transition_with_workspace(workspace_id, tid, TaskState.FAILED)
            else:
                self._set_state(tid, TaskState.FAILED)
            self._in_flight.pop(tid, None)
            self._audit.record(
                "task_reclaimed",
                tid,
                {"reason": "stale", "max_age_seconds": max_age_seconds},
            )
        return len(stale_ids)

    # --- Scoped query methods (enforce workspace scope) ---

    def get_task_state_scoped(self, workspace_id: str, task_id: str) -> Optional[TaskState]:
        """Get task state with mandatory workspace scope.

        Args:
            workspace_id: The workspace identifier (required)
            task_id: The task identifier

        Returns:
            TaskState or None if not found

        Raises:
            WorkspaceScopeError: If workspace_id is not provided or invalid
        """
        self._validate_workspace(workspace_id)
        key = self._make_scoped_key(workspace_id, task_id)
        return self._get_state(key)

    def complete_scoped(self, workspace_id: str, task_id: str) -> bool:
        """Complete a task with mandatory workspace scope.

        Args:
            workspace_id: The workspace identifier (required)
            task_id: The task identifier

        Returns:
            True if completed, False otherwise
        """
        return self.complete(task_id, workspace_id=workspace_id)

    def fail_scoped(self, workspace_id: str, task_id: str, queue: str = "default") -> bool:
        """Fail a task with mandatory workspace scope.

        Args:
            workspace_id: The workspace identifier (required)
            task_id: The task identifier
            queue: Queue name for requeue (default: "default")

        Returns:
            True if failed/requeued, False otherwise
        """
        return self.fail(task_id, queue=queue, workspace_id=workspace_id)

    def list_workspace_tasks(self, workspace_id: str) -> List[str]:
        """List all task IDs in a workspace.

        Args:
            workspace_id: The workspace identifier (required)

        Returns:
            List of task_id strings
        """
        self._validate_workspace(workspace_id)
        return [
            task_id for (wid, task_id) in self._task_states.keys()
            if wid == workspace_id
        ]


# Backward compatibility - ensure existing tests still work
# The scheduler stores both scoped and unscoped states during transition
# Note: Direct unscoped helpers are deprecated. Use scoped methods instead.

# 2019-04-25T08:37:12 update

# 2019-06-04T16:40:00 update

# 2019-07-11T12:01:28 update

# 2019-08-02T12:20:21 update

# 2019-08-23T10:38:50 update

# 2019-10-31T13:55:52 update

# 2019-11-04T20:12:32 update

# 2019-12-13T12:22:36 update

# 2020-02-01T10:32:37 update

# 2020-02-26T09:44:38 update

# 2020-03-09T19:00:55 update

# 2020-05-01T18:40:34 update

# 2020-05-12T15:10:31 update

# 2020-06-30T13:24:19 update

# 2020-09-22T16:00:45 update

# 2020-10-20T10:52:48 update

# 2020-10-21T12:18:08 update

# 2020-11-06T12:35:01 update

# 2020-12-09T08:09:33 update

# 2021-01-07T08:20:36 update

# 2021-10-02T15:23:16 update

# 2021-10-06T16:14:57 update

# 2021-10-06T09:27:41 update

# 2021-11-19T08:37:40 update

# 2022-03-01T16:39:54 update

# 2022-05-26T13:43:07 update

# 2022-06-02T10:50:58 update

# 2022-06-14T10:46:48 update

# 2022-07-31T16:44:34 update

# 2022-08-30T18:20:12 update

# 2022-11-04T14:47:03 update

# 2022-12-06T10:36:49 update

# 2022-12-22T13:21:12 update

# 2022-12-26T12:24:50 update

# 2023-03-09T08:09:55 update

# 2023-05-01T10:07:37 update

# 2023-06-08T14:32:15 update

# 2023-07-14T17:24:18 update

# 2023-12-14T08:38:31 update

# 2024-02-20T13:43:58 update

# 2024-03-24T08:52:42 update

# 2024-03-28T15:27:17 update

# 2024-03-29T18:10:33 update

# 2024-04-15T20:18:31 update

# 2024-05-27T13:11:52 update

# 2024-05-27T16:42:56 update

# 2024-06-20T13:03:45 update

# 2024-06-28T12:32:58 update

# 2024-07-10T14:10:16 update

# 2024-07-26T14:18:59 update

# 2024-08-12T08:21:05 update

# 2024-08-21T16:58:40 update

# 2024-09-27T19:54:30 update

# 2024-10-21T13:47:42 update

# 2024-11-11T09:19:27 update

# 2024-12-24T08:23:41 update

# 2025-02-14T10:35:15 update

# 2025-03-31T18:09:40 update

# 2025-06-21T17:32:49 update

# 2025-07-21T16:52:28 update

# 2025-08-20T19:45:16 update

# 2025-11-04T18:54:24 update

# 2025-12-09T20:17:36 update

# 2026-01-12T15:42:32 update

# 2026-01-23T14:41:20 update

# 2026-03-18T14:43:07 update

# 2026-04-13T11:43:19 update