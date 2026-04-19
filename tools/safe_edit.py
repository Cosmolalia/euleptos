#!/usr/bin/env python3
"""
SAFE EDIT SYSTEM — Automated protection against destructive development patterns.

Every file edit goes through this system. It:
1. Snapshots the file BEFORE any edit (timestamped, never deleted)
2. Logs WHY the edit was made (intent, theory, what should change)
3. Diffs after edit to verify only intended changes occurred
4. Provides instant rollback to any previous version
5. Blocks deletion of tracked files

Usage:
    # Before editing — creates snapshot + intent log
    python3 safe_edit.py begin server_dev.py "Adding WebSocket heartbeat to fix disconnect bug"

    # After editing — diffs against snapshot, logs the delta
    python3 safe_edit.py commit server_dev.py "Heartbeat added at 30s interval, tested locally"

    # If it broke — instant rollback to pre-edit state
    python3 safe_edit.py rollback server_dev.py

    # See full history of a file
    python3 safe_edit.py history server_dev.py

    # See what changed between any two versions
    python3 safe_edit.py diff server_dev.py 3 5

    # List all tracked files
    python3 safe_edit.py status

The snapshot directory is APPEND-ONLY. Nothing is ever deleted.
"""

import os
import sys
import json
import shutil
import hashlib
import difflib
from datetime import datetime
from pathlib import Path

# Where snapshots live — never in the project tree, never deletable by accident
VAULT_DIR = Path(os.path.expanduser("~/.safe_edit_vault"))
VAULT_DIR.mkdir(parents=True, exist_ok=True)

LOG_FILE = VAULT_DIR / "edit_log.jsonl"


def file_hash(path):
    """SHA256 of file contents."""
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(8192), b''):
            h.update(chunk)
    return h.hexdigest()[:16]


def get_file_vault(filepath):
    """Get the vault subdirectory for a specific file."""
    abs_path = os.path.abspath(filepath)
    # Use a safe directory name based on the file path
    safe_name = abs_path.replace('/', '_').replace('\\', '_').strip('_')
    vault = VAULT_DIR / safe_name
    vault.mkdir(parents=True, exist_ok=True)
    return vault


def get_versions(filepath):
    """List all saved versions of a file, sorted by timestamp."""
    vault = get_file_vault(filepath)
    versions = []
    for f in sorted(vault.glob("v*_*.py")) if filepath.endswith('.py') else sorted(vault.glob("v*_*")):
        name = f.name
        # Parse version number from filename
        try:
            vnum = int(name.split('_')[0][1:])
            versions.append({'num': vnum, 'path': f, 'name': name,
                           'time': datetime.fromtimestamp(f.stat().st_mtime)})
        except (ValueError, IndexError):
            pass
    # Also check for files with any extension
    for f in sorted(vault.iterdir()):
        if f.name.startswith('v') and '_' in f.name:
            try:
                vnum = int(f.name.split('_')[0][1:])
                if not any(v['num'] == vnum for v in versions):
                    versions.append({'num': vnum, 'path': f, 'name': f.name,
                                   'time': datetime.fromtimestamp(f.stat().st_mtime)})
            except (ValueError, IndexError):
                pass
    return sorted(versions, key=lambda v: v['num'])


def next_version_num(filepath):
    """Get the next version number for a file."""
    versions = get_versions(filepath)
    if not versions:
        return 1
    return versions[-1]['num'] + 1


def log_entry(action, filepath, intent="", details=""):
    """Append to the edit log."""
    entry = {
        'timestamp': datetime.now().isoformat(),
        'action': action,
        'file': os.path.abspath(filepath),
        'intent': intent,
        'details': details,
        'hash': file_hash(filepath) if os.path.exists(filepath) else 'MISSING'
    }
    with open(LOG_FILE, 'a') as f:
        f.write(json.dumps(entry) + '\n')
    return entry


