"""Tests for SDK decorators."""

import pytest
from src.sdk.decorators import agent, _validate_version
from src.common.errors import VersionError


class TestVersionValidation:
    """Tests for version string validation."""

    def test_valid_simple_version(self):
        """Test valid simple semver versions."""
        valid_versions = ["0.0.0", "1.0.0", "2.3.4", "10.20.30", "999.999.999"]
        for version in valid_versions:
            _validate_version(version)  # Should not raise

    def test_valid_version_with_pre_release(self):
        """Test valid semver with pre-release identifiers."""
        valid_versions = [
            "1.0.0-alpha",
            "1.0.0-alpha.1",
            "1.0.0-0.3.7",
            "1.0.0-x.7.z.92",
            "1.0.0-alpha.beta",
            "1.0.0-beta.2",
            "1.0.0-beta.11",
            "1.0.0-rc.1",
        ]
        for version in valid_versions:
            _validate_version(version)  # Should not raise

    def test_valid_version_with_build_metadata(self):
        """Test valid semver with build metadata."""
        valid_versions = [
            "1.0.0+build",
            "1.0.0+build.1",
            "1.0.0+001",
            "1.0.0+beta.11.exp.aa.6",
        ]
        for version in valid_versions:
            _validate_version(version)  # Should not raise

    def test_valid_full_version(self):
        """Test valid semver with pre-release and build metadata."""
        valid_versions = [
            "1.0.0-alpha+build",
            "1.0.0-alpha.1+build.1",
            "1.0.0-beta.2+build.2",
        ]
        for version in valid_versions:
            _validate_version(version)  # Should not raise

    def test_invalid_version_no_periods(self):
        """Test invalid version with no periods."""
        with pytest.raises(VersionError):
            _validate_version("invalid")

    def test_invalid_version_too_few_components(self):
        """Test invalid version with too few components."""
        with pytest.raises(VersionError):
            _validate_version("1.0")
        with pytest.raises(VersionError):
            _validate_version("1")

    def test_invalid_version_too_many_components(self):
        """Test invalid version with too many components."""
        with pytest.raises(VersionError):
            _validate_version("1.0.0.0")

    def test_invalid_version_leading_zeros(self):
        """Test invalid version with leading zeros in major/minor/patch."""
        with pytest.raises(VersionError):
            _validate_version("01.0.0")
        with pytest.raises(VersionError):
            _validate_version("1.01.0")
        with pytest.raises(VersionError):
            _validate_version("1.0.01")

    def test_invalid_version_negative_numbers(self):
        """Test invalid version with negative numbers."""
        with pytest.raises(VersionError):
            _validate_version("-1.0.0")
        with pytest.raises(VersionError):
            _validate_version("1.-1.0")
        with pytest.raises(VersionError):
            _validate_version("1.0.-1")

    def test_empty_version(self):
        """Test empty version string."""
        with pytest.raises(VersionError):
            _validate_version("")

    def test_invalid_version_special_characters(self):
        """Test invalid version with special characters."""
        with pytest.raises(VersionError):
            _validate_version("1.0.0!")
        with pytest.raises(VersionError):
            _validate_version("1.0.0#")
        with pytest.raises(VersionError):
            _validate_version("v1.0.0")


class TestAgentDecorator:
    """Tests for the agent decorator with version validation."""

    def test_agent_decorator_valid_version(self):
        """Test agent decorator accepts valid version."""
        @agent(name="test-agent", version="1.0.0", description="Test agent")
        class TestAgent:
            pass

        assert TestAgent.__agent_config__["name"] == "test-agent"
        assert TestAgent.__agent_config__["version"] == "1.0.0"
        assert TestAgent.__agent_config__["description"] == "Test agent"

    def test_agent_decorator_valid_pre_release_version(self):
        """Test agent decorator accepts valid pre-release version."""
        @agent(name="test-agent", version="2.0.0-beta.1", description="Beta agent")
        class TestAgent:
            pass

        assert TestAgent.__agent_config__["version"] == "2.0.0-beta.1"

    def test_agent_decorator_default_version(self):
        """Test agent decorator uses default version 1.0.0."""
        @agent(name="test-agent")
        class TestAgent:
            pass

        assert TestAgent.__agent_config__["version"] == "1.0.0"

    def test_agent_decorator_invalid_version(self):
        """Test agent decorator raises VersionError for invalid version."""
        with pytest.raises(VersionError):
            @agent(name="test-agent", version="invalid", description="Bad version")
            class TestAgent:
                pass

    def test_agent_decorator_invalid_version_too_many_parts(self):
        """Test agent decorator raises VersionError for version with too many parts."""
        with pytest.raises(VersionError):
            @agent(name="test-agent", version="1.0.0.0", description="Bad version")
            class TestAgent:
                pass

    def test_agent_decorator_invalid_version_leading_zero(self):
        """Test agent decorator raises VersionError for version with leading zeros."""
        with pytest.raises(VersionError):
            @agent(name="test-agent", version="01.0.0", description="Bad version")
            class TestAgent:
                pass

    def test_agent_decorator_multiple_agents_same_module(self):
        """Test that multiple agents with valid versions work correctly."""
        @agent(name="agent-one", version="1.0.0")
        class AgentOne:
            pass

        @agent(name="agent-two", version="2.5.0")
        class AgentTwo:
            pass

        assert AgentOne.__agent_config__["name"] == "agent-one"
        assert AgentOne.__agent_config__["version"] == "1.0.0"
        assert AgentTwo.__agent_config__["name"] == "agent-two"
        assert AgentTwo.__agent_config__["version"] == "2.5.0"

    def test_agent_decorator_one_invalid_one_valid(self):
        """Test that a valid agent can be defined alongside a failed invalid one attempt."""
        # This test validates that validation happens at decoration time
        # First we define a valid agent
        @agent(name="valid-agent", version="1.0.0")
        class ValidAgent:
            pass

        assert ValidAgent.__agent_config__["version"] == "1.0.0"

        # Then verify an invalid version raises
        with pytest.raises(VersionError):
            @agent(name="invalid-agent", version="bad-version")
            class InvalidAgent:
                pass

# 2026-05-25T22:04:00 update
