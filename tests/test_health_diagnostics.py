"""Tests for health endpoint and diagnostics API redaction."""

import pytest
from fastapi.testclient import TestClient

from src.api.server import create_app


class TestHealthEndpoint:
    """Test suite for public health endpoint (unauthenticated /health)."""

    def setup_method(self):
        self.app = create_app()
        self.client = TestClient(self.app)

    def test_health_returns_200(self):
        """Health endpoint should return 200."""
        response = self.client.get("/health")
        assert response.status_code == 200

    def test_health_returns_status_healthy(self):
        """Health endpoint should return status healthy."""
        response = self.client.get("/health")
        assert response.json()["status"] == "healthy"

    def test_health_excludes_execution_metadata_by_default(self):
        """Public health response should not include execution metadata."""
        response = self.client.get("/health")
        data = response.json()
        assert "diagnostics" not in data
        assert "execution_id" not in data

    def test_health_with_execution_metadata_includes_redacted_data(self):
        """Health with include_execution_metadata should return redacted data."""
        response = self.client.get("/health?include_execution_metadata=true")
        data = response.json()
        assert "diagnostics" in data
        # Verify sensitive fields are redacted
        assert data["diagnostics"].get("execution_id") == "[REDACTED]"
        assert data["diagnostics"].get("agent_id") == "[REDACTED]"


class TestRedactExecutionMetadata:
    """Test suite for execution metadata redaction."""

    def test_redact_dict_with_sensitive_keys(self):
        """Dictionaries with sensitive keys should be redacted."""
        from src.api.routes import redact_execution_metadata
        data = {
            "execution_id": "exec-123",
            "agent_id": "agent-456",
            "status": "running"
        }
        result = redact_execution_metadata(data)
        assert result["execution_id"] == "[REDACTED]"
        assert result["agent_id"] == "[REDACTED]"
        assert result["status"] == "running"

    def test_redact_nested_structures(self):
        """Nested dicts and lists should be recursively redacted."""
        from src.api.routes import redact_execution_metadata
        data = {
            "outer": {
                "execution_id": "exec-789",
                "nested_list": [
                    {"task_id": "task-1"},
                    {"trace_id": "trace-abc"}
                ]
            }
        }
        result = redact_execution_metadata(data)
        assert result["outer"]["execution_id"] == "[REDACTED]"
        assert result["outer"]["nested_list"][0]["task_id"] == "[REDACTED]"
        assert result["outer"]["nested_list"][1]["trace_id"] == "[REDACTED]"

    def test_redact_token_like_values(self):
        """Values with token-like names should be redacted."""
        from src.api.routes import redact_execution_metadata
        data = {
            "access_token": "secret-value",
            "api_key": "key-value",
            "my_secret": "hidden"
        }
        result = redact_execution_metadata(data)
        assert result["access_token"] == "[REDACTED]"
        assert result["api_key"] == "[REDACTED]"
        assert result["my_secret"] == "[REDACTED]"

    def test_redact_preserves_non_sensitive_values(self):
        """Non-sensitive values should be preserved."""
        from src.api.routes import redact_execution_metadata
        data = {
            "status": "running",
            "name": "test-agent",
            "version": "1.0.0"
        }
        result = redact_execution_metadata(data)
        assert result["status"] == "running"
        assert result["name"] == "test-agent"
        assert result["version"] == "1.0.0"

    def test_redact_empty_dict(self):
        """Empty dict should return empty dict."""
        from src.api.routes import redact_execution_metadata
        result = redact_execution_metadata({})
        assert result == {}

    def test_redact_list_of_strings(self):
        """List of strings should be recursively processed."""
        from src.api.routes import redact_execution_metadata
        data = [
            {"execution_id": "exec-1"},
            {"execution_id": "exec-2"}
        ]
        result = redact_execution_metadata(data)
        assert result[0]["execution_id"] == "[REDACTED]"
        assert result[1]["execution_id"] == "[REDACTED]"


class TestDiagnosticsEndpoint:
    """Test suite for diagnostics API endpoint (authenticated /api/v2 routes)."""

    def setup_method(self):
        self.app = create_app()
        self.client = TestClient(self.app)
        # Auth header for /api/v2 routes
        self.auth_header = {"Authorization": "Bearer test-token"}

    def test_diagnostics_returns_200(self):
        """Diagnostics endpoint should return 200 with auth."""
        response = self.client.get("/api/v2/diagnostics", headers=self.auth_header)
        assert response.status_code == 200

    def test_diagnostics_with_invalid_agent_id_returns_400(self):
        """Malformed agent_id should return 400."""
        response = self.client.get(
            "/api/v2/diagnostics?agent_id=",
            headers=self.auth_header
        )
        assert response.status_code == 400
        assert "Invalid agent_id" in response.json()["detail"]

    def test_diagnostics_with_unauthorized_execution_returns_401(self):
        """Unauthorized execution access should return 401."""
        response = self.client.get(
            "/api/v2/diagnostics?execution_id=unauthorized-exec-123",
            headers=self.auth_header
        )
        assert response.status_code == 401
        assert "Unauthorized" in response.json()["detail"]

    def test_diagnostics_health_excludes_execution_metadata(self):
        """Diagnostics health section should not leak execution metadata."""
        response = self.client.get("/api/v2/diagnostics", headers=self.auth_header)
        data = response.json()
        assert "health" in data
        assert "execution_id" not in str(data)

    def test_diagnostics_rejects_missing_auth(self):
        """Diagnostics endpoint should reject requests without auth."""
        response = self.client.get("/api/v2/diagnostics")
        assert response.status_code == 401
