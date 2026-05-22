"""Tests for SDK decorators."""

import pytest
from src.sdk.decorators import task, agent, on_event


class TestTaskDecorator:
    """Tests for the @task decorator."""

    def test_task_decorator_with_valid_timeout(self):
        """Test that task decorator works with valid positive timeout."""
        @task(name="test_task", timeout=30)
        async def my_task():
            return "done"
        
        assert my_task.__task_config__["name"] == "test_task"
        assert my_task.__task_config__["timeout"] == 30

    def test_task_decorator_default_timeout(self):
        """Test that task decorator has default timeout of 300."""
        @task(name="test_task")
        async def my_task():
            return "done"
        
        assert my_task.__task_config__["timeout"] == 300

    def test_task_decorator_rejects_zero_timeout(self):
        """Test that task decorator rejects zero timeout."""
        with pytest.raises(ValueError, match="timeout must be a positive integer"):
            @task(name="bad_task", timeout=0)
            async def zero_timeout_task():
                pass

    def test_task_decorator_rejects_negative_timeout(self):
        """Test that task decorator rejects negative timeout."""
        with pytest.raises(ValueError, match="timeout must be a positive integer"):
            @task(name="bad_task", timeout=-5)
            async def negative_timeout_task():
                pass

    def test_task_decorator_rejects_negative_one(self):
        """Test that task decorator rejects timeout=-1."""
        with pytest.raises(ValueError, match="timeout must be a positive integer"):
            @task(name="bad_task", timeout=-1)
            async def neg_one_timeout_task():
                pass


class TestAgentDecorator:
    """Tests for the @agent decorator."""

    def test_agent_decorator_basic(self):
        """Test basic agent decorator usage."""
        @agent(name="my_agent", version="2.0.0")
        class MyAgent:
            pass
        
        assert MyAgent.__agent_config__["name"] == "my_agent"
        assert MyAgent.__agent_config__["version"] == "2.0.0"


class TestOnEventDecorator:
    """Tests for the @on_event decorator."""

    def test_on_event_decorator_basic(self):
        """Test basic on_event decorator usage."""
        @on_event("click")
        async def handle_click():
            return "clicked"
        
        assert handle_click.__event_handler__ == "click"