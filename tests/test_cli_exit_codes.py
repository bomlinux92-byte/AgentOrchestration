"""Regression tests for CLI exit-code propagation (issue #3453)."""

import os
import pytest

from src.cli.main import cli


class TestCLIExitCodes:
    def test_deploy_missing_manifest_returns_nonzero(self):
        with pytest.raises(SystemExit) as exc_info:
            cli(["deploy", "/no/such/manifest.yml"])
        assert exc_info.value.code == 1

    def test_deploy_success_returns_zero(self, tmp_path):
        manifest = tmp_path / "agent.yml"
        manifest.write_text("name: test-agent\n")
        config = tmp_path / "config.json"
        config.write_text('{"orchestrator": {"url": "http://localhost:9090"}}')
        with pytest.raises(SystemExit) as exc_info:
            cli(["-c", str(config), "deploy", str(manifest)])
        assert exc_info.value.code == 0

    def test_deploy_no_orchestrator_url_returns_nonzero(self, tmp_path):
        manifest = tmp_path / "agent.yml"
        manifest.write_text("name: test-agent\n")
        config = tmp_path / "config.json"
        config.write_text('{}')
        with pytest.raises(SystemExit) as exc_info:
            cli(["-c", str(config), "deploy", str(manifest)])
        assert exc_info.value.code == 1

    def test_no_command_returns_nonzero(self):
        with pytest.raises(SystemExit) as exc_info:
            cli([])
        assert exc_info.value.code == 1

    def test_logs_returns_zero(self):
        with pytest.raises(SystemExit) as exc_info:
            cli(["logs", "agent-42"])
        assert exc_info.value.code == 0

    def test_status_returns_zero(self):
        with pytest.raises(SystemExit) as exc_info:
            cli(["status"])
        assert exc_info.value.code == 0

    def test_init_returns_zero(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        with pytest.raises(SystemExit) as exc_info:
            cli(["init", "my-project"])
        assert exc_info.value.code == 0
        assert os.path.isdir(os.path.join(tmp_path, "my-project"))
