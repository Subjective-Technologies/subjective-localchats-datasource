#!/usr/bin/env python3
import os
import hashlib
import json
from datetime import datetime
from pathlib import Path
import difflib

# File extensions we want to track
INCLUDE_EXTS = {
    ".py", ".yml", ".yaml", ".json", ".js", ".ts",
    ".java", ".c", ".cpp", ".h", ".html", ".css"
}

# Directories to ignore (you can add more as needed)
IGNORE_DIRS = {
    ".git", ".snapshots", ".venv", "venv", "env", "__pycache__", "build", "dist", "node_modules","myenv"
}

SNAPSHOT_DIR = ".snapshots"
BASELINE_FILE = os.path.join(SNAPSHOT_DIR, "baseline.json")


def list_files():
    """Return all relevant code files recursively from current folder, ignoring env/build dirs."""
    files = []
    for root, dirs, filenames in os.walk("."):
        # remove ignored dirs from traversal
        dirs[:] = [d for d in dirs if d not in IGNORE_DIRS]

        for fn in filenames:
            ext = Path(fn).suffix.lower()
            if ext in INCLUDE_EXTS:
                files.append(os.path.join(root, fn))
    return sorted(files)


def file_hash(path):
    """Return sha1 hash of a file’s contents."""
    h = hashlib.sha1()
    with open(path, "rb") as f:
        while chunk := f.read(8192):
            h.update(chunk)
    return h.hexdigest()


def snapshot_name():
    return f"snapshot-{datetime.now().strftime('%Y%m%d%H%M%S')}.patch"


def generate_patch(old_content, new_content, fpath):
    """Return unified diff string for one file."""
    diff = difflib.unified_diff(
        old_content.splitlines(),
        new_content.splitlines(),
        fromfile=f"{fpath} (old)",
        tofile=f"{fpath} (new)",
        lineterm=""
    )
    return "\n".join(diff)


def main():
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)

    # Load baseline hashes
    if os.path.exists(BASELINE_FILE):
        with open(BASELINE_FILE, "r") as f:
            baseline = json.load(f)
    else:
        baseline = {}

    current = {}
    patch_chunks = []

    for fpath in list_files():
        h = file_hash(fpath)
        current[fpath] = h
        if baseline.get(fpath) != h:
            old_content = ""
            if fpath in baseline:
                # we don’t store old content, so just mark difference
                # (could load from a versioned backup if desired)
                with open(fpath, "r", encoding="utf-8", errors="ignore") as fin:
                    new_content = fin.read()
                patch_chunks.append(generate_patch("", new_content, fpath))
            else:
                # new file
                with open(fpath, "r", encoding="utf-8", errors="ignore") as fin:
                    new_content = fin.read()
                patch_chunks.append(generate_patch("", new_content, fpath))

    deleted = [f for f in baseline if f not in current]
    for fpath in deleted:
        patch_chunks.append(generate_patch("deleted file", "", fpath))

    if not baseline:
        # First snapshot: dump full contents as /dev/null → file
        patch_file = os.path.join(SNAPSHOT_DIR, snapshot_name())
        with open(patch_file, "w", encoding="utf-8") as out:
            for f in current:
                with open(f, "r", encoding="utf-8", errors="ignore") as fin:
                    content = fin.read()
                out.write(generate_patch("", content, f))
                out.write("\n")
        print(f"Initial snapshot created: {patch_file}")
    elif patch_chunks:
        patch_file = os.path.join(SNAPSHOT_DIR, snapshot_name())
        with open(patch_file, "w", encoding="utf-8") as out:
            out.write("\n".join(patch_chunks))
        print(f"Patch created: {patch_file}")
    else:
        print("No changes detected.")

    # Save updated baseline
    with open(BASELINE_FILE, "w") as f:
        json.dump(current, f, indent=2)


if __name__ == "__main__":
    main()
