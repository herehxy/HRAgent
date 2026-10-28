from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import sys
import unittest

from tests.test_profile_migrate import ProfileMigrationTest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "repo_src" / "app"))
spec = importlib.util.spec_from_file_location("setup_wizard", ROOT / "installer" / "setup_wizard.py")
setup_wizard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup_wizard)


class InstallerMigrationTest(ProfileMigrationTest):
    def setUp(self):
        super().setUp()
        self.old_root = Path(self.legacy).parent
        (self.old_root / setup_wizard.EXE_NAME).write_bytes(b"old-app")

    def test_internal_layout_removes_while_old_app_root(self):
        self.assertEqual(self.old_root, setup_wizard.legacy_program_root(self.legacy))

    def test_rejects_deleting_new_program_or_new_data_parent(self):
        with self.assertRaises(ValueError):
            setup_wizard.validate_legacy_removal(self.legacy, self.old_root, Path(self.target))
        with self.assertRaises(ValueError):
            setup_wizard.validate_legacy_removal(self.legacy, Path(self.target), self.old_root)

    def test_rejects_directory_without_old_executable(self):
        (self.old_root / setup_wizard.EXE_NAME).unlink()
        with self.assertRaises(ValueError):
            setup_wizard.legacy_program_root(self.legacy)
