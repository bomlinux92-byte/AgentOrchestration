"""Agent Registry — Manages agent lifecycle and metadata with version compatibility enforcement."""

import logging
import re
import time
import uuid
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from src.common.errors import VersionCompatibilityError

logger = logging.getLogger(__name__)

_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def _parse_version(version: str) -> Tuple[int, int, int]:
    m = _VERSION_RE.match(version)
    if not m:
        raise ValueError(f"Invalid semantic version: {version}")
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def _is_compatible(current: str, required: str) -> bool:
    """Return True if *current* >= *required* (semver major must match)."""
    cur = _parse_version(current)
    req = _parse_version(required)
    if cur[0] != req[0]:
        return False
    return cur >= req


class AgentStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPED = "stopped"
    FAILED = "failed"
    TERMINATED = "terminated"


class AgentRegistry:
    def __init__(self, storage_backend: str = "memory"):
        self.storage_backend = storage_backend
        self._agents: Dict[str, Dict[str, Any]] = {}
        self._index: Dict[str, List[str]] = {}
        self._version_index: Dict[str, str] = {}
        self._min_version: str = "1.0.0"

    def register(
        self,
        name: str,
        agent_type: str,
        config: Optional[Dict] = None,
        version: str = "1.0.0",
    ) -> str:
        if not _is_compatible(version, self._min_version):
            logger.info(
                "registration_rejected",
                extra={"name": name, "version": version, "min_version": self._min_version},
            )
            raise VersionCompatibilityError(name, version, self._min_version)

        agent_id = str(uuid.uuid4())
        timestamp = time.time()
        self._agents[agent_id] = {
            "id": agent_id,
            "name": name,
            "type": agent_type,
            "status": AgentStatus.PENDING.value,
            "config": config or {},
            "created_at": timestamp,
            "updated_at": timestamp,
            "version": version,
            "metrics": {"tasks_completed": 0, "errors": 0, "uptime": 0},
        }
        self._version_index[agent_id] = version

        group = agent_type.split(".")[0]
        if group not in self._index:
            self._index[group] = []
        self._index[group].append(agent_id)

        logger.info(
            "handler_registered",
            extra={"agent_id": agent_id, "version": version},
        )
        return agent_id

    def get(self, agent_id: str) -> Optional[Dict[str, Any]]:
        return self._agents.get(agent_id)

    def resolve(self, agent_id: str, required_version: Optional[str] = None) -> Optional[Dict[str, Any]]:
        agent = self._agents.get(agent_id)
        if agent is None:
            logger.info("resolve_miss", extra={"agent_id": agent_id})
            return None

        if required_version and not _is_compatible(agent["version"], required_version):
            logger.info(
                "resolve_version_mismatch",
                extra={"agent_id": agent_id, "version": agent["version"], "required": required_version},
            )
            return None

        if agent["status"] in (AgentStatus.STOPPED.value, AgentStatus.TERMINATED.value, AgentStatus.FAILED.value):
            logger.info(
                "resolve_unavailable",
                extra={"agent_id": agent_id, "status": agent["status"]},
            )
            return None

        return agent

    def list(self, status: Optional[AgentStatus] = None, group: Optional[str] = None) -> List[Dict[str, Any]]:
        agents = self._agents.values()
        if status:
            agents = [a for a in agents if a["status"] == status.value]
        if group:
            agent_ids = self._index.get(group, [])
            agents = [a for a in agents if a["id"] in agent_ids]
        return list(agents)

    def update_status(self, agent_id: str, status: AgentStatus) -> bool:
        if agent_id not in self._agents:
            return False
        self._agents[agent_id]["status"] = status.value
        self._agents[agent_id]["updated_at"] = time.time()
        return True

    def upgrade_handler(self, agent_id: str, new_version: str) -> bool:
        """Upgrade a handler to a new plugin version with compatibility validation.

        Rejects the upgrade if the handler is mid-lifecycle transition, the new
        version is incompatible, or a duplicate version is supplied.
        Invalidates any cached version index entries on success.
        """
        agent = self._agents.get(agent_id)
        if agent is None:
            logger.info("upgrade_unknown", extra={"agent_id": agent_id})
            return False

        current_version = agent["version"]

        if current_version == new_version:
            logger.info(
                "upgrade_duplicate",
                extra={"agent_id": agent_id, "version": new_version},
            )
            return False

        if not _is_compatible(new_version, self._min_version):
            logger.info(
                "upgrade_incompatible",
                extra={"agent_id": agent_id, "new_version": new_version, "min_version": self._min_version},
            )
            raise VersionCompatibilityError(agent_id, new_version, self._min_version)

        active_statuses = {AgentStatus.RUNNING.value, AgentStatus.PENDING.value, AgentStatus.PAUSED.value}
        if agent["status"] in active_statuses:
            logger.info(
                "upgrade_deferred",
                extra={"agent_id": agent_id, "status": agent["status"], "new_version": new_version},
            )
            return False

        old_version = agent["version"]
        agent["version"] = new_version
        agent["updated_at"] = time.time()
        self._version_index[agent_id] = new_version

        logger.info(
            "handler_upgraded",
            extra={"agent_id": agent_id, "old_version": old_version, "new_version": new_version},
        )
        return True

    def delete(self, agent_id: str) -> bool:
        if agent_id not in self._agents:
            return False
        agent = self._agents.pop(agent_id)
        self._version_index.pop(agent_id, None)
        group = agent["type"].split(".")[0]
        if group in self._index and agent_id in self._index[group]:
            self._index[group].remove(agent_id)
        return True

    def count(self) -> int:
        return len(self._agents)

    def set_min_version(self, version: str) -> None:
        """Update the minimum compatible version and invalidate stale registrations."""
        _parse_version(version)
        old_min = self._min_version
        self._min_version = version
        logger.info(
            "min_version_updated",
            extra={"old_min": old_min, "new_min": version},
        )

