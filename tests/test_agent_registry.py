import pytest
from src.agent.registry import AgentRegistry, AgentStatus
from src.common.errors import VersionCompatibilityError


class TestAgentRegistry:
    def setup_method(self):
        self.registry = AgentRegistry()

    def test_register_agent(self):
        agent_id = self.registry.register("test-agent", "worker.processor")
        assert agent_id is not None
        assert self.registry.count() == 1

    def test_get_agent(self):
        agent_id = self.registry.register("test-agent", "worker.processor")
        agent = self.registry.get(agent_id)
        assert agent is not None
        assert agent["name"] == "test-agent"
        assert agent["type"] == "worker.processor"

    def test_get_nonexistent_agent(self):
        agent = self.registry.get("nonexistent-id")
        assert agent is None

    def test_list_agents(self):
        self.registry.register("agent-1", "worker.processor")
        self.registry.register("agent-2", "worker.analyzer")
        self.registry.register("agent-3", "monitor.watcher")
        assert len(self.registry.list()) == 3

    def test_list_agents_by_group(self):
        self.registry.register("agent-1", "worker.processor")
        self.registry.register("agent-2", "monitor.watcher")
        workers = self.registry.list(group="worker")
        assert len(workers) == 1

    def test_update_status(self):
        agent_id = self.registry.register("test-agent", "worker.processor")
        assert self.registry.update_status(agent_id, AgentStatus.RUNNING)
        agent = self.registry.get(agent_id)
        assert agent["status"] == "running"

    def test_delete_agent(self):
        agent_id = self.registry.register("test-agent", "worker.processor")
        assert self.registry.delete(agent_id)
        assert self.registry.count() == 0

    def test_delete_nonexistent_agent(self):
        assert not self.registry.delete("nonexistent-id")


class TestVersionCompatibility:
    """Regression tests for plugin upgrade version validation (issue #2494)."""

    def setup_method(self):
        self.registry = AgentRegistry()

    def test_register_with_explicit_version(self):
        agent_id = self.registry.register("v2-agent", "worker.processor", version="1.2.0")
        agent = self.registry.get(agent_id)
        assert agent["version"] == "1.2.0"

    def test_register_rejects_incompatible_major(self):
        with pytest.raises(VersionCompatibilityError):
            self.registry.register("bad-agent", "worker.processor", version="2.0.0")

    def test_register_rejects_old_version(self):
        self.registry.set_min_version("1.3.0")
        with pytest.raises(VersionCompatibilityError):
            self.registry.register("old-agent", "worker.processor", version="1.2.0")

    def test_resolve_returns_compatible_handler(self):
        agent_id = self.registry.register("handler", "worker.processor", version="1.1.0")
        result = self.registry.resolve(agent_id, required_version="1.0.0")
        assert result is not None
        assert result["id"] == agent_id

    def test_resolve_rejects_version_mismatch(self):
        agent_id = self.registry.register("handler", "worker.processor", version="1.0.0")
        result = self.registry.resolve(agent_id, required_version="1.2.0")
        assert result is None

    def test_resolve_rejects_stopped_handler(self):
        agent_id = self.registry.register("handler", "worker.processor")
        self.registry.update_status(agent_id, AgentStatus.STOPPED)
        assert self.registry.resolve(agent_id) is None

    def test_resolve_rejects_terminated_handler(self):
        agent_id = self.registry.register("handler", "worker.processor")
        self.registry.update_status(agent_id, AgentStatus.TERMINATED)
        assert self.registry.resolve(agent_id) is None

    def test_resolve_rejects_failed_handler(self):
        agent_id = self.registry.register("handler", "worker.processor")
        self.registry.update_status(agent_id, AgentStatus.FAILED)
        assert self.registry.resolve(agent_id) is None

    def test_resolve_unknown_handler_returns_none(self):
        assert self.registry.resolve("nonexistent", required_version="1.0.0") is None

    def test_upgrade_handler_succeeds(self):
        agent_id = self.registry.register("handler", "worker.processor", version="1.0.0")
        self.registry.update_status(agent_id, AgentStatus.STOPPED)
        assert self.registry.upgrade_handler(agent_id, "1.1.0") is True
        agent = self.registry.get(agent_id)
        assert agent["version"] == "1.1.0"

    def test_upgrade_rejects_active_handler(self):
        agent_id = self.registry.register("handler", "worker.processor", version="1.0.0")
        self.registry.update_status(agent_id, AgentStatus.RUNNING)
        assert self.registry.upgrade_handler(agent_id, "1.1.0") is False
        agent = self.registry.get(agent_id)
        assert agent["version"] == "1.0.0"

    def test_upgrade_rejects_duplicate_version(self):
        agent_id = self.registry.register("handler", "worker.processor", version="1.0.0")
        self.registry.update_status(agent_id, AgentStatus.STOPPED)
        assert self.registry.upgrade_handler(agent_id, "1.0.0") is False

    def test_upgrade_rejects_incompatible_version(self):
        agent_id = self.registry.register("handler", "worker.processor", version="1.0.0")
        self.registry.update_status(agent_id, AgentStatus.STOPPED)
        with pytest.raises(VersionCompatibilityError):
            self.registry.upgrade_handler(agent_id, "2.0.0")

    def test_upgrade_unknown_handler_returns_false(self):
        assert self.registry.upgrade_handler("nonexistent", "1.1.0") is False

    def test_plugin_upgrade_during_lifecycle_transition(self):
        """Deterministic regression: upgrade deferred while handler is running."""
        agent_id = self.registry.register("handler", "worker.processor", version="1.0.0")
        self.registry.update_status(agent_id, AgentStatus.RUNNING)

        assert self.registry.upgrade_handler(agent_id, "1.2.0") is False
        agent = self.registry.get(agent_id)
        assert agent["version"] == "1.0.0"
        assert agent["status"] == "running"

        self.registry.update_status(agent_id, AgentStatus.STOPPED)
        assert self.registry.upgrade_handler(agent_id, "1.2.0") is True
        agent = self.registry.get(agent_id)
        assert agent["version"] == "1.2.0"

    def test_cache_invalidated_after_upgrade(self):
        """Version index stays consistent after an upgrade."""
        agent_id = self.registry.register("handler", "worker.processor", version="1.0.0")
        self.registry.update_status(agent_id, AgentStatus.STOPPED)
        self.registry.upgrade_handler(agent_id, "1.3.0")
        assert self.registry._version_index[agent_id] == "1.3.0"

    def test_cache_cleaned_on_delete(self):
        agent_id = self.registry.register("handler", "worker.processor")
        assert agent_id in self.registry._version_index
        self.registry.delete(agent_id)
        assert agent_id not in self.registry._version_index

    def test_resolve_allows_running_handler(self):
        agent_id = self.registry.register("handler", "worker.processor")
        self.registry.update_status(agent_id, AgentStatus.RUNNING)
        assert self.registry.resolve(agent_id) is not None

    def test_resolve_allows_pending_handler(self):
        agent_id = self.registry.register("handler", "worker.processor")
        assert self.registry.resolve(agent_id) is not None

