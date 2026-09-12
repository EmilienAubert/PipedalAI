from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from pipedal_ai.rtx.diagnostics import MAX_RECORDS, save_exchange


class DiagnosticsTests(unittest.TestCase):
    def test_retention_only_removes_owned_trace_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            unrelated = root / "ollama-user-notes.json"
            unrelated.write_text("user notes", encoding="utf-8")
            for _ in range(MAX_RECORDS + 4):
                self.assertIsNotNone(save_exchange(root, "plan", 1, {"model": "test"}, "{}", 200, None))
            traces = [p for p in root.glob("ollama-*.json") if p != unrelated]
            self.assertLessEqual(len(traces), MAX_RECORDS)
            self.assertEqual(unrelated.read_text(encoding="utf-8"), "user notes")
            self.assertTrue(all(json.loads(p.read_text(encoding="utf-8"))["stage"] == "plan" for p in traces))

    def test_disabled_or_unwritable_diagnostics_do_not_break_generation(self):
        self.assertIsNone(save_exchange(None, "plan", 1, {}, "{}", 200, None))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "not-a-directory"
            path.write_text("fixture", encoding="utf-8")
            self.assertIsNone(save_exchange(path, "plan", 1, {}, "{}", 200, None))
