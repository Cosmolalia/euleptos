#!/usr/bin/env python3
"""
claude-snapshot: browse and revert auto-snapshotted file edits.

Snapshots are written automatically by tools/claude_hooks/auto_snapshot.py
(registered as a PreToolUse hook in ~/.claude/settings.json). Each edit to a
file produces one snapshot of its pre-edit state under:

    ~/.claude_auto_snapshots/<encoded-abs-path>/<timestamp>.bak

Usage:
    claude_snapshot.py list <file>
        Show snapshot history (newest first).

    claude_snapshot.py revert <file> [--steps N]
        Restore the Nth-most-recent snapshot (default N=1, i.e. undo last edit).
        The current file state is snapshotted first, so the revert itself is
        reversible.

    claude_snapshot.py diff <file> [--steps N]
        Show unified diff between current file and Nth-most-recent snapshot.

    claude_snapshot.py prune <file> --keep N
        Delete all but the N most recent snapshots for this file.

    claude_snapshot.py sessions
        Show recent snapshot activity grouped by session_id.

    claude_snapshot.py status
        List all files with snapshot history and the count of snapshots each.
"""
import argparse
import difflib
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

SNAPSHOT_ROOT = Path.home() / ".claude_auto_snapshots"


def encode_path(abs_path: str) -> str:
    return abs_path.replace("/", "__").replace("\\", "__").strip("_")


def snap_dir_for(file_path: str) -> Path:
    return SNAPSHOT_ROOT / encode_path(os.path.abspath(file_path))


def list_snapshots(file_path: str):
    """Return [(path, mtime), ...] sorted newest-first."""
    d = snap_dir_for(file_path)
    if not d.is_dir():
        return []
    snaps = [p for p in d.iterdir() if p.name.endswith(".bak")]
    snaps.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return snaps


def read_history(file_path: str):
    d = snap_dir_for(file_path)
    hist = d / "history.jsonl"
    if not hist.is_file():
        return []
    entries = []
    with open(hist) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return entries


def cmd_list(args):
    snaps = list_snapshots(args.file)
    if not snaps:
        print(f"No snapshots for {args.file}")
        return
    history = {e.get("snapshot"): e for e in read_history(args.file) if e.get("snapshot")}
    print(f"\n{len(snaps)} snapshot(s) for {os.path.abspath(args.file)}:\n")
    for i, p in enumerate(snaps, start=1):
        mtime = datetime.fromtimestamp(p.stat().st_mtime)
        size = p.stat().st_size
        entry = history.get(str(p), {})
        tool = entry.get("tool", "?")
        sid = entry.get("session_id", "?")
        sid_short = sid[:8] if isinstance(sid, str) else "?"
        summary_bits = []
        if "edit_summary" in entry:
            es = entry["edit_summary"]
            summary_bits.append(f"edit -{es['old_len']}/+{es['new_len']}")
            if es.get("replace_all"):
                summary_bits.append("replace_all")
        elif "write_summary" in entry:
            summary_bits.append(f"write {entry['write_summary']['content_len']}b")
        summary = " ".join(summary_bits) or ""
        print(
            f"  [{i:>3}] {mtime.strftime('%Y-%m-%d %H:%M:%S')}  "
            f"{size:>9}b  {tool:<12} session:{sid_short}  {summary}"
        )
    print(f"\n  Revert to most recent: claude_snapshot.py revert {args.file}")


def cmd_revert(args):
    snaps = list_snapshots(args.file)
    if not snaps:
        print(f"ERROR: no snapshots for {args.file}", file=sys.stderr)
        sys.exit(1)
    steps = max(1, args.steps)
    if steps > len(snaps):
        print(
            f"ERROR: requested step {steps} but only {len(snaps)} snapshot(s) exist",
            file=sys.stderr,
        )
        sys.exit(1)
    target = snaps[steps - 1]

    target_file = os.path.abspath(args.file)

    # Snapshot current state first so the revert itself is reversible.
    if os.path.isfile(target_file):
        d = snap_dir_for(target_file)
        d.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%dT%H%M%S_%f")
        ext = os.path.splitext(target_file)[1] or ""
        pre_revert = d / f"{ts}{ext}.pre_revert.bak"
        shutil.copy2(target_file, pre_revert)
        with open(d / "history.jsonl", "a") as f:
            f.write(
                json.dumps(
                    {
                        "timestamp": datetime.now().isoformat(timespec="microseconds"),
                        "tool": "claude_snapshot.revert",
                        "file": target_file,
                        "kind": "pre_revert_snapshot",
                        "snapshot": str(pre_revert),
                        "reverting_to": str(target),
                    }
                )
                + "\n"
            )

    shutil.copy2(target, target_file)
    print(f"Reverted {target_file}")
    print(f"  from snapshot: {target.name}")
    print(f"  (step {steps} of {len(snaps)})")


