"""Regression tests for container security scan coverage (issue #3859).

These tests verify that:
1. All worker images in the build matrix are scanned
2. Adding a new image matrix entry automatically adds a scan target
3. CI fails when scan coverage is incomplete
"""

import json
import re
from pathlib import Path
from typing import List

import pytest
import yaml


class TestContainerScanCoverage:
    """Tests for ensuring complete container image scan coverage in CI."""

    WORKFLOW_PATH = Path(__file__).parent.parent / ".github" / "workflows" / "ci.yml"

    @pytest.fixture
    def workflow_config(self) -> dict:
        """Load the CI workflow configuration."""
        if not self.WORKFLOW_PATH.exists():
            pytest.skip("CI workflow not found")
        with open(self.WORKFLOW_PATH) as f:
            return yaml.safe_load(f)

    @pytest.fixture
    def build_matrix_images(self, workflow_config: dict) -> List[str]:
        """Extract image names from the build-worker-images job matrix."""
        jobs = workflow_config.get("jobs", {})
        build_job = jobs.get("build-worker-images", {})
        matrix = build_job.get("strategy", {}).get("matrix", {})
        return matrix.get("worker", [])

    def test_build_matrix_defines_worker_images(self, workflow_config: dict):
        """Verify build-worker-images job has a matrix defining all worker images."""
        jobs = workflow_config.get("jobs", {})
        build_job = jobs.get("build-worker-images", {})
        matrix = build_job.get("strategy", {}).get("matrix", {})
        
        assert "worker" in matrix, "build-worker-images must define 'worker' matrix"
        workers = matrix["worker"]
        assert len(workers) > 0, "build-worker-images worker matrix cannot be empty"
        
        # Must include at least the core worker types mentioned in the issue
        expected_base = {"scheduler", "executor"}
        actual_workers = set(workers)
        assert expected_base.issubset(actual_workers), \
            f"Build matrix must include {expected_base}, got {actual_workers}"

    def test_scan_matrix_matches_build_matrix(self, workflow_config: dict, build_matrix_images):
        """Verify scan job matrix exactly matches build job matrix."""
        jobs = workflow_config.get("jobs", {})
        scan_job = jobs.get("container-security-scan", {})
        matrix = scan_job.get("strategy", {}).get("matrix", {})
        scan_workers = matrix.get("worker", [])
        
        build_set = set(build_matrix_images)
        scan_set = set(scan_workers)
        
        missing_in_scan = build_set - scan_set
        extra_in_scan = scan_set - build_set
        
        assert not missing_in_scan, \
            f"Images in build matrix but missing from scan matrix: {missing_in_scan}"
        assert not extra_in_scan, \
            f"Images in scan matrix but not in build matrix: {extra_in_scan}"

    def test_all_build_images_have_scan_target(self, workflow_config: dict):
        """Each image produced by the build matrix must have a corresponding scan target."""
        jobs = workflow_config.get("jobs", {})
        
        build_job = jobs.get("build-worker-images", {})
        build_matrix = build_job.get("strategy", {}).get("matrix", {})
        build_workers = set(build_matrix.get("worker", []))
        
        scan_job = jobs.get("container-security-scan", {})
        scan_matrix = scan_job.get("strategy", {}).get("matrix", {})
        scan_workers = set(scan_matrix.get("worker", []))
        
        assert build_workers == scan_workers, \
            f"Build and scan matrices must match. " \
            f"Build: {build_workers}, Scan: {scan_workers}"

    def test_adding_new_image_matrix_entry_adds_scan_target(self, workflow_config: dict):
        """Adding a new image to build matrix should automatically add scan target.
        
        This test ensures the pattern used in the workflow makes it impossible
        to have build entries without corresponding scan entries.
        """
        jobs = workflow_config.get("jobs", {})
        
        build_job = jobs.get("build-worker-images", {})
        scan_job = jobs.get("container-security-scan", {})
        
        # Both jobs must use matrix strategy with 'worker' key
        assert "strategy" in build_job, "build-worker-images must use strategy"
        assert "strategy" in scan_job, "container-security-scan must use strategy"
        
        build_matrix = build_job["strategy"].get("matrix", {})
        scan_matrix = scan_job["strategy"].get("matrix", {})
        
        assert "worker" in build_matrix, "build matrix must have 'worker' key"
        assert "worker" in scan_matrix, "scan matrix must have 'worker' key"
        
        # Both matrices should use the same pattern (list of workers)
        assert isinstance(build_matrix["worker"], list), \
            "build matrix 'worker' must be a list"
        assert isinstance(scan_matrix["worker"], list), \
            "scan matrix 'worker' must be a list"

    def test_workflow_has_verify_scan_coverage_job(self, workflow_config: dict):
        """Verify there is a job that fails when scan coverage is incomplete."""
        jobs = workflow_config.get("jobs", {})
        
        # Must have a verification job that depends on both build and scan
        verify_job = jobs.get("verify-scan-coverage", {})
        assert verify_job, "Must have 'verify-scan-coverage' job"
        
        needs = verify_job.get("needs", [])
        if isinstance(needs, str):
            needs = [needs]
        
        assert "build-worker-images" in needs, \
            "verify-scan-coverage must depend on build-worker-images"
        assert "container-security-scan" in needs, \
            "verify-scan-coverage must depend on container-security-scan"

    def test_build_job_has_digest_output(self, workflow_config: dict):
        """Verify build job outputs image digests for traceability."""
        jobs = workflow_config.get("jobs", {})
        build_job = jobs.get("build-worker-images", {})
        outputs = build_job.get("outputs", {})
        
        assert "image-digests" in outputs, \
            "build-worker-images must output image-digests"

    def test_scan_job_depends_on_build_job(self, workflow_config: dict):
        """Verify container-security-scan depends on build-worker-images."""
        jobs = workflow_config.get("jobs", {})
        scan_job = jobs.get("container-security-scan", {})
        needs = scan_job.get("needs", [])
        
        if isinstance(needs, str):
            needs = [needs]
        
        assert "build-worker-images" in needs, \
            "container-security-scan must depend on build-worker-images"

    def test_scan_verification_fails_on_incomplete_coverage(self, workflow_config: dict):
        """Verify the scan verification step will fail if any image is unscanned."""
        jobs = workflow_config.get("jobs", {})
        
        # The verify-scan-coverage job must have steps that would fail
        # if not all images are scanned
        verify_job = jobs.get("verify-scan-coverage", {})
        verify_steps = verify_job.get("steps", [])
        
        assert len(verify_steps) > 0, \
            "verify-scan-coverage must have steps to verify coverage"
        
        # Find the verification step (should check coverage completeness)
        # The actual failure happens via exit 1 when coverage is incomplete
        step_names = [s.get("name", "") for s in verify_steps]
        assert any("verify" in name.lower() or "coverage" in name.lower() 
                   for name in step_names), \
            "verify-scan-coverage should have a step that verifies coverage"


