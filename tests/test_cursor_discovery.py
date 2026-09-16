"""Cursor discovery regressions; framework imports are stubbed for standalone tests."""

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch


framework = types.ModuleType("subjective_abstract_data_source_package")
framework.SubjectiveDataSource = object
logger = types.ModuleType("brainboost_data_source_logger_package.BBLogger")
logger.BBLogger = types.SimpleNamespace(log=lambda *args: None)
spec = importlib.util.spec_from_file_location(
    "localchats_under_test",
    Path(__file__).resolve().parents[1] / "SubjectiveLocalchatsDataSource.py",
)
module = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {
    "subjective_abstract_data_source_package": framework,
    "brainboost_data_source_logger_package.BBLogger": logger,
}):
    spec.loader.exec_module(module)
DataSource = module.SubjectiveLocalchatsDataSource


class CursorDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.homes = self.root / "home"
        self.current = self.homes / "current"
        self.gordon = self.homes / "gordon"
        self.current.mkdir(parents=True)
        self.gordon.mkdir()
        self.ds = DataSource.__new__(DataSource)
        self.ds.include_cursor = True
        self.ds.include_codex = False
        self.ds.include_claude = False
        self.ds.include_gemini = False
        self.ds.max_messages_per_chat = 4000
        self.ds._current_home_dir = lambda: str(self.current)
        self.ds._common_user_roots = lambda: [str(self.homes)]
        self.warnings = []
        self.ds._active_user_homes = self.ds._resolve_user_home_dirs([], True, self.warnings)

    def transcript(self, home, relative):
        path = home / ".cursor" / "projects" / "project" / "agent-transcripts" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        messages = [
            {"role": "user", "message": {"content": [{"type": "text", "text": "Fix the bug"}]}},
            {"role": "assistant", "message": {"content": [{"type": "text", "text": "Fixed"}]}},
        ]
        path.write_text("\n".join(json.dumps(m) for m in messages), encoding="utf-8")
        return str(path)

    def discover(self, additional_paths=None):
        return self.ds._discover_candidate_files(additional_paths or [], self.warnings)

    def test_flat_nested_and_subagent_chats_across_users(self):
        expected = {
            self.transcript(self.current, "flat.jsonl"): "current",
            self.transcript(self.gordon, "flat.jsonl"): "gordon",
            self.transcript(self.gordon, "chat/chat.jsonl"): "gordon",
            self.transcript(self.gordon, "chat/subagents/agent.jsonl"): "gordon",
        }
        candidates = self.discover()
        self.assertEqual({c["path"]: c["source_user"] for c in candidates}, expected)
        self.assertEqual(len(candidates), len(expected))
        for candidate in candidates:
            transcript = self.ds._parse_candidate(candidate)
            self.assertEqual(transcript["product"], "cursor")
            self.assertEqual(transcript["kind"], "agent")
            self.assertEqual(transcript["source_home"], str(self.homes / expected[candidate["path"]]))
            self.assertEqual(
                [(m["role"], m["text"]) for m in transcript["messages"]],
                [("user", "Fix the bug"), ("assistant", "Fixed")],
            )
        self.assertEqual(self.warnings, [])

    def test_symlinked_cursor_folder_and_overlapping_additional_path(self):
        backing = self.root / "backing-cursor"
        backing.mkdir()
        (self.gordon / ".cursor").symlink_to(backing, target_is_directory=True)
        path = self.transcript(self.gordon, "chat/chat.jsonl")
        candidates = self.discover([str(self.gordon / ".cursor")])
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["path"], path)
        self.assertEqual(candidates[0]["source_user"], "gordon")
        self.assertEqual(candidates[0]["product"], "cursor")

    def test_scan_opt_out_explicit_username_and_cursor_toggle(self):
        path = self.transcript(self.gordon, "chat/chat.jsonl")
        self.ds._active_user_homes = self.ds._resolve_user_home_dirs([], False, self.warnings)
        self.assertEqual(self.discover(), [])
        self.ds._active_user_homes = self.ds._resolve_user_home_dirs(["gordon"], False, self.warnings)
        self.assertEqual([c["path"] for c in self.discover()], [path])
        self.ds.include_cursor = False
        self.assertEqual(self.discover(), [])


if __name__ == "__main__":
    unittest.main()
