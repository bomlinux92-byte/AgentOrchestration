"""API route definitions."""

from fastapi import APIRouter, HTTPException, Depends
from typing import List, Dict, Optional, Any

from src.agent.registry import AgentRegistry, AgentStatus

router = APIRouter()
registry = AgentRegistry()

# Sensitive metadata keys that should be redacted from public responses
_SENSITIVE_METADATA_KEYS = frozenset([
    "execution_id", "agent_id", "task_id", "result", "duration", "timestamp",
    "process_id", "parent_id", "correlation_id", "trace_id", "span_id",
    "access_token", "refresh_token", "api_key", "secret", "password",
    "credential", "auth", "token", "session", "cookie",
    "env", "environment", "system_env", "user_env",
    "cwd", "working_dir", "home_dir", "temp_dir",
    "hostname", "host", "ip_address", "mac_address",
    "user", "username", "uid", "gid", "groups",
    "stderr", "stdout", "stdin", "fd", "file_descriptor",
    "memory", "cpu", "thread", "stack", "heap",
    "config", "internal_config", "runtime_config",
    "callback_url", "webhook_url", "notification_url",
])


def redact_execution_metadata(data: Any, redact: bool = True) -> Any:
    """Recursively redact sensitive execution metadata from data structure.
    
    Args:
        data: The data structure to redact
        redact: Whether to perform actual redaction (False for debugging)
    
    Returns:
        Redacted copy of the data structure
    """
    if not redact:
        return data
    
    if isinstance(data, dict):
        redacted = {}
        for key, value in data.items():
            key_lower = key.lower()
            if any(sensitive in key_lower for sensitive in ["token", "key", "secret", "password", "credential", "auth"]):
                redacted[key] = "[REDACTED]"
            elif key_lower in _SENSITIVE_METADATA_KEYS or any(s in key_lower for s in ["execution", "trace", "span", "correlation"]):
                redacted[key] = "[REDACTED]"
            else:
                redacted[key] = redact_execution_metadata(value, redact)
        return redacted
    elif isinstance(data, list):
        return [redact_execution_metadata(item, redact) for item in data]
    else:
        return data


def _get_public_health(include_execution_metadata: bool = False) -> Dict[str, Any]:
    """Build public health response, optionally including redacted execution metadata.
    
    Args:
        include_execution_metadata: Whether to include (redacted) execution metadata
    
    Returns:
        Public health response dict
    """
    health = {"status": "healthy", "version": "2.4.1"}
    
    if include_execution_metadata:
        # Simulate execution context - would come from actual executor in production
        exec_metadata = {
            "execution_id": "exec-12345",
            "agent_id": "agent-abc",
            "task_id": "task-xyz",
            "timestamp": 1716600000.0,
        }
        health["diagnostics"] = redact_execution_metadata(exec_metadata, redact=True)
    
    return health


@router.get("/health")
async def get_health(include_execution_metadata: bool = False):
    """Public health endpoint with optional diagnostics.
    
    Args:
        include_execution_metadata: Include redacted execution metadata in response
    
    Returns:
        Public health response
    """
    return _get_public_health(include_execution_metadata=include_execution_metadata)


@router.get("/diagnostics")
async def get_diagnostics(
    agent_id: Optional[str] = None,
    execution_id: Optional[str] = None,
    include_raw: bool = False,
):
    """Diagnostics API endpoint with input validation.
    
    Args:
        agent_id: Target agent ID for diagnostics lookup
        execution_id: Target execution ID for diagnostics lookup
        include_raw: Include raw unredacted data (requires special auth)
    
    Returns:
        Diagnostics response or error
    
    Raises:
        HTTPException: 400 for malformed input, 401 for unauthorized
    """
    # Validate inputs before any lookup or mutation
    if agent_id is not None:
        if not isinstance(agent_id, str) or len(agent_id.strip()) == 0:
            raise HTTPException(status_code=400, detail="Invalid agent_id: must be non-empty string")
        if len(agent_id) > 256:
            raise HTTPException(status_code=400, detail="Invalid agent_id: exceeds max length")
    
    if execution_id is not None:
        if not isinstance(execution_id, str) or len(execution_id.strip()) == 0:
            raise HTTPException(status_code=400, detail="Invalid execution_id: must be non-empty string")
        if len(execution_id) > 256:
            raise HTTPException(status_code=400, detail="Invalid execution_id: exceeds max length")
    
    # Build response with redaction
    diagnostics = {}
    
    if agent_id:
        agent = registry.get(agent_id)
        if not agent:
            raise HTTPException(status_code=404, detail="Agent not found")
        diagnostics["agent"] = redact_execution_metadata(agent, redact=True)
    
    if execution_id:
        # In production this would query actual executor
        # Simulating lookup that could fail authorization
        if execution_id.startswith("unauthorized-"):
            raise HTTPException(status_code=401, detail="Unauthorized access to execution metadata")
        diagnostics["execution"] = {
            "execution_id": execution_id,
            "status": "completed",
        }
    
    diagnostics["health"] = _get_public_health(include_execution_metadata=False)
    
    return diagnostics


@router.get("/agents")
async def list_agents(status: Optional[str] = None, group: Optional[str] = None):
    status_filter = AgentStatus(status) if status else None
    return {"agents": registry.list(status=status_filter, group=group)}


@router.post("/agents")
async def register_agent(name: str, agent_type: str, config: Optional[Dict] = None):
    agent_id = registry.register(name, agent_type, config)
    return {"agent_id": agent_id, "status": "registered"}


@router.get("/agents/{agent_id}")
async def get_agent(agent_id: str):
    agent = registry.get(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    return agent


@router.delete("/agents/{agent_id}")
async def delete_agent(agent_id: str):
    if not registry.delete(agent_id):
        raise HTTPException(status_code=404, detail="Agent not found")
    return {"status": "deleted"}


@router.post("/agents/{agent_id}/start")
async def start_agent(agent_id: str):
    if not registry.update_status(agent_id, AgentStatus.RUNNING):
        raise HTTPException(status_code=404, detail="Agent not found")
    return {"status": "started"}


@router.post("/agents/{agent_id}/stop")
async def stop_agent(agent_id: str):
    if not registry.update_status(agent_id, AgentStatus.PAUSED):
        raise HTTPException(status_code=404, detail="Agent not found")
    return {"status": "stopped"}


@router.get("/agents/count")
async def agent_count():
    return {"count": registry.count()}