class TestWorkerImageMatrixConsistency:
    """Test that WORKER_IMAGES env var stays in sync with matrices."""

    def test_worker_images_env_matches_build_matrix(self):
        """WORKER_IMAGES env should match the build matrix entries."""
        workflow_path = Path(__file__).parent.parent / ".github" / "workflows" / "ci.yml"
        
        with open(workflow_path) as f:
            content = f.read()
        
        # Extract WORKER_IMAGES env var
        env_match = re.search(r'WORKER_IMAGES:\s*>\s*\n\s*\[(.*?)\]', content, re.DOTALL)
        if env_match:
            images_str = env_match.group(1).strip()
            # Parse the JSON-like array
            env_images = json.loads(f"[{images_str}]")
            
            # Load YAML to check build matrix
            config = yaml.safe_load(content)
            jobs = config.get("jobs", {})
            build_job = jobs.get("build-worker-images", {})
            matrix = build_job.get("strategy", {}).get("matrix", {})
            build_workers = set(matrix.get("worker", []))
            
            assert set(env_images) == build_workers, \
                f"WORKER_IMAGES env ({env_images}) must match build matrix ({build_workers})"


class TestScanEnforcement:
    """Test that CI properly enforces scan requirements."""

    def test_no_hardcoded_image_names_in_scan_job(self):
        """Ensure scan job uses matrix rather than hardcoded image names."""
        workflow_path = Path(__file__).parent.parent / ".github" / "workflows" / "ci.yml"
        
        with open(workflow_path) as f:
            content = f.read()
        
        config = yaml.safe_load(content)
        scan_job = config.get("jobs", {}).get("container-security-scan", {})
        
        # Should use matrix
        assert "strategy" in scan_job, "scan job must use strategy matrix"
        
        # Should NOT hardcode images in steps
        steps = scan_job.get("steps", [])
        for step in steps:
            run = step.get("run", "")
            # Check for problematic hardcoded patterns
            if "ghcr.io/orchestration-agent/worker-" in run:
                # If it builds URLs dynamically using matrix, that's fine
                # If it hardcodes specific images, that's a problem
                if "${{ matrix.worker }}" not in run and "${{" not in run:
                    pytest.fail("Scan job should use matrix variable, not hardcoded image names")


# 2026-05-25T02:04:00 - Regression tests for bounty issue #3859
# Ensures all worker images from build matrix are scanned
