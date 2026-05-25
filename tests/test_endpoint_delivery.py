"""Tests for endpoint registry and webhook delivery validation.

These tests cover:
- Valid delivery to active endpoints
- Rejected delivery to disabled/rotated/deleted endpoints
- Retry behavior with idempotency
- Workspace isolation for endpoints
"""

import pytest
import time

from src.orchestrator.endpoint import (
    EndpointRegistry,
    EndpointStatus,
    EndpointScope,
    get_endpoint_registry,
    reset_endpoint_registry,
)


class TestEndpointRegistry:
    """Tests for the endpoint registry."""
    
    def setup_method(self):
        """Reset registry before each test."""
        reset_endpoint_registry()
        self.registry = get_endpoint_registry()
    
    def test_register_endpoint(self):
        """Test registering a new endpoint."""
        result = self.registry.register(
            endpoint_id="ep1",
            url="https://example.com/webhook",
            workspace="workspace1",
        )
        assert result is True
        assert self.registry.get("ep1") is not None
        assert self.registry.get("ep1")["status"] == EndpointStatus.ACTIVE.value
    
    def test_register_duplicate_fails(self):
        """Test that registering same endpoint twice fails."""
        self.registry.register(
            endpoint_id="ep1",
            url="https://example.com/webhook",
        )
        result = self.registry.register(
            endpoint_id="ep1",
            url="https://example.com/webhook2",
        )
        assert result is False
    
    def test_is_active(self):
        """Test checking if endpoint is active."""
        self.registry.register(endpoint_id="ep1", url="https://example.com/webhook")
        assert self.registry.is_active("ep1") is True
    
    def test_is_active_not_found(self):
        """Test is_active returns False for non-existent endpoint."""
        assert self.registry.is_active("nonexistent") is False
    
    def test_disable_endpoint(self):
        """Test disabling an endpoint."""
        self.registry.register(endpoint_id="ep1", url="https://example.com/webhook")
        result = self.registry.disable("ep1", reason="Testing disable")
        assert result is True
        assert self.registry.is_active("ep1") is False
        assert self.registry.get("ep1")["disabled_reason"] == "Testing disable"
    
    def test_disable_nonexistent_fails(self):
        """Test disabling non-existent endpoint fails."""
        result = self.registry.disable("nonexistent")
        assert result is False
    
    def test_rotate_endpoint(self):
        """Test rotating an endpoint."""
        self.registry.register(endpoint_id="ep1", url="https://example.com/webhook")
        new_id = self.registry.rotate("ep1", new_url="https://example.com/webhook-v2")
        assert new_id is not None
        assert new_id != "ep1"
        # Old endpoint should be rotated
        assert self.registry.get("ep1")["status"] == EndpointStatus.ROTATED.value
        # New endpoint should be active
        assert self.registry.get(new_id)["status"] == EndpointStatus.ACTIVE.value
    
    def test_delete_endpoint(self):
        """Test deleting an endpoint."""
        self.registry.register(endpoint_id="ep1", url="https://example.com/webhook")
        result = self.registry.delete("ep1")
        assert result is True
        assert self.registry.is_active("ep1") is False
        assert self.registry.get("ep1")["status"] == EndpointStatus.DELETED.value
    
    def test_workspace_isolation(self):
        """Test that workspace isolation works."""
        self.registry.register(endpoint_id="ep1", url="https://example.com/webhook", workspace="workspace1")
        self.registry.register(endpoint_id="ep2", url="https://example.com/webhook2", workspace="workspace2")
        
        workspace1_endpoints = self.registry.get_by_workspace("workspace1")
        assert len(workspace1_endpoints) == 1
        assert workspace1_endpoints[0]["id"] == "ep1"
        
        workspace2_endpoints = self.registry.get_by_workspace("workspace2")
        assert len(workspace2_endpoints) == 1
        assert workspace2_endpoints[0]["id"] == "ep2"
    
    def test_get_active_endpoints(self):
        """Test getting all active endpoints."""
        self.registry.register(endpoint_id="ep1", url="https://example.com/webhook")
        self.registry.register(endpoint_id="ep2", url="https://example.com/webhook2")
        self.registry.disable("ep1")
        
        active = self.registry.get_active()
        assert len(active) == 1
        assert active[0]["id"] == "ep2"
    
    def test_mark_delivered_idempotent(self):
        """Test that marking delivered is idempotent."""
        self.registry.register(endpoint_id="ep1", url="https://example.com/webhook")
        
        # First mark
        result1 = self.registry.mark_delivered("ep1", "task1")
        assert result1 is True
        
        # Second mark - should still succeed (idempotent)
        result2 = self.registry.mark_delivered("ep1", "task1")
        assert result2 is True
    
    def test_is_delivered(self):
        """Test checking if task was delivered."""
        self.registry.register(endpoint_id="ep1", url="https://example.com/webhook")
        self.registry.mark_delivered("ep1", "task1")
        
        assert self.registry.is_delivered("ep1", "task1") is True
        assert self.registry.is_delivered("ep1", "task2") is False
    
    def test_validate_refreshes_timestamp(self):
        """Test that validate() refreshes validation timestamp."""
        self.registry.register(endpoint_id="ep1", url="https://example.com/webhook")
        
        # Initially no validation
        assert self.registry._validated_at.get("ep1") is None
        
        # Validate
        self.registry.validate("ep1")
        first_validate = self.registry._validated_at["ep1"]
        
        time.sleep(0.01)
        
        # Validate again
        self.registry.validate("ep1")
        second_validate = self.registry._validated_at["ep1"]
        
        assert second_validate > first_validate


