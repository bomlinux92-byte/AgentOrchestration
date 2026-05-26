"""Webhook data models and event type allowlist."""

import time
import uuid
from enum import Enum
from typing import Any, Dict, List, Optional

ALLOWED_EVENT_TYPES: frozenset[str] = frozenset({
    "agent.created",
    "agent.started",
    "agent.stopped",
    "agent.failed",
    "agent.terminated",
    "task.created",
    "task.completed",
    "task.failed",
    "task.timeout",
    "workflow.started",
    "workflow.completed",
    "workflow.failed",
    "deployment.started",
    "deployment.completed",
    "deployment.failed",
})


class WebhookDeliveryStatus(Enum):
    PENDING = "pending"
    DELIVERED = "delivered"
    FAILED = "failed"
    RETRYING = "retrying"


class WebhookSubscription:
    def __init__(
        self,
        endpoint_url: str,
        event_types: List[str],
        workspace_id: str,
        secret: Optional[str] = None,
        description: Optional[str] = None,
    ):
        self.id = str(uuid.uuid4())
        self.endpoint_url = endpoint_url
        self.event_types = list(event_types)
        self.workspace_id = workspace_id
        self.secret = secret or str(uuid.uuid4())
        self.description = description or ""
        self.enabled = True
        self.created_at = time.time()
        self.updated_at = time.time()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "endpoint_url": self.endpoint_url,
            "event_types": self.event_types,
            "workspace_id": self.workspace_id,
            "description": self.description,
            "enabled": self.enabled,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class WebhookDelivery:
    def __init__(
        self,
        subscription_id: str,
        event_type: str,
        payload: Dict[str, Any],
        workspace_id: str,
    ):
        self.id = str(uuid.uuid4())
        self.subscription_id = subscription_id
        self.event_type = event_type
        self.payload = payload
        self.workspace_id = workspace_id
        self.status = WebhookDeliveryStatus.PENDING
        self.attempts = 0
        self.max_attempts = 5
        self.created_at = time.time()
        self.last_attempt_at: Optional[float] = None
        self.response_status: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "subscription_id": self.subscription_id,
            "event_type": self.event_type,
            "payload": self.payload,
            "status": self.status.value,
            "attempts": self.attempts,
            "created_at": self.created_at,
            "last_attempt_at": self.last_attempt_at,
        }
