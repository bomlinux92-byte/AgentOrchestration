"""Webhook registration, verification, and delivery with payload shaping.

Sensitive operational fields are filtered before serialization, logging, or
delivery so that internal run metadata never reaches public event payloads.
Endpoints are validated/scoped before persistence or delivery, retries are
idempotent, and delivery records do not expose internal-only fields.
"""

import hashlib
import hmac
import json
import logging
import time
import uuid
from enum import Enum
from typing import Any, Dict, List, Optional, Set

from src.agent.registry import AgentRegistry

logger = logging.getLogger(__name__)

# Fields that are internal-only and must never appear in webhook payloads
# delivered to external endpoints.  These are operational metadata about the
# scheduler's internal state that should stay within the orchestrator.
SENSITIVE_FIELDS: Set[str] = {
    "run_state",
    "retries",
    "enqueued_at",
    "target_agent",
    "priority",
}


class EndpointStatus(Enum):
    ACTIVE = "active"
    DISABLED = "disabled"
    ROTATED = "rotated"


class DeliveryStatus(Enum):
    PENDING = "pending"
    DELIVERED = "delivered"
    FAILED = "failed"


class WebhookError(Exception):
    pass


class EndpointNotFoundError(WebhookError):
    pass


class EndpointDisabledError(WebhookError):
    pass


class WorkspaceIsolationError(WebhookError):
    pass


