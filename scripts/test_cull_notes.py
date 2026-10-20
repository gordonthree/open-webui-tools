import datetime
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

import cull_notes as cn

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import agent_notes_full as mod  # noqa: E402


def iso(days_ago):
    return (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days_ago)).isoformat()


class CullTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = str(Path(self.tmp) / "n.sqlite3")
        mod.notes_db_connect(self.db).close()
        conn = sqlite3.connect(self.db)
        conn.execute("PRAGMA foreign_keys=ON;")
        now = iso(0)
        for pk, name in ((1, "autonomy-log"), (2, "other")):
            conn.execute("INSERT INTO note_id (note_pk, note_name, note_comment, created_at, updated_at, author_id, next_entry_no) VALUES (?,?,?,?,?,1,10)", (pk, name, "", now, now))
        rows = [(1, 1, 30, None), (1, 2, 10, None), (1, 3, 8, 3), (1, 4, 2, 3), (1, 5, 1, None), (2, 1, 40, None)]
        for pk, no, age, chain in rows:
            conn.execute("INSERT INTO note_data (note_pk, entry_no, note_text, created_at, author_id, chain_id) VALUES (?,?,?,?,1,?)", (pk, no, f"e{no}", iso(age), chain))
        conn.execute("INSERT INTO note_summary (note_pk, summary_text, last_summarized, source_updated_at, source_entries, author_id) VALUES (1, 's', ?, ?, 5, 1)", (now, now))
        conn.commit()
        conn.close()

    def entries(self, pk=1):
        conn = sqlite3.connect(self.db)
        try:
            return [r[0] for r in conn.execute("SELECT entry_no FROM note_data WHERE note_pk = ? ORDER BY entry_no", (pk,))]
        finally:
            conn.close()

    def test_dry_run_changes_nothing(self):
        self.assertEqual(cn.main(["--note", "autonomy-log", "--days", "7", "--db", self.db, "--dry-run"]), 0)
        self.assertEqual(self.entries(), [1, 2, 3, 4, 5])

    def test_culls_old_entries_keeps_a_chain_with_a_recent_piece_and_other_notes(self):
        self.assertEqual(cn.main(["--note", "AUTONOMY-LOG", "--days", "7", "--db", self.db, "--yes", "--no-backup"]), 0)
        self.assertEqual(self.entries(), [3, 4, 5])  # 3 is old but chained to recent 4
        self.assertEqual(self.entries(2), [1])

    def test_pinned_entries_are_never_culled(self):
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE note_data SET pinned = 1 WHERE note_pk = 1 AND entry_no = 1")
        conn.commit()
        conn.close()
        self.assertEqual(cn.main(["--note", "autonomy-log", "--days", "7", "--db", self.db, "--yes", "--no-backup"]), 0)
        self.assertEqual(self.entries(), [1, 3, 4, 5])

    def test_a_database_without_the_pinned_column_still_culls(self):
        conn = sqlite3.connect(self.db)
        conn.execute("ALTER TABLE note_data DROP COLUMN pinned")
        conn.commit()
        conn.close()
        self.assertEqual(cn.main(["--note", "autonomy-log", "--days", "7", "--db", self.db, "--yes", "--no-backup"]), 0)
        self.assertEqual(self.entries(), [3, 4, 5])

    def test_summary_cleared_only_on_request(self):
        cn.main(["--note", "autonomy-log", "--days", "7", "--db", self.db, "--yes", "--no-backup"])
        conn = sqlite3.connect(self.db)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM note_summary").fetchone()[0], 1)
        conn.close()
        cn.main(["--note", "autonomy-log", "--days", "1.5", "--clear-summary", "--db", self.db, "--yes", "--no-backup"])
        conn = sqlite3.connect(self.db)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM note_summary").fetchone()[0], 0)
        conn.close()

    def test_unknown_note_is_refused_before_any_change(self):
        self.assertEqual(cn.main(["--note", "autonomy-log,nope", "--days", "7", "--db", self.db, "--yes", "--no-backup"]), 2)
        self.assertEqual(self.entries(), [1, 2, 3, 4, 5])

    def test_backup_is_made(self):
        cn.BACKUP_DIR = Path(self.tmp) / "bk"
        cn.main(["--note", "autonomy-log", "--days", "7", "--db", self.db, "--yes"])
        self.assertEqual(len(list(cn.BACKUP_DIR.glob("*.sqlite3"))), 1)


if __name__ == "__main__":
    unittest.main()
