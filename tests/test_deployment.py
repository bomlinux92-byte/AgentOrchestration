"""Tests for multi-arch manifest validation gate."""

import pytest
from datetime import datetime

from src.orchestrator.deployment import (
    MultiArchManifestGate,
    MultiArchManifest,
    ArchitectureDigest,
    ValidationStatus,
    ScanStatus,
    ManifestValidationError,
    validate_manifest_push,
    get_manifest_status,
    get_validation_gate,
)


class TestArchitectureDigest:
    """Tests for ArchitectureDigest dataclass."""

    def test_arch_digest_default_state(self):
        """Default state should be PENDING/PENDING with no test result."""
        digest = ArchitectureDigest(arch="amd64", digest="sha256:abc123")
        assert digest.arch == "amd64"
        assert digest.digest == "sha256:abc123"
        assert digest.validation_status == ValidationStatus.PENDING
        assert digest.scan_status == ScanStatus.PENDING
        assert digest.test_result is None
        assert digest.is_ready is False

    def test_arch_digest_full_validation(self):
        """Full validation requires PASSED validation, COMPLETED scan, and pass test."""
        digest = ArchitectureDigest(arch="amd64", digest="sha256:abc123")
        digest.validation_status = ValidationStatus.PASSED
        digest.scan_status = ScanStatus.COMPLETED
        digest.test_result = "pass"
        digest.validated_at = datetime.utcnow()
        digest.scanned_at = datetime.utcnow()
        assert digest.is_ready is True

    def test_arch_digest_partial_not_ready(self):
        """Partial validation should not be ready."""
        digest = ArchitectureDigest(arch="amd64", digest="sha256:abc123")
        digest.validation_status = ValidationStatus.PASSED
        # Missing scan status and test result
        assert digest.is_ready is False


class TestMultiArchManifest:
    """Tests for MultiArchManifest."""

    def test_add_architecture(self):
        """Can add architecture digests to manifest."""
        manifest = MultiArchManifest(tag="v1.0.0")
        manifest.add_architecture("amd64", "sha256:amd64digest")
        manifest.add_architecture("arm64", "sha256:arm64digest")
        
        assert "amd64" in manifest.architectures
        assert "arm64" in manifest.architectures
        assert manifest.architectures["amd64"].digest == "sha256:amd64digest"

    def test_validate_architecture(self):
        """Can mark architecture as validated."""
        manifest = MultiArchManifest(tag="v1.0.0")
        manifest.add_architecture("amd64", "sha256:amd64digest")
        manifest.validate_architecture("amd64", ValidationStatus.PASSED)
        
        assert manifest.architectures["amd64"].validation_status == ValidationStatus.PASSED
        assert manifest.architectures["amd64"].validated_at is not None

    def test_set_scan_result(self):
        """Can set scan result for architecture."""
        manifest = MultiArchManifest(tag="v1.0.0")
        manifest.add_architecture("amd64", "sha256:amd64digest")
        manifest.set_scan_result("amd64", ScanStatus.COMPLETED, "pass")
        
        assert manifest.architectures["amd64"].scan_status == ScanStatus.COMPLETED
        assert manifest.architectures["amd64"].test_result == "pass"
        assert manifest.architectures["amd64"].scanned_at is not None

    def test_get_missing_architectures(self):
        """get_missing_architectures returns arches not fully validated."""
        manifest = MultiArchManifest(tag="v1.0.0")
        manifest.add_architecture("amd64", "sha256:amd64digest")
        manifest.add_architecture("arm64", "sha256:arm64digest")
        
        # Neither is ready
        missing = manifest.get_missing_architectures()
        assert "amd64" in missing
        assert "arm64" in missing
        
        # Mark one as ready
        manifest.architectures["amd64"].validation_status = ValidationStatus.PASSED
        manifest.architectures["amd64"].scan_status = ScanStatus.COMPLETED
        manifest.architectures["amd64"].test_result = "pass"
        
        missing = manifest.get_missing_architectures()
        assert "amd64" not in missing
        assert "arm64" in missing

    def test_is_complete(self):
        """is_complete is True only when all archs are ready."""
        manifest = MultiArchManifest(tag="v1.0.0")
        manifest.add_architecture("amd64", "sha256:amd64digest")
        
        assert manifest.is_complete() is False
        
        manifest.architectures["amd64"].validation_status = ValidationStatus.PASSED
        manifest.architectures["amd64"].scan_status = ScanStatus.COMPLETED
        manifest.architectures["amd64"].test_result = "pass"
        
        assert manifest.is_complete() is True

    def test_get_release_summary(self):
        """Release summary contains per-architecture status."""
        manifest = MultiArchManifest(tag="v1.0.0")
        manifest.add_architecture("amd64", "sha256:amd64digest")
        manifest.validate_architecture("amd64", ValidationStatus.PASSED)
        manifest.set_scan_result("amd64", ScanStatus.COMPLETED, "pass")
        
        summary = manifest.get_release_summary()
        
        assert summary["tag"] == "v1.0.0"
        assert summary["total_architectures"] == 1
        assert summary["complete"] is True
        assert summary["architectures"]["amd64"]["digest"] == "sha256:amd64digest"
        assert summary["architectures"]["amd64"]["validation_status"] == "passed"
        assert summary["architectures"]["amd64"]["scan_status"] == "completed"
        assert summary["architectures"]["amd64"]["test_result"] == "pass"


