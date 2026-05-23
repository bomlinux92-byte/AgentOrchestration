"""Tests for dependency review exception validation."""

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "validate_dependency_exceptions.py"
spec = importlib.util.spec_from_file_location("validate_dependency_exceptions", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_empty_manifest_is_valid(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "dependency-review-exceptions.json").write_text('{"exceptions": []}')

    assert module.main() == 0
    assert "validated 0 dependency review exception(s)" in capsys.readouterr().out


def test_missing_owner_or_expired_exception_fails(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "dependency-review-exceptions.json").write_text(
        '{"exceptions": [{"package":"pkg","ecosystem":"pip","reason":"temp","expires":"2000-01-01","issue":"https://example.test/review"}]}'
    )

    assert module.main() == 1
    out = capsys.readouterr().out
    assert "missing required field(s): owner" in out
    assert "expired on 2000-01-01" in out


def test_valid_exception_passes(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "dependency-review-exceptions.json").write_text(
        '{"exceptions": [{"package":"acme","ecosystem":"pip","owner":"alice","reason":"temp workaround","expires":"2030-12-31","issue":"https://github.com/orchestration-agent/AgentOrchestration/issues/2958"}]}'
    )

    assert module.main() == 0
    assert "validated 1 dependency review exception(s)" in capsys.readouterr().out


def test_missing_expires_fails(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "dependency-review-exceptions.json").write_text(
        '{"exceptions": [{"package":"acme","ecosystem":"pip","owner":"alice","reason":"temp","issue":"https://github.com/orchestration-agent/AgentOrchestration/issues/2958"}]}'
    )

    assert module.main() == 1
    out = capsys.readouterr().out
    assert "missing required field(s): expires" in out


def test_missing_link_fails(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "dependency-review-exceptions.json").write_text(
        '{"exceptions": [{"package":"acme","ecosystem":"pip","owner":"alice","reason":"temp","expires":"2030-12-31"}]}'
    )

    assert module.main() == 1
    out = capsys.readouterr().out
    assert "missing review summary link" in out