class TestDeliveryValidation:
    """Tests for delivery validation with disabled endpoints."""
    
    def setup_method(self):
        """Reset registry before each test."""
        reset_endpoint_registry()
        self.registry = get_endpoint_registry()
    
    def test_delivery_to_disabled_endpoint_rejected(self):
        """Test that delivery to disabled endpoint is rejected."""
        self.registry.register(endpoint_id="ep1", url="https://example.com/webhook")
        self.registry.disable("ep1")
        
        # Validate should fail for disabled endpoint
        result = self.registry.is_valid_for_delivery("ep1", "task1")
        assert result is False
    
    def test_delivery_to_rotated_endpoint_rejected(self):
        """Test that delivery to rotated endpoint is rejected."""
        self.registry.register(endpoint_id="ep1", url="https://example.com/webhook")
        self.registry.rotate("ep1")
        
        # Validate should fail for rotated endpoint
        result = self.registry.is_valid_for_delivery("ep1", "task1")
        assert result is False
    
    def test_delivery_to_deleted_endpoint_rejected(self):
        """Test that delivery to deleted endpoint is rejected."""
        self.registry.register(endpoint_id="ep1", url="https://example.com/webhook")
        self.registry.delete("ep1")
        
        # Validate should fail for deleted endpoint
        result = self.registry.is_valid_for_delivery("ep1", "task1")
        assert result is False
    
    def test_already_delivered_task_rejected(self):
        """Test that already delivered task is rejected (idempotency)."""
        self.registry.register(endpoint_id="ep1", url="https://example.com/webhook")
        self.registry.mark_delivered("ep1", "task1")
        
        # Validate should fail for already delivered task
        result = self.registry.is_valid_for_delivery("ep1", "task1")
        assert result is False
    
    def test_active_endpoint_valid_for_delivery(self):
        """Test that active endpoint is valid for delivery."""
        self.registry.register(endpoint_id="ep1", url="https://example.com/webhook")
        self.registry.validate("ep1")
        
        result = self.registry.is_valid_for_delivery("ep1", "task1")
        assert result is True
    
    def test_workspace_scope_enforcement(self):
        """Test that workspace scope is enforced."""
        self.registry.register(
            endpoint_id="ep1",
            url="https://example.com/webhook",
            workspace="workspace1",
            scope=EndpointScope.WORKSPACE,
        )
        
        # Same workspace - should work
        endpoints = self.registry.get_active(workspace="workspace1")
        assert len(endpoints) == 1
        
        # Different workspace - should not see endpoint
        endpoints = self.registry.get_active(workspace="workspace2")
        assert len(endpoints) == 0
    
    def test_global_scope_accessible_from_any_workspace(self):
        """Test that global scope endpoints are accessible from any workspace."""
        self.registry.register(
            endpoint_id="ep1",
            url="https://example.com/webhook",
            workspace="workspace1",
            scope=EndpointScope.GLOBAL,
        )
        
        # Global endpoint should be accessible from any workspace
        endpoints = self.registry.get_active(workspace="workspace2")
        assert len(endpoints) == 1
        assert endpoints[0]["id"] == "ep1"


class TestEndpointListStatus:
    """Tests for endpoint status listing."""
    
    def setup_method(self):
        """Reset registry before each test."""
        reset_endpoint_registry()
        self.registry = get_endpoint_registry()
    
    def test_list_status_counts(self):
        """Test that status counts are correct."""
        self.registry.register(endpoint_id="ep1", url="https://example.com/webhook")
        self.registry.register(endpoint_id="ep2", url="https://example.com/webhook2")
        self.registry.register(endpoint_id="ep3", url="https://example.com/webhook3")
        
        self.registry.disable("ep2")
        self.registry.rotate("ep3")
        
        status_counts = self.registry.list_status()
        assert status_counts["active"] == 1
        assert status_counts["disabled"] == 1
        assert status_counts["rotated"] == 1
        assert status_counts["deleted"] == 0


# 2026-05-25T18:00:00 update - Tests for issue #4202
# Test coverage for: valid delivery, rejected delivery, retry behavior, workspace isolation