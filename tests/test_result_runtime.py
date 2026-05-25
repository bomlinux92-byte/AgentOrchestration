"""Tests for result runtime JSON serialization validation and state machine guards."""

import asyncio
import json
from datetime import datetime

import pytest

from src.agent.executor import (
    AgentExecutor,
    ExecutionState,
    validate_json_serializable,
)
from src.orchestrator.engine import OrchestrationEngine


# --- validate_json_serializable ---


class TestValidateJsonSerializable:
    def test_valid_dict(self):
        validate_json_serializable({"key": "value", "num": 42})

    def test_valid_list(self):
        validate_json_serializable([1, 2, "three", None])

    def test_valid_nested(self):
        validate_json_serializable({"a": {"b": [1, 2]}, "c": None})

    def test_rejects_datetime(self):
        with pytest.raises(TypeError, match="not JSON-serializable"):
            validate_json_serializable({"ts": datetime.now()})

    def test_rejects_bytes(self):
        with pytest.raises(TypeError, match="not JSON-serializable"):
            validate_json_serializable({"data": b"raw"})

    def test_rejects_set(self):
        with pytest.raises(TypeError, match="not JSON-serializable"):
            validate_json_serializable({"items": {1, 2, 3}})

    def test_rejects_custom_object(self):
        class Foo:
            pass

        with pytest.raises(TypeError, match="not JSON-serializable"):
            validate_json_serializable(Foo())

    def test_rejects_nested_non_serializable(self):
        with pytest.raises(TypeError, match="not JSON-serializable"):
            validate_json_serializable({"ok": 1, "bad": {"nested": datetime(2024, 1, 1)}})


# --- AgentExecutor state machine ---


def _run(coro):
    return asyncio.run(coro)


class TestExecutorStateMachine:
    def setup_method(self):
        self.executor = AgentExecutor(max_concurrent=2)

    def test_valid_state_transitions(self):
        async def handler(aid, task):
            return {"output": "ok"}

        eid = _run(self.executor.execute("agent-1", {"id": "t1"}, handler))
        assert self.executor.get_state(eid) == ExecutionState.COMPLETED
        result = self.executor.get_result(eid)
        assert result["result"] == {"output": "ok"}

    def test_failed_state_on_non_serializable_result(self):
        async def bad_handler(aid, task):
            return {"ts": datetime.now()}

        eid = _run(self.executor.execute("agent-2", {"id": "t2"}, bad_handler))
        assert self.executor.get_state(eid) == ExecutionState.FAILED
        result = self.executor.get_result(eid)
        assert "error" in result

    def test_result_is_json_serializable_after_success(self):
        async def handler(aid, task):
            return {"data": [1, 2, 3]}

        eid = _run(self.executor.execute("agent-3", {"id": "t3"}, handler))
        result = self.executor.get_result(eid)
        serialized = json.dumps(result)
        assert json.loads(serialized) == result

    def test_concurrent_executions_independent_states(self):
        async def run_both():
            async def handler(aid, task):
                return {"aid": aid}

            return await asyncio.gather(
                self.executor.execute("a1", {"id": "t1"}, handler),
                self.executor.execute("a2", {"id": "t2"}, handler),
            )

        eids = _run(run_both())
        for eid in eids:
            assert self.executor.get_state(eid) == ExecutionState.COMPLETED


# --- OrchestrationEngine result validation ---


class TestEngineResultValidation:
    def setup_method(self):
        self.engine = OrchestrationEngine(max_workers=2)

    def test_validate_result_accepts_serializable(self):
        self.engine._validate_result({"status": "ok", "data": [1, 2]})

    def test_validate_result_rejects_non_serializable(self):
        with pytest.raises(TypeError, match="not JSON-serializable"):
            self.engine._validate_result({"bad": datetime.now()})

    def test_record_outcome_prevents_duplicates(self):
        self.engine._record_outcome("t1", {"status": "completed"})
        self.engine._record_outcome("t1", {"status": "completed"})
        assert len([k for k in self.engine._task_outcomes if k == "t1"]) == 1

    def test_execute_task_records_durable_outcome(self):
        agent_id = self.engine.registry.register("test-agent", "test")
        task = {"id": "task-1", "target_agent": agent_id}

        _run(self.engine._execute_task(task))

        assert "task-1" in self.engine._task_outcomes
        outcome = self.engine._task_outcomes["task-1"]
        assert outcome["status"] == "completed"
        assert outcome["task_id"] == "task-1"
        assert outcome["agent_id"] == agent_id

    def test_execute_task_skips_duplicate(self):
        agent_id = self.engine.registry.register("test-agent", "test")
        task = {"id": "task-dup", "target_agent": agent_id}

        _run(self.engine._execute_task(task))
        _run(self.engine._execute_task(task))

        outcome = self.engine._task_outcomes["task-dup"]
        assert outcome["status"] == "completed"

    def test_failed_task_records_failed_outcome(self):
        task = {"id": "task-fail", "target_agent": "nonexistent"}

        _run(self.engine._execute_task(task))

        assert "task-fail" in self.engine._task_outcomes
        assert self.engine._task_outcomes["task-fail"]["status"] == "failed"

    def test_hooks_called_after_durable_state(self):
        agent_id = self.engine.registry.register("hook-agent", "test")
        task = {"id": "task-hooks", "target_agent": agent_id}

        hook_order = []

        async def post_hook(t, result):
            hook_order.append("post_execute")
            assert "task-hooks" in self.engine._task_outcomes

        async def complete_hook(t, result):
            hook_order.append("on_complete")
            assert "task-hooks" in self.engine._task_outcomes

        self.engine.register_hook("post_execute", post_hook)
        self.engine.register_hook("on_complete", complete_hook)

        _run(self.engine._execute_task(task))

        assert hook_order == ["post_execute", "on_complete"]
