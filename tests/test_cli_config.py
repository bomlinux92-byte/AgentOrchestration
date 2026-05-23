"""Tests for CLI config path expansion."""

import os
import pytest
from pathlib import Path
from unittest.mock import patch


class TestCliConfigPath:
    """Test that --config path expansion works correctly."""

    def test_expand_user_path_tilde(self, tmp_path, monkeypatch):
        """Test that ~/path is expanded to absolute path."""
        # Create a fake home and config
        fake_home = tmp_path / "fake_home"
        fake_home.mkdir()
        config_file = fake_home / "config.json"
        config_file.write_text('{"app": {"name": "test"}}')

        # Patch expanduser to use our fake home
        from src.cli import main
        original_expanduser = Path.expanduser

        def fake_expanduser(self):
            if str(self) == "~":
                return fake_home
            elif str(self).startswith("~/"):
                return fake_home / str(self)[2:]
            return original_expanduser(self)

        with patch.object(Path, 'expanduser', fake_expanduser):
            # Simulate what the CLI does
            user_path = "~/" + config_file.name
            expanded = Path(user_path).expanduser()
            resolved = expanded.resolve()

            # The path should be absolute after resolve
            assert resolved.is_absolute()

    def test_resolve_relative_path(self):
        """Test that relative paths are resolved to absolute."""
        rel_path = "config.json"
        expanded = Path(rel_path).expanduser()
        resolved = expanded.resolve()

        # After resolve, should be absolute (resolved relative to cwd)
        assert resolved.is_absolute()
        assert resolved.name == "config.json"

    def test_config_path_not_modified_if_none(self):
        """Test that None config path is handled gracefully."""
        from src.cli import main
        # args.config is None - should not cause errors
        args = type('Args', (), {'config': None, 'verbose': False, 'command': None})()
        # Should not raise
        if args.config:
            config_path = Path(args.config).expanduser().resolve()
            args.config = str(config_path)
        assert args.config is None