# 2019-01-23T10:28:57 update

# 2019-01-28T18:15:57 update

# 2019-02-22T11:46:37 update

# 2019-03-27T14:43:52 update

# 2019-04-12T16:58:25 update

# 2019-05-27T15:15:18 update

# 2019-07-17T14:36:58 update

# 2019-09-06T12:29:31 update

# 2019-11-27T17:43:26 update

# 2019-11-28T08:42:43 update

# 2019-12-03T20:34:02 update

# 2019-12-26T08:15:09 update

# 2020-01-07T09:36:32 update

# 2020-01-10T12:44:52 update

# 2020-07-05T19:33:32 update

# 2020-07-07T14:16:11 update

# 2020-07-28T08:29:39 update

# 2020-08-26T18:58:21 update

# 2020-08-28T09:50:37 update

# 2020-09-17T15:23:33 update

# 2020-09-23T16:22:24 update

# 2020-10-14T13:27:24 update

# 2020-11-20T11:40:04 update

# 2020-12-10T13:55:01 update

# 2020-12-25T20:33:02 update

# 2021-03-22T19:53:48 update

# 2021-03-26T15:02:19 update

# 2021-07-16T20:24:40 update

# 2021-07-22T13:19:23 update

# 2021-08-16T19:11:26 update

# 2021-10-02T13:32:20 update

# 2021-10-23T18:31:31 update

# 2021-10-29T13:55:10 update

# 2022-07-31T17:35:39 update

# 2022-09-27T09:32:34 update

# 2022-11-07T14:44:52 update

# 2023-01-23T14:07:09 update

# 2023-03-16T15:23:38 update

# 2023-07-03T18:33:44 update

# 2023-07-27T09:35:11 update

# 2023-11-16T11:22:59 update

# 2023-12-20T14:25:29 update

# 2024-03-07T17:32:49 update

# 2024-04-10T10:50:42 update

# 2024-06-19T19:57:49 update

# 2024-12-05T18:02:46 update

# 2025-01-15T16:13:24 update

# 2025-03-12T20:58:57 update

# 2025-06-24T20:33:23 update

# 2025-08-25T10:56:35 update

# 2025-09-12T17:09:51 update

# 2025-10-06T20:01:10 update

# 2025-10-14T11:48:40 update

# 2026-01-29T13:09:29 update
