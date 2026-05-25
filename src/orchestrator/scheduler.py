"""Task Scheduler — Priority-based task queuing and dispatch."""

import asyncio
import heapq
import random
import time
from enum import Enum
from typing import Any, Dict, Optional
from uuid import uuid4


class TaskState(Enum):
    """Task state machine states for idempotent transitions."""
    PENDING = "pending"
    SCHEDULED = "scheduled"
    IN_FLIGHT = "in_flight"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


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
    """Task scheduler with idempotent state transitions and duplicate compensation guards."""
    
    # Jitter configuration: base delay 1s, max delay 30s, multiplier 2x
    RETRY_BASE_DELAY = 1.0
    RETRY_MAX_DELAY = 30.0
    RETRY_JITTER_FACTOR = 0.5

    def __init__(self):
        self._queues: Dict[str, PriorityQueue] = {}
        self._scheduled: Dict[str, tuple] = {}  # task_id -> (delay, task)
        self._in_flight: Dict[str, Dict] = {}  # task_id -> {task, claimed_at}
        self._max_retries = 3
        # Track task state machine for idempotent transitions
        self._task_states: Dict[str, TaskState] = {}
        # Track terminal outcomes durably (task_id -> outcome)
        self._terminal_outcomes: Dict[str, Dict] = {}
        # Track pending retry scheduled times
        self._retry_scheduled: Dict[str, float] = {}
        # Worker disconnect reclaim window (seconds)
        self._lease_timeout: float = 60.0

    def enqueue(self, task: Dict, queue: str = "default", priority: int = 0) -> str:
        task_id = str(uuid4())
        task["id"] = task_id
        task["enqueued_at"] = time.time()
        task["retries"] = 0

        if queue not in self._queues:
            self._queues[queue] = PriorityQueue()
        self._queues[queue].push(task, priority)
        return task_id

    def schedule(self, task: Dict, delay: float, queue: str = "default", priority: int = 0) -> str:
        task_id = str(uuid4())
        task["id"] = task_id
        task["scheduled_at"] = time.time()
        task["retry_at"] = time.time() + delay
        # Store (delay, task) tuple for later retrieval
        self._scheduled[task_id] = (delay, task)
        # Track scheduled state
        self._task_states[task_id] = TaskState.SCHEDULED
        return task_id

    def _compute_retry_delay(self, attempt: int) -> float:
        """Compute jittered retry delay with exponential backoff."""
        base_delay = self.RETRY_BASE_DELAY * (2 ** attempt)
        jitter = base_delay * self.RETRY_JITTER_FACTOR * random.random()
        return min(base_delay + jitter, self.RETRY_MAX_DELAY)

    async def dequeue(self, queue: str = "default", timeout: float = 1.0) -> Optional[Dict]:
        now = time.time()
        # Process expired scheduled tasks (promote to pending queue)
        expired = []
        for tid, (delay, task) in list(self._scheduled.items()):
            if task.get("retry_at", 0) <= now:
                expired.append(tid)

        for tid in expired:
            _, task = self._scheduled.pop(tid)
            self._retry_scheduled.pop(tid, None)
            if task:
                # Guard: reject if task already reached terminal state
                current_state = self._task_states.get(tid)
                if current_state in (TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED):
                    # Skip - task already has terminal outcome
                    continue
                if queue not in self._queues:
                    self._queues[queue] = PriorityQueue()
                self._queues[queue].push(task, priority=task.get("priority", 0))
                self._task_states[tid] = TaskState.PENDING

        if queue in self._queues and len(self._queues[queue]) > 0:
            task = self._queues[queue].pop()
            if task:
                task_id = task["id"]
                # Guard: reject if task already has terminal state
                current_state = self._task_states.get(task_id)
                if current_state in (TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED):
                    # Skip stale task - already processed
                    return None
                self._in_flight[task_id] = {"task": task, "claimed_at": time.time()}
                self._task_states[task_id] = TaskState.IN_FLIGHT
                return task
        return None

    def complete(self, task_id: str) -> bool:
        """Mark task as completed with state machine guard.
        
        Returns True if transition was made, False if task already terminal.
        Records one durable terminal outcome.
        """
        # Guard: check if already terminal
        current_state = self._task_states.get(task_id)
        if current_state in (TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED):
            return False
        
        # Only remove from in_flight if present
        entry = self._in_flight.pop(task_id, None)
        if entry is not None:
            self._task_states[task_id] = TaskState.COMPLETED
            # Record durable terminal outcome
            self._terminal_outcomes[task_id] = {
                "status": "completed",
                "completed_at": time.time(),
            }
            return True
        return False

    def fail(self, task_id: str, queue: str = "default") -> bool:
        """Mark task as failed with jittered retry and state machine guard.
        
        Returns True if retry scheduled, False if terminal (max retries reached
        or already terminal state).
        
        Guards against duplicate compensation scheduling by checking task state
        before committing any state changes.
        """
        current_state = self._task_states.get(task_id)
        # Guard: reject if already terminal - prevents duplicate compensation
        if current_state in (TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED):
            return False
        
        entry = self._in_flight.pop(task_id, None)
        if not entry:
            # Task may be in _scheduled (waiting for retry) - check there
            if task_id in self._scheduled:
                _, task = self._scheduled.pop(task_id)
                self._retry_scheduled.pop(task_id, None)
            else:
                return False
        else:
            task = entry["task"]
        
        if task:
            task["retries"] += 1
            # Record this failure in terminal outcomes if exhausted
            if task["retries"] >= self._max_retries:
                self._task_states[task_id] = TaskState.FAILED
                self._terminal_outcomes[task_id] = {
                    "status": "failed",
                    "retries": task["retries"],
                    "failed_at": time.time(),
                }
                return False
            
            # Compute jittered delay and schedule retry
            delay = self._compute_retry_delay(task["retries"])
            task["retry_at"] = time.time() + delay
            task["retry_delay"] = delay
            
            # Store in scheduled for deferred retry
            self._scheduled[task_id] = (delay, task)
            self._retry_scheduled[task_id] = time.time() + delay
            self._task_states[task_id] = TaskState.SCHEDULED
            return True
        return False

    def cancel(self, task_id: str) -> bool:
        """Cancel a task if not already terminal."""
        current_state = self._task_states.get(task_id)
        if current_state in (TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED):
            return False
        
        # Remove from all tracking structures
        self._in_flight.pop(task_id, None)
        self._scheduled.pop(task_id, None)
        self._retry_scheduled.pop(task_id, None)
        
        self._task_states[task_id] = TaskState.CANCELLED
        self._terminal_outcomes[task_id] = {
            "status": "cancelled",
            "cancelled_at": time.time(),
        }
        return True

    def reclaim_abandoned(self, queue: str = "default") -> int:
        """Reclaim in-flight tasks whose workers have disconnected.
        
        Moves tasks that have been in-flight beyond the lease timeout back
        to the pending queue for re-assignment. Guards against reclaiming
        tasks that have already reached terminal state.
        
        Returns the count of reclaimed tasks.
        """
        now = time.time()
        reclaimed = 0
        
        # Collect stale in-flight entries
        stale_ids = []
        for task_id, entry in list(self._in_flight.items()):
            claimed_at = entry.get("claimed_at", 0)
            if now - claimed_at > self._lease_timeout:
                stale_ids.append(task_id)
        
        for task_id in stale_ids:
            current_state = self._task_states.get(task_id)
            # Guard: only reclaim non-terminal tasks
            if current_state in (TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED):
                continue
            
            entry = self._in_flight.pop(task_id, None)
            if entry:
                task = entry["task"]
                # Reset retry count so task gets a full retry window
                task["retries"] = 0
                
                if queue not in self._queues:
                    self._queues[queue] = PriorityQueue()
                self._queues[queue].push(task, priority=task.get("priority", 0))
                self._task_states[task_id] = TaskState.PENDING
                reclaimed += 1
        
        return reclaimed

    def get_terminal_outcome(self, task_id: str) -> Optional[Dict]:
        """Get the durable terminal outcome for a task."""
        return self._terminal_outcomes.get(task_id)

    def get_task_state(self, task_id: str) -> Optional[TaskState]:
        """Get current task state."""
        return self._task_states.get(task_id)

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