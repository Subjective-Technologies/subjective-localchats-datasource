# Subjective Local Chats Data Source

This datasource finds **coding-assistant chat transcripts stored on your machine** (from CLIs and editors), normalizes them into a single shape, and writes **one JSON context file per chat** into your Subjective context folder so pipelines and retrieval can use them as project memory.

It is **local-only**: it reads files and local databases the tools already created. It does **not** call OpenAI, Anthropic, Google, or any other cloud API to download history.

## What it does

1. **Discovers** candidate chat logs across a set of known paths for popular tools (see below), plus any extra folders you configure.
2. **Parses** JSON, JSONL, or plain text using flexible heuristics (most vendors don't share one schema).
3. **Normalizes** each session into a list of messages with `role`, optional `timestamp`, and `text`.
4. **Exports** each session as a structured JSON document (metadata + `messages`) in the resolved context directory, using the framework’s context filename rules.

Connection toggles let you turn sources on or off: **Codex**, **Claude**, **Gemini**, **Cursor**, plus **additional paths** for anything else you want scanned. You can also add other readable user home folders so the same built-in discovery rules run against more than the current account.

## How it works for most LLM / IDE tools

Coding assistants generally store conversations in one of three ways. This plugin is built around that reality:

### 1. Log files (JSON / JSONL / text)

Many tools (CLI sessions, editor global storage, scratch logs) write **files** under the user profile or project folders: `.json`, `.jsonl`, `.md`, `.txt`, `.log`. Structures vary: arrays of turns, nested `messages`, `content`, `parts`, `events`, request/response pairs, etc.

The datasource uses a **single generic extractor** that walks those objects and pulls out human-readable text wherever it can recognize common patterns (message lists, content arrays, prompt/completion fields, and similar). That is why **one implementation can cover multiple vendors**: it targets the *shape* of stored chats, not one official API per model.

### 2. VS Code–style `globalStorage`

Extensions often persist state under paths like:

`…/User/globalStorage/**/<vendor>/**/*.json`

The same glob-based discovery applies: if a file looks like chat JSON, it goes through the same parser.

### 3. Cursor-specific: SQLite (`state.vscdb`)

**Cursor Composer** stores sessions in a local SQLite DB (`User/globalStorage/state.vscdb`, table `cursorDiskKV`):

- `composerData:*` rows hold session metadata.
- `bubbleId:{sessionId}:*` rows hold per-message “bubbles.”

The datasource reads those rows, maps bubble types to user/assistant roles, orders by timestamps, and emits the **same normalized JSON** as file-based chats. It also picks up **agent transcript JSONL** under `~/.cursor/projects/**/agent-transcripts/` when Cursor is enabled.

### 4. Codex-specific enrichment

For **Codex** sessions tied to files on disk, the datasource can optionally merge metadata from Codex’s local SQLite thread index (`~/.codex/state_5.sqlite`) so titles and timestamps align with Codex’s own view of the session.

### Why “most LLMs” and not literally every tool

The plugin does **not** rely on each model having a public “export chats” API. It relies on **whatever the client app already saved locally**. Any new tool that writes similar JSON/JSONL or ends up under an extra path you add can be picked up without code changes, as long as the on-disk format is parseable by the generic extractor.

Formats change between app versions; parsing is **best-effort**. Bad or empty extractions are skipped so one broken file does not fail the entire run.

## What it collects (built-in sources)

| Source   | Typical locations (simplified) |
|----------|---------------------------------|
| **Codex** | CLI under `~/.codex`, VS Code `globalStorage` areas with Codex-related paths |
| **Claude** | CLI under `~/.claude` / config, VS Code `globalStorage` for Claude-related extensions |
| **Gemini** | CLI under `~/.gemini`, VS Code `globalStorage` for Gemini-related paths |
| **Cursor** | Composer: `Cursor/User/globalStorage/state.vscdb` (and remote-SSH layout under `~/.cursor-server` where applicable); agent JSONL under `~/.cursor/projects/**/agent-transcripts/` |

Paths differ slightly on **Windows** (`%APPDATA%`), **Linux** (`~/.config`), and **macOS** (`Library/Application Support`). The implementation includes those variants.

Use **Additional Paths** in the connection or request to include portable installs, WSL home directories, or any other folder that holds compatible logs.

## Scanning other local users

By default, the datasource scans the current user home plus other readable user homes discovered under common roots. To target specific local accounts, set **Other User Homes** to a comma-separated list of usernames or home paths, for example:

`gordon, subjective, /home/gordon, /media/goldenthinker/Windows/Users/subjective`

When you provide a username such as `gordon`, the datasource looks for a readable home directory under common roots including `/home`, `/Users`, `/mnt/*/Users`, and mounted Windows `Users` folders under `/media`.

**Scan Accessible User Homes** controls the automatic broader scan. When enabled, it enumerates readable home directories under those same common roots and applies the built-in Codex, Claude, Gemini, and Cursor path discovery to each one. Unreadable or missing homes are skipped with warnings.

Exports include `source_user` and `source_home` fields so transcripts from different local accounts can be distinguished.

## Output

Each successful chat becomes **one `.json` file** in the chosen context folder (optionally under an export subfolder). The payload includes at least:

- `type`: `"localchat"`
- `title` / `chat_name`
- `product`, `kind` (e.g. cli, vscode, composer, agent)
- `source_file` (for Cursor Composer this may be a virtual id like `…state.vscdb#composer:{uuid}`)
- Timing fields where available
- `messages`: array of `{ role, timestamp, text }`

There is no separate mandatory `index.json` from this datasource; Subjective’s pipeline uses the exported context files themselves.

### Incremental exports, continued chats, and idempotency

The datasource keeps a small manifest named `.subjective_localchats_manifest.json` in the export folder. On each run, it compares each source chat's fingerprint against the previous run and skips sources that are unchanged and already have an exported JSON file.

New chats and changed chats are parsed and written. When a chat **continues** (new lines in JSONL, new bubbles in Cursor, etc.), the backing file or database changes, so the next export contains the **updated** conversation.

Export filenames are tied to a **stable session identity**: normalized start time (from transcript metadata, Codex thread enrichment, or the first message timestamp) plus a short hash of `source_file`. They **do not** use “now” or the source file’s modification time in the filename, so the same logical chat **overwrites the same context JSON** on each run instead of creating duplicates. If no start time can be inferred, the human-readable part of the filename is the fixed prefix `session` plus the same hash—still one file per source.

The JSON field `source_mtime` / `source_timestamp` still reflects the file’s current modification time when relevant, so you can see when the backing store last changed.

To force a full rebuild, delete `.subjective_localchats_manifest.json` from the export folder and run the datasource again.

## Connection options (summary)

- **Include Codex / Claude / Gemini / Cursor**: enable or disable each family of built-in paths.
- **Additional Paths**: comma-separated extra files or directories to scan.
- **Other User Homes**: comma-separated usernames or home folder paths to scan using the built-in source layouts.
- **Scan Accessible User Homes**: enabled by default; enumerates readable local user homes under common roots.
- **Context export subfolder / filename prefix**: organize output under the resolved context root.
- **Max files / max messages per chat**: guardrails for very large machines or huge sessions.

## Typical use cases

- Bring **local** IDE and CLI chats into Subjective context for search and retrieval.
- Preserve reasoning and code discussion that never appears in a web “chat history” UI.
- Audit what the assistant suggested in past sessions on this machine.

## Limitations

- Only data that exists **locally** and is **readable** by the current process/user.
- Storage layouts **change with app updates**; some sessions may export partially or not at all.
- Very large files are skipped; Cursor DB rows with missing JSON are skipped.
- Cursor must not exclusively lock SQLite while reading (best-effort; warnings if open fails).

## Privacy

Exports may contain prompts, code, paths, and secrets from your chats. Treat the context folder like sensitive project data.

## Summary

**Subjective Local Chats** turns **on-disk chat artifacts** from common AI coding tools into **normalized JSON transcripts** for Subjective. It works across **multiple LLM backends** because it follows how **clients** store sessions (files and local DBs), not because each provider exposes a single standard API.
