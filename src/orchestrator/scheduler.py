"""Task Scheduler — Priority-based task queuing and dispatch."""

import asyncio
import heapq
import time
import logging
from typing import Any, Dict, Optional, Set
from uuid import uuid4

from src.common.errors import AgentOrchestratorError
from src.common.metrics import metrics

logger = logging.getLogger(__name__)


class QueuePayloadError(AgentOrchestratorError):
    """Raised when a queue payload fails validation."""
    def __init__(self, reason: str, task_id: str = None):
        self.task_id = task_id
        self.reason = reason
        super().__init__(f"Queue payload validation failed: {reason}")


class DuplicateTaskError(QueuePayloadError):
    """Raised when a duplicate task is detected."""
    def __init__(self, task_id: str):
        super().__init__(f"Duplicate task detected: {task_id}", task_id)


class StaleTaskError(QueuePayloadError):
    """Raised when a stale task is detected."""
    def __init__(self, task_id: str, reason: str):
        super().__init__(f"Stale task rejected: {reason}", task_id)


class PayloadValidator:
    """Validates queue job payloads to ensure invariants are maintained."""

    VALID_TASK_TYPES = {"agent_run", "task", "handler", "test"}
    MAX_PAYLOAD_SIZE = 1024 * 1024  # 1MB max payload

    def __init__(self):
        self._seen_task_ids: Set[str] = set()
        self._metrics = metrics

    def validate(self, task: Dict[str, Any]) -> None:
        if not task or not isinstance(task, dict):
            raise QueuePayloadError("Task must be a non-empty dictionary")

        # Check for required field: type
        if "type" not in task:
            raise QueuePayloadError("Missing required field: type")

        # Validate task type (allows legacy "test" type for backwards compatibility)
        task_type = task.get("type")
        valid_extended = self.VALID_TASK_TYPES | {"test", "low", "high"}
        if task_type not in valid_extended:
            raise QueuePayloadError(f"Invalid task type: {task_type}")

        # Payload is required for agent_run/task/handler but optional for legacy
        if task_type in ("agent_run", "task", "handler"):
            if "payload" not in task:
                raise QueuePayloadError(f"Missing required field: payload for {task_type}")

        # Check for duplicate task_id if present
        task_id = task.get("id")
        if task_id:
            if task_id in self._seen_task_ids:
                logger.warning(f"Duplicate task_id detected: {task_id}")
                raise DuplicateTaskError(task_id)
            self._seen_task_ids.add(task_id)

        # Validate payload size (only if payload exists)
        import json
        try:
            payload = task.get("payload", {})
            payload_str = json.dumps(payload)
            if len(payload_str) > self.MAX_PAYLOAD_SIZE:
                raise QueuePayloadError(
                    f"Payload size exceeds limit: {len(payload_str)} > {self.MAX_PAYLOAD_SIZE}"
                )
        except (TypeError, ValueError) as e:
            raise QueuePayloadError(f"Invalid payload serialization: {e}")

        # Record metrics for validation success
        self._metrics.increment("scheduler.payload_validated")
    def validate_lifecycle_transition(
        self, 
        task_id: str, 
        current_state: str, 
        proposed_state: str,
        allowed_transitions: Dict[str, Set[str]]
    ) -> None:
        """
        Validate a lifecycle state transition.
        
        Args:
            task_id: The task identifier
            current_state: Current lifecycle state
            proposed_state: Proposed new state
            allowed_transitions: Map of current_state -> set of allowed next states
            
        Raises:
            StaleTaskError: If the transition is not allowed
        """
        allowed = allowed_transitions.get(current_state, set())
        if proposed_state not in allowed:
            raise StaleTaskError(
                task_id, 
                f"Invalid transition {current_state} -> {proposed_state}"
            )
        
        logger.info(
            f"Validated transition for task {task_id}: {current_state} -> {proposed_state}"
        )

    def clear_seen_task(self, task_id: str) -> None:
        """Clear a task_id from the seen set to allow retry after completion."""
        self._seen_task_ids.discard(task_id)

    def reset(self) -> None:
        """Reset all validation state."""
        self._seen_task_ids.clear()


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


