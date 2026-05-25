"""Webhook service — subscription management, validation, and delivery."""

import time
from typing import Any, Dict, List, Optional

from .models import (
    ALLOWED_EVENT_TYPES,
    WebhookDelivery,
    WebhookDeliveryStatus,
    WebhookSubscription,
)
from ..common.errors import (
    WebhookEndpointDisabledError,
    WebhookEventTypeError,
    WebhookNotFoundError,
)


class WebhookService:
    def __init__(self):
        self._subscriptions: Dict[str, WebhookSubscription] = {}
        self._deliveries: Dict[str, WebhookDelivery] = {}
        self._delivery_index: Dict[str, List[str]] = {}

    def validate_event_types(self, event_types: List[str]) -> None:
        """Reject any event type not on the allowlist.
        
        Raises WebhookEventTypeError if any event type is not in ALLOWED_EVENT_TYPES.
        """
        for et in event_types:
            if et not in ALLOWED_EVENT_TYPES:
                raise WebhookEventTypeError(et)

    def create_subscription(
        self,
        endpoint_url: str,
        event_types: List[str],
        workspace_id: str,
        secret: Optional[str] = None,
        description: Optional[str] = None,
    ) -> WebhookSubscription:
        if not event_types:
            raise WebhookEventTypeError("(empty list)")
        self.validate_event_types(event_types)
        # Validate endpoint_url before persistence (fail-closed)
        if not endpoint_url or not endpoint_url.startswith(("http://", "https://")):
            raise ValueError("Invalid endpoint URL: must be a non-empty HTTP/HTTPS URL")
        sub = WebhookSubscription(
            endpoint_url=endpoint_url,
            event_types=event_types,
            workspace_id=workspace_id,
            secret=secret,
            description=description,
        )
        # Persist only after all validation passes
        self._subscriptions[sub.id] = sub
        return sub

    def get_subscription(self, subscription_id: str, workspace_id: str) -> WebhookSubscription:
        sub = self._subscriptions.get(subscription_id)
        if sub is None or sub.workspace_id != workspace_id:
            raise WebhookNotFoundError(subscription_id)
        return sub

    def list_subscriptions(self, workspace_id: str) -> List[WebhookSubscription]:
        return [s for s in self._subscriptions.values() if s.workspace_id == workspace_id]

    def update_subscription(
        self,
        subscription_id: str,
        workspace_id: str,
        event_types: Optional[List[str]] = None,
        endpoint_url: Optional[str] = None,
        description: Optional[str] = None,
    ) -> WebhookSubscription:
        sub = self.get_subscription(subscription_id, workspace_id)
        if event_types is not None:
            if not event_types:
                raise WebhookEventTypeError("(empty list)")
            self.validate_event_types(event_types)
            sub.event_types = event_types
        if endpoint_url is not None:
            # Validate endpoint_url before updating
            if not endpoint_url or not endpoint_url.startswith(("http://", "https://")):
                raise ValueError("Invalid endpoint URL: must be a non-empty HTTP/HTTPS URL")
            sub.endpoint_url = endpoint_url
        if description is not None:
            sub.description = description
        sub.updated_at = time.time()
        return sub

    def delete_subscription(self, subscription_id: str, workspace_id: str) -> bool:
        sub = self._subscriptions.get(subscription_id)
        if sub is None or sub.workspace_id != workspace_id:
            raise WebhookNotFoundError(subscription_id)
        del self._subscriptions[subscription_id]
        return True

    def disable_subscription(self, subscription_id: str, workspace_id: str) -> WebhookSubscription:
        sub = self.get_subscription(subscription_id, workspace_id)
        sub.enabled = False
        sub.updated_at = time.time()
        return sub

    def enable_subscription(self, subscription_id: str, workspace_id: str) -> WebhookSubscription:
        sub = self.get_subscription(subscription_id, workspace_id)
        sub.enabled = True
        sub.updated_at = time.time()
        return sub

    def rotate_secret(self, subscription_id: str, workspace_id: str) -> WebhookSubscription:
        import uuid
        sub = self.get_subscription(subscription_id, workspace_id)
        sub.secret = str(uuid.uuid4())
        sub.updated_at = time.time()
        return sub

    def deliver_event(
        self,
        event_type: str,
        payload: Dict[str, Any],
        workspace_id: str,
        idempotency_key: Optional[str] = None,
    ) -> Optional[WebhookDelivery]:
        """Deliver an event to all matching, enabled subscriptions in the workspace.

        If idempotency_key is provided and a delivery with that key already exists,
        return the existing delivery instead of creating a duplicate.
        
        Validation (fail-closed):
        - Idempotency key checked FIRST to short-circuit duplicate deliveries
        - Event type validated BEFORE any state mutation
        - Delivery persisted ONLY after all validation passes
        """
        # Check idempotency BEFORE validation to avoid side effects from re-validation
        if idempotency_key and idempotency_key in self._deliveries:
            return self._deliveries[idempotency_key]

        # Validate event type BEFORE any state mutation or delivery (fail-closed)
        if event_type not in ALLOWED_EVENT_TYPES:
            raise WebhookEventTypeError(event_type)

        matching_subs = [
            s for s in self._subscriptions.values()
            if s.workspace_id == workspace_id
            and s.enabled
            and event_type in s.event_types
        ]

        # No matching subscription is not an error - silent drop
        if not matching_subs:
            return None

        delivery = WebhookDelivery(
            subscription_id=matching_subs[0].id,
            event_type=event_type,
            payload=payload,
            workspace_id=workspace_id,
        )

        # Assign idempotency key after validation
        if idempotency_key:
            delivery.id = idempotency_key

        delivery.status = WebhookDeliveryStatus.DELIVERED
        delivery.attempts = 1
        delivery.last_attempt_at = time.time()
        delivery.response_status = 200

        # Persist only after all validation passes
        self._deliveries[delivery.id] = delivery
        sub_key = delivery.subscription_id
        if sub_key not in self._delivery_index:
            self._delivery_index[sub_key] = []
        self._delivery_index[sub_key].append(delivery.id)

        return delivery

    def retry_delivery(self, delivery_id: str, workspace_id: str) -> WebhookDelivery:
        """Retry a failed delivery. Idempotent: no-ops if already delivered."""
        delivery = self._deliveries.get(delivery_id)
        if delivery is None or delivery.workspace_id != workspace_id:
            raise WebhookNotFoundError(delivery_id)

        sub = self._subscriptions.get(delivery.subscription_id)
        if sub is None:
            raise WebhookNotFoundError(delivery.subscription_id)
        if not sub.enabled:
            raise WebhookEndpointDisabledError(delivery.subscription_id)

        # Idempotent: return already-delivered deliveries as-is
        if delivery.status == WebhookDeliveryStatus.DELIVERED:
            return delivery

        if delivery.attempts >= delivery.max_attempts:
            delivery.status = WebhookDeliveryStatus.FAILED
            return delivery

        delivery.attempts += 1
        delivery.last_attempt_at = time.time()
        delivery.response_status = 200
        delivery.status = WebhookDeliveryStatus.DELIVERED
        return delivery

    def get_delivery(self, delivery_id: str, workspace_id: str) -> WebhookDelivery:
        delivery = self._deliveries.get(delivery_id)
        if delivery is None or delivery.workspace_id != workspace_id:
            raise WebhookNotFoundError(delivery_id)
        return delivery

    def list_deliveries(self, subscription_id: str, workspace_id: str) -> List[WebhookDelivery]:
        sub = self.get_subscription(subscription_id, workspace_id)
        delivery_ids = self._delivery_index.get(sub.id, [])
        return [self._deliveries[did] for did in delivery_ids if did in self._deliveries]
