import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from src.orchestrator.engine import OrchestrationEngine
from src.agent import AgentStatus


class TestOrchestrationEngineTaskLifecycle:
    def setup_method(self):
        self.engine = OrchestrationEngine()

    @pytest.mark.asyncio
    async def test_task_completed_on_success(self):
        agent_id = self.engine.registry.register("test-agent", "test")
        task_id = self.engine.scheduler.enqueue(
            {"type": "test", "target_agent": agent_id}
        )
        task = await self.engine.scheduler.dequeue()
        assert task is not None

        await self.engine._execute_task(task)

        assert self.engine.scheduler.complete(task_id) is False
        entries = self.engine.scheduler.audit_log.get_entries(task_id)
        assert any(e["event"] == "task_completed" for e in entries)

    @pytest.mark.asyncio
    async def test_task_failed_on_exception(self):
        agent_id = self.engine.registry.register("test-agent", "test")
        task_id = self.engine.scheduler.enqueue(
            {"type": "test", "target_agent": agent_id}
        )
        task = await self.engine.scheduler.dequeue()
        assert task is not None

        with patch.object(
            self.engine, "_run_agent_task", side_effect=RuntimeError("boom")
        ):
            await self.engine._execute_task(task)

        entries = self.engine.scheduler.audit_log.get_entries(task_id)
        assert any(e["event"] == "task_failed" for e in entries)
        agent = self.engine.registry.get(agent_id)
        assert agent["status"] == AgentStatus.FAILED.value

    @pytest.mark.asyncio
    async def test_task_failed_on_missing_agent(self):
        task_id = self.engine.scheduler.enqueue(
            {"type": "test", "target_agent": "nonexistent"}
        )
        task = await self.engine.scheduler.dequeue()
        assert task is not None
        await self.engine._execute_task(task)

        entries = self.engine.scheduler.audit_log.get_entries(task_id)
        assert any(e["event"] == "task_failed" for e in entries)

    @pytest.mark.asyncio
    async def test_no_orphaned_in_flight_tasks(self):
        agent_id = self.engine.registry.register("test-agent", "test")
        self.engine.scheduler.enqueue(
            {"type": "test", "target_agent": agent_id}
        )
        task = await self.engine.scheduler.dequeue()

        with patch.object(
            self.engine, "_run_agent_task", side_effect=RuntimeError("boom")
        ):
            await self.engine._execute_task(task)

        assert len(self.engine.scheduler._in_flight) == 0
