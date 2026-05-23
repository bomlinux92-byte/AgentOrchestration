"""Storage layer for Agent Orchestration.

Provides workspace-scoped repositories for task state management.
See: https://github.com/orchestration-agent/AgentOrchestration/issues/3370
"""

from src.storage.task_state_repo import TaskStateRepo, get_task_state_repo, WorkspaceScopeError

__all__ = ["TaskStateRepo", "get_task_state_repo", "WorkspaceScopeError"]