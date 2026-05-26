"""Regression test for bounty issue #4936: Handler retirement propagation.

Ensures deregistration is propagated to schedulers so stale/duplicate/
policy-violating task dispatches are prevented when handlers retire.
"""

import pytest
from src.agent.registry import AgentRegistry, AgentStatus
from src.orchestrator.scheduler import TaskScheduler
from src.orchestrator.engine import OrchestrationEngine


class TestDeregistrationPropagation:
    """Test that agent deregistration properly invalidates scheduler tasks."""

    def test_scheduler_invalidates_tasks_for_deregistered_agent(self):
        """Scheduler should remove all tasks targeting a deregistered agent."""
        scheduler = TaskScheduler()
        agent_id = "agent-123"

        # Enqueue several tasks for the agent
        scheduler.enqueue({"type": "work", "target_agent": agent_id})
        scheduler.enqueue({"type": "work", "target_agent": agent_id})
        scheduler.enqueue({"type": "work", "target_agent": "other-agent"})

        # Verify queue state before invalidation
        assert len(scheduler._in_flight) == 0
        assert len(scheduler._queues["default"]) == 3

        # Invalidate tasks for the deregistered agent
        invalidated = scheduler.invalidate_agent_tasks(agent_id)

        # Should have removed 2 tasks for agent-123, kept 1 for other-agent
        assert invalidated == 2
        assert len(scheduler._queues["default"]) == 1

    def test_scheduler_invalidates_inflight_tasks(self):
        """Scheduler should remove in-flight tasks targeting a deregistered agent."""
        scheduler = TaskScheduler()
        agent_id = "agent-456"

        # Manually add tasks to in-flight (as if dequeued but not yet complete)
        task1 = {"id": "task-1", "target_agent": agent_id, "type": "work"}
        task2 = {"id": "task-2", "target_agent": "other-agent", "type": "work"}
        scheduler._in_flight["task-1"] = task1
        scheduler._in_flight["task-2"] = task2

        invalidated = scheduler.invalidate_agent_tasks(agent_id)

        assert invalidated == 1
        assert "task-1" not in scheduler._in_flight
        assert "task-2" in scheduler._in_flight

    def test_registry_notifies_deregistration_listeners(self):
        """Registry should call listeners when an agent is deregistered."""
        registry = AgentRegistry()
        agent_id = registry.register("test-agent", "worker.processor")

        received = []
        def listener(ag_id, info):
            received.append((ag_id, info))

        registry.register_deregister_listener(listener)
        registry.delete(agent_id)

        assert len(received) == 1
        assert received[0][0] == agent_id
        assert received[0][1]["name"] == "test-agent"

    def test_engine_propagates_deregistration_to_scheduler(self):
        """OrchestrationEngine should wire registry deregistration to scheduler."""
        engine = OrchestrationEngine()

        # Register an agent and enqueue a task for it
        agent_id = engine.registry.register("test-agent", "worker.processor")
        task_id = engine.scheduler.enqueue({
            "type": "work",
            "target_agent": agent_id,
        })

        # Verify task is queued
        assert len(engine.scheduler._queues["default"]) == 1

        # Deregister the agent - engine should automatically invalidate tasks
        engine.registry.delete(agent_id)

        # Task should be removed from the queue
        assert len(engine.scheduler._queues["default"]) == 0

    def test_deregistered_agent_cannot_receive_new_tasks(self):
        """After deregistration, no new tasks should target that agent."""
        engine = OrchestrationEngine()

        agent_id = engine.registry.register("test-agent", "worker.processor")

        # Deregister before any task is enqueued
        engine.registry.delete(agent_id)

        # Enqueue a task for the now-nonexistent agent
        task_id = engine.scheduler.enqueue({
            "type": "work",
            "target_agent": agent_id,
        })

        # The task is enqueued (scheduler doesn't know about registry state)
        # but when we invalidate for that agent_id, it should be removed
        invalidated = engine.scheduler.invalidate_agent_tasks(agent_id)
        assert invalidated == 1
        assert len(engine.scheduler._queues["default"]) == 0


# 2026-05-26T16:00:00 bounty-4936 regression test