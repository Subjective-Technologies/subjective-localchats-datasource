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
    Datasource that discovers locally stored Codex / Claude chat transcripts
    (CLI and VS Code style locations) and exports one JSON context file per
    chat into the framework-provided output directory.

    Design goals:
    - zero external dependencies
    - safe best-effort parsing for JSON / JSONL / plain text logs
    - broad path discovery for Windows / Linux / macOS
    - one JSON per chat: top-level metadata + `messages` array
    """

    TEXT_EXTENSIONS = {".jsonl", ".json", ".md", ".txt", ".log"}
    MAX_FILE_BYTES = 500 * 1024 * 1024

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        conn = getattr(self, "_connection", {}) or {}
        self.additional_paths = self._csv_to_list(
            conn.get("additional_paths") or self.params.get("additional_paths", "")
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
            "additional_paths": {
                "type": "text",
                "label": "Additional Paths",
                "description": "Comma-separated folders or files to scan in addition to the built-in locations.",
                "required": False,
                "placeholder": "/data/chatlogs, C:\\Users\\me\\AppData\\Roaming\\Claude",
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
        max_files = self._to_int(request.get("max_files", self.max_files), default=self.max_files)

        candidates = self._discover_candidate_files(additional_paths, warnings)
        candidates = sorted(
            candidates,
            key=lambda item: self._safe_mtime(item.get("path", "")),
            reverse=True,
        )
        if max_files > 0:
            candidates = candidates[:max_files]

        processed = 0

        for candidate in candidates:
            try:
                transcript = self._parse_candidate(candidate)
                if not transcript or not transcript.get("messages"):
                    continue

                self._write_transcript(export_folder, transcript)
                processed += 1
            except Exception as exc:
                warning = f"Failed to process {candidate.get('path')}: {exc}"
                warnings.append(warning)
                BBLogger.log(warning)

        BBLogger.log(
            f"[SubjectiveLocalchatsDataSource] Exported {processed} chat context files to {export_folder} "
            f"(warnings: {len(warnings)})"
        )

        return None

    def _discover_candidate_files(
        self, additional_paths: List[str], warnings: List[str]
    ) -> List[Dict[str, str]]:
        discovered: List[Dict[str, str]] = []
        seen: set = set()

        for product, kind, pattern in self._candidate_globs(additional_paths):
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

                discovered.append({"product": product, "kind": kind, "path": path})

        discovered.sort(key=lambda item: self._safe_mtime(item["path"]), reverse=True)
        return discovered

    def _candidate_globs(self, additional_paths: List[str]) -> List[Tuple[str, str, str]]:
        home = os.path.expanduser("~")
        patterns: List[Tuple[str, str, str]] = []

        if self.include_codex:
            patterns.extend(
                [
                    ("codex", "cli", os.path.join(home, ".codex", "sessions", "**", "*.jsonl")),
                    ("codex", "cli", os.path.join(home, ".codex", "history.jsonl")),
                    ("codex", "cli", os.path.join(home, ".config", "codex", "**", "*.jsonl")),
                    (
                        "codex",
                        "vscode",
                        os.path.join(home, "AppData", "Roaming", "Code", "User", "globalStorage", "**", "*codex*", "**", "*.json*"),
                    ),
                    (
                        "codex",
                        "vscode",
                        os.path.join(home, ".config", "Code", "User", "globalStorage", "**", "*codex*", "**", "*.json*"),
                    ),
                    (
                        "codex",
                        "vscode",
                        os.path.join(home, "Library", "Application Support", "Code", "User", "globalStorage", "**", "*codex*", "**", "*.json*"),
                    ),
                ]
            )

        if self.include_claude:
            patterns.extend(
                [
                    ("claude", "cli", os.path.join(home, ".claude", "**", "*.jsonl")),
                    ("claude", "cli", os.path.join(home, ".config", "claude", "**", "*.jsonl")),
                    (
                        "claude",
                        "vscode",
                        os.path.join(home, "AppData", "Roaming", "Code", "User", "globalStorage", "**", "*claude*", "**", "*.json*"),
                    ),
                    (
                        "claude",
                        "vscode",
                        os.path.join(home, ".config", "Code", "User", "globalStorage", "**", "*claude*", "**", "*.json*"),
                    ),
                    (
                        "claude",
                        "vscode",
                        os.path.join(home, "Library", "Application Support", "Code", "User", "globalStorage", "**", "*claude*", "**", "*.json*"),
                    ),
                ]
            )

        if self.include_gemini:
            patterns.extend(
                [
                    ("gemini", "cli", os.path.join(home, ".gemini", "tmp", "**", "logs.json")),
                    ("gemini", "cli", os.path.join(home, ".gemini", "tmp", "**", "checkpoint-*.json")),
                    ("gemini", "cli", os.path.join(home, ".gemini", "tmp", "**", "*.jsonl")),
                    ("gemini", "cli", os.path.join(home, ".gemini", "sessions", "**", "*.json*")),
                    ("gemini", "cli", os.path.join(home, ".gemini", "history", "**", "*.json*")),
                    ("gemini", "cli", os.path.join(home, ".config", "gemini", "**", "*.json*")),
                    (
                        "gemini",
                        "vscode",
                        os.path.join(home, "AppData", "Roaming", "Code", "User", "globalStorage", "**", "*gemini*", "**", "*.json*"),
                    ),
                    (
                        "gemini",
                        "vscode",
                        os.path.join(home, ".config", "Code", "User", "globalStorage", "**", "*gemini*", "**", "*.json*"),
                    ),
                    (
                        "gemini",
                        "vscode",
                        os.path.join(home, "Library", "Application Support", "Code", "User", "globalStorage", "**", "*gemini*", "**", "*.json*"),
                    ),
                ]
            )

        for custom_path in additional_paths:
            if not custom_path:
                continue
            expanded = os.path.expandvars(os.path.expanduser(custom_path.strip()))
            if os.path.isdir(expanded):
                patterns.append(("custom", "manual", os.path.join(expanded, "**", "*")))
            else:
                patterns.append(("custom", "manual", expanded))

        return patterns

    def _expand_glob(self, pattern: str) -> List[str]:
        return glob.glob(pattern, recursive=True)

    def _is_supported_file(self, path: str) -> bool:
        return Path(path).suffix.lower() in self.TEXT_EXTENSIONS

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

    def _format_utc_timestamp(self, value: float) -> str:
        if value <= 0:
            return ""
        return datetime.utcfromtimestamp(value).isoformat() + "Z"

    def _write_transcript(self, export_folder: str, transcript: Dict[str, Any]) -> str:
        source_file = transcript.get("source_file") or ""
        chat_ts = float(transcript.get("chat_started_at") or 0.0)
        if chat_ts <= 0:
            chat_ts = self._safe_mtime(source_file)

        enrichment = (
            self._codex_thread_for(source_file)
            if transcript.get("product") == "codex"
            else None
        )

        if enrichment:
            for key in ("created_at_ms", "created_at", "updated_at_ms", "updated_at"):
                ts_val = enrichment.get(key)
                if not ts_val:
                    continue
                ts_float = float(ts_val) / 1000.0 if key.endswith("_ms") else float(ts_val)
                if ts_float > 0:
                    chat_ts = ts_float
                    break

        timestamp_label = self._format_framework_timestamp(chat_ts)
        if not timestamp_label:
            timestamp_label = datetime.now().strftime("%Y_%m_%d_%H_%M_%S")
        digest = hashlib.sha1(source_file.encode("utf-8", errors="ignore")).hexdigest()[:10]
        filename = self.build_context_filename(
            datasource_name=self.get_data_source_type_name(),
            connection_label=self._get_connection_label(),
            output_format="json",
            timestamp=f"{timestamp_label}_{digest}",
        )
        export_path = os.path.join(export_folder, filename)

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
            "source_file": source_file,
            "chat_started_at": chat_ts,
            "chat_started_timestamp": self._format_utc_timestamp(chat_ts),
            "source_mtime": self._safe_mtime(source_file),
            "source_timestamp": self._format_utc_timestamp(self._safe_mtime(source_file)),
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
        if not source_file:
            return None
        index = self._load_codex_thread_index()
        if not index:
            return None
        key = os.path.normcase(os.path.normpath(source_file))
        return index.get(key)

    def _load_codex_thread_index(self) -> Dict[str, Dict[str, Any]]:
        if self._codex_thread_index is not None:
            return self._codex_thread_index

        index: Dict[str, Dict[str, Any]] = {}
        db_path = os.path.join(os.path.expanduser("~"), ".codex", "state_5.sqlite")
        if not os.path.isfile(db_path):
            self._codex_thread_index = index
            return index

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
        return index

    def _format_framework_timestamp(self, value: float) -> str:
        if value <= 0:
            return ""
        return datetime.utcfromtimestamp(value).strftime("%Y_%m_%d_%H_%M_%S")

    def _chat_start_timestamp(self, source_file: str, messages: List[Dict[str, Any]]) -> float:
        for message in messages:
            ts = self._parse_any_timestamp(message.get("timestamp"))
            if ts > 0:
                return ts
        ts = self._parse_timestamp_from_filename(source_file)
        if ts > 0:
            return ts
        try:
            return os.path.getctime(source_file)
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
        return Path(path).stem

    def _pick_first(self, obj: Dict[str, Any], keys: Iterable[str], default: Any = None) -> Any:
        for key in keys:
            if key in obj and obj[key] not in (None, "", []):
                return obj[key]
        return default

    def _safe_mtime(self, path: str) -> float:
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
