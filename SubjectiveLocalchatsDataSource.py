import os
import re
import json
import glob
import hashlib
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from subjective_abstract_data_source_package import SubjectiveDataSource
from brainboost_data_source_logger_package.BBLogger import BBLogger


class SubjectiveLocalchatsDataSource(SubjectiveDataSource):
    """
    Datasource that discovers locally stored Codex / Claude / Gemini / Cursor chat
    transcripts (CLI, VS Code-style locations, and Cursor Composer SQLite stores)
    and exports one JSON context file per chat into the framework-provided output
    directory.

    Design goals:
    - zero external dependencies
    - safe best-effort parsing for JSON / JSONL / plain text logs
    - broad path discovery for Windows / Linux / macOS
    - one JSON per chat: top-level metadata + `messages` array
    """

    TEXT_EXTENSIONS = {".jsonl", ".json", ".md", ".txt", ".log"}
    MAX_FILE_BYTES = 500 * 1024 * 1024
    MANIFEST_FILENAME = ".subjective_localchats_manifest.json"
    EXTRACTOR_VERSION = 1

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        conn = getattr(self, "_connection", {}) or {}
        self.additional_paths = self._csv_to_list(
            conn.get("additional_paths") or self.params.get("additional_paths", "")
        )
        self.other_user_homes = self._csv_to_list(
            conn.get("other_user_homes") or self.params.get("other_user_homes", "")
        )
        self.scan_other_user_homes = self._to_bool(
            conn.get("scan_other_user_homes", self.params.get("scan_other_user_homes", True))
        )
        self.include_codex = self._to_bool(
            conn.get("include_codex", self.params.get("include_codex", True))
        )
        self.include_claude = self._to_bool(
            conn.get("include_claude", self.params.get("include_claude", True))
        )
        self.include_gemini = self._to_bool(
            conn.get("include_gemini", self.params.get("include_gemini", True))
        )
        self.include_cursor = self._to_bool(
            conn.get("include_cursor", self.params.get("include_cursor", True))
        )
        self.max_files = self._to_int(
            conn.get("max_files", self.params.get("max_files", 500)),
            default=500,
        )
        self.max_messages_per_chat = self._to_int(
            conn.get("max_messages_per_chat", self.params.get("max_messages_per_chat", 4000)),
            default=4000,
        )
        self.export_subfolder = (
            conn.get("export_subfolder")
            or self.params.get("export_subfolder")
            or ""
        )
        self.filename_prefix = (
            conn.get("filename_prefix") or self.params.get("filename_prefix") or "context"
        )
        self._codex_thread_index: Optional[Dict[str, Dict[str, Any]]] = None
        self._codex_thread_index_cache_key: Optional[Tuple[str, ...]] = None
        self._active_user_homes: Optional[List[Dict[str, str]]] = None

    @classmethod
    def connection_schema(cls) -> dict:
        return {
            "include_codex": {
                "type": "boolean",
                "label": "Include Codex",
                "description": "Discover locally stored Codex CLI / editor chat logs.",
                "required": False,
                "default": True,
            },
            "include_claude": {
                "type": "boolean",
                "label": "Include Claude",
                "description": "Discover locally stored Claude CLI / editor chat logs.",
                "required": False,
                "default": True,
            },
            "include_gemini": {
                "type": "boolean",
                "label": "Include Gemini",
                "description": "Discover locally stored Gemini CLI / editor chat logs.",
                "required": False,
                "default": True,
            },
            "include_cursor": {
                "type": "boolean",
                "label": "Include Cursor",
                "description": "Discover locally stored Cursor Composer chats (SQLite) and agent transcript JSONL.",
                "required": False,
                "default": True,
            },
            "additional_paths": {
                "type": "text",
                "label": "Additional Paths",
                "description": "Comma-separated folders or files to scan in addition to the built-in locations.",
                "required": False,
                "placeholder": "/data/chatlogs, C:\\Users\\me\\AppData\\Roaming\\Claude",
            },
            "other_user_homes": {
                "type": "text",
                "label": "Other User Homes",
                "description": "Comma-separated usernames or home folder paths to scan with the built-in Codex, Claude, Gemini, and Cursor locations.",
                "required": False,
                "placeholder": "gordon, subjective, /home/gordon, C:\\Users\\subjective",
            },
            "scan_other_user_homes": {
                "type": "boolean",
                "label": "Scan Accessible User Homes",
                "description": "Also scan readable user homes discovered under common roots such as /home, /Users, and mounted Windows Users folders.",
                "required": False,
                "default": True,
            },
            "export_subfolder": {
                "type": "text",
                "label": "Context Export Subfolder",
                "description": "Optional subfolder within the resolved context folder. Leave blank to write per-chat files directly to the context folder.",
                "required": False,
                "placeholder": "",
            },
            "filename_prefix": {
                "type": "text",
                "label": "Filename Prefix",
                "description": "Prefix used for exported transcript files.",
                "required": False,
                "placeholder": "chat",
            },
            "max_files": {
                "type": "number",
                "label": "Maximum Files",
                "description": "Maximum number of candidate local transcript files to process.",
                "required": False,
                "default": 500,
            },
            "max_messages_per_chat": {
                "type": "number",
                "label": "Maximum Messages Per Chat",
                "description": "Safety guard for extremely large transcript files.",
                "required": False,
                "default": 4000,
            },
        }

    @classmethod
    def request_schema(cls) -> dict:
        return {
            "context_folder": {
                "type": "text",
                "label": "Context Folder",
                "description": "Absolute or relative path where exported transcripts should be written.",
                "required": False,
                "placeholder": "./context",
            },
            "additional_paths": {
                "type": "text",
                "label": "Additional Paths",
                "description": "Per-run comma-separated extra folders or files to scan.",
                "required": False,
            },
            "other_user_homes": {
                "type": "text",
                "label": "Other User Homes",
                "description": "Per-run comma-separated usernames or home folder paths to scan with built-in source discovery.",
                "required": False,
            },
            "scan_other_user_homes": {
                "type": "boolean",
                "label": "Scan Accessible User Homes",
                "description": "Per-run override to include all readable homes found under common user roots.",
                "required": False,
            },
            "max_files": {
                "type": "number",
                "label": "Maximum Files",
                "description": "Override the configured processing limit for this run.",
                "required": False,
            },
        }

    @classmethod
    def output_schema(cls) -> dict:
        return {}

    @classmethod
    def icon(cls) -> str:
        icon_path = os.path.join(os.path.dirname(__file__), "icon.svg")
        try:
            with open(icon_path, "r", encoding="utf-8") as handle:
                return handle.read()
        except Exception as exc:
            BBLogger.log(f"Error reading icon file: {exc}")
            return ""

    def run(self, request: dict) -> Any:
        request = request or {}
        warnings: List[str] = []

        context_root = self._resolve_context_folder(request)
        export_folder = (
            os.path.join(context_root, self.export_subfolder)
            if self.export_subfolder
            else context_root
        )
        os.makedirs(export_folder, exist_ok=True)

        additional_paths = self.additional_paths + self._csv_to_list(request.get("additional_paths", ""))
        other_user_homes = self.other_user_homes + self._csv_to_list(request.get("other_user_homes", ""))
        scan_other_user_homes = self._to_bool(
            request.get("scan_other_user_homes", self.scan_other_user_homes)
        )
        self._active_user_homes = self._resolve_user_home_dirs(
            other_user_homes,
            scan_other_user_homes,
            warnings,
        )
        self._codex_thread_index = None
        self._codex_thread_index_cache_key = None
        max_files = self._to_int(request.get("max_files", self.max_files), default=self.max_files)

        file_candidates = self._discover_candidate_files(additional_paths, warnings)
        cursor_composer_candidates = self._discover_cursor_composer_candidates(warnings)
        combined: List[Tuple[float, Dict[str, str]]] = []
        for item in file_candidates:
            combined.append((self._safe_mtime(item.get("path", "")), item))
        for item in cursor_composer_candidates:
            cms = self._safe_float(item.get("cursor_created_ms"), 0.0)
            score = cms / 1000.0
            if score <= 0:
                score = self._safe_mtime(item.get("path", "") or "")
            combined.append((score, item))
        combined.sort(key=lambda pair: pair[0], reverse=True)
        candidates = [pair[1] for pair in combined]

        manifest = self._load_incremental_manifest(export_folder)
        processed = 0
        skipped_unchanged = 0

        for candidate in candidates:
            try:
                if max_files > 0 and processed >= max_files:
                    break
                source_file = self._candidate_source_file(candidate)
                fingerprint = self._candidate_fingerprint(candidate)
                if self._is_candidate_unchanged(export_folder, manifest, source_file, fingerprint):
                    skipped_unchanged += 1
                    continue

                if candidate.get("cursor_composer_id"):
                    transcript = self._parse_cursor_composer_session(candidate)
                else:
                    transcript = self._parse_candidate(candidate)
                if not transcript or not transcript.get("messages"):
                    continue

                export_path = self._write_transcript(export_folder, transcript)
                self._update_incremental_manifest(
                    manifest,
                    source_file,
                    fingerprint,
                    export_path,
                    transcript,
                )
                processed += 1
            except Exception as exc:
                label = candidate.get("path") or ""
                if candidate.get("cursor_composer_id"):
                    label = f"{label} composer:{candidate.get('cursor_composer_id')}"
                warning = f"Failed to process {label}: {exc}"
                warnings.append(warning)
                BBLogger.log(warning)

        self._save_incremental_manifest(export_folder, manifest, warnings)

        BBLogger.log(
            f"[SubjectiveLocalchatsDataSource] Exported {processed} chat context files to {export_folder}; "
            f"skipped {skipped_unchanged} unchanged "
            f"(warnings: {len(warnings)})"
        )

        return None

    def _discover_candidate_files(
        self, additional_paths: List[str], warnings: List[str]
    ) -> List[Dict[str, str]]:
        discovered: List[Dict[str, str]] = []
        seen: set = set()

        for product, kind, pattern, source_user, source_home in self._candidate_globs(additional_paths):
            for path in self._expand_glob(pattern):
                if path in seen:
                    continue
                seen.add(path)
                if not os.path.isfile(path):
                    continue
                if not self._is_supported_file(path):
                    continue
                try:
                    if os.path.getsize(path) > self.MAX_FILE_BYTES:
                        warnings.append(f"Skipping oversized file: {path}")
                        continue
                except OSError:
                    continue

                discovered.append(
                    {
                        "product": product,
                        "kind": kind,
                        "path": path,
                        "source_user": source_user,
                        "source_home": source_home,
                    }
                )

        discovered.sort(key=lambda item: self._safe_mtime(item["path"]), reverse=True)
        return discovered

    def _current_home_dir(self) -> str:
        return os.path.abspath(os.path.expanduser("~"))

    def _user_label_for_home(self, home: str) -> str:
        label = Path(home).name
        return label or "current"

    def _default_user_home_record(self) -> Dict[str, str]:
        home = self._current_home_dir()
        return {"home": home, "source_user": self._user_label_for_home(home)}

    def _common_user_roots(self) -> List[str]:
        current_home = self._current_home_dir()
        roots = [str(Path(current_home).parent), "/home", "/Users", r"C:\Users"]
        roots.extend(glob.glob("/mnt/*/Users"))
        roots.extend(glob.glob("/media/*/*/Users"))
        roots.extend(glob.glob("/media/*/Windows/Users"))

        seen: set = set()
        out: List[str] = []
        for root in roots:
            if not root:
                continue
            expanded = os.path.abspath(os.path.expandvars(os.path.expanduser(root)))
            norm = os.path.normcase(os.path.normpath(expanded))
            if norm in seen or not os.path.isdir(expanded):
                continue
            seen.add(norm)
            out.append(expanded)
        return out

    def _is_readable_dir(self, path: str) -> bool:
        return os.path.isdir(path) and os.access(path, os.R_OK | os.X_OK)

    def _resolve_user_home_entry(self, entry: str, warnings: List[str]) -> List[str]:
        entry = (entry or "").strip()
        if not entry:
            return []

        expanded = os.path.abspath(os.path.expandvars(os.path.expanduser(entry)))
        looks_like_path = (
            os.path.isabs(entry)
            or entry.startswith("~")
            or "/" in entry
            or "\\" in entry
        )
        if looks_like_path:
            if self._is_readable_dir(expanded):
                return [expanded]
            warnings.append(f"Other user home is not readable or does not exist: {entry}")
            return []

        matches = []
        for root in self._common_user_roots():
            candidate = os.path.join(root, entry)
            if self._is_readable_dir(candidate):
                matches.append(os.path.abspath(candidate))

        if not matches:
            warnings.append(f"Could not resolve readable user home for: {entry}")
        return matches

    def _discover_accessible_user_homes(self) -> List[str]:
        skip_names = {
            "all users",
            "default",
            "default user",
            "desktop.ini",
            "public",
            "shared",
        }
        homes: List[str] = []
        current_home = os.path.normcase(os.path.normpath(self._current_home_dir()))
        for root in self._common_user_roots():
            try:
                names = os.listdir(root)
            except OSError:
                continue
            for name in names:
                if name.lower() in skip_names:
                    continue
                candidate = os.path.join(root, name)
                norm = os.path.normcase(os.path.normpath(candidate))
                if norm == current_home:
                    continue
                if self._is_readable_dir(candidate):
                    homes.append(os.path.abspath(candidate))
        return homes

    def _resolve_user_home_dirs(
        self,
        configured_homes: List[str],
        scan_other_user_homes: bool,
        warnings: List[str],
    ) -> List[Dict[str, str]]:
        records = [self._default_user_home_record()]
        candidates: List[str] = []
        for entry in configured_homes:
            candidates.extend(self._resolve_user_home_entry(entry, warnings))
        if scan_other_user_homes:
            candidates.extend(self._discover_accessible_user_homes())

        seen = {os.path.normcase(os.path.normpath(records[0]["home"]))}
        for home in candidates:
            norm = os.path.normcase(os.path.normpath(home))
            if norm in seen:
                continue
            seen.add(norm)
            records.append({"home": home, "source_user": self._user_label_for_home(home)})
        return records

    def _active_home_records(self) -> List[Dict[str, str]]:
        return self._active_user_homes or [self._default_user_home_record()]

    def _source_user_for_path(self, path: str) -> Tuple[str, str]:
        norm_path = os.path.normcase(os.path.normpath(os.path.abspath(path)))
        best: Optional[Dict[str, str]] = None
        best_len = -1
        for record in self._active_home_records():
            home = record.get("home") or ""
            norm_home = os.path.normcase(os.path.normpath(os.path.abspath(home)))
            try:
                common = os.path.commonpath([norm_path, norm_home])
            except ValueError:
                continue
            if common == norm_home and len(norm_home) > best_len:
                best = record
                best_len = len(norm_home)
        if best:
            return best.get("source_user", ""), best.get("home", "")
        return "", ""

    def _cursor_user_base_dirs(self) -> List[Dict[str, str]]:
        current_home = self._current_home_dir()
        candidates: List[Dict[str, str]] = []
        for record in self._active_home_records():
            home = record.get("home") or current_home
            source_user = record.get("source_user") or self._user_label_for_home(home)
            paths = [
                os.path.join(home, "AppData", "Roaming", "Cursor", "User"),
                os.path.join(home, ".config", "Cursor", "User"),
                os.path.join(home, "Library", "Application Support", "Cursor", "User"),
                os.path.join(home, ".cursor-server", "data", "User"),
            ]
            if os.path.normcase(os.path.normpath(home)) == os.path.normcase(os.path.normpath(current_home)):
                appdata = os.environ.get("APPDATA", "")
                if appdata:
                    paths.insert(0, os.path.join(appdata, "Cursor", "User"))
            for path in paths:
                candidates.append(
                    {"path": path, "source_user": source_user, "source_home": home}
                )
        seen: set = set()
        resolved: List[Dict[str, str]] = []
        for candidate in candidates:
            path = candidate["path"]
            norm = os.path.normcase(os.path.normpath(path))
            if norm in seen or not os.path.isdir(path):
                continue
            seen.add(norm)
            resolved.append(candidate)
        return resolved

    def _cursor_global_state_db_paths(self) -> List[Dict[str, str]]:
        paths: List[Dict[str, str]] = []
        for base in self._cursor_user_base_dirs():
            db_path = os.path.join(base["path"], "globalStorage", "state.vscdb")
            if os.path.isfile(db_path):
                paths.append(
                    {
                        "path": db_path,
                        "source_user": base.get("source_user", ""),
                        "source_home": base.get("source_home", ""),
                    }
                )
        return paths

    def _discover_cursor_composer_candidates(self, warnings: List[str]) -> List[Dict[str, str]]:
        if not self.include_cursor:
            return []
        out: List[Dict[str, str]] = []
        for db_record in self._cursor_global_state_db_paths():
            db_path = db_record["path"]
            uri = f"file:{db_path.replace(os.sep, '/')}?mode=ro"
            try:
                conn = sqlite3.connect(uri, uri=True, timeout=5.0)
            except sqlite3.Error as exc:
                warnings.append(f"Cursor Composer: could not open {db_path}: {exc}")
                continue
            try:
                cur = conn.execute(
                    "SELECT key, value FROM cursorDiskKV WHERE key LIKE 'composerData:%'"
                )
                for key, raw in cur.fetchall():
                    if raw is None:
                        continue
                    try:
                        payload = json.loads(raw)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    composer_id = str(payload.get("composerId") or "").strip()
                    if not composer_id and isinstance(key, str) and key.startswith("composerData:"):
                        composer_id = key.split(":", 1)[1].strip()
                    if not composer_id:
                        continue
                    created = payload.get("createdAt")
                    created_ms = 0.0
                    if isinstance(created, (int, float)):
                        created_ms = float(created)
                    elif isinstance(created, str):
                        ts = self._parse_any_timestamp(created)
                        if ts > 0:
                            created_ms = ts * 1000.0
                    out.append(
                        {
                            "product": "cursor",
                            "kind": "composer",
                            "path": db_path,
                            "cursor_composer_id": composer_id,
                            "cursor_created_ms": str(int(created_ms)) if created_ms else "0",
                            "source_user": db_record.get("source_user", ""),
                            "source_home": db_record.get("source_home", ""),
                        }
                    )
            except sqlite3.Error as exc:
                warnings.append(f"Cursor Composer: query failed {db_path}: {exc}")
            finally:
                conn.close()
        return out

    def _cursor_bubble_role(self, bubble_type: Any) -> str:
        if bubble_type == 1:
            return "user"
        if bubble_type == 2:
            return "assistant"
        return "unknown"

    def _parse_cursor_composer_session(self, candidate: Dict[str, str]) -> Optional[Dict[str, Any]]:
        db_path = candidate.get("path") or ""
        composer_id = candidate.get("cursor_composer_id") or ""
        if not db_path or not composer_id or not os.path.isfile(db_path):
            return None
        uri = f"file:{db_path.replace(os.sep, '/')}?mode=ro"
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=5.0)
        except sqlite3.Error:
            return None
        messages: List[Dict[str, Any]] = []
        title_hint = ""
        chat_started_ms = 0.0
        try:
            row = conn.execute(
                "SELECT value FROM cursorDiskKV WHERE key = ?",
                (f"composerData:{composer_id}",),
            ).fetchone()
            if row and row[0] is not None:
                try:
                    meta = json.loads(row[0])
                    t = meta.get("text")
                    if isinstance(t, str) and t.strip():
                        title_hint = t.strip()
                    created = meta.get("createdAt")
                    if isinstance(created, (int, float)):
                        chat_started_ms = float(created)
                    elif isinstance(created, str):
                        chat_started_ms = self._parse_any_timestamp(created) * 1000.0
                except (json.JSONDecodeError, TypeError):
                    pass
            cur = conn.execute(
                "SELECT value FROM cursorDiskKV WHERE key LIKE ?",
                (f"bubbleId:{composer_id}:%",),
            )
            for (raw,) in cur.fetchall():
                if raw is None:
                    continue
                try:
                    bubble = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue
                text = (bubble.get("text") or "").strip()
                if not text:
                    continue
                role = self._cursor_bubble_role(bubble.get("type"))
                ts = bubble.get("createdAt")
                messages.append({"role": role, "timestamp": ts, "text": text})
        finally:
            conn.close()

        messages.sort(key=lambda m: self._parse_any_timestamp(m.get("timestamp")))
        if self.max_messages_per_chat > 0:
            messages = messages[: self.max_messages_per_chat]
        if not messages:
            return None
        virtual_path = f"{db_path}#composer:{composer_id}"
        title = title_hint or self._derive_title(virtual_path, messages)
        chat_started_at = chat_started_ms / 1000.0 if chat_started_ms > 0 else 0.0
        if chat_started_at <= 0:
            chat_started_at = self._chat_start_timestamp(virtual_path, messages)
        return {
            "product": candidate.get("product"),
            "kind": candidate.get("kind"),
            "source_user": candidate.get("source_user"),
            "source_home": candidate.get("source_home"),
            "source_file": virtual_path,
            "title": title,
            "messages": messages,
            "chat_started_at": chat_started_at,
        }

    def _candidate_globs(self, additional_paths: List[str]) -> List[Tuple[str, str, str, str, str]]:
        patterns: List[Tuple[str, str, str, str, str]] = []

        def add(product: str, kind: str, pattern: str, source_user: str, source_home: str) -> None:
            patterns.append((product, kind, pattern, source_user, source_home))

        for record in self._active_home_records():
            home = record.get("home") or self._current_home_dir()
            source_user = record.get("source_user") or self._user_label_for_home(home)

            if self.include_codex:
                for kind, pattern in [
                    ("cli", os.path.join(home, ".codex", "sessions", "**", "*.jsonl")),
                    ("cli", os.path.join(home, ".codex", "history.jsonl")),
                    ("cli", os.path.join(home, ".config", "codex", "**", "*.jsonl")),
                    (
                        "vscode",
                        os.path.join(home, "AppData", "Roaming", "Code", "User", "globalStorage", "**", "*codex*", "**", "*.json*"),
                    ),
                    (
                        "vscode",
                        os.path.join(home, ".config", "Code", "User", "globalStorage", "**", "*codex*", "**", "*.json*"),
                    ),
                    (
                        "vscode",
                        os.path.join(home, "Library", "Application Support", "Code", "User", "globalStorage", "**", "*codex*", "**", "*.json*"),
                    ),
                ]:
                    add("codex", kind, pattern, source_user, home)

            if self.include_claude:
                for kind, pattern in [
                    ("cli", os.path.join(home, ".claude", "**", "*.jsonl")),
                    ("cli", os.path.join(home, ".config", "claude", "**", "*.jsonl")),
                    (
                        "vscode",
                        os.path.join(home, "AppData", "Roaming", "Code", "User", "globalStorage", "**", "*claude*", "**", "*.json*"),
                    ),
                    (
                        "vscode",
                        os.path.join(home, ".config", "Code", "User", "globalStorage", "**", "*claude*", "**", "*.json*"),
                    ),
                    (
                        "vscode",
                        os.path.join(home, "Library", "Application Support", "Code", "User", "globalStorage", "**", "*claude*", "**", "*.json*"),
                    ),
                ]:
                    add("claude", kind, pattern, source_user, home)

            if self.include_gemini:
                for kind, pattern in [
                    ("cli", os.path.join(home, ".gemini", "tmp", "**", "logs.json")),
                    ("cli", os.path.join(home, ".gemini", "tmp", "**", "checkpoint-*.json")),
                    ("cli", os.path.join(home, ".gemini", "tmp", "**", "*.jsonl")),
                    ("cli", os.path.join(home, ".gemini", "sessions", "**", "*.json*")),
                    ("cli", os.path.join(home, ".gemini", "history", "**", "*.json*")),
                    ("cli", os.path.join(home, ".config", "gemini", "**", "*.json*")),
                    (
                        "vscode",
                        os.path.join(home, "AppData", "Roaming", "Code", "User", "globalStorage", "**", "*gemini*", "**", "*.json*"),
                    ),
                    (
                        "vscode",
                        os.path.join(home, ".config", "Code", "User", "globalStorage", "**", "*gemini*", "**", "*.json*"),
                    ),
                    (
                        "vscode",
                        os.path.join(home, "Library", "Application Support", "Code", "User", "globalStorage", "**", "*gemini*", "**", "*.json*"),
                    ),
                ]:
                    add("gemini", kind, pattern, source_user, home)

            if self.include_cursor:
                add(
                    "cursor",
                    "agent",
                    os.path.join(home, ".cursor", "projects", "**", "agent-transcripts", "*.jsonl"),
                    source_user,
                    home,
                )

        for custom_path in additional_paths:
            if not custom_path:
                continue
            expanded = os.path.expandvars(os.path.expanduser(custom_path.strip()))
            source_user, source_home = self._source_user_for_path(expanded)
            if os.path.isdir(expanded):
                patterns.append(("custom", "manual", os.path.join(expanded, "**", "*"), source_user, source_home))
            else:
                patterns.append(("custom", "manual", expanded, source_user, source_home))

        return patterns

    def _expand_glob(self, pattern: str) -> List[str]:
        return glob.glob(pattern, recursive=True)

    def _is_supported_file(self, path: str) -> bool:
        return Path(path).suffix.lower() in self.TEXT_EXTENSIONS

    def _candidate_source_file(self, candidate: Dict[str, str]) -> str:
        path = candidate.get("path") or ""
        composer_id = candidate.get("cursor_composer_id") or ""
        if composer_id:
            return f"{path}#composer:{composer_id}"
        return path

    def _manifest_key(self, source_file: str) -> str:
        normalized = os.path.normcase(os.path.normpath(source_file or ""))
        return hashlib.sha1(normalized.encode("utf-8", errors="ignore")).hexdigest()

    def _candidate_fingerprint(self, candidate: Dict[str, str]) -> Dict[str, Any]:
        source_file = self._candidate_source_file(candidate)
        fs_path = self._path_for_fs_stat(source_file)
        fingerprint: Dict[str, Any] = {
            "source_file": source_file,
            "product": candidate.get("product") or "",
            "kind": candidate.get("kind") or "",
            "source_user": candidate.get("source_user") or "",
            "source_home": candidate.get("source_home") or "",
            "cursor_composer_id": candidate.get("cursor_composer_id") or "",
            "connection_label": self._get_connection_label(),
            "datasource_name": self.get_data_source_type_name(),
            "max_messages_per_chat": self.max_messages_per_chat,
            "extractor_version": self.EXTRACTOR_VERSION,
        }
        try:
            stat_result = os.stat(fs_path)
            fingerprint["size"] = int(stat_result.st_size)
            fingerprint["mtime_ns"] = int(stat_result.st_mtime_ns)
        except OSError:
            fingerprint["size"] = -1
            fingerprint["mtime_ns"] = -1
        if candidate.get("cursor_created_ms"):
            fingerprint["cursor_created_ms"] = candidate.get("cursor_created_ms")
        return fingerprint

    def _manifest_path(self, export_folder: str) -> str:
        return os.path.join(export_folder, self.MANIFEST_FILENAME)

    def _load_incremental_manifest(self, export_folder: str) -> Dict[str, Any]:
        path = self._manifest_path(export_folder)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, json.JSONDecodeError, TypeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        if not isinstance(payload.get("sources"), dict):
            payload["sources"] = {}
        payload["version"] = 1
        return payload

    def _save_incremental_manifest(
        self, export_folder: str, manifest: Dict[str, Any], warnings: List[str]
    ) -> None:
        path = self._manifest_path(export_folder)
        tmp_path = f"{path}.tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as handle:
                json.dump(manifest, handle, indent=2, ensure_ascii=False)
            os.replace(tmp_path, path)
        except OSError as exc:
            warning = f"Failed to write local chats manifest at {path}: {exc}"
            warnings.append(warning)
            BBLogger.log(warning)

    def _manifest_output_exists(self, export_folder: str, record: Dict[str, Any]) -> bool:
        export_path = record.get("export_path") or ""
        if export_path and os.path.isfile(export_path):
            return True
        export_filename = record.get("export_filename") or ""
        if export_filename and os.path.isfile(os.path.join(export_folder, export_filename)):
            return True
        return False

    def _is_candidate_unchanged(
        self,
        export_folder: str,
        manifest: Dict[str, Any],
        source_file: str,
        fingerprint: Dict[str, Any],
    ) -> bool:
        if not source_file:
            return False
        record = (manifest.get("sources") or {}).get(self._manifest_key(source_file))
        if not isinstance(record, dict):
            return False
        if record.get("fingerprint") != fingerprint:
            return False
        return self._manifest_output_exists(export_folder, record)

    def _update_incremental_manifest(
        self,
        manifest: Dict[str, Any],
        source_file: str,
        fingerprint: Dict[str, Any],
        export_path: str,
        transcript: Dict[str, Any],
    ) -> None:
        if not source_file:
            return
        sources = manifest.setdefault("sources", {})
        sources[self._manifest_key(source_file)] = {
            "source_file": source_file,
            "fingerprint": fingerprint,
            "export_path": export_path,
            "export_filename": os.path.basename(export_path),
            "message_count": len(transcript.get("messages") or []),
            "title": transcript.get("title") or "",
        }

    def _parse_candidate(self, candidate: Dict[str, str]) -> Optional[Dict[str, Any]]:
        path = candidate["path"]
        suffix = Path(path).suffix.lower()

        if suffix == ".jsonl":
            messages = self._parse_jsonl_messages(path)
        elif suffix == ".json":
            messages = self._parse_json_messages(path)
        else:
            messages = self._parse_plain_text(path)

        messages = [m for m in messages if (m.get("text") or "").strip()]
        if not messages:
            return None

        if self.max_messages_per_chat > 0:
            messages = messages[: self.max_messages_per_chat]

        title = self._derive_title(path, messages)
        chat_started_at = self._chat_start_timestamp(path, messages)
        return {
            "product": candidate.get("product"),
            "kind": candidate.get("kind"),
            "source_user": candidate.get("source_user"),
            "source_home": candidate.get("source_home"),
            "source_file": path,
            "title": title,
            "messages": messages,
            "chat_started_at": chat_started_at,
        }

    def _parse_jsonl_messages(self, path: str) -> List[Dict[str, Any]]:
        messages: List[Dict[str, Any]] = []
        with open(path, "r", encoding="utf-8", errors="ignore") as handle:
            for line_number, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    messages.append(
                        {
                            "role": "unknown",
                            "timestamp": None,
                            "text": line,
                            "source_line": line_number,
                        }
                    )
                    continue
                messages.extend(self._extract_messages_from_object(obj, fallback_timestamp=None))
        return messages

    def _parse_json_messages(self, path: str) -> List[Dict[str, Any]]:
        with open(path, "r", encoding="utf-8", errors="ignore") as handle:
            payload = json.load(handle)
        return self._extract_messages_from_object(payload, fallback_timestamp=None)

    def _parse_plain_text(self, path: str) -> List[Dict[str, Any]]:
        with open(path, "r", encoding="utf-8", errors="ignore") as handle:
            text = handle.read().strip()
        if not text:
            return []
        return [{"role": "transcript", "timestamp": None, "text": text}]

    def _extract_messages_from_object(
        self, obj: Any, fallback_timestamp: Optional[str]
    ) -> List[Dict[str, Any]]:
        if obj is None:
            return []

        if isinstance(obj, list):
            messages: List[Dict[str, Any]] = []
            for item in obj:
                messages.extend(self._extract_messages_from_object(item, fallback_timestamp))
            return messages

        if isinstance(obj, str):
            text = obj.strip()
            return [{"role": "unknown", "timestamp": fallback_timestamp, "text": text}] if text else []

        if not isinstance(obj, dict):
            return []

        role = self._pick_first(obj, ["role", "speaker", "author", "type", "sender"])
        timestamp = self._pick_first(
            obj,
            ["timestamp", "created_at", "updated_at", "time", "date", "ts"],
            default=fallback_timestamp,
        )
        text = self._extract_text(obj)
        if role and text:
            return [{"role": str(role), "timestamp": timestamp, "text": text}]

        for key in [
            "messages",
            "items",
            "conversation",
            "conversations",
            "chat",
            "chats",
            "entries",
            "transcript",
            "turns",
            "history",
            "events",
            "content",
        ]:
            if key in obj and isinstance(obj[key], (list, dict)):
                return self._extract_messages_from_object(obj[key], timestamp)

        extracted: List[Dict[str, Any]] = []
        prompt_text = self._extract_text(obj.get("request")) or self._extract_text(obj.get("prompt"))
        response_text = self._extract_text(obj.get("response")) or self._extract_text(obj.get("completion"))
        if prompt_text:
            extracted.append({"role": "user", "timestamp": timestamp, "text": prompt_text})
        if response_text:
            extracted.append({"role": "assistant", "timestamp": timestamp, "text": response_text})
        if extracted:
            return extracted

        nested_messages: List[Dict[str, Any]] = []
        for value in obj.values():
            if isinstance(value, (list, dict)):
                nested_messages.extend(self._extract_messages_from_object(value, timestamp))
        return nested_messages

    def _extract_text(self, value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, (int, float, bool)):
            return str(value)
        if isinstance(value, list):
            parts = [self._extract_text(item) for item in value]
            return "\n".join([p for p in parts if p]).strip()
        if isinstance(value, dict):
            for key in [
                "text",
                "content",
                "message",
                "body",
                "value",
                "output_text",
                "input_text",
                "prompt",
                "completion",
            ]:
                if key in value:
                    text = self._extract_text(value[key])
                    if text:
                        return text

            if "parts" in value:
                return self._extract_text(value["parts"])

            if "chunks" in value:
                return self._extract_text(value["chunks"])

            if value.get("type") in {"text", "input_text", "output_text"} and "text" in value:
                return self._extract_text(value.get("text"))

            if "delta" in value:
                return self._extract_text(value["delta"])

            if "arguments" in value and isinstance(value["arguments"], str):
                return value["arguments"].strip()

        return ""

    def _path_for_fs_stat(self, path: str) -> str:
        """Strip URI-style fragment (e.g. Cursor virtual source `db#composer:uuid`) for os.path ops."""
        if not path or "#" not in path:
            return path
        return path.split("#", 1)[0]

    def _safe_float(self, value: Any, default: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _coerce_epoch_seconds(self, ts: float) -> float:
        """Normalize ms/mixed encodings; avoid OSError from utcfromtimestamp on Windows."""
        if ts <= 0 or ts != ts:
            return 0.0
        guard = 0
        while ts > 1e11 and guard < 4:
            ts /= 1000.0
            guard += 1
        return ts

    def _format_utc_timestamp(self, value: float) -> str:
        value = self._coerce_epoch_seconds(self._safe_float(value, 0.0))
        if value <= 0:
            return ""
        try:
            return datetime.utcfromtimestamp(value).isoformat() + "Z"
        except (OSError, OverflowError, ValueError):
            return ""

    def _stable_export_start_ts(
        self, transcript: Dict[str, Any], enrichment: Optional[Dict[str, Any]]
    ) -> float:
        """
        Session start time for export *identity* (filename): transcript metadata, Codex
        enrichment, or first message timestamp. Does not use file mtime (changes when a
        chat grows) or wall-clock now(), so the same logical chat maps to one output file.
        """
        ts = self._coerce_epoch_seconds(self._safe_float(transcript.get("chat_started_at"), 0.0))
        if enrichment:
            for key in ("created_at_ms", "created_at", "updated_at_ms", "updated_at"):
                ts_val = enrichment.get(key)
                if not ts_val:
                    continue
                try:
                    ts_float = float(ts_val) / 1000.0 if key.endswith("_ms") else float(ts_val)
                except (TypeError, ValueError):
                    continue
                ts_float = self._coerce_epoch_seconds(ts_float)
                if ts_float > 0:
                    ts = ts_float
                    break
        if ts <= 0:
            for message in transcript.get("messages") or []:
                t = self._parse_any_timestamp(message.get("timestamp"))
                if t > 0:
                    return t
        return ts

    def _write_transcript(self, export_folder: str, transcript: Dict[str, Any]) -> str:
        source_file = transcript.get("source_file") or ""

        enrichment = (
            self._codex_thread_for(self._path_for_fs_stat(source_file))
            if transcript.get("product") == "codex"
            else None
        )

        stable_ts = self._stable_export_start_ts(transcript, enrichment)
        filename_label = self._format_framework_timestamp(stable_ts)
        if not filename_label:
            filename_label = "session"
        digest = hashlib.sha1(source_file.encode("utf-8", errors="ignore")).hexdigest()[:10]
        filename = self.build_context_filename(
            datasource_name=self.get_data_source_type_name(),
            connection_label=self._get_connection_label(),
            output_format="json",
            timestamp=f"{filename_label}_{digest}",
        )
        export_path = os.path.join(export_folder, filename)

        chat_ts = stable_ts
        if chat_ts <= 0:
            chat_ts = self._safe_mtime(self._path_for_fs_stat(source_file))

        title = transcript.get("title") or Path(source_file).stem or "Untitled chat"
        if enrichment:
            real_title = (enrichment.get("title") or enrichment.get("first_user_message") or "").strip()
            if real_title:
                title = " ".join(real_title.split())[:120]

        messages = [
            {
                "role": (message.get("role") or "unknown"),
                "timestamp": message.get("timestamp"),
                "text": (message.get("text") or "").rstrip(),
            }
            for message in (transcript.get("messages") or [])
        ]

        context_data = {
            "type": "localchat",
            "title": title,
            "chat_name": title,
            "product": transcript.get("product"),
            "kind": transcript.get("kind"),
            "source_user": transcript.get("source_user") or "",
            "source_home": transcript.get("source_home") or "",
            "source_file": source_file,
            "chat_started_at": chat_ts,
            "chat_started_timestamp": self._format_utc_timestamp(chat_ts),
            "source_mtime": self._safe_mtime(self._path_for_fs_stat(source_file)),
            "source_timestamp": self._format_utc_timestamp(
                self._safe_mtime(self._path_for_fs_stat(source_file))
            ),
            "message_count": len(messages),
            "messages": messages,
        }

        if enrichment:
            context_data["thread_id"] = enrichment.get("id")
            context_data["model"] = enrichment.get("model")
            context_data["git_branch"] = enrichment.get("git_branch")
            context_data["git_sha"] = enrichment.get("git_sha")
            context_data["cwd"] = enrichment.get("cwd")
            context_data["archived"] = bool(enrichment.get("archived"))
            context_data["tokens_used"] = enrichment.get("tokens_used")
            context_data["source_surface"] = enrichment.get("source")

        with open(export_path, "w", encoding="utf-8") as handle:
            json.dump(context_data, handle, indent=2, ensure_ascii=False)

        return export_path

    def _codex_thread_for(self, source_file: str) -> Optional[Dict[str, Any]]:
        source_file = self._path_for_fs_stat(source_file)
        if not source_file:
            return None
        index = self._load_codex_thread_index()
        if not index:
            return None
        key = os.path.normcase(os.path.normpath(source_file))
        return index.get(key)

    def _load_codex_thread_index(self) -> Dict[str, Dict[str, Any]]:
        home_records = self._active_home_records()
        cache_key = tuple(
            os.path.normcase(os.path.normpath(record.get("home", "")))
            for record in home_records
        )
        if (
            self._codex_thread_index is not None
            and self._codex_thread_index_cache_key == cache_key
        ):
            return self._codex_thread_index

        index: Dict[str, Dict[str, Any]] = {}
        db_paths = []
        for record in home_records:
            home = record.get("home") or self._current_home_dir()
            db_path = os.path.join(home, ".codex", "state_5.sqlite")
            if os.path.isfile(db_path):
                db_paths.append(db_path)

        if not db_paths:
            self._codex_thread_index = index
            self._codex_thread_index_cache_key = cache_key
            return index

        for db_path in db_paths:
            try:
                uri = f"file:{db_path.replace(os.sep, '/')}?mode=ro"
                conn = sqlite3.connect(uri, uri=True, timeout=2.0)
                try:
                    cursor = conn.execute(
                        "SELECT rollout_path, id, title, first_user_message, model, "
                        "git_branch, git_sha, cwd, archived, source, tokens_used, "
                        "created_at, updated_at, created_at_ms, updated_at_ms "
                        "FROM threads"
                    )
                    columns = [c[0] for c in cursor.description]
                    for row in cursor.fetchall():
                        record = dict(zip(columns, row))
                        path = record.get("rollout_path")
                        if not path:
                            continue
                        index[os.path.normcase(os.path.normpath(str(path)))] = record
                finally:
                    conn.close()
            except sqlite3.Error as exc:
                BBLogger.log(f"Failed to read Codex thread index at {db_path}: {exc}")

        self._codex_thread_index = index
        self._codex_thread_index_cache_key = cache_key
        return index

    def _format_framework_timestamp(self, value: float) -> str:
        value = self._coerce_epoch_seconds(self._safe_float(value, 0.0))
        if value <= 0:
            return ""
        try:
            return datetime.utcfromtimestamp(value).strftime("%Y_%m_%d_%H_%M_%S")
        except (OSError, OverflowError, ValueError):
            return ""

    def _chat_start_timestamp(self, source_file: str, messages: List[Dict[str, Any]]) -> float:
        for message in messages:
            ts = self._parse_any_timestamp(message.get("timestamp"))
            if ts > 0:
                return ts
        ts = self._parse_timestamp_from_filename(source_file)
        if ts > 0:
            return ts
        fs_path = self._path_for_fs_stat(source_file)
        try:
            return os.path.getctime(fs_path)
        except OSError:
            return 0.0

    def _parse_any_timestamp(self, value: Any) -> float:
        if value is None or value == "":
            return 0.0
        if isinstance(value, bool):
            return 0.0
        if isinstance(value, (int, float)):
            numeric = float(value)
            if numeric > 1e12:
                numeric /= 1000.0
            return numeric if numeric > 0 else 0.0
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return 0.0
            try:
                cleaned = text.replace("Z", "+00:00")
                return datetime.fromisoformat(cleaned).timestamp()
            except Exception:
                pass
            try:
                numeric = float(text)
                if numeric > 1e12:
                    numeric /= 1000.0
                return numeric if numeric > 0 else 0.0
            except ValueError:
                pass
        return 0.0

    def _parse_timestamp_from_filename(self, source_file: str) -> float:
        basename = os.path.basename(source_file)
        match = re.search(
            r"(\d{4})-(\d{2})-(\d{2})[T_](\d{2})[-:](\d{2})[-:](\d{2})",
            basename,
        )
        if not match:
            return 0.0
        try:
            parts = [int(p) for p in match.groups()]
            return datetime(*parts).timestamp()
        except (ValueError, OverflowError):
            return 0.0

    def _resolve_context_folder(self, request: Dict[str, Any]) -> str:
        candidates = [
            request.get("context_folder"),
            getattr(self, "output_dir", "") or "",
        ]
        config = getattr(self, "_config", {}) or {}
        for key in ("output_dir", "context_dir", "TARGET_DIRECTORY", "target_directory", "CONTEXT_DIR"):
            value = config.get(key)
            if value:
                candidates.append(value)
        if hasattr(self, "params") and isinstance(self.params, dict):
            candidates.append(self.params.get("context_folder"))
        for candidate in candidates:
            if candidate:
                path = os.path.abspath(os.path.expanduser(str(candidate)))
                os.makedirs(path, exist_ok=True)
                return path
        fallback = os.path.abspath(os.path.join(os.getcwd(), "context"))
        os.makedirs(fallback, exist_ok=True)
        return fallback

    PREAMBLE_TAG_PREFIXES = (
        "<environment_context",
        "<user_instructions",
        "<system-reminder",
        "<system>",
        "<tool_result",
        "<tool_use",
        "<context>",
        "<metadata",
    )

    def _is_preamble_text(self, text: str) -> bool:
        stripped = (text or "").lstrip()
        if not stripped:
            return True
        lowered = stripped.lower()
        return any(lowered.startswith(p) for p in self.PREAMBLE_TAG_PREFIXES)

    def _derive_title(self, path: str, messages: List[Dict[str, Any]]) -> str:
        for message in messages:
            role = str(message.get("role", "")).lower()
            if role not in {"user", "human"}:
                continue
            text = message.get("text", "") or ""
            if self._is_preamble_text(text):
                continue
            title = " ".join(text.split())[:80].strip()
            if title:
                return title
        stem_path = self._path_for_fs_stat(path)
        return Path(stem_path).stem if stem_path else "Untitled chat"

    def _pick_first(self, obj: Dict[str, Any], keys: Iterable[str], default: Any = None) -> Any:
        for key in keys:
            if key in obj and obj[key] not in (None, "", []):
                return obj[key]
        return default

    def _safe_mtime(self, path: str) -> float:
        path = self._path_for_fs_stat(path)
        try:
            return os.path.getmtime(path)
        except OSError:
            return 0.0

    def _csv_to_list(self, value: Any) -> List[str]:
        if value is None:
            return []
        if isinstance(value, list):
            return [str(v).strip() for v in value if str(v).strip()]
        return [part.strip() for part in str(value).split(",") if part.strip()]

    def _to_bool(self, value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if value is None:
            return False
        return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}

    def _to_int(self, value: Any, default: int) -> int:
        try:
            return int(value)
        except Exception:
            return default

    def _slugify(self, value: str) -> str:
        cleaned = []
        for char in str(value).lower():
            if char.isalnum():
                cleaned.append(char)
            elif char in {" ", "-", "_"}:
                cleaned.append("-")
        slug = "".join(cleaned).strip("-")
        while "--" in slug:
            slug = slug.replace("--", "-")
        return slug[:80] or "chat"