def cmd_diff(args):
    snaps = list_snapshots(args.file)
    if not snaps:
        print(f"No snapshots for {args.file}", file=sys.stderr)
        sys.exit(1)
    steps = max(1, args.steps)
    if steps > len(snaps):
        print(f"ERROR: only {len(snaps)} snapshot(s) exist", file=sys.stderr)
        sys.exit(1)
    target = snaps[steps - 1]

    target_file = os.path.abspath(args.file)
    if not os.path.isfile(target_file):
        print(f"Current file {target_file} does not exist", file=sys.stderr)
        sys.exit(1)

    try:
        with open(target) as f:
            old = f.readlines()
        with open(target_file) as f:
            new = f.readlines()
    except UnicodeDecodeError:
        print("(binary file — cannot diff textually)")
        return

    diff = difflib.unified_diff(
        old,
        new,
        fromfile=f"snapshot:{target.name}",
        tofile=f"current:{target_file}",
    )
    sys.stdout.writelines(diff)


def cmd_prune(args):
    snaps = list_snapshots(args.file)
    keep = max(0, args.keep)
    if len(snaps) <= keep:
        print(f"Only {len(snaps)} snapshot(s); nothing to prune.")
        return
    to_delete = snaps[keep:]
    for p in to_delete:
        try:
            p.unlink()
        except OSError:
            pass
    print(f"Pruned {len(to_delete)} snapshot(s); kept {keep} most recent.")


def cmd_sessions(args):
    if not SNAPSHOT_ROOT.is_dir():
        print("No snapshots yet.")
        return
    by_session = {}
    for sub in SNAPSHOT_ROOT.iterdir():
        hist = sub / "history.jsonl"
        if not hist.is_file():
            continue
        with open(hist) as f:
            for line in f:
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                sid = e.get("session_id") or "unknown"
                by_session.setdefault(sid, []).append(e)
    if not by_session:
        print("No session activity logged.")
        return
    for sid, entries in sorted(by_session.items(), key=lambda kv: kv[1][-1].get("unix", 0), reverse=True):
        files = {}
        for e in entries:
            files.setdefault(e.get("file", "?"), 0)
            files[e.get("file", "?")] += 1
        sid_show = sid[:8] if sid != "unknown" else sid
        last = entries[-1].get("timestamp", "?")
        print(f"\nsession {sid_show}  last:{last}  edits:{len(entries)}")
        for fp, n in sorted(files.items(), key=lambda kv: -kv[1]):
            print(f"  {n:>4} × {fp}")


def cmd_status(args):
    if not SNAPSHOT_ROOT.is_dir():
        print("No snapshots yet.")
        return
    rows = []
    for sub in SNAPSHOT_ROOT.iterdir():
        if not sub.is_dir():
            continue
        snaps = [p for p in sub.iterdir() if p.name.endswith(".bak")]
        if not snaps:
            continue
        snaps.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        newest = datetime.fromtimestamp(snaps[0].stat().st_mtime)
        # Recover original path from history if available
        orig = None
        hist = sub / "history.jsonl"
        if hist.is_file():
            with open(hist) as f:
                for line in f:
                    try:
                        e = json.loads(line)
                        if e.get("file"):
                            orig = e["file"]
                            break
                    except json.JSONDecodeError:
                        pass
        total_size = sum(p.stat().st_size for p in snaps)
        rows.append((newest, len(snaps), total_size, orig or sub.name))
    rows.sort(reverse=True)
    print(f"\n{'last edit':<20}  {'snaps':>5}  {'size':>10}  file")
    print("-" * 80)
    for newest, count, size, fp in rows:
        size_h = f"{size/1024:.1f}K" if size < 1_048_576 else f"{size/1_048_576:.1f}M"
        print(f"{newest.strftime('%Y-%m-%d %H:%M:%S'):<20}  {count:>5}  {size_h:>10}  {fp}")


def main():
    parser = argparse.ArgumentParser(description="Browse and revert Claude auto-snapshots.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="Show snapshot history for a file")
    p_list.add_argument("file")
    p_list.set_defaults(func=cmd_list)

    p_rev = sub.add_parser("revert", help="Restore a snapshot")
    p_rev.add_argument("file")
    p_rev.add_argument("--steps", type=int, default=1, help="N-th most recent (default 1)")
    p_rev.set_defaults(func=cmd_revert)

    p_diff = sub.add_parser("diff", help="Diff current file against a snapshot")
    p_diff.add_argument("file")
    p_diff.add_argument("--steps", type=int, default=1)
    p_diff.set_defaults(func=cmd_diff)

    p_prune = sub.add_parser("prune", help="Keep only the N most recent snapshots")
    p_prune.add_argument("file")
    p_prune.add_argument("--keep", type=int, required=True)
    p_prune.set_defaults(func=cmd_prune)

    p_sess = sub.add_parser("sessions", help="Group edits by session_id")
    p_sess.set_defaults(func=cmd_sessions)

    p_stat = sub.add_parser("status", help="List all tracked files")
    p_stat.set_defaults(func=cmd_status)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
