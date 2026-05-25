"""Webhook Delivery — Validates endpoint delivery with idempotent retries.

This module integrates with the scheduler and endpoint registry to ensure:
1. Events are only delivered to active, validated endpoints
2. Retries are idempotent (same task won't be delivered twice to same endpoint)
3. Workspace isolation is enforced for endpoint access
"""

import logging
import time
from enum import Enum
from typing import Any, Callable, Dict, Optional

from src.orchestrator.endpoint import (
    EndpointRegistry,
    EndpointStatus,
    get_endpoint_registry,
)
from src.orchestrator.scheduler import TaskScheduler


logger = logging.getLogger(__name__)


class DeliveryState(Enum):
    """Delivery state machine states."""
    PENDING = "pending"
    VALIDATED = "validated"
    DELIVERED = "delivered"
    RETRYING = "retrying"
    FAILED = "failed"
    REJECTED = "rejected"


class DeliveryResult:
    """Result of a delivery attempt."""
    
    def __init__(
        self,
        success: bool,
        task_id: str,
        endpoint_id: str,
        state: DeliveryState,
        error: Optional[str] = None,
        retryable: bool = False,
    ):
        self.success = success
        self.task_id = task_id
        self.endpoint_id = endpoint_id
        self.state = state
        self.error = error
        self.retryable = retryable
        self.timestamp = time.time()


