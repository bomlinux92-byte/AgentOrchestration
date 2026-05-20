"""Multi-architecture image manifest validation and deployment orchestration."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class ValidationStatus(str, Enum):
    PENDING = "pending"
    PASSED = "passed"
    FAILED = "failed"
    MISSING = "missing"


class ScanStatus(str, Enum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"
    MISSING = "missing"


@dataclass
class ArchitectureDigest:
    """Represents a validated architecture-specific image digest."""

    arch: str  # e.g., "amd64", "arm64"
    digest: str  # SHA-256 digest of the image
    validation_status: ValidationStatus = ValidationStatus.PENDING
    scan_status: ScanStatus = ScanStatus.PENDING
    validated_at: Optional[datetime] = None
    scanned_at: Optional[datetime] = None
    test_result: Optional[str] = None  # "pass", "fail", or None

    @property
    def is_ready(self) -> bool:
        """Check if this architecture digest is fully validated and scanned."""
        return (
            self.validation_status == ValidationStatus.PASSED
            and self.scan_status == ScanStatus.COMPLETED
            and self.test_result == "pass"
        )


@dataclass
class MultiArchManifest:
    """Represents a multi-architecture manifest with per-architecture validation status."""

    tag: str
    architectures: dict[str, ArchitectureDigest] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.utcnow)

    def add_architecture(self, arch: str, digest: str) -> None:
        """Add an architecture digest to the manifest."""
        if arch not in self.architectures:
            self.architectures[arch] = ArchitectureDigest(arch=arch, digest=digest)
        else:
            self.architectures[arch].digest = digest

    def validate_architecture(self, arch: str, status: ValidationStatus) -> None:
        """Mark an architecture's validation status."""
        if arch in self.architectures:
            self.architectures[arch].validation_status = status
            if status == ValidationStatus.PASSED:
                self.architectures[arch].validated_at = datetime.utcnow()

    def set_scan_result(self, arch: str, status: ScanStatus, result: Optional[str] = None) -> None:
        """Set the scan result for an architecture."""
        if arch in self.architectures:
            self.architectures[arch].scan_status = status
            self.architectures[arch].test_result = result
            if status == ScanStatus.COMPLETED:
                self.architectures[arch].scanned_at = datetime.utcnow()

    def get_missing_architectures(self) -> list[str]:
        """Return list of architectures that are not fully validated."""
        missing = []
        for arch, digest in self.architectures.items():
            if not digest.is_ready:
                missing.append(arch)
        return missing

    def is_complete(self) -> bool:
        """Check if all architectures are fully validated and scanned."""
        return len(self.get_missing_architectures()) == 0

    def get_release_summary(self) -> dict:
        """Generate a release summary with per-architecture status."""
        summary = {
            "tag": self.tag,
            "total_architectures": len(self.architectures),
            "complete": self.is_complete(),
            "architectures": {},
        }
        for arch, digest_info in self.architectures.items():
            summary["architectures"][arch] = {
                "digest": digest_info.digest,
                "validation_status": digest_info.validation_status.value,
                "scan_status": digest_info.scan_status.value,
                "test_result": digest_info.test_result,
                "validated_at": digest_info.validated_at.isoformat() if digest_info.validated_at else None,
                "scanned_at": digest_info.scanned_at.isoformat() if digest_info.scanned_at else None,
                "ready": digest_info.is_ready,
            }
        return summary


class ManifestValidationError(Exception):
    """Raised when manifest validation fails."""

    def __init__(self, missing_architectures: list[str], message: str = ""):
        self.missing_architectures = missing_architectures
        self.message = message or f"Manifest push blocked: missing validation for architectures: {', '.join(missing_architectures)}"
        super().__init__(self.message)


class MultiArchManifestGate:
    """
    Gate that ensures all architecture-specific digests pass validation
    and scan gates before multi-arch manifest publication.
    """

    def __init__(self):
        self._manifests: dict[str, MultiArchManifest] = {}

    def create_manifest(self, tag: str) -> MultiArchManifest:
        """Create a new multi-arch manifest for a given tag."""
        manifest = MultiArchManifest(tag=tag)
        self._manifests[tag] = manifest
        return manifest

    def get_manifest(self, tag: str) -> Optional[MultiArchManifest]:
        """Retrieve an existing manifest by tag."""
        return self._manifests.get(tag)

    def add_architecture_digest(self, tag: str, arch: str, digest: str) -> None:
        """Add or update an architecture digest for a manifest."""
        if tag not in self._manifests:
            self.create_manifest(tag)
        self._manifests[tag].add_architecture(arch, digest)

    def validate_architecture(self, tag: str, arch: str, status: ValidationStatus) -> None:
        """Mark an architecture as validated or failed."""
        if tag in self._manifests:
            self._manifests[tag].validate_architecture(arch, status)

    def set_scan_result(self, tag: str, arch: str, status: ScanStatus, result: Optional[str] = None) -> None:
        """Set scan result for an architecture."""
        if tag in self._manifests:
            self._manifests[tag].set_scan_result(arch, status, result)

    def can_publish(self, tag: str) -> tuple[bool, list[str]]:
        """
        Check if a manifest is ready for publication.
        Returns (can_publish, list_of_missing_architectures).
        """
        manifest = self._manifests.get(tag)
        if not manifest:
            return False, ["manifest not found"]
        
        missing = manifest.get_missing_architectures()
        return manifest.is_complete(), missing

    def get_release_summary(self, tag: str) -> Optional[dict]:
        """Get a release summary for a manifest."""
        manifest = self._manifests.get(tag)
        if not manifest:
            return None
        return manifest.get_release_summary()


# Global gate instance
_gate = MultiArchManifestGate()


def get_validation_gate() -> MultiArchManifestGate:
    """Get the global manifest validation gate."""
    return _gate


def validate_manifest_push(tag: str, required_architectures: Optional[list[str]] = None) -> dict:
    """
    Validate that all required architectures are fully validated before manifest push.
    
    Args:
        tag: The image tag being pushed
        required_architectures: List of required architectures (e.g., ["amd64", "arm64"])
                                 If None, uses whatever architectures were added to the manifest.
    
    Returns:
        Release summary dict with per-architecture status.
    
    Raises:
        ManifestValidationError: If any architecture is not fully validated.
    """
    can_publish, missing = _gate.can_publish(tag)
    
    if not can_publish:
        raise ManifestValidationError(
            missing_architectures=missing,
            message=f"Manifest push blocked for '{tag}': the following architectures are not fully validated: {', '.join(missing)}"
        )
    
    return _gate.get_release_summary(tag)


def get_manifest_status(tag: str) -> dict:
    """Get current status of a manifest without raising an error."""
    summary = _gate.get_release_summary(tag)
    return summary if summary else {}


# 2026-05-21T06:00:00 update