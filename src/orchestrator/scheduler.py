"""Task Scheduler — Priority-based task queuing and dispatch."""

import asyncio
import heapq
import logging
import time
from enum import Enum
from typing import Any, Dict, List, Optional
from uuid import uuid4

logger = logging.getLogger(__name__)


class SchedulerState(Enum):
    """Atomic states for scheduler transitions."""
    IDLE = "idle"
    ACTIVE = "active"
    RECOVERING = "recovering"
    DRAINING = "draining"


class AuditEntry:
    """Bounded audit metadata for scheduler decisions."""
    MAX_ENTRIES = 1000

    def __init__(self, tenant_id: str, task_id: str, action: str, state_before: str, state_after: str, reason: str = ""):
        self.tenant_id = tenant_id
        self.task_id = task_id
        self.action = action
        self.state_before = state_before
        self.state_after = state_after
        self.timestamp = time.time()
        self.reason = reason
        # Sanitize: truncate long fields
        self.task_id = self.task_id[:64] if self.task_id else ""
        self.reason = self.reason[:256] if self.reason else ""

    def to_dict(self) -> Dict:
        return {
            "tenant_id": self.tenant_id,
            "task_id": self.task_id,
            "action": self.action,
            "state_before": self.state_before,
            "state_after": self.state_after,
            "timestamp": self.timestamp,
            "reason": self.reason,
        }


