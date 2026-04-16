# Subjective Local Chats Data Source

This datasource exports locally stored chat transcripts from developer tools into the Subjective context folder so they can be indexed and used as project context.

## What it collects

The datasource looks for locally executed chat/session logs from tools such as:

- Codex CLI
- Claude CLI
- VS Code plugin or extension storage for Codex- or Claude-style chats
- Additional folders you explicitly provide through datasource connection or request parameters

It is designed for local/offline transcript discovery. It does not call provider APIs to fetch cloud chat history.

## What it produces

When the datasource runs, it:

1. Scans likely local storage locations for chat/session files
2. Parses supported transcript formats such as JSON, JSONL, and plain text logs
3. Extracts readable user/assistant conversation text where possible
4. Writes normalized `.txt` transcript files into the resolved context export folder
5. Writes an `index.json` manifest describing the exported items

## Supported inputs

The implementation uses a best-effort parser and recognizes common patterns such as:

- message arrays
- event streams
- JSONL records
- nested `messages`, `content`, `parts`, or `text` fields
- plain text log files

Because local tool formats vary by version, export quality depends on the structure of the stored files. Unknown or partial formats are skipped rather than failing the whole run.

## Discovery behavior

The datasource combines:

- built-in path guesses for common Codex and Claude local storage locations
- optional extra scan roots passed in connection settings
- optional per-request scan roots

This makes it possible to support custom folders, portable installs, WSL/home-directory layouts, and editor-specific storage paths.

## Output structure

Typical output:

```text
context/
  local_chats/
    codex_cli_2026-04-16_001.txt
    claude_vscode_2026-04-16_002.txt
    index.json
```

Each exported transcript contains:

- source product/kind when detected
- source file path
- session title when available
- normalized conversation text

The manifest includes metadata such as source file, detected product, transcript title, export path, and timestamps when available.

## Typical use cases

- Recover local AI coding conversations that are not visible in the provider web UI
- Bring Codex/Claude local sessions into Subjective context for retrieval
- Preserve project reasoning and implementation history from CLI/editor sessions
- Audit or search earlier assistant suggestions made during local development

## Limitations

- It only exports chats that exist locally on disk and are readable by the current user
- Provider/editor storage formats can change over time
- Some sessions may contain incomplete metadata or fragmented content
- Very large or obviously non-chat files are skipped for safety/performance

## Privacy notes

This datasource is intentionally local-first. It may export sensitive development conversations, prompts, file paths, and code snippets into the Subjective context folder. Review the resulting exports before sharing or syncing that folder.

## Summary

In short, this datasource turns locally stored Codex/Claude-style chat logs into plain text context documents plus a manifest, so Subjective can use them as searchable project memory.
