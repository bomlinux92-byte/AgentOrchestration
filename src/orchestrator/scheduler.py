"""Task Scheduler — Priority-based task queuing and dispatch."""

import asyncio
import heapq
import random
import time
from typing import Any, Dict, Optional
from uuid import uuid4


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
    """
    Priority-based task scheduler with bounded retries, jitter, and
    idempotent state-machine guard.

    Bug fixes for issue #1546:
    - Retries use jitter (exponential backoff with random noise) instead of
      immediate re-enqueue, preventing thundering-herd on transient failures.
    - Scheduled tasks are persisted as (task_dict, deadline) pairs, not bare
      timestamps, so expired entries carry the correct payload.
    - A durable terminal-outcome set prevents duplicate completions from
      orphaning work or overwriting newer state.
    """

    # Terminal outcomes — once a task reaches one of these states it must
    # not be accepted as fresh work again.
    TERMINAL_OUTCOMES: frozenset = frozenset({"completed", "failed", "cancelled"})

    def __init__(self):
        self._queues: Dict[str, PriorityQueue] = {}
        # Store (task_dict, deadline) pairs so expired entries carry payload.
        self._scheduled: Dict[str, tuple] = {}
        self._in_flight: Dict[str, Dict] = {}
        # Terminal outcomes are persisted here and checked before mutation.
        self._outcome: Dict[str, str] = {}
        self._max_retries = 3
        # Jitter configuration: base delay and max jitter fraction.
        self._retry_base_delay = 0.5
        self._jitter_fraction = 0.3

    def _jitter_delay(self, attempt: int) -> float:
        """Compute an exponential-backoff delay with random jitter."""
        base = self._retry_base_delay * (2 ** attempt)
        jitter_range = base * self._jitter_fraction
        return base + random.uniform(-jitter_range, jitter_range)

    def _is_terminal(self, task_id: str) -> bool:
        """Return True if the task has already reached a terminal outcome."""
        return task_id in self._outcome

    def _set_outcome(self, task_id: str, outcome: str) -> None:
        """
        Persist a terminal outcome for a task.

        Prevents duplicate events (e.g. a second 'complete' call) from
        overwriting a newer state or leaving orphaned locks.
        """
        if task_id in self._outcome:
            # Already has an outcome — reject if it's a different one.
            if self._outcome[task_id] != outcome:
                raise ValueError(
                    f"Task {task_id} already has outcome "
                    f"'{self._outcome[task_id]}', cannot set '{outcome}'"
                )
            # Idempotent: same outcome is a no-op.
            return
        self._outcome[task_id] = outcome

    def enqueue(
        self, task: Dict, queue: str = "default", priority: int = 0
    ) -> str:
        task_id = task.get("id")
        if task_id is None:
            task_id = str(uuid4())
            task["id"] = task_id
        else:
            # Idempotency guard: do not re-enqueue a task that already has
            # a terminal outcome recorded.
            if self._is_terminal(task_id):
                return task_id

        task["enqueued_at"] = time.time()
        task["retries"] = task.get("retries", 0)

        if queue not in self._queues:
            self._queues[queue] = PriorityQueue()
        self._queues[queue].push(task, priority)
        return task_id

    def schedule(
        self,
        task: Dict,
        delay: float,
        queue: str = "default",
        priority: int = 0,
    ) -> str:
        task_id = task.get("id")
        if task_id is None:
            task_id = str(uuid4())
            task["id"] = task_id

        if self._is_terminal(task_id):
            return task_id

        deadline = time.time() + delay
        self._scheduled[task_id] = (task, deadline)
        return task_id

    async def dequeue(
        self, queue: str = "default", timeout: float = 1.0
    ) -> Optional[Dict]:
        now = time.time()
        # Move expired scheduled tasks into the queue.
        expired_ids = [tid for tid, (_, dl) in self._scheduled.items() if dl <= now]
        for tid in expired_ids:
            task_dict, _ = self._scheduled.pop(tid)
            if task_dict and not self._is_terminal(tid):
                self.enqueue(task_dict, queue)

        if queue in self._queues and len(self._queues[queue]) > 0:
            task = self._queues[queue].pop()
            if task:
                task_id = task["id"]
                if self._is_terminal(task_id):
                    # Do not hand out a task that has already concluded.
                    return None
                self._in_flight[task_id] = task
                return task
        return None

    def complete(self, task_id: str) -> bool:
        """
        Record task completion.  Idempotent — calling complete() twice with
        the same task_id is a no-op on the second call.
        """
        if task_id not in self._in_flight:
            return False
        if self._is_terminal(task_id):
            # Already concluded — reject state overwrite.
            return False
        self._set_outcome(task_id, "completed")
        self._in_flight.pop(task_id, None)
        return True

    def fail(self, task_id: str, queue: str = "default") -> bool:
        """
        Record a transient failure and re-enqueue with bounded retry and
        jitter if the retry budget remains.  Returns True when the task was
        retried; False when it was dropped (no budget left or already
        terminal).
        """
        task = self._in_flight.pop(task_id, None)
        if task is None:
            return False
        if self._is_terminal(task_id):
            return False

        task["retries"] = task.get("retries", 0) + 1
        if task["retries"] >= self._max_retries:
            # Exhausted budget — record terminal failure.
            self._set_outcome(task_id, "failed")
            return False

        # Compute jittered delay and re-schedule instead of immediate enqueue.
        delay = self._jitter_delay(task["retries"])
        self.schedule(task, delay, queue, priority=task.get("priority", 0))
        return True

    def cancel(self, task_id: str) -> bool:
        """
        Cancel a task regardless of its current state (queued, scheduled,
        or in-flight).  Idempotent — cancelling an already-cancelled or
        completed task returns True without changing the existing outcome.
        """
        existing = self._outcome.get(task_id)
        if existing is not None:
            # Already concluded — idempotent, return True.
            return True

        self._set_outcome(task_id, "cancelled")

        # Remove from all mutable state locations.
        self._in_flight.pop(task_id, None)
        self._scheduled.pop(task_id, None)
        return True

    def get_outcome(self, task_id: str) -> Optional[str]:
        """Return the recorded terminal outcome for a task, or None."""
        return self._outcome.get(task_id, None)

    @property
    def max_retries(self) -> int:
        return self._max_retries

    @max_retries.setter
    def max_retries(self, value: int) -> None:
        if value < 0:
            raise ValueError("max_retries must be non-negative")
        self._max_retries = value


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

# 2026-05-21 update — issue #1546: bounded retry with jitter,
#   scheduled task payload fix, terminal-outcome idempotency guard