class TenantCapacity:
    """Per-tenant concurrency tracking."""
    def __init__(self, tenant_id: str, max_concurrent: int = 10):
        self.tenant_id = tenant_id
        self.max_concurrent = max_concurrent
        self.active_tasks: Dict[str, float] = {}
        self._lock = asyncio.Lock()

    def can_dispatch(self) -> bool:
        return len(self.active_tasks) < self.max_concurrent

    def acquire(self, task_id: str) -> bool:
        if task_id in self.active_tasks:
            return True  # Already tracking
        if not self.can_dispatch():
            return False
        self.active_tasks[task_id] = time.time()
        return True

    def release(self, task_id: str) -> bool:
        if task_id in self.active_tasks:
            del self.active_tasks[task_id]
            return True
        return False

    @property
    def utilization(self) -> float:
        return len(self.active_tasks) / self.max_concurrent if self.max_concurrent > 0 else 0.0


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
    def __init__(self, default_tenant_max_concurrent: int = 10):
        self._queues: Dict[str, PriorityQueue] = {}
        self._scheduled: Dict[str, Dict] = {}  # task_id -> {task, expires_at}
        self._in_flight: Dict[str, Dict] = {}
        self._max_retries = 3
        self._default_tenant_max = default_tenant_max_concurrent
        # Per-tenant capacity tracking
        self._tenant_capacity: Dict[str, TenantCapacity] = {}
        # Audit log (bounded)
        self._audit_log: List[Dict] = []
        self._audit_lock = asyncio.Lock()
        # Scheduler state for atomic transitions
        self._state = SchedulerState.IDLE

    def _get_tenant_id(self, task: Dict) -> str:
        """Extract tenant ID from task, default to 'default' if not present."""
        return task.get("tenant_id", "default")

    def _get_tenant_capacity(self, tenant_id: str) -> TenantCapacity:
        """Get or create capacity tracker for tenant."""
        if tenant_id not in self._tenant_capacity:
            max_concurrent = self._default_tenant_max
            self._tenant_capacity[tenant_id] = TenantCapacity(tenant_id, max_concurrent)
        return self._tenant_capacity[tenant_id]

    async def _add_audit(self, tenant_id: str, task_id: str, action: str, 
                        state_before: str, state_after: str, reason: str = "") -> None:
        """Add audit entry with bounded retention."""
        entry = AuditEntry(tenant_id, task_id, action, state_before, state_after, reason)
        async with self._audit_lock:
            self._audit_log.append(entry.to_dict())
            # Bounded retention
            if len(self._audit_log) > AuditEntry.MAX_ENTRIES:
                self._audit_log = self._audit_log[-AuditEntry.MAX_ENTRIES:]

    def _can_transition_state(self, new_state: SchedulerState) -> bool:
        """Validate atomic state transitions."""
        transitions = {
            SchedulerState.IDLE: {SchedulerState.ACTIVE, SchedulerState.RECOVERING},
            SchedulerState.ACTIVE: {SchedulerState.DRAINING, SchedulerState.IDLE},
            SchedulerState.RECOVERING: {SchedulerState.ACTIVE, SchedulerState.IDLE},
            SchedulerState.DRAINING: {SchedulerState.IDLE},
        }
        return new_state in transitions.get(self._state, set())

    def enqueue(self, task: Dict, queue: str = "default", priority: int = 0) -> str:
        task_id = str(uuid4())
        task["id"] = task_id
        task["enqueued_at"] = time.time()
        task["retries"] = 0

        # Capacity tracking happens at dispatch time (dequeue), not enqueue time.
        # This allows tasks to queue up and be dispatched when capacity frees up.

        if queue not in self._queues:
            self._queues[queue] = PriorityQueue()
        self._queues[queue].push(task, priority)
        return task_id

    def schedule(self, task: Dict, delay: float, queue: str = "default", priority: int = 0) -> str:
        task_id = str(uuid4())
        task["id"] = task_id
        self._scheduled[task_id] = {"task": task, "expires_at": time.time() + delay, "queue": queue, "priority": priority}
        return task_id

    async def _check_and_acquire_tenant_capacity(self, task: Dict) -> bool:
        """Atomically check tenant capacity and acquire slot. Returns False if at capacity."""
        tenant_id = self._get_tenant_id(task)
        capacity = self._get_tenant_capacity(tenant_id)
        
        if not capacity.can_dispatch():
            return False
        
        task_id = task.get("id", "")
        return capacity.acquire(task_id)

    async def dequeue(self, queue: str = "default", timeout: float = 1.0) -> Optional[Dict]:
        now = time.time()
        # Process expired scheduled tasks
        expired = [tid for tid, data in self._scheduled.items() if data["expires_at"] <= now]
        for tid in expired:
            data = self._scheduled.pop(tid)
            task = data.get("task")
            if task:
                self.enqueue(task, data.get("queue", "default"), data.get("priority", 0))

        if queue in self._queues and len(self._queues[queue]) > 0:
            task = self._queues[queue].pop()
            if task:
                tenant_id = self._get_tenant_id(task)
                capacity = self._get_tenant_capacity(tenant_id)
                
                # Enforce per-tenant concurrency check
                if not capacity.can_dispatch():
                    # Re-queue and return None - at capacity
                    self._queues[queue].push(task, task.get("priority", 0))
                    await self._add_audit(tenant_id, task.get("id", ""), "DEFER", 
                                          self._state.value, self._state.value,
                                          f"At capacity: {len(capacity.active_tasks)}/{capacity.max_concurrent}")
                    logger.info(f"Tenant {tenant_id} at capacity, deferred task {task.get('id')}")
                    return None
                
                # Atomic capacity acquisition and state transition check
                if not await self._check_and_acquire_tenant_capacity(task):
                    self._queues[queue].push(task, task.get("priority", 0))
                    await self._add_audit(tenant_id, task.get("id", ""), "DEFER",
                                          self._state.value, self._state.value,
                                          "Capacity acquire failed")
                    return None
                
                # Update state if recovering
                if self._state == SchedulerState.RECOVERING:
                    if self._can_transition_state(SchedulerState.ACTIVE):
                        old_state = self._state.value
                        self._state = SchedulerState.ACTIVE
                        logger.info(f"Scheduler state transition: {old_state} -> {self._state.value}")
                
                self._in_flight[task["id"]] = task
                await self._add_audit(tenant_id, task["id"], "DISPATCH",
                                      self._state.value, self._state.value,
                                      "Recovery dispatch" if self._state == SchedulerState.RECOVERING else "Normal dispatch")
                return task
        return None

    async def recover_in_flight(self) -> Dict[str, Any]:
        """
        Recovery scanner for process restarts.
        Enforces per-tenant concurrency by checking in-flight tasks against capacity.
        Returns audit info about recovery actions taken.
        """
        await self._add_audit("system", "recovery", "START_RECOVERY",
                              self._state.value, SchedulerState.RECOVERING.value,
                              f"Recovering {len(self._in_flight)} in-flight tasks")
        
        old_state = self._state.value
        if not self._can_transition_state(SchedulerState.RECOVERING):
            logger.warning(f"Cannot transition to RECOVERING from {self._state.value}")
            return {"status": "skipped", "reason": f"Invalid state transition from {self._state.value}"}
        
        self._state = SchedulerState.RECOVERING
        
        recovered = []
        rejected = []
        deferred = []
        
        for task_id, task in list(self._in_flight.items()):
            tenant_id = self._get_tenant_id(task)
            capacity = self._get_tenant_capacity(tenant_id)
            
            if len(capacity.active_tasks) >= capacity.max_concurrent:
                # At capacity - reject the in-flight task
                self._in_flight.pop(task_id, None)
                rejected.append({"task_id": task_id, "tenant_id": tenant_id, "reason": "capacity_exceeded"})
                await self._add_audit(tenant_id, task_id, "REJECT",
                                      SchedulerState.RECOVERING.value, SchedulerState.RECOVERING.value,
                                      f"Capacity exceeded: {len(capacity.active_tasks)}/{capacity.max_concurrent}")
                logger.warning(f"Recovery rejected task {task_id} for tenant {tenant_id} - at capacity")
            else:
                # Within capacity - keep tracking
                capacity.acquire(task_id)
                recovered.append({"task_id": task_id, "tenant_id": tenant_id})
                await self._add_audit(tenant_id, task_id, "KEEP",
                                      SchedulerState.RECOVERING.value, SchedulerState.RECOVERING.value,
                                      "Within capacity, kept in recovery")
        
        result = {
            "status": "completed",
            "previous_state": old_state,
            "current_state": self._state.value,
            "recovered_count": len(recovered),
            "rejected_count": len(rejected),
            "deferred_count": len(deferred),
            "recovered": recovered,
            "rejected": rejected,
            "deferred": deferred,
        }
        
        logger.info(f"Recovery scan completed: {len(recovered)} recovered, {len(rejected)} rejected, {len(deferred)} deferred")
        return result

    def complete(self, task_id: str) -> bool:
        task = self._in_flight.pop(task_id, None)
        if task:
            # Release tenant capacity
            tenant_id = self._get_tenant_id(task)
            capacity = self._get_tenant_capacity(tenant_id)
            capacity.release(task_id)
            return True
        return False

    def fail(self, task_id: str, queue: str = "default") -> bool:
        task = self._in_flight.pop(task_id, None)
        if task:
            # Release tenant capacity
            tenant_id = self._get_tenant_id(task)
            capacity = self._get_tenant_capacity(tenant_id)
            capacity.release(task_id)
            
            task["retries"] += 1
            if task["retries"] < self._max_retries:
                self.enqueue(task, queue, priority=task.get("priority", 0))
                return True
        return False

    def get_tenant_utilization(self, tenant_id: str) -> float:
        """Get utilization for a specific tenant."""
        if tenant_id in self._tenant_capacity:
            return self._tenant_capacity[tenant_id].utilization
        return 0.0

    async def get_audit_log(self, limit: int = 100) -> List[Dict]:
        """Get recent audit entries."""
        async with self._audit_lock:
            return self._audit_log[-limit:]

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
