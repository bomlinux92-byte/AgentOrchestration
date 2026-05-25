"""Endpoint Registry — Tracks endpoint availability and status for webhook delivery validation."""

import time
from enum import Enum
from typing import Dict, List, Optional, Set


class EndpointStatus(Enum):
    """Endpoint lifecycle states."""
    ACTIVE = "active"
    DISABLED = "disabled"
    ROTATED = "rotated"
    DELETED = "deleted"


class EndpointScope(Enum):
    """Workspace isolation scope for endpoints."""
    WORKSPACE = "workspace"
    GLOBAL = "global"


class EndpointRegistry:
    """Registry for tracking endpoint availability and enforcing workspace isolation.
    
    This prevents events from being delivered to disabled, rotated, or deleted
    endpoints. Endpoints are scoped to workspaces for isolation.
    """
    
    # Default TTL for endpoint validation (seconds)
    DEFAULT_VALIDATION_TTL = 300
    
    def __init__(self):
        # endpoint_id -> endpoint metadata
        self._endpoints: Dict[str, Dict] = {}
        # workspace -> set of endpoint_ids (for workspace isolation)
        self._workspace_endpoints: Dict[str, Set[str]] = {}
        # endpoint_id -> last validation timestamp
        self._validated_at: Dict[str, float] = {}
        # endpoint_id -> task_ids already delivered (for idempotent delivery)
        self._delivered_tasks: Dict[str, Set[str]] = {}
    
    def register(
        self,
        endpoint_id: str,
        url: str,
        workspace: str = "default",
        scope: EndpointScope = EndpointScope.WORKSPACE,
        metadata: Optional[Dict] = None,
    ) -> bool:
        """Register a new endpoint.
        
        Returns True if registered, False if endpoint_id already exists.
        """
        if endpoint_id in self._endpoints:
            return False
        
        timestamp = time.time()
        self._endpoints[endpoint_id] = {
            "id": endpoint_id,
            "url": url,
            "workspace": workspace,
            "scope": scope.value,
            "status": EndpointStatus.ACTIVE.value,
            "created_at": timestamp,
            "updated_at": timestamp,
            "metadata": metadata or {},
        }
        
        # Track workspace membership
        if workspace not in self._workspace_endpoints:
            self._workspace_endpoints[workspace] = set()
        self._workspace_endpoints[workspace].add(endpoint_id)
        
        # Initialize delivery tracking
        self._delivered_tasks[endpoint_id] = set()
        
        return True
    
    def get(self, endpoint_id: str) -> Optional[Dict]:
        """Get endpoint metadata."""
        return self._endpoints.get(endpoint_id)
    
    def get_by_workspace(self, workspace: str) -> List[Dict]:
        """Get all endpoints in a workspace (workspace isolation)."""
        endpoint_ids = self._workspace_endpoints.get(workspace, set())
        return [self._endpoints[eid] for eid in endpoint_ids if eid in self._endpoints]
    
    def get_active(self, workspace: Optional[str] = None) -> List[Dict]:
        """Get all active endpoints, optionally filtered by workspace.
        
        Global scope endpoints are accessible from any workspace.
        """
        if workspace:
            # Get workspace endpoints and add global scope endpoints
            workspace_endpoints = set(self._workspace_endpoints.get(workspace, set()))
            # Also include global scope endpoints
            for eid, endpoint in self._endpoints.items():
                if endpoint.get("scope") == EndpointScope.GLOBAL.value:
                    workspace_endpoints.add(eid)
            endpoints = [self._endpoints[eid] for eid in workspace_endpoints if eid in self._endpoints]
        else:
            endpoints = list(self._endpoints.values())
        
        return [
            e for e in endpoints
            if e.get("status") == EndpointStatus.ACTIVE.value
        ]
    
    def is_active(self, endpoint_id: str) -> bool:
        """Check if an endpoint is active and valid for delivery."""
        endpoint = self._endpoints.get(endpoint_id)
        if not endpoint:
            return False
        return endpoint.get("status") == EndpointStatus.ACTIVE.value
    
    def is_valid_for_delivery(self, endpoint_id: str, task_id: str, ttl: int = None) -> bool:
        """Validate endpoint is active and task hasn't already been delivered.
        
        This ensures:
        1. Endpoint exists and is ACTIVE (not disabled/rotated/deleted)
        2. Task hasn't already been delivered to this endpoint (idempotency)
        3. Validation timestamp is within TTL (prevents stale validation)
        """
        ttl = ttl if ttl is not None else self.DEFAULT_VALIDATION_TTL
        
        # Check endpoint exists and is active
        if not self.is_active(endpoint_id):
            return False
        
        # Check validation timestamp hasn't expired
        validated_at = self._validated_at.get(endpoint_id, 0)
        if time.time() - validated_at > ttl:
            # Re-validation required
            return False
        
        # Check task hasn't already been delivered to this endpoint
        delivered = self._delivered_tasks.get(endpoint_id, set())
        if task_id in delivered:
            # Already delivered - idempotent rejection
            return False
        
        return True
    
    def validate(self, endpoint_id: str) -> bool:
        """Mark endpoint as validated (refreshes validation timestamp)."""
        if endpoint_id not in self._endpoints:
            return False
        self._validated_at[endpoint_id] = time.time()
        self._endpoints[endpoint_id]["updated_at"] = time.time()
        return True
    
    def mark_delivered(self, endpoint_id: str, task_id: str) -> bool:
        """Mark a task as delivered to an endpoint.
        
        Returns True if marked successfully, False if endpoint not active
        or task already delivered.
        """
        if not self.is_active(endpoint_id):
            return False
        
        if endpoint_id not in self._delivered_tasks:
            return False
        
        delivered = self._delivered_tasks[endpoint_id]
        
        if task_id in delivered:
            # Already marked - idempotent
            return True
        
        delivered.add(task_id)
        return True
    
    def disable(self, endpoint_id: str, reason: str = "") -> bool:
        """Disable an endpoint (prevents further delivery).
        
        Returns True if disabled, False if not found.
        """
        if endpoint_id not in self._endpoints:
            return False
        
        self._endpoints[endpoint_id]["status"] = EndpointStatus.DISABLED.value
        self._endpoints[endpoint_id]["updated_at"] = time.time()
        self._endpoints[endpoint_id]["disabled_reason"] = reason
        
        # Clear delivered tasks to prevent stale deliveries
        self._delivered_tasks.pop(endpoint_id, None)
        
        return True
    
    def rotate(self, endpoint_id: str, new_url: str = None) -> Optional[str]:
        """Rotate an endpoint (mark old as rotated, return new endpoint_id).
        
        Returns the new endpoint_id if successful, None if old endpoint not found.
        """
        if endpoint_id not in self._endpoints:
            return None
        
        old_endpoint = self._endpoints[endpoint_id]
        
        # Mark old as rotated
        old_endpoint["status"] = EndpointStatus.ROTATED.value
        old_endpoint["updated_at"] = time.time()
        old_endpoint["rotated_at"] = time.time()
        
        # Create new endpoint with incremented version
        base_id = endpoint_id.rsplit("_v", 1)[0] if "_v" in endpoint_id else endpoint_id
        new_id = f"{base_id}_v{int(time.time() * 1000)}"
        
        new_url_value = new_url if new_url is not None else old_endpoint["url"]
        self.register(
            endpoint_id=new_id,
            url=new_url_value,
            workspace=old_endpoint["workspace"],
            scope=EndpointScope(old_endpoint["scope"]),
            metadata=old_endpoint.get("metadata", {}),
        )
        
        return new_id
    
    def delete(self, endpoint_id: str) -> bool:
        """Delete an endpoint (marks as deleted, prevents delivery).
        
        Returns True if deleted, False if not found.
        """
        if endpoint_id not in self._endpoints:
            return False
        
        endpoint = self._endpoints[endpoint_id]
        workspace = endpoint["workspace"]
        
        # Mark as deleted
        endpoint["status"] = EndpointStatus.DELETED.value
        endpoint["updated_at"] = time.time()
        endpoint["deleted_at"] = time.time()
        
        # Remove from workspace index
        if workspace in self._workspace_endpoints:
            self._workspace_endpoints[workspace].discard(endpoint_id)
        
        # Clear delivery tracking
        self._delivered_tasks.pop(endpoint_id, None)
        
        return True
    
    def is_delivered(self, endpoint_id: str, task_id: str) -> bool:
        """Check if task has already been delivered to endpoint (idempotency check)."""
        delivered = self._delivered_tasks.get(endpoint_id, set())
        return task_id in delivered
    
    def clear_delivered(self, endpoint_id: str) -> None:
        """Clear delivery history for an endpoint (used after rotation/disable)."""
        self._delivered_tasks.pop(endpoint_id, None)
    
    def list_status(self, workspace: Optional[str] = None) -> Dict[str, int]:
        """Get count of endpoints by status."""
        endpoints = list(self._endpoints.values()) if not workspace else self.get_by_workspace(workspace)
        counts = {s.value: 0 for s in EndpointStatus}
        for e in endpoints:
            status = e.get("status", EndpointStatus.ACTIVE.value)
            counts[status] = counts.get(status, 0) + 1
        return counts


# Global endpoint registry instance
_global_registry: Optional[EndpointRegistry] = None


def get_endpoint_registry() -> EndpointRegistry:
    """Get the global endpoint registry instance."""
    global _global_registry
    if _global_registry is None:
        _global_registry = EndpointRegistry()
    return _global_registry


def reset_endpoint_registry() -> None:
    """Reset the global registry (for testing)."""
    global _global_registry
    _global_registry = None


# 2026-05-25T18:00:00 update - Initial implementation for issue #4202
# Prevent disabled endpoints from receiving queued events