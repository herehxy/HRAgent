from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest

from app import profile_migrate


class ProfileMigrationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = self.temp.name
        self.legacy = os.path.join(self.root, "old", "_internal")
        self.target = os.path.join(self.root, "new-profile")
        os.makedirs(os.path.join(self.legacy, "data", "archive"))
        os.makedirs(os.path.join(self.legacy, "data", "avatars"))
        os.makedirs(os.path.join(self.legacy, "config"))

        archive = os.path.join(self.legacy, "data", "archive", "resume.pdf")
        with open(archive, "wb") as fh:
            fh.write(b"%PDF-1.4 test")
        avatar = os.path.join(self.legacy, "data", "avatars", "1.jpg")
        with open(avatar, "wb") as fh:
            fh.write(b"jpeg")
        with open(os.path.join(self.legacy, "config", "master.key"), "w",
                  encoding="utf-8") as fh:
            fh.write("fixed-master-key")
        with open(os.path.join(self.legacy, "config", "model.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"model": "legacy-model", "enabled": True}, fh)
        with open(os.path.join(self.legacy, "config", "mailbox.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"mode": "imap", "imap": {"host": "mail.example"}}, fh)

        db_path = os.path.join(self.legacy, "data", "workbench.db")
        conn = sqlite3.connect(db_path)
        try:
            conn.executescript(
                """
                CREATE TABLE candidates(id INTEGER PRIMARY KEY, name TEXT,
                                        avatar_path TEXT);
                CREATE TABLE applications(id INTEGER PRIMARY KEY, candidate_id INTEGER);
                CREATE TABLE documents(id INTEGER PRIMARY KEY, file_path TEXT,
                                       archived_path TEXT);
                CREATE TABLE settings(k TEXT PRIMARY KEY, v TEXT);
                CREATE TABLE users(id INTEGER PRIMARY KEY, username TEXT);
                INSERT INTO candidates VALUES (1, 'A', 'data/avatars/1.jpg');
                INSERT INTO applications VALUES (1, 1);
                INSERT INTO documents VALUES (1, 'data/archive/resume.pdf',
                                              'data/archive/resume.pdf');
                INSERT INTO documents VALUES (2, 'absolute-source',
                                              'absolute-source');
                INSERT INTO settings VALUES ('theme', 'dark');
                INSERT INTO users VALUES (1, 'hr');
                """
            )
            legacy_archive = os.path.join(self.legacy, "data", "archive",
                                          "resume.pdf")
            conn.execute(
                "UPDATE documents SET file_path=?, archived_path=? WHERE id=2",
                (legacy_archive, legacy_archive),
            )
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        self.temp.cleanup()

    def test_migrate_is_copy_only_and_rewrites_absolute_paths(self):
        result = profile_migrate.migrate_profile(
            self.legacy, self.target, log=lambda *_args: None
        )
        self.assertTrue(result["old_data_retained"])
        self.assertTrue(os.path.isfile(
            os.path.join(self.legacy, "data", "workbench.db")
        ))
        self.assertEqual(result["counts"]["candidates"], 1)
        self.assertEqual(result["counts"]["documents"], 2)
        with open(os.path.join(self.legacy, "config", "master.key"), "rb") as fh:
            old_key = fh.read()
        with open(os.path.join(self.target, "config", "master.key"), "rb") as fh:
            new_key = fh.read()
        self.assertEqual(old_key, new_key)
        self.assertTrue(os.path.isfile(
            os.path.join(self.target, "data", "archive", "resume.pdf")
        ))
        conn = sqlite3.connect(profile_migrate.profile_db(self.target))
        try:
            relative_path = conn.execute(
                "SELECT archived_path FROM documents WHERE id=1"
            ).fetchone()[0]
            absolute_path = conn.execute(
                "SELECT archived_path FROM documents WHERE id=2"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(relative_path, "data/archive/resume.pdf")
        self.assertIn(os.path.join(self.target, "data").replace("\\", "/"),
                      absolute_path.replace("\\", "/"))

    def test_second_migration_refuses_to_overwrite(self):
        profile_migrate.migrate_profile(self.legacy, self.target,
                                        log=lambda *_args: None)
        with self.assertRaises(FileExistsError):
            profile_migrate.migrate_profile(self.legacy, self.target,
                                            log=lambda *_args: None)

    def test_seed_config_preserves_existing_values(self):
        template = os.path.join(self.root, "template")
        config = os.path.join(self.target, "config")
        os.makedirs(template)
        os.makedirs(config)
        with open(os.path.join(template, "model.json"), "w", encoding="utf-8") as fh:
            json.dump({"model": "new-default", "timeout": 60,
                       "extra": {"new": 1}}, fh)
        with open(os.path.join(config, "model.json"), "w", encoding="utf-8") as fh:
            json.dump({"model": "user-value", "extra": {"keep": 2}}, fh)
        with open(os.path.join(template, "master.key"), "w", encoding="utf-8") as fh:
            fh.write("must-not-copy")

        profile_migrate.seed_config(template, config, log=lambda *_args: None)
        with open(os.path.join(config, "model.json"), encoding="utf-8") as fh:
            merged = json.load(fh)
        self.assertEqual(merged["model"], "user-value")
        self.assertEqual(merged["timeout"], 60)
        self.assertEqual(merged["extra"], {"keep": 2, "new": 1})
        self.assertFalse(os.path.exists(os.path.join(config, "master.key")))


if __name__ == "__main__":
    unittest.main()
