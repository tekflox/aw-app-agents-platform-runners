#!/usr/bin/env python3
"""Unit tests for template_app/installer.py with subprocess mocked out — no
real filesystem writes/network involved, so this is safe to run in CI on a
plain GitHub-hosted runner (see aw-marketplace's app-release.yml, which runs
this before any version bump/tag/marketplace sync).

TEMPLATE: this is the pattern for every install_X() function your real app
adds — assert it invokes bash on the EXACT expected script path under
SCRIPTS_DIR (proves it's installing from the correct path in the repo), and
that any config-driven env var lands with the right value. See
aw-app-essentials/tests/test_installer.py for a bigger example (16 CLIs
across apt/binary-download/corepack/git-clone installers).

Run: .venv/aw/bin/python -m pytest tests/test_installer.py -q
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from template_app import installer  # noqa: E402


class HelloInstallerTest(unittest.TestCase):
    @patch("template_app.installer.subprocess.run")
    def test_install_hello_runs_script_at_the_correct_path_with_greeting_env(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="Hello, template!\n", stderr="")

        out = installer.install_hello("Hello")

        self.assertEqual(out, "Hello, template!")
        args, kwargs = mock_run.call_args
        self.assertEqual(args[0][-1], str(installer.SCRIPTS_DIR / "install_hello.sh"))
        self.assertEqual(kwargs["env"]["AW_APP_HELLO_GREETING"], "Hello")

    @patch("template_app.installer.subprocess.run")
    def test_install_hello_raises_on_failure(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="boom")

        with self.assertRaises(installer.InstallError):
            installer.install_hello("Hello")

    @patch("template_app.installer.subprocess.run")
    def test_uninstall_hello_runs_uninstall_script(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        installer.uninstall_hello()

        args, _ = mock_run.call_args
        self.assertEqual(args[0][-1], str(installer.SCRIPTS_DIR / "uninstall.sh"))


if __name__ == "__main__":
    unittest.main()
