"""Tests for webhook payload shaping and sensitive field filtering.

Verifies that internal run metadata never leaks into public webhook events
as required by bounty issue #52.
"""

import pytest
from src.orchestrator.webhook import (
    SENSITIVE_FIELDS,
    WebhookManager,
    filter_sensitive_fields,
    shape_payload,
    WebhookEndpoint,
    DeliveryRecord,
)


class TestFilterSensitiveFields:
    """Unit tests for filter_sensitive_fields()."""

    def test_removes_run_state(self):
        """run_state is internal scheduler state — must not appear in payloads."""
        payload = {
            "event": "task.completed",
            "run_state": "in_flight",
            "result": "success",
        }
        filtered = filter_sensitive_fields(payload)
        assert "run_state" not in filtered
        assert "event" in filtered
        assert "result" in filtered

    def test_removes_retries(self):
        """retries is internal retry counter — must not appear in payloads."""
        payload = {
            "task_id": "abc",
            "retries": 3,
            "output": "data",
        }
        filtered = filter_sensitive_fields(payload)
        assert "retries" not in filtered
        assert "task_id" in filtered
        assert "output" in filtered

    def test_removes_enqueued_at(self):
        """enqueued_at is internal queue metadata — must not appear in payloads."""
        payload = {
            "task_id": "abc",
            "enqueued_at": 1716212345.0,
            "payload": {},
        }
        filtered = filter_sensitive_fields(payload)
        assert "enqueued_at" not in filtered
        assert "task_id" in filtered
        assert "payload" in filtered

    def test_removes_target_agent(self):
        """target_agent is internal routing info — must not appear in payloads."""
        payload = {
            "task_id": "abc",
            "target_agent": "agent-42",
            "data": {},
        }
        filtered = filter_sensitive_fields(payload)
        assert "target_agent" not in filtered
        assert "task_id" in filtered

    def test_removes_priority(self):
        """priority is internal scheduling metadata — must not appear in payloads."""
        payload = {
            "task_id": "abc",
            "priority": 10,
            "data": {},
        }
        filtered = filter_sensitive_fields(payload)
        assert "priority" not in filtered
        assert "task_id" in filtered

    def test_removes_all_sensitive_fields_at_once(self):
        """All sensitive fields present simultaneously are all removed."""
        payload = {
            "event": "task.completed",
            "run_state": "pending",
            "retries": 1,
            "enqueued_at": 1716212345.0,
            "target_agent": "agent-99",
            "priority": 5,
            "public_field": "visible",
        }
        filtered = filter_sensitive_fields(payload)
        assert "run_state" not in filtered
        assert "retries" not in filtered
        assert "enqueued_at" not in filtered
        assert "target_agent" not in filtered
        assert "priority" not in filtered
        assert "event" in filtered
        assert "public_field" in filtered

    def test_passthrough_when_no_sensitive_fields(self):
        """Payloads without sensitive fields pass through unchanged."""
        payload = {"event": "task.started", "task_id": "123"}
        filtered = filter_sensitive_fields(payload)
        assert filtered == payload

    def test_empty_payload(self):
        """Empty payload is returned as empty."""
        filtered = filter_sensitive_fields({})
        assert filtered == {}

    def test_does_not_modify_original(self):
        """Original payload is not mutated."""
        original = {"task_id": "abc", "run_state": "done", "priority": 1}
        original_copy = original.copy()
        filter_sensitive_fields(original)
        assert original == original_copy


class TestShapePayload:
    """Unit tests for shape_payload()."""

    def test_wraps_event_with_metadata(self):
        """Shaped payload adds event_id and timestamp."""
        event = {"task_id": "abc", "type": "test"}
        shaped = shape_payload(event)
        assert "event_id" in shaped
        assert "timestamp" in shaped
        assert shaped["data"] == event

    def test_strips_sensitive_fields_from_inner_event(self):
        """Sensitive fields inside the event are removed from shaped payload."""
        event = {
            "task_id": "abc",
            "run_state": "in_flight",
            "retries": 2,
            "enqueued_at": 1716212345.0,
            "target_agent": "agent-1",
            "priority": 5,
        }
        shaped = shape_payload(event)
        data = shaped["data"]
        assert "run_state" not in data
        assert "retries" not in data
        assert "enqueued_at" not in data
        assert "target_agent" not in data
        assert "priority" not in data
        assert "task_id" in data

    def test_adds_scope_when_provided(self):
        """Scope is included in shaped payload when specified."""
        event = {"task_id": "abc"}
        shaped = shape_payload(event, scope="workspace-42")
        assert shaped["scope"] == "workspace-42"

    def test_no_scope_when_none(self):
        """No scope key when scope is None."""
        event = {"task_id": "abc"}
        shaped = shape_payload(event)
        assert "scope" not in shaped

    def test_sensitive_fields_not_in_any_serialized_key(self):
        """Sensitive fields never appear in shaped payload at any level."""
        import json

        event = {
            "task_id": "abc",
            "run_state": "pending",
            "retries": 0,
            "enqueued_at": 1716212345.0,
            "target_agent": "agent-x",
            "priority": 3,
            "result": "ok",
        }
        shaped = shape_payload(event)
        json_str = json.dumps(shaped)
        for field in SENSITIVE_FIELDS:
            assert field not in json_str, f"敏感字段 {field} 不应出现在序列化后的 payload 中"