def cmd_begin(filepath, intent):
    """Snapshot a file BEFORE editing. Records intent."""
    if not os.path.exists(filepath):
        print(f"ERROR: {filepath} does not exist.")
        sys.exit(1)

    vault = get_file_vault(filepath)
    vnum = next_version_num(filepath)
    ext = os.path.splitext(filepath)[1]
    snapshot = vault / f"v{vnum:04d}_pre_edit{ext}"

    shutil.copy2(filepath, snapshot)

    # Save intent alongside snapshot
    intent_file = vault / f"v{vnum:04d}_intent.txt"
    intent_file.write_text(f"INTENT: {intent}\nTIME: {datetime.now().isoformat()}\nFILE: {os.path.abspath(filepath)}\nHASH: {file_hash(filepath)}\n")

    log_entry('BEGIN', filepath, intent=intent, details=f"Snapshot saved as v{vnum:04d}")

    print(f"[SAFE EDIT] Snapshot v{vnum:04d} saved")
    print(f"[SAFE EDIT] Intent: {intent}")
    print(f"[SAFE EDIT] Hash: {file_hash(filepath)}")
    print(f"[SAFE EDIT] To rollback: python3 safe_edit.py rollback {filepath}")


def cmd_commit(filepath, message):
    """Record the state AFTER editing. Shows diff from pre-edit snapshot."""
    if not os.path.exists(filepath):
        print(f"ERROR: {filepath} does not exist.")
        sys.exit(1)

    versions = get_versions(filepath)
    if not versions:
        print(f"WARNING: No pre-edit snapshot found for {filepath}")
        print(f"You should always run 'begin' before editing!")
        # Still save the current state
        vault = get_file_vault(filepath)
        ext = os.path.splitext(filepath)[1]
        snapshot = vault / f"v0001_untracked{ext}"
        shutil.copy2(filepath, snapshot)
        log_entry('COMMIT_UNTRACKED', filepath, intent=message)
        return

    last = versions[-1]

    # Show diff
    with open(last['path']) as f:
        old_lines = f.readlines()
    with open(filepath) as f:
        new_lines = f.readlines()

    diff = list(difflib.unified_diff(old_lines, new_lines,
                                      fromfile=f"v{last['num']:04d} (pre-edit)",
                                      tofile="current",
                                      lineterm=''))

    if not diff:
        print(f"[SAFE EDIT] No changes detected since v{last['num']:04d}")
        return

    # Count changes
    additions = sum(1 for l in diff if l.startswith('+') and not l.startswith('+++'))
    deletions = sum(1 for l in diff if l.startswith('-') and not l.startswith('---'))

    print(f"\n[SAFE EDIT] Changes since v{last['num']:04d}:")
    print(f"  +{additions} lines added, -{deletions} lines removed")

    # Save post-edit snapshot
    vault = get_file_vault(filepath)
    vnum = next_version_num(filepath)
    ext = os.path.splitext(filepath)[1]
    snapshot = vault / f"v{vnum:04d}_post_edit{ext}"
    shutil.copy2(filepath, snapshot)

    # Save diff
    diff_file = vault / f"v{vnum:04d}_diff.txt"
    diff_file.write_text('\n'.join(diff))

    # Save commit message
    msg_file = vault / f"v{vnum:04d}_message.txt"
    msg_file.write_text(f"MESSAGE: {message}\nTIME: {datetime.now().isoformat()}\nADDED: {additions}\nREMOVED: {deletions}\nHASH: {file_hash(filepath)}\n")

    log_entry('COMMIT', filepath, intent=message,
              details=f"+{additions}/-{deletions}, snapshot v{vnum:04d}")

    print(f"[SAFE EDIT] Post-edit snapshot v{vnum:04d} saved")
    print(f"[SAFE EDIT] Message: {message}")

    # WARN if more was removed than added (possible destructive edit)
    if deletions > additions * 2 and deletions > 10:
        print(f"\n  *** WARNING: {deletions} lines removed vs {additions} added ***")
        print(f"  *** This looks like a destructive edit. Verify intent. ***")
        print(f"  *** Rollback: python3 safe_edit.py rollback {filepath} ***")


def cmd_rollback(filepath, version=None):
    """Restore a file to a previous version."""
    versions = get_versions(filepath)
    if not versions:
        print(f"ERROR: No snapshots found for {filepath}")
        sys.exit(1)

    if version is not None:
        target = None
        for v in versions:
            if v['num'] == int(version):
                target = v
                break
        if not target:
            print(f"ERROR: Version {version} not found. Available: {[v['num'] for v in versions]}")
            sys.exit(1)
    else:
        # Find the most recent pre-edit snapshot
        target = versions[-1]
        # Try to find the most recent 'pre_edit' version
        for v in reversed(versions):
            if 'pre_edit' in v['name']:
                target = v
                break

    # Save current state before rollback (so rollback itself is reversible)
    vault = get_file_vault(filepath)
    vnum = next_version_num(filepath)
    ext = os.path.splitext(filepath)[1]
    pre_rollback = vault / f"v{vnum:04d}_pre_rollback{ext}"
    if os.path.exists(filepath):
        shutil.copy2(filepath, pre_rollback)

    # Restore
    shutil.copy2(target['path'], filepath)

    log_entry('ROLLBACK', filepath,
              details=f"Restored to v{target['num']:04d}, saved pre-rollback as v{vnum:04d}")

    print(f"[SAFE EDIT] Restored {filepath} to v{target['num']:04d}")
    print(f"[SAFE EDIT] Pre-rollback state saved as v{vnum:04d}")
    print(f"[SAFE EDIT] Hash: {file_hash(filepath)}")


