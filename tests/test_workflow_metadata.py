"""Tests for workflow metadata validation — issue #1285."""

import pytest
import sys
sys.path.insert(0, "src")

from sdk.decorators import (
    validate_metadata_key,
    MetadataValidationError,
    RESERVED_METADATA_KEYS,
)


class TestWorkflowMetadataValidation:
    """Regression tests for issue #1285: reject reserved metadata keys."""

    def test_reserved_keys_defined(self):
        """Verify reserved keys are defined."""
        assert len(RESERVED_METADATA_KEYS) > 0
        assert "id" in RESERVED_METADATA_KEYS
        assert "name" in RESERVED_METADATA_KEYS
        assert "status" in RESERVED_METADATA_KEYS
        assert "version" in RESERVED_METADATA_KEYS
        print("PASS: reserved keys defined")

    def test_validate_metadata_key_accepts_valid(self):
        """Valid non-reserved keys should pass validation."""
        valid_keys = ["my_key", "customData", "user_field", "task_config"]
        for key in valid_keys:
            validate_metadata_key(key)  # Should not raise
        print("PASS: valid keys accepted")

    def test_validate_metadata_key_rejects_reserved(self):
        """Reserved keys should raise MetadataValidationError."""
        for key in RESERVED_METADATA_KEYS:
            with pytest.raises(MetadataValidationError) as exc_info:
                validate_metadata_key(key)
            assert key in str(exc_info.value)
        print("PASS: reserved keys rejected")

    def test_validate_metadata_key_rejects_id(self):
        """The 'id' key should be rejected."""
        with pytest.raises(MetadataValidationError) as exc_info:
            validate_metadata_key("id")
        assert "id" in str(exc_info.value)
        print("PASS: 'id' key rejected")

    def test_validate_metadata_key_rejects_name(self):
        """The 'name' key should be rejected."""
        with pytest.raises(MetadataValidationError) as exc_info:
            validate_metadata_key("name")
        assert "name" in str(exc_info.value)
        print("PASS: 'name' key rejected")

    def test_validate_metadata_key_rejects_status(self):
        """The 'status' key should be rejected."""
        with pytest.raises(MetadataValidationError) as exc_info:
            validate_metadata_key("status")
        assert "status" in str(exc_info.value)
        print("PASS: 'status' key rejected")

    def test_validate_metadata_key_rejects_type(self):
        """The 'type' key should be rejected."""
        with pytest.raises(MetadataValidationError) as exc_info:
            validate_metadata_key("type")
        assert "type" in str(exc_info.value)
        print("PASS: 'type' key rejected")

    def test_validate_metadata_key_rejects_version(self):
        """The 'version' key should be rejected."""
        with pytest.raises(MetadataValidationError) as exc_info:
            validate_metadata_key("version")
        assert "version" in str(exc_info.value)
        print("PASS: 'version' key rejected")

    def test_validate_metadata_key_rejects_created_at(self):
        """The 'created_at' key should be rejected."""
        with pytest.raises(MetadataValidationError) as exc_info:
            validate_metadata_key("created_at")
        assert "created_at" in str(exc_info.value)
        print("PASS: 'created_at' key rejected")

    def test_validate_metadata_key_rejects_updated_at(self):
        """The 'updated_at' key should be rejected."""
        with pytest.raises(MetadataValidationError) as exc_info:
            validate_metadata_key("updated_at")
        assert "updated_at" in str(exc_info.value)
        print("PASS: 'updated_at' key rejected")

    def test_validate_metadata_key_with_context(self):
        """Validation error should include context information."""
        with pytest.raises(MetadataValidationError) as exc_info:
            validate_metadata_key("id", context="step metadata")
        assert "step metadata" in str(exc_info.value)
        assert "Reserved" in str(exc_info.value)
        print("PASS: context included in error")

    def test_non_reserved_keys_accepted_in_workflow_context(self):
        """Non-reserved keys should be accepted without error."""
        valid_keys = ["custom_field", "my_data", "user_metadata"]
        for key in valid_keys:
            # Should not raise
            validate_metadata_key(key, context="workflow")
            validate_metadata_key(key, context="step")
        print("PASS: non-reserved keys accepted")


if __name__ == "__main__":
    test = TestWorkflowMetadataValidation()
    test.test_reserved_keys_defined()
    test.test_validate_metadata_key_accepts_valid()
    test.test_validate_metadata_key_rejects_reserved()
    test.test_validate_metadata_key_rejects_id()
    test.test_validate_metadata_key_rejects_name()
    test.test_validate_metadata_key_rejects_status()
    test.test_validate_metadata_key_rejects_type()
    test.test_validate_metadata_key_rejects_version()
    test.test_validate_metadata_key_rejects_created_at()
    test.test_validate_metadata_key_rejects_updated_at()
    test.test_validate_metadata_key_with_context()
    test.test_non_reserved_keys_accepted_in_workflow_context()
    print("\nAll tests passed!")