class TaskScheduler:
    # Allowed lifecycle state transitions
    LIFECYCLE_TRANSITIONS = {
        "pending": {"running", "skipped"},
        "running": {"completed", "failed"},
        "failed": {"pending"},  # Allow retry
    }

    def __init__(self):
        self._queues: Dict[str, PriorityQueue] = {}
        self._scheduled: Dict[str, float] = {}
        self._in_flight: Dict[str, Dict] = {}
        self._task_states: Dict[str, str] = {}  # Track task lifecycle states
        self._max_retries = 3
        self._validator = PayloadValidator()

    def enqueue(self, task: Dict, queue: str = "default", priority: int = 0) -> str:
        """
        Enqueue a task after validating the payload.
        
        Raises:
            QueuePayloadError: If payload validation fails
        """
        # Validate payload before enqueueing
        self._validator.validate(task)
        
        task_id = str(uuid4())
        task["id"] = task_id
        task["enqueued_at"] = time.time()
        task["retries"] = 0
        task["state"] = task.get("state", "pending")

        if queue not in self._queues:
            self._queues[queue] = PriorityQueue()
        self._queues[queue].push(task, priority)
        
        # Track initial lifecycle state
        self._task_states[task_id] = task["state"]
        
        logger.info(f"Task {task_id} enqueued to queue '{queue}'")
        metrics.increment("scheduler.task_enqueued")
        return task_id

    def schedule(self, task: Dict, delay: float, queue: str = "default", priority: int = 0) -> str:
        task_id = str(uuid4())
        task["id"] = task_id
        self._scheduled[task_id] = time.time() + delay
        return task_id

    async def dequeue(self, queue: str = "default", timeout: float = 1.0) -> Optional[Dict]:
        now = time.time()
        expired = [tid for tid, t in self._scheduled.items() if t <= now]
        for tid in expired:
            task = self._scheduled.pop(tid)
            if task:
                self.enqueue(task, queue)

        if queue in self._queues and len(self._queues[queue]) > 0:
            task = self._queues[queue].pop()
            if task:
                task_id = task.get("id")
                current_state = self._task_states.get(task_id, "pending")
                
                # Validate lifecycle transition before moving to in_flight
                try:
                    self._validator.validate_lifecycle_transition(
                        task_id,
                        current_state,
                        "running",
                        self.LIFECYCLE_TRANSITIONS
                    )
                except StaleTaskError as e:
                    logger.warning(f"Rejecting stale task {task_id}: {e.reason}")
                    metrics.increment("scheduler.task_rejected_stale")
                    return None
                
                # Update state to running
                self._task_states[task_id] = "running"
                self._in_flight[task_id] = task
                metrics.increment("scheduler.task_dequeued")
                return task
        return None

    def complete(self, task_id: str) -> bool:
        """Complete a task and update lifecycle state."""
        task = self._in_flight.pop(task_id, None)
        if task:
            current_state = self._task_states.get(task_id, "running")
            try:
                self._validator.validate_lifecycle_transition(
                    task_id,
                    current_state,
                    "completed",
                    self.LIFECYCLE_TRANSITIONS
                )
                self._task_states[task_id] = "completed"
                # Clear from seen set to allow new task with same payload
                self._validator.clear_seen_task(task_id)
                metrics.increment("scheduler.task_completed")
                return True
            except StaleTaskError:
                metrics.increment("scheduler.task_rejected_stale")
                return False
        return False

    def fail(self, task_id: str, queue: str = "default") -> bool:
        """Fail a task and optionally re-enqueue for retry."""
        task = self._in_flight.pop(task_id, None)
        if task:
            current_state = self._task_states.get(task_id, "running")
            try:
                self._validator.validate_lifecycle_transition(
                    task_id,
                    current_state,
                    "failed",
                    self.LIFECYCLE_TRANSITIONS
                )
                self._task_states[task_id] = "failed"
            except StaleTaskError:
                metrics.increment("scheduler.task_rejected_stale")
                return False
                
            task["retries"] += 1
            if task["retries"] < self._max_retries:
                # Transition from failed to pending for retry
                self._task_states[task_id] = "pending"
                self.enqueue(task, queue, priority=task.get("priority", 0))
                metrics.increment("scheduler.task_retried")
                return True
            metrics.increment("scheduler.task_exhausted")
        return False

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
