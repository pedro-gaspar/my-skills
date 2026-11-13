"""Obsidian is a mirror: notes are written only by `sync-obsidian` (and for an agent or session that reads one)."""

import unittest
from pathlib import Path

from tests.fixtures import RepoFixture
from wonka import store, ticket
from wonka.machine import KILLED


class SyncObsidianTest(RepoFixture, unittest.TestCase):
    def sync(self):
        with self.f() as f:
            return f.sync_obsidian()

    def live_note(self, tid):
        return store.tickets_dir(self.cfg) / f"{tid}.md"

    def test_a_write_creates_no_note(self):
        tid = self.add()
        with self.f() as f:
            f.set_field(tid, "size", "s")
        self.assertTrue(ticket.exists(tid))
        self.assertFalse(self.live_note(tid).exists())

    def test_sync_writes_each_note_with_its_status(self):
        tid = self.add()
        lines = self.sync()
        self.assertIn("status: 📏 Triage", self.live_note(tid).read_text())
        self.assertEqual(lines[-1], "synced 1 notes to Obsidian (1 changed)")
        self.assertIn(f"{tid}: written to golden-tickets/{tid}.md", lines)

    def test_sync_is_idempotent(self):
        tid = self.add()
        self.sync()
        path = self.live_note(tid)
        before = path.stat().st_mtime_ns, path.read_bytes()
        self.assertEqual(self.sync(), ["synced 1 notes to Obsidian (0 changed)"])
        self.assertEqual((path.stat().st_mtime_ns, path.read_bytes()), before)

    def test_sync_follows_a_status_change(self):
        tid = self.add()
        self.sync()
        with self.f() as f:
            f.move(tid, KILLED, "human")
        self.sync()
        self.assertIn("status: 🗑️ Wont Do", self.live_note(tid).read_text())

    def test_sync_moves_the_stale_live_note_of_an_archived_ticket(self):
        tid = self.add()
        with self.f() as f:
            f.move(tid, KILLED, "human")
        self.sync()
        with self.f() as f:
            f.archive_all()
        stale = self.live_note(tid)
        stale.write_text("stale\n")
        lines = self.sync()
        archived = store.archive_dir(self.cfg) / "wont-do" / f"{tid}.md"
        self.assertFalse(stale.exists())
        self.assertIn("status: 🗑️ Wont Do", archived.read_text())
        self.assertIn(f"{tid}: moved and written to archive/wont-do/{tid}.md", lines)

    def test_sync_writes_an_archived_ticket_that_has_no_note(self):
        tid = self.add()
        with self.f() as f:
            f.move(tid, KILLED, "human")
            f.archive_all()
        self.sync()
        self.assertTrue((store.archive_dir(self.cfg) / "wont-do" / f"{tid}.md").exists())
        self.assertFalse(self.live_note(tid).exists())

    def test_archive_works_when_the_ticket_has_no_note(self):
        tid = self.add()
        with self.f() as f:
            f.move(tid, KILLED, "human")
            lines = f.archive_all()
        self.assertEqual(lines, [f"{tid}: archived to archive/wont-do"])
        self.assertFalse(self.live_note(tid).exists())

    def test_sync_records_the_time_and_bumps_the_board_version(self):
        self.add()
        version = ticket.tickets_dir() / ".version"
        before = version.read_text()
        self.sync()
        with self.f() as f:
            self.assertIn("obsidian_synced_at", f.db)
        self.assertNotEqual(version.read_text(), before)

    def test_a_tick_runs_without_the_vault(self):
        self.add()
        self.assertFalse(Path(self.cfg["vault"]).exists())
        self.tick()
