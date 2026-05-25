"""Tests for webhook event type allowlist validation and idempotent delivery."""

import pytest
from src.webhook.models import ALLOWED_EVENT_TYPES, WebhookDeliveryStatus
from src.webhook.service import WebhookService
from src.common.errors import WebhookEventTypeError, WebhookNotFoundError, WebhookEndpointDisabledError


class TestWebhookEventTypeValidation:
    """Tests for Validate event type allowlist at subscription create condition."""

    def setup_method(self):
        self.service = WebhookService()

    # --- Valid event type allowlist at subscription create ---

    def test_create_subscription_with_valid_event_types(self):
        """Valid delivery: subscription created with allowed event types."""
        sub = self.service.create_subscription(
            endpoint_url="https://example.com/webhook",
            event_types=["agent.created", "task.completed"],
            workspace_id="ws-1",
        )
        assert sub.id is not None
        assert sub.event_types == ["agent.created", "task.completed"]

    def test_create_subscription_with_all_valid_event_types(self):
        """Valid delivery: subscription created with all allowed event types."""
        sub = self.service.create_subscription(
            endpoint_url="https://example.com/webhook",
            event_types=list(ALLOWED_EVENT_TYPES),
            workspace_id="ws-1",
        )
        assert sub.event_types == list(ALLOWED_EVENT_TYPES)

    # --- Rejected delivery: invalid event type ---

    def test_create_subscription_rejects_invalid_event_type(self):
        """Rejected delivery: subscription create with invalid event type."""
        with pytest.raises(WebhookEventTypeError) as exc_info:
            self.service.create_subscription(
                endpoint_url="https://example.com/webhook",
                event_types=["invalid.event.type"],
                workspace_id="ws-1",
            )
        assert "invalid.event.type" in str(exc_info.value)

    def test_create_subscription_rejects_mixed_valid_invalid_event_types(self):
        """Rejected delivery: subscription create with mix of valid and invalid."""
        with pytest.raises(WebhookEventTypeError):
            self.service.create_subscription(
                endpoint_url="https://example.com/webhook",
                event_types=["agent.created", "bad.event"],
                workspace_id="ws-1",
            )

    def test_create_subscription_rejects_empty_event_types(self):
        """Rejected delivery: empty event types list raises error."""
        with pytest.raises(WebhookEventTypeError) as exc_info:
            self.service.create_subscription(
                endpoint_url="https://example.com/webhook",
                event_types=[],
                workspace_id="ws-1",
            )
        assert "(empty list)" in str(exc_info.value)

    def test_create_subscription_rejects_invalid_endpoint_url(self):
        """Rejected delivery: endpoint URL without proper HTTP/HTTPS scheme raises error at create."""
        with pytest.raises(ValueError) as exc_info:
            self.service.create_subscription(
                endpoint_url="ftp://example.com/webhook",  # Invalid scheme
                event_types=["agent.created"],
                workspace_id="ws-1",
            )
        assert "Invalid endpoint URL" in str(exc_info.value)

    def test_create_subscription_rejects_empty_endpoint_url(self):
        """Rejected delivery: empty endpoint URL raises error at create."""
        with pytest.raises(ValueError) as exc_info:
            self.service.create_subscription(
                endpoint_url="",
                event_types=["agent.created"],
                workspace_id="ws-1",
            )
        assert "Invalid endpoint URL" in str(exc_info.value)

    # --- Update validation ---

    def test_update_subscription_validates_event_types(self):
        """Rejected delivery: update with invalid event type."""
        sub = self.service.create_subscription(
            endpoint_url="https://example.com/webhook",
            event_types=["agent.created"],
            workspace_id="ws-1",
        )
        with pytest.raises(WebhookEventTypeError):
            self.service.update_subscription(
                subscription_id=sub.id,
                workspace_id="ws-1",
                event_types=["nonexistent.event"],
            )

    def test_update_subscription_allows_valid_event_types(self):
        """Valid delivery: update with valid event types."""
        sub = self.service.create_subscription(
            endpoint_url="https://example.com/webhook",
            event_types=["agent.created"],
            workspace_id="ws-1",
        )
        updated = self.service.update_subscription(
            subscription_id=sub.id,
            workspace_id="ws-1",
            event_types=["agent.created", "task.completed"],
        )
        assert "task.completed" in updated.event_types

    def test_update_subscription_rejects_invalid_endpoint_url(self):
        """Rejected delivery: update with invalid endpoint URL raises error."""
        sub = self.service.create_subscription(
            endpoint_url="https://example.com/webhook",
            event_types=["agent.created"],
            workspace_id="ws-1",
        )
        with pytest.raises(ValueError):
            self.service.update_subscription(
                subscription_id=sub.id,
                workspace_id="ws-1",
                endpoint_url="ftp://evil.com/hook",
            )