class TestWebhookEndpointPublicDict:
    """Tests that WebhookEndpoint.to_dict() never exposes the signing secret."""

    def test_to_dict_excludes_secret(self):
        """Public dict representation must not include the secret."""
        endpoint = WebhookEndpoint(
            url="https://example.com/webhook",
            secret="super-secret-key",
            events=["task.completed"],
        )
        public = endpoint.to_dict()
        assert "secret" not in public
        assert "super-secret-key" not in str(public.values())

    def test_to_dict_includes_public_fields(self):
        """Public fields are included in to_dict()."""
        endpoint = WebhookEndpoint(
            url="https://example.com/webhook",
            secret="secret",
            events=["task.completed"],
            workspace_id="ws-1",
        )
        public = endpoint.to_dict()
        assert "id" in public
        assert "url" in public
        assert "events" in public
        assert "status" in public


class TestDeliveryRecordPublicDict:
    """Tests that DeliveryRecord.to_dict() never exposes sensitive fields."""

    def test_to_dict_excludes_payload(self):
        """Delivery record public dict does not include the full payload."""
        record = DeliveryRecord(
            endpoint_id="ep-1",
            event_id="ev-1",
            payload={"task_id": "abc", "run_state": "done"},
        )
        public = record.to_dict()
        assert "payload" not in public

    def test_to_dict_includes_delivery_metadata(self):
        """Only delivery metadata is exposed."""
        record = DeliveryRecord(
            endpoint_id="ep-1",
            event_id="ev-1",
            payload={"task_id": "abc", "run_state": "done", "priority": 5},
        )
        public = record.to_dict()
        assert "id" in public
        assert "endpoint_id" in public
        assert "event_id" in public
        assert "status" in public
        assert "attempts" in public
        assert "created_at" in public


class TestWebhookManagerDeliver:
    """Integration tests for WebhookManager.deliver()."""

    def test_deliver_strips_sensitive_fields(self):
        """Events delivered to endpoints have sensitive fields removed."""
        manager = WebhookManager()
        endpoint = manager.register_endpoint(
            url="https://example.com/webhook",
            secret="signing-secret",
            events=["task.completed"],
        )
        records = manager.deliver(
            event={
                "task_id": "task-123",
                "run_state": "in_flight",
                "retries": 2,
                "enqueued_at": 1716212345.0,
                "target_agent": "agent-42",
                "priority": 7,
                "result": "success",
            },
            event_type="task.completed",
        )
        assert len(records) == 1
        record = records[0]
        # Sensitive fields must not be in the stored payload
        import json

        payload_json = json.dumps(record.payload)
        assert "run_state" not in payload_json
        assert "retries" not in payload_json
        assert "enqueued_at" not in payload_json
        assert "target_agent" not in payload_json
        assert "priority" not in payload_json
        # Public fields must be present
        assert "task_id" in payload_json
        assert "result" in payload_json

    def test_deliver_skips_disabled_endpoints(self):
        """Deliveries are not created for disabled endpoints."""
        manager = WebhookManager()
        endpoint = manager.register_endpoint(
            url="https://example.com/webhook",
            secret="secret",
            events=["task.completed"],
        )
        manager.disable_endpoint(endpoint.id)
        records = manager.deliver(
            event={"task_id": "abc", "priority": 5},
            event_type="task.completed",
        )
        assert len(records) == 0

    def test_deliver_respects_workspace_isolation(self):
        """Events are only delivered to endpoints in the same workspace."""
        manager = WebhookManager()
        ep_ws1 = manager.register_endpoint(
            url="https://example.com/ws1",
            secret="secret",
            events=["task.completed"],
            workspace_id="workspace-1",
        )
        manager.register_endpoint(
            url="https://example.com/ws2",
            secret="secret",
            events=["task.completed"],
            workspace_id="workspace-2",
        )
        # Deliver with workspace-1 scope — only ws1 endpoint should receive
        records = manager.deliver(
            event={"task_id": "abc", "priority": 5},
            event_type="task.completed",
            workspace_id="workspace-1",
        )
        assert len(records) == 1
        assert records[0].endpoint_id == ep_ws1.id