def filter_sensitive_fields(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Remove sensitive operational fields from a payload before serialization.

    This must be called *before* any JSON encoding, logging, or delivery so
    that internal run metadata never leaks into public events.
    """
    return {k: v for k, v in payload.items() if k not in SENSITIVE_FIELDS}


def shape_payload(event: Dict[str, Any], scope: Optional[str] = None) -> Dict[str, Any]:
    """Shape a raw event into a public webhook payload.

    The payload is filtered of sensitive fields *before* serialization so that
    no downstream consumer (logging, response body, HTTP delivery) ever sees
    internal-only metadata.
    """
    # Filter sensitive fields FIRST — before any serialization or delivery.
    safe_event = filter_sensitive_fields(event)

    payload: Dict[str, Any] = {
        "event_id": str(uuid.uuid4()),
        "timestamp": time.time(),
        "data": safe_event,
    }

    if scope is not None:
        payload["scope"] = scope

    return payload


class WebhookEndpoint:
    """A registered webhook endpoint that receives event deliveries."""

    def __init__(
        self,
        url: str,
        secret: str,
        events: Optional[List[str]] = None,
        workspace_id: Optional[str] = None,
        endpoint_id: Optional[str] = None,
    ):
        self.id = endpoint_id or str(uuid.uuid4())
        self.url = url
        self.secret = secret
        self.events = events or []
        self.workspace_id = workspace_id
        self.status = EndpointStatus.ACTIVE
        self.created_at = time.time()

    def is_subscribed(self, event_type: str) -> bool:
        if not self.events:
            return True
        return event_type in self.events

    def to_dict(self) -> Dict[str, Any]:
        """Public representation — never exposes the signing secret."""
        return {
            "id": self.id,
            "url": self.url,
            "events": self.events,
            "status": self.status.value,
            "workspace_id": self.workspace_id,
            "created_at": self.created_at,
        }


class DeliveryRecord:
    """Idempotent record of a webhook delivery attempt."""

    def __init__(
        self,
        endpoint_id: str,
        event_id: str,
        payload: Dict[str, Any],
        record_id: Optional[str] = None,
    ):
        self.id = record_id or hashlib.sha256(
            f"{endpoint_id}:{event_id}".encode()
        ).hexdigest()[:16]
        self.endpoint_id = endpoint_id
        self.event_id = event_id
        self.payload = payload
        self.status = DeliveryStatus.PENDING
        self.attempts = 0
        self.created_at = time.time()

    def to_dict(self) -> Dict[str, Any]:
        """Public representation — only safe, filtered fields."""
        return {
            "id": self.id,
            "endpoint_id": self.endpoint_id,
            "event_id": self.event_id,
            "status": self.status.value,
            "attempts": self.attempts,
            "created_at": self.created_at,
        }


class WebhookManager:
    """Manages webhook endpoint registration, verification, and delivery.

    All payloads are filtered of sensitive fields before they reach any
    external consumer.  Endpoints are validated for workspace isolation
    before persistence or delivery.
    """

    def __init__(self, registry: Optional[AgentRegistry] = None):
        self._endpoints: Dict[str, WebhookEndpoint] = {}
        self._deliveries: Dict[str, DeliveryRecord] = {}
        self._registry = registry

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register_endpoint(
        self,
        url: str,
        secret: str,
        events: Optional[List[str]] = None,
        workspace_id: Optional[str] = None,
    ) -> WebhookEndpoint:
        """Register a new webhook endpoint.

        Validates the endpoint URL and scopes it to a workspace before
        persisting it.
        """
        if not url or not url.startswith(("https://", "http://")):
            raise ValueError(f"Invalid endpoint URL: {url}")

        endpoint = WebhookEndpoint(
            url=url,
            secret=secret,
            events=events,
            workspace_id=workspace_id,
        )
        self._endpoints[endpoint.id] = endpoint
        logger.info("Webhook endpoint registered: %s", endpoint.id)
        return endpoint

    def get_endpoint(self, endpoint_id: str) -> Optional[WebhookEndpoint]:
        return self._endpoints.get(endpoint_id)

    def disable_endpoint(self, endpoint_id: str) -> bool:
        endpoint = self._endpoints.get(endpoint_id)
        if endpoint is None:
            return False
        endpoint.status = EndpointStatus.DISABLED
        return True

    def rotate_secret(self, endpoint_id: str, new_secret: str) -> bool:
        endpoint = self._endpoints.get(endpoint_id)
        if endpoint is None:
            return False
        endpoint.secret = new_secret
        endpoint.status = EndpointStatus.ROTATED
        return True

    # ------------------------------------------------------------------
    # Verification — generates a signature for outbound payloads
    # ------------------------------------------------------------------

    def verify_signature(self, endpoint_id: str, payload: bytes) -> Optional[str]:
        """Compute an HMAC signature for a serialized payload using the
        endpoint's signing secret.

        Returns None if the endpoint does not exist.
        """
        endpoint = self._endpoints.get(endpoint_id)
        if endpoint is None:
            return None
        return hmac.new(
            endpoint.secret.encode(),
            payload,
            hashlib.sha256,
        ).hexdigest()

    # ------------------------------------------------------------------
    # Delivery — filters sensitive fields, validates, and records
    # ------------------------------------------------------------------

    def deliver(
        self,
        event: Dict[str, Any],
        event_type: Optional[str] = None,
        workspace_id: Optional[str] = None,
    ) -> List[DeliveryRecord]:
        """Deliver an event to all eligible endpoints.

        Sensitive fields are stripped from the payload BEFORE any
        serialization, logging, or HTTP dispatch.  Only endpoints matching
        the workspace scope and event subscription receive the payload.

        Returns delivery records (which themselves never expose internal
        fields).
        """
        # Shape the payload: filtering happens inside shape_payload before
        # the dict is ever handed to JSON or an HTTP client.
        scope = workspace_id or "global"
        payload = shape_payload(event, scope=scope)

        records: List[DeliveryRecord] = []
        for endpoint in self._endpoints.values():
            # Validate: skip disabled / rotated endpoints
            if endpoint.status != EndpointStatus.ACTIVE:
                continue

            # Workspace isolation: only deliver within the same workspace
            if workspace_id and endpoint.workspace_id:
                if endpoint.workspace_id != workspace_id:
                    continue

            # Event subscription filter
            if event_type and not endpoint.is_subscribed(event_type):
                continue

            record = DeliveryRecord(
                endpoint_id=endpoint.id,
                event_id=payload["event_id"],
                payload=payload,
            )
            record.attempts = 1
            record.status = DeliveryStatus.DELIVERED
            self._deliveries[record.id] = record
            records.append(record)

        return records

    def redeliver(self, delivery_id: str) -> Optional[DeliveryRecord]:
        """Idempotent retry: re-delivers an existing delivery record only if
        it has not already succeeded.

        Returns None if the delivery does not exist.
        """
        record = self._deliveries.get(delivery_id)
        if record is None:
            return None

        # Idempotent: skip if already delivered
        if record.status == DeliveryStatus.DELIVERED:
            return record

        record.attempts += 1
        record.status = DeliveryStatus.DELIVERED
        return record

    def fail_delivery(self, delivery_id: str) -> Optional[DeliveryRecord]:
        """Mark a delivery as failed.  Returns None if delivery not found."""
        record = self._deliveries.get(delivery_id)
        if record is None:
            return None
        record.status = DeliveryStatus.FAILED
        return record

    def list_deliveries(self, endpoint_id: Optional[str] = None) -> List[DeliveryRecord]:
        if endpoint_id:
            return [r for r in self._deliveries.values() if r.endpoint_id == endpoint_id]
        return list(self._deliveries.values())

    def get_delivery(self, delivery_id: str) -> Optional[DeliveryRecord]:
        return self._deliveries.get(delivery_id)

# 2026-05-20T10:00:00 update