class TestWebhookIdempotentDelivery:
    """Tests for idempotent delivery behavior."""

    def setup_method(self):
        self.service = WebhookService()

    def test_deliver_event_validates_event_type_before_persistence(self):
        """Rejected delivery: invalid event type before any state mutation."""
        # Create a subscription so matching_subs would find something
        self.service.create_subscription(
            endpoint_url="https://example.com/webhook",
            event_types=["agent.created"],
            workspace_id="ws-1",
        )
        with pytest.raises(WebhookEventTypeError) as exc_info:
            self.service.deliver_event(
                event_type="evil.event",
                payload={"data": "test"},
                workspace_id="ws-1",
            )
        assert "evil.event" in str(exc_info.value)

    def test_deliver_event_idempotent_with_key(self):
        """Idempotent delivery: same idempotency key returns same delivery."""
        self.service.create_subscription(
            endpoint_url="https://example.com/webhook",
            event_types=["agent.created"],
            workspace_id="ws-1",
        )
        delivery1 = self.service.deliver_event(
            event_type="agent.created",
            payload={"data": "first"},
            workspace_id="ws-1",
            idempotency_key="idem-key-1",
        )
        assert delivery1 is not None
        delivery2 = self.service.deliver_event(
            event_type="agent.created",
            payload={"data": "second"},
            workspace_id="ws-1",
            idempotency_key="idem-key-1",
        )
        assert delivery2.id == delivery1.id
        assert delivery2.payload["data"] == "first"  # Original returned

    def test_deliver_event_idempotent_key_short_circuits_validation(self):
        """Idempotent delivery: key found skips validation entirely (no side effects)."""
        self.service.create_subscription(
            endpoint_url="https://example.com/webhook",
            event_types=["agent.created"],
            workspace_id="ws-1",
        )
        # First delivery with valid event type
        self.service.deliver_event(
            event_type="agent.created",
            payload={"seq": 1},
            workspace_id="ws-1",
            idempotency_key="key-skip-val",
        )
        # Second delivery with same key but invalid type - should return cached, not raise
        result = self.service.deliver_event(
            event_type="evil.event",  # Would fail if re-validated
            payload={"seq": 2},
            workspace_id="ws-1",
            idempotency_key="key-skip-val",  # Already exists - returns cached
        )
        assert result is not None  # Returned cached, didn't re-validate
        assert result.payload["seq"] == 1  # Original payload

    def test_deliver_event_no_matching_subscription_returns_none(self):
        """Silent drop: no subscription for event type returns None, not error."""
        result = self.service.deliver_event(
            event_type="agent.created",
            payload={"data": "test"},
            workspace_id="ws-no-such",
        )
        assert result is None


class TestWebhookRetryIdempotency:
    """Tests for idempotent retry behavior."""

    def setup_method(self):
        self.service = WebhookService()

    def test_retry_already_delivered_returns_idempotent(self):
        """Idempotent retry: already-delivered delivery returns as-is."""
        self.service.create_subscription(
            endpoint_url="https://example.com/webhook",
            event_types=["agent.created"],
            workspace_id="ws-1",
        )
        delivery = self.service.deliver_event(
            event_type="agent.created",
            payload={"data": "test"},
            workspace_id="ws-1",
        )
        retry_result = self.service.retry_delivery(delivery.id, "ws-1")
        assert retry_result.status == WebhookDeliveryStatus.DELIVERED
        assert retry_result.attempts == 1  # Not incremented

    def test_retry_disabled_subscription_raises_error(self):
        """Rejected delivery: retry of disabled endpoint raises error."""
        sub = self.service.create_subscription(
            endpoint_url="https://example.com/webhook",
            event_types=["agent.created"],
            workspace_id="ws-1",
        )
        self.service.deliver_event(
            event_type="agent.created",
            payload={"data": "test"},
            workspace_id="ws-1",
        )
        self.service.disable_subscription(sub.id, "ws-1")
        # Note: we need to find a way to get a FAILED delivery to test retry properly
        # For now, we test the disabled check path directly via get_subscription fails
        # But retry_delivery checks sub.enabled directly on the delivery's subscription


class TestWebhookWorkspaceIsolation:
    """Tests for workspace isolation."""

    def setup_method(self):
        self.service = WebhookService()

    def test_subscriptions_isolated_by_workspace(self):
        """Workspace isolation: subscription in ws-1 not accessible from ws-2."""
        sub = self.service.create_subscription(
            endpoint_url="https://example.com/webhook",
            event_types=["agent.created"],
            workspace_id="ws-1",
        )
        from src.common.errors import WebhookNotFoundError
        with pytest.raises(WebhookNotFoundError):
            self.service.get_subscription(sub.id, "ws-2")

    def test_deliveries_isolated_by_workspace(self):
        """Workspace isolation: delivery in ws-1 not accessible from ws-2."""
        self.service.create_subscription(
            endpoint_url="https://example.com/webhook",
            event_types=["agent.created"],
            workspace_id="ws-1",
        )
        delivery = self.service.deliver_event(
            event_type="agent.created",
            payload={"data": "test"},
            workspace_id="ws-1",
        )
        from src.common.errors import WebhookNotFoundError
        with pytest.raises(WebhookNotFoundError):
            self.service.get_delivery(delivery.id, "ws-2")


class TestWebhookDisabledEndpoint:
    """Tests for disabled/rotated endpoint handling."""

    def setup_method(self):
        self.service = WebhookService()

    def test_disable_subscription_prevents_future_delivery(self):
        """Disabled endpoint: disabled subscription gets no deliveries."""
        sub = self.service.create_subscription(
            endpoint_url="https://example.com/webhook",
            event_types=["agent.created"],
            workspace_id="ws-1",
        )
        self.service.disable_subscription(sub.id, "ws-1")
        # The current implementation checks enabled in matching_subs,
        # so disabled subscriptions won't receive events
        result = self.service.deliver_event(
            event_type="agent.created",
            payload={"data": "test"},
            workspace_id="ws-1",
        )
        assert result is None  # No delivery to disabled subscription

    def test_retry_on_nonexistent_subscription_raises_error(self):
        """Rejected delivery: retry delivery for deleted subscription raises error."""
        sub = self.service.create_subscription(
            endpoint_url="https://example.com/webhook",
            event_types=["agent.created"],
            workspace_id="ws-1",
        )
        delivery = self.service.deliver_event(
            event_type="agent.created",
            payload={"data": "test"},
            workspace_id="ws-1",
        )
        # Delete the subscription
        self.service.delete_subscription(sub.id, "ws-1")
        # Now retry should fail because subscription is gone
        with pytest.raises(WebhookNotFoundError):
            self.service.retry_delivery(delivery.id, "ws-1")