class WebhookDeliveryManager:
    """Manages webhook delivery with endpoint validation and idempotent retries.
    
    This ensures:
    1. Endpoints are validated before delivery (not disabled/rotated/deleted)
    2. Workspace isolation is enforced
    3. Retries are idempotent (task already delivered won't be re-delivered)
    4. Stale state is rejected before protected actions continue
    """
    
    def __init__(
        self,
        scheduler: Optional[TaskScheduler] = None,
        endpoint_registry: Optional[EndpointRegistry] = None,
    ):
        self.scheduler = scheduler or TaskScheduler()
        self.endpoint_registry = endpoint_registry or get_endpoint_registry()
        # Track delivery states (task_id -> state)
        self._delivery_states: Dict[str, DeliveryState] = {}
        # Track delivery attempts for idempotency
        self._delivery_attempts: Dict[str, Dict[str, int]] = {}  # task_id -> {endpoint_id: attempt}
    
    def validate_endpoint_for_delivery(
        self,
        endpoint_id: str,
        task_id: str,
        workspace: Optional[str] = None,
    ) -> DeliveryResult:
        """Validate that an endpoint can receive delivery.
        
        Returns a DeliveryResult indicating if delivery should proceed.
        """
        # Check endpoint exists
        endpoint = self.endpoint_registry.get(endpoint_id)
        if not endpoint:
            return DeliveryResult(
                success=False,
                task_id=task_id,
                endpoint_id=endpoint_id,
                state=DeliveryState.REJECTED,
                error=f"Endpoint {endpoint_id} not found",
                retryable=False,
            )
        
        # Check endpoint status
        status = endpoint.get("status")
        if status != EndpointStatus.ACTIVE.value:
            return DeliveryResult(
                success=False,
                task_id=task_id,
                endpoint_id=endpoint_id,
                state=DeliveryState.REJECTED,
                error=f"Endpoint {endpoint_id} is {status}, not active",
                retryable=False,
            )
        
        # Validate workspace isolation if specified
        if workspace and endpoint.get("workspace") != workspace:
            # Check scope - global endpoints can be accessed from any workspace
            if endpoint.get("scope") != "global":
                return DeliveryResult(
                    success=False,
                    task_id=task_id,
                    endpoint_id=endpoint_id,
                    state=DeliveryState.REJECTED,
                    error=f"Endpoint {endpoint_id} belongs to different workspace",
                    retryable=False,
                )
        
        # Check if task was already delivered to this endpoint (idempotency)
        if self.endpoint_registry.is_delivered(endpoint_id, task_id):
            return DeliveryResult(
                success=True,  # Already delivered, consider it success for idempotency
                task_id=task_id,
                endpoint_id=endpoint_id,
                state=DeliveryState.DELIVERED,
                error="Already delivered (idempotent)",
                retryable=False,
            )
        
        # Validate the endpoint is still within TTL
        if not self.endpoint_registry.is_valid_for_delivery(endpoint_id, task_id):
            # Refresh validation
            self.endpoint_registry.validate(endpoint_id)
        
        return DeliveryResult(
            success=True,
            task_id=task_id,
            endpoint_id=endpoint_id,
            state=DeliveryState.VALIDATED,
            retryable=True,
        )
    
    async def deliver(
        self,
        task: Dict,
        endpoint_id: str,
        deliver_func: Callable,
        workspace: Optional[str] = None,
    ) -> DeliveryResult:
        """Attempt to deliver a task to an endpoint.
        
        Args:
            task: The task to deliver
            endpoint_id: Target endpoint ID
            deliver_func: Async function to call for actual delivery
            workspace: Optional workspace for isolation validation
        
        Returns:
            DeliveryResult indicating outcome
        """
        task_id = task.get("id", "")
        
        # Track delivery attempt for idempotency
        if task_id not in self._delivery_attempts:
            self._delivery_attempts[task_id] = {}
        attempts = self._delivery_attempts[task_id]
        attempt_count = attempts.get(endpoint_id, 0) + 1
        attempts[endpoint_id] = attempt_count
        
        # Validate endpoint before delivery
        validation_result = self.validate_endpoint_for_delivery(endpoint_id, task_id, workspace)
        if not validation_result.success:
            self._delivery_states[task_id] = validation_result.state
            return validation_result
        
        # Mark state as validated
        self._delivery_states[task_id] = DeliveryState.VALIDATED
        
        # Perform the actual delivery
        try:
            result = await deliver_func(task, endpoint_id)
            
            # Mark as delivered (idempotent)
            self.endpoint_registry.mark_delivered(endpoint_id, task_id)
            self._delivery_states[task_id] = DeliveryState.DELIVERED
            
            return DeliveryResult(
                success=True,
                task_id=task_id,
                endpoint_id=endpoint_id,
                state=DeliveryState.DELIVERED,
            )
            
        except Exception as e:
            logger.error(f"Delivery failed for task {task_id} to {endpoint_id}: {e}")
            
            # Check if retryable
            retryable = self._is_retryable_error(e)
            
            if retryable:
                self._delivery_states[task_id] = DeliveryState.RETRYING
            else:
                self._delivery_states[task_id] = DeliveryState.FAILED
            
            return DeliveryResult(
                success=False,
                task_id=task_id,
                endpoint_id=endpoint_id,
                state=self._delivery_states[task_id],
                error=str(e),
                retryable=retryable,
            )
    
    def _is_retryable_error(self, error: Exception) -> bool:
        """Determine if an error is retryable."""
        non_retryable_messages = [
            "not found",
            "unauthorized",
            "forbidden",
            "disabled",
            "rotated",
            "deleted",
        ]
        error_msg = str(error).lower()
        return not any(msg in error_msg for msg in non_retryable_messages)
    
    def get_delivery_state(self, task_id: str) -> Optional[DeliveryState]:
        """Get the current delivery state for a task."""
        return self._delivery_states.get(task_id)
    
    def is_delivered(self, endpoint_id: str, task_id: str) -> bool:
        """Check if task was already delivered to endpoint (for idempotency)."""
        return self.endpoint_registry.is_delivered(endpoint_id, task_id)
    
    def should_retry(self, task_id: str, endpoint_id: str, max_retries: int = 3) -> bool:
        """Check if a task should be retried for a specific endpoint.
        
        This ensures retries are idempotent by tracking attempts.
        """
        attempts = self._delivery_attempts.get(task_id, {})
        current_attempt = attempts.get(endpoint_id, 0)
        return current_attempt < max_retries
    
    def reset_delivery_state(self, task_id: str) -> None:
        """Reset delivery state for a task (used after successful retry)."""
        self._delivery_states.pop(task_id, None)
    
    def reject_delivery(self, endpoint_id: str, task_id: str, reason: str) -> DeliveryResult:
        """Reject a delivery with a specific reason.
        
        Used when endpoint validation fails before delivery attempt.
        """
        self._delivery_states[task_id] = DeliveryState.REJECTED
        return DeliveryResult(
            success=False,
            task_id=task_id,
            endpoint_id=endpoint_id,
            state=DeliveryState.REJECTED,
            error=reason,
            retryable=False,
        )


# Global delivery manager instance
_delivery_manager: Optional[WebhookDeliveryManager] = None


def get_delivery_manager() -> WebhookDeliveryManager:
    """Get the global delivery manager instance."""
    global _delivery_manager
    if _delivery_manager is None:
        _delivery_manager = WebhookDeliveryManager()
    return _delivery_manager


def reset_delivery_manager() -> None:
    """Reset the global delivery manager (for testing)."""
    global _delivery_manager
    _delivery_manager = None


# 2026-05-25T18:00:00 update - Implementation for issue #4202
# Prevent disabled endpoints from receiving queued events - delivery state fix