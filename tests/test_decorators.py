"""Tests for SDK decorators."""

import pytest
from src.sdk.decorators import task


def test_task_decorator_valid_timeout():
    """Decorator accepts positive timeout values."""
    @task(name="test_task", timeout=60)
    async def my_task():
        return "done"

    assert my_task.__task_config__["timeout"] == 60


def test_task_decorator_invalid_timeout_zero():
    """Decorator rejects timeout of zero."""
    with pytest.raises(ValueError, match="timeout must be a positive number"):
        @task(name="bad_task", timeout=0)
        async def zero_task():
            pass


def test_task_decorator_invalid_timeout_negative():
    """Decorator rejects negative timeout values."""
    with pytest.raises(ValueError, match="timeout must be a positive number"):
        @task(name="bad_task", timeout=-5)
        async def negative_task():
            pass


def test_task_decorator_default_timeout():
    """Decorator uses default timeout of 300."""
    @task(name="default_task")
    async def default_task():
        pass

    assert default_task.__task_config__["timeout"] == 300