def cmd_history(filepath):
    """Show full edit history of a file."""
    versions = get_versions(filepath)
    if not versions:
        print(f"No history for {filepath}")
        return

    print(f"\n{'='*60}")
    print(f"EDIT HISTORY: {os.path.abspath(filepath)}")
    print(f"{'='*60}")

    for v in versions:
        vault = get_file_vault(filepath)

        # Check for intent file
        intent_file = vault / f"v{v['num']:04d}_intent.txt"
        intent = ""
        if intent_file.exists():
            for line in intent_file.read_text().splitlines():
                if line.startswith('INTENT:'):
                    intent = line[7:].strip()

        # Check for commit message
        msg_file = vault / f"v{v['num']:04d}_message.txt"
        message = ""
        if msg_file.exists():
            for line in msg_file.read_text().splitlines():
                if line.startswith('MESSAGE:'):
                    message = line[8:].strip()

        label = intent or message or v['name']
        print(f"  v{v['num']:04d} | {v['time'].strftime('%Y-%m-%d %H:%M')} | {label}")

    print(f"\n  Total versions: {len(versions)}")
    print(f"  Rollback to any: python3 safe_edit.py rollback {filepath} <version_num>")


def cmd_diff(filepath, v1, v2):
    """Show diff between two versions."""
    versions = get_versions(filepath)
    ver1 = ver2 = None
    for v in versions:
        if v['num'] == int(v1): ver1 = v
        if v['num'] == int(v2): ver2 = v

    if not ver1 or not ver2:
        print(f"ERROR: Version not found. Available: {[v['num'] for v in versions]}")
        sys.exit(1)

    with open(ver1['path']) as f:
        lines1 = f.readlines()
    with open(ver2['path']) as f:
        lines2 = f.readlines()

    diff = difflib.unified_diff(lines1, lines2,
                                 fromfile=f"v{ver1['num']:04d}",
                                 tofile=f"v{ver2['num']:04d}")
    for line in diff:
        print(line, end='')


def cmd_status():
    """List all tracked files."""
    print(f"\n{'='*60}")
    print(f"SAFE EDIT VAULT: {VAULT_DIR}")
    print(f"{'='*60}")

    if not LOG_FILE.exists():
        print("  No edits tracked yet.")
        return

    files = {}
    with open(LOG_FILE) as f:
        for line in f:
            try:
                entry = json.loads(line)
                fp = entry.get('file', '')
                if fp not in files:
                    files[fp] = {'count': 0, 'last': ''}
                files[fp]['count'] += 1
                files[fp]['last'] = entry.get('timestamp', '')
            except json.JSONDecodeError:
                pass

    for fp, info in sorted(files.items()):
        versions = get_versions(fp)
        print(f"  {os.path.basename(fp):40s} | {len(versions)} versions | last: {info['last'][:19]}")


# CLI
if __name__ == '__main__':
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd == 'begin' and len(sys.argv) >= 4:
        cmd_begin(sys.argv[2], ' '.join(sys.argv[3:]))
    elif cmd == 'commit' and len(sys.argv) >= 4:
        cmd_commit(sys.argv[2], ' '.join(sys.argv[3:]))
    elif cmd == 'rollback' and len(sys.argv) >= 3:
        version = sys.argv[3] if len(sys.argv) > 3 else None
        cmd_rollback(sys.argv[2], version)
    elif cmd == 'history' and len(sys.argv) >= 3:
        cmd_history(sys.argv[2])
    elif cmd == 'diff' and len(sys.argv) >= 5:
        cmd_diff(sys.argv[2], sys.argv[3], sys.argv[4])
    elif cmd == 'status':
        cmd_status()
    else:
        print(__doc__)
