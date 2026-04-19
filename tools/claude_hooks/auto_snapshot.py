#!/usr/bin/env python3
"""
Auto-snapshot hook for Claude Code.

Registered as a PreToolUse hook for Edit / Write / NotebookEdit.
Fires BEFORE the edit applies, reads the file's current contents, and copies
them to ~/.claude_auto_snapshots/<encoded-path>/<timestamp>.bak.

The point: when Claude edits a file, the pre-edit state is preserved
automatically, without Claude needing to remember to snapshot first. This
removes the "I don't remember what I changed" failure mode — the filesystem
remembers.

Rollback: tools/claude_hooks/claude_snapshot.py revert <file>

Snapshot depth: unbounded. Prune manually with:
    claude_snapshot.py prune <file> --keep N

Hook is non-blocking: any error here exits 0 so the edit still happens.
"""
import json
import os
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

SNAPSHOT_ROOT = Path.home() / ".claude_auto_snapshots"
ACTIVE_SESSIONS_DIR = Path.home() / ".claude_active_sessions"
MAX_SNAPSHOT_SIZE = 50 * 1024 * 1024  # 50 MB — skip snapshot if file is larger
WATCHED_TOOLS = {"Edit", "Write", "NotebookEdit"}


def is_session_active(session_id) -> bool:
    """Gate: only snapshot for sessions that have an active flag file.

    Default OFF — if ACTIVE_SESSIONS_DIR/<session_id>.flag does not exist,
    the hook is a no-op. This keeps snapshotting scoped to the one chat
    where the user toggled coding mode on, instead of bleeding across
    every simultaneously-running Claude session.
    """
    if not session_id or not isinstance(session_id, str):
        return False
    flag = ACTIVE_SESSIONS_DIR / f"{session_id}.flag"
    return flag.is_file()


def encode_path(abs_path: str) -> str:
    """Encode an absolute file path as a single safe directory name."""
    return abs_path.replace("/", "__").replace("\\", "__").strip("_")


def log_history(snap_dir: Path, entry: dict) -> None:
    """Append a JSONL line to the per-file history."""
    try:
        snap_dir.mkdir(parents=True, exist_ok=True)
        with open(snap_dir / "history.jsonl", "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        # Never let logging failures block the edit.
        pass


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    tool_name = payload.get("tool_name", "")
    if tool_name not in WATCHED_TOOLS:
        sys.exit(0)

    # Use the stable harness session ID (env var), not Claude's per-turn session ID
    session_id = os.environ.get("HARNESS_SESSION_ID") or payload.get("session_id")

    # Gate: if this session hasn't opted in (no flag file), do nothing.
    # Keeps snapshotting scoped per-chat instead of bleeding across all sessions.
    if not is_session_active(session_id):
        sys.exit(0)

    tool_input = payload.get("tool_input", {}) or {}
    file_path = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not file_path:
        sys.exit(0)

    abs_path = os.path.abspath(file_path)
    snap_dir = SNAPSHOT_ROOT / encode_path(abs_path)

    base_entry = {
        "timestamp": datetime.now().isoformat(timespec="microseconds"),
        "unix": time.time(),
        "tool": tool_name,
        "file": abs_path,
        "session_id": session_id,
    }

    # Summarize the intended change so history is grep-friendly.
    if tool_name == "Edit":
        base_entry["edit_summary"] = {
            "old_len": len(tool_input.get("old_string", "")),
            "new_len": len(tool_input.get("new_string", "")),
            "replace_all": bool(tool_input.get("replace_all", False)),
        }
    elif tool_name == "Write":
        base_entry["write_summary"] = {
            "content_len": len(tool_input.get("content", "")),
        }

    # Case 1: file does not yet exist — Write is creating it. Nothing to snapshot.
    if not os.path.isfile(abs_path):
        base_entry["kind"] = "create"
        base_entry["snapshot"] = None
        log_history(snap_dir, base_entry)
        sys.exit(0)

    # Case 2: file too large to snapshot safely.
    try:
        size = os.path.getsize(abs_path)
    except OSError:
        sys.exit(0)
    if size > MAX_SNAPSHOT_SIZE:
        base_entry["kind"] = "too_large"
        base_entry["snapshot"] = None
        base_entry["size"] = size
        log_history(snap_dir, base_entry)
        sys.exit(0)

    # Case 3: normal path — copy pre-edit state.
    ts = datetime.now().strftime("%Y%m%dT%H%M%S_%f")
    ext = os.path.splitext(abs_path)[1] or ""
    snap_name = f"{ts}{ext}.bak"
    snap_path = snap_dir / snap_name

    try:
        snap_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(abs_path, snap_path)
    except Exception as e:
        base_entry["kind"] = "error"
        base_entry["snapshot"] = None
        base_entry["error"] = str(e)
        log_history(snap_dir, base_entry)
        sys.exit(0)

    base_entry["kind"] = "snapshot"
    base_entry["snapshot"] = str(snap_path)
    base_entry["size"] = size
    log_history(snap_dir, base_entry)
    sys.exit(0)


if __name__ == "__main__":
    main()