class TestMultiArchManifestGate:
    """Tests for MultiArchManifestGate."""

    def test_create_and_get_manifest(self):
        """Can create and retrieve manifests."""
        gate = MultiArchManifestGate()
        manifest = gate.create_manifest("v1.0.0")
        
        assert manifest.tag == "v1.0.0"
        assert gate.get_manifest("v1.0.0") is manifest

    def test_add_architecture_digest(self):
        """Can add arch digest via gate."""
        gate = MultiArchManifestGate()
        gate.create_manifest("v1.0.0")
        gate.add_architecture_digest("v1.0.0", "amd64", "sha256:amd64digest")
        
        manifest = gate.get_manifest("v1.0.0")
        assert manifest is not None
        assert "amd64" in manifest.architectures

    def test_can_publish_incomplete(self):
        """can_publish returns False for incomplete manifests."""
        gate = MultiArchManifestGate()
        gate.create_manifest("v1.0.0")
        can_pub, missing = gate.can_publish("v1.0.0")
        
        assert can_pub is False
        assert "amd64" in missing  # manifest was created but no arch added

    def test_can_publish_complete(self):
        """can_publish returns True for fully validated manifests."""
        gate = MultiArchManifestGate()
        gate.create_manifest("v1.0.0")
        gate.add_architecture_digest("v1.0.0", "amd64", "sha256:amd64digest")
        gate.validate_architecture("v1.0.0", "amd64", ValidationStatus.PASSED)
        gate.set_scan_result("v1.0.0", "amd64", ScanStatus.COMPLETED, "pass")
        
        can_pub, missing = gate.can_publish("v1.0.0")
        
        assert can_pub is True
        assert missing == []

    def test_can_publish_unknown_manifest(self):
        """can_publish returns False for unknown manifests."""
        gate = MultiArchManifestGate()
        can_pub, missing = gate.can_publish("unknown")
        
        assert can_pub is False
        assert "manifest not found" in missing


class TestValidateManifestPush:
    """Tests for validate_manifest_push function."""

    def test_validate_success(self):
        """validate_manifest_push returns summary on success."""
        gate = get_validation_gate()
        gate.create_manifest("v1.0.0")
        gate.add_architecture_digest("v1.0.0", "amd64", "sha256:amd64digest")
        gate.validate_architecture("v1.0.0", "amd64", ValidationStatus.PASSED)
        gate.set_scan_result("v1.0.0", "amd64", ScanStatus.COMPLETED, "pass")
        
        summary = validate_manifest_push("v1.0.0")
        
        assert summary["tag"] == "v1.0.0"
        assert summary["complete"] is True

    def test_validate_raises_on_incomplete(self):
        """validate_manifest_push raises ManifestValidationError when incomplete."""
        gate = get_validation_gate()
        gate.create_manifest("v1.0.0")
        gate.add_architecture_digest("v1.0.0", "amd64", "sha256:amd64digest")
        # Not fully validated
        
        with pytest.raises(ManifestValidationError) as exc_info:
            validate_manifest_push("v1.0.0")
        
        assert "amd64" in exc_info.value.missing_architectures

    def test_get_manifest_status_unknown(self):
        """get_manifest_status returns empty dict for unknown manifests."""
        status = get_manifest_status("nonexistent")
        assert status == {}


class TestManifestValidationError:
    """Tests for ManifestValidationError."""

    def test_error_message(self):
        """Error message includes missing architectures."""
        error = ManifestValidationError(
            missing_architectures=["amd64", "arm64"],
            message="Test error"
        )
        
        assert "amd64" in error.message
        assert "arm64" in error.message
        assert error.missing_architectures == ["amd64", "arm64"]

    def test_default_message(self):
        """Default message is generated from missing architectures."""
        error = ManifestValidationError(missing_architectures=["amd64"])
        
        assert "amd64" in error.message


# 2026-05-21T06:00:00 update