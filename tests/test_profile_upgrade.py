from __future__ import annotations

import os
from unittest import mock

from app import profile_migrate
from tests.test_profile_migrate import ProfileMigrationTest


class ProfileUpgradeTest(ProfileMigrationTest):
    def test_launcher_created_empty_target_is_replaced_atomically(self):
        os.makedirs(os.path.join(self.target, "data"))
        os.makedirs(os.path.join(self.target, "config"))
        result = profile_migrate.migrate_profile(self.legacy, self.target, log=lambda *_: None)
        self.assertEqual(result["counts"]["candidates"], 1)
        self.assertTrue(os.path.isfile(profile_migrate.profile_db(self.target)))

    def test_new_data_subdirectories_are_preserved(self):
        extra = os.path.join(self.legacy, "data", "exports")
        os.makedirs(extra)
        with open(os.path.join(extra, "report.csv"), "w", encoding="utf-8") as fh:
            fh.write("id,name\n1,A\n")
        profile_migrate.migrate_profile(self.legacy, self.target, log=lambda *_: None)
        with open(os.path.join(self.target, "data", "exports", "report.csv"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "id,name\n1,A\n")

    def test_failed_verification_does_not_publish_partial_profile(self):
        with mock.patch.object(profile_migrate, "_integrity_check", side_effect=[None, RuntimeError("bad copy")]):
            with self.assertRaisesRegex(RuntimeError, "bad copy"):
                profile_migrate.migrate_profile(self.legacy, self.target, log=lambda *_: None)
        self.assertFalse(os.path.exists(profile_migrate.profile_db(self.target)))
        self.assertEqual([], [n for n in os.listdir(os.path.dirname(self.target)) if ".migrating-" in n])