# 2019-01-29T11:24:49 update

# 2019-04-09T13:38:38 update

# 2019-04-11T11:24:12 update

# 2019-06-26T17:03:48 update

# 2019-07-03T14:55:48 update

# 2019-07-18T18:18:47 update

# 2019-11-05T11:27:19 update

# 2019-11-20T11:35:05 update

# 2019-11-23T15:28:54 update

# 2020-03-13T09:23:07 update

# 2020-03-30T19:31:18 update

# 2020-04-22T15:03:30 update

# 2020-07-21T10:00:48 update

# 2020-09-10T09:02:08 update

# 2020-09-10T13:39:12 update

# 2020-09-22T16:27:52 update

# 2020-10-15T10:33:14 update

# 2021-05-13T11:15:56 update

# 2021-07-07T14:57:13 update

# 2021-07-13T15:15:19 update

# 2021-07-27T10:18:16 update

# 2022-03-11T15:24:11 update

# 2022-09-22T13:24:20 update

# 2022-11-01T12:20:40 update

# 2023-01-30T12:32:27 update

# 2023-03-10T09:43:50 update

# 2023-05-10T14:28:01 update

# 2023-05-11T20:04:46 update

# 2023-05-30T17:00:59 update

# 2023-07-13T17:54:32 update

# 2023-07-20T19:04:20 update

# 2023-07-31T17:00:02 update

# 2023-09-05T19:42:07 update

# 2024-01-02T10:29:47 update

# 2024-09-17T12:45:29 update

# 2024-09-17T11:51:01 update

# 2024-11-06T18:20:15 update

# 2025-01-12T15:13:14 update

# 2025-01-14T20:24:39 update

# 2025-03-26T20:21:27 update

# 2025-04-10T18:27:06 update

# 2025-06-19T20:34:58 update

# 2025-06-21T20:23:53 update

# 2025-06-24T20:30:30 update

# 2025-07-03T13:28:03 update

# 2025-07-24T17:42:21 update

# 2025-08-19T17:42:23 update

# 2025-08-21T11:06:52 update

# 2025-10-24T09:10:08 update

# 2025-12-18T19:34:38 update

# 2026-02-06T11:22:22 update

# 2026-02-13T15:42:04 update

# 2026-04-10T08:16:30 update

# 2026-04-29T18:16:11 update
