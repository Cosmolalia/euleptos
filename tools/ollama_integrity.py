#!/usr/bin/env python3
"""
Ollama Model Integrity Monitor
Checks model blobs for unauthorized modifications.
Uses file size + mtime for large blobs (fast), sha256 for manifests/configs (small).
Alerts on any change.
"""

import os, json, hashlib, time, datetime, sys

MODELS_DIR = "/usr/share/ollama/.ollama/models"
BLOBS_DIR = os.path.join(MODELS_DIR, "blobs")
MANIFESTS_DIR = os.path.join(MODELS_DIR, "manifests")
BASELINE_FILE = os.path.expanduser("~/.ollama_integrity_baseline.json")
ALERT_LOG = os.path.expanduser("~/.ollama_integrity_alerts.jsonl")

def fingerprint_file(path):
    """For large files: size + mtime. For small files (<1MB): sha256."""
    stat = os.stat(path)
    if stat.st_size < 1_000_000:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return {"size": stat.st_size, "mtime": stat.st_mtime, "sha256": h.hexdigest()}
    else:
        return {"size": stat.st_size, "mtime": stat.st_mtime, "sha256": None}

def scan_models():
    """Scan all model files and return fingerprints."""
    result = {}
    for root, dirs, files in os.walk(MODELS_DIR):
        for f in files:
            path = os.path.join(root, f)
            try:
                result[path] = fingerprint_file(path)
            except (PermissionError, OSError):
                result[path] = {"error": "permission_denied"}
    return result

def save_baseline():
    """Save current state as the trusted baseline."""
    state = scan_models()
    state["_timestamp"] = datetime.datetime.utcnow().isoformat() + "Z"
    state["_file_count"] = len([k for k in state if not k.startswith("_")])
    with open(BASELINE_FILE, "w") as f:
        json.dump(state, f, indent=2)
    print(f"Baseline saved: {state['_file_count']} files at {state['_timestamp']}")
    return state

def check_integrity():
    """Compare current state against baseline. Return list of alerts."""
    if not os.path.exists(BASELINE_FILE):
        print("No baseline found. Run with --baseline first.")
        return []

    with open(BASELINE_FILE) as f:
        baseline = json.load(f)

    current = scan_models()
    alerts = []
    now = datetime.datetime.utcnow().isoformat() + "Z"

    # Check for modified files
    for path, cur_fp in current.items():
        if path.startswith("_"):
            continue
        if path not in baseline:
            alerts.append({
                "timestamp": now,
                "type": "NEW_FILE",
                "path": path,
                "details": cur_fp
            })
        elif "error" in cur_fp or "error" in baseline[path]:
            continue
        else:
            base_fp = baseline[path]
            changes = []
            if cur_fp["size"] != base_fp["size"]:
                changes.append(f"size: {base_fp['size']} -> {cur_fp['size']}")
            if cur_fp["mtime"] != base_fp["mtime"]:
                changes.append(f"mtime changed")
            if cur_fp.get("sha256") and base_fp.get("sha256"):
                if cur_fp["sha256"] != base_fp["sha256"]:
                    changes.append(f"SHA256 MISMATCH")
            if changes:
                alerts.append({
                    "timestamp": now,
                    "type": "MODIFIED",
                    "path": path,
                    "changes": changes,
                    "baseline": base_fp,
                    "current": cur_fp
                })

    # Check for deleted files
    for path in baseline:
        if path.startswith("_"):
            continue
        if path not in current:
            alerts.append({
                "timestamp": now,
                "type": "DELETED",
                "path": path,
                "baseline": baseline[path]
            })

    return alerts

def run_check():
    """Run integrity check and log alerts."""
    alerts = check_integrity()
    if not alerts:
        print(f"[{datetime.datetime.utcnow().isoformat()}Z] Ollama integrity OK — no changes detected")
        return 0

    # Log alerts
    os.makedirs(os.path.dirname(ALERT_LOG), exist_ok=True)
    with open(ALERT_LOG, "a") as f:
        for alert in alerts:
            f.write(json.dumps(alert) + "\n")
            severity = "CRITICAL" if alert["type"] == "MODIFIED" and "SHA256 MISMATCH" in alert.get("changes", []) else "WARNING"
            print(f"[{severity}] {alert['type']}: {alert['path']}")
            if "changes" in alert:
                for c in alert["changes"]:
                    print(f"  - {c}")

    print(f"\n{len(alerts)} alert(s) logged to {ALERT_LOG}")
    return len(alerts)

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--baseline":
        save_baseline()
    elif len(sys.argv) > 1 and sys.argv[1] == "--check":
        exit(run_check())
    else:
        print("Usage:")
        print("  python3 ollama_integrity.py --baseline   # Save current state as trusted")
        print("  python3 ollama_integrity.py --check      # Check for modifications")
