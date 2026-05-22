"""Regression tests for GitHub issue #185 - CancelledError handling in AgentExecutor."""

import asyncio
import pytest

from src.agent.executor import AgentExecutor


@pytest.fixture
def executor():
    return AgentExecutor(max_concurrent=5)


@pytest.mark.asyncio
async def test_execute_catches_cancelled_error(executor):
    """Test that execute() stores cancelled status when task is cancelled mid-execution."""
    slow_task_started = asyncio.Event()

    async def slow_handler(agent_id, task):
        slow_task_started.set()
        await asyncio.sleep(10)  # Long sleep to allow cancellation
        return "completed"

    task = {"id": "test-task-1"}

    # Start execution in background - don't await it yet
    execute_task = asyncio.create_task(
        executor.execute("agent-1", task, slow_handler)
    )

    # Wait for task to actually start
    await slow_task_started.wait()

    # Now there should be an active task
    assert len(executor._active_tasks) == 1
    exec_id = list(executor._active_tasks.keys())[0]

    # Cancel the task
    result = executor.cancel(exec_id)
    assert result is True

    # Try to wait for the execute to complete (it will raise CancelledError)
    with pytest.raises(asyncio.CancelledError):
        await execute_task

    # Verify cancelled status is stored
    stored_result = executor.get_result(exec_id)
    assert stored_result is not None
    assert stored_result.get("status") == "cancelled"
    assert stored_result.get("execution_id") == exec_id


@pytest.mark.asyncio
async def test_cancel_stores_cancelled_status(executor):
    """Test that cancel() stores cancelled status in results when task is running."""
    task_started = asyncio.Event()

    async def slow_handler(agent_id, task):
        task_started.set()
        await asyncio.sleep(10)
        return "done"

    task = {"id": "test-task-2"}

    # Start execution in background
    exec_task = asyncio.create_task(
        executor.execute("agent-2", task, slow_handler)
    )

    # Wait for task to start
    await task_started.wait()

    # Get the execution id
    exec_id = list(executor._active_tasks.keys())[0]

    cancelled = executor.cancel(exec_id)
    assert cancelled is True

    # Wait for execution to complete (will raise CancelledError)
    with pytest.raises(asyncio.CancelledError):
        await exec_task

    stored_result = executor.get_result(exec_id)
    assert stored_result is not None
    assert stored_result["status"] == "cancelled"
    assert stored_result["execution_id"] == exec_id


@pytest.mark.asyncio
async def test_get_result_returns_none_for_unknown_id(executor):
    """Test that get_result returns None for unknown execution IDs."""
    result = executor.get_result("non-existent-id")
    assert result is None


@pytest.mark.asyncio
async def test_cancel_returns_false_for_unknown_id(executor):
    """Test that cancel returns False for unknown execution IDs."""
    result = executor.cancel("non-existent-id")
    assert result is False


@pytest.mark.asyncio
async def test_execute_stores_result_on_success(executor):
    """Test that execute() stores result on successful completion."""
    async def fast_handler(agent_id, task):
        return {"data": "success"}

    task = {"id": "test-task-3"}
    exec_id = await executor.execute("agent-3", task, fast_handler)

    # Wait for completion
    await asyncio.sleep(0.1)

    stored_result = executor.get_result(exec_id)
    assert stored_result is not None
    assert stored_result.get("result", {}).get("data") == "success"