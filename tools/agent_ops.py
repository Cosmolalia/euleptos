"""
Agent Operations Tool — Autonomous backend infrastructure.

This is the agent's own operations center. File watching, change tracking,
research indexing, report generation, system digests. Everything an
autonomous agent needs to stay aware between wake-up cycles.

The key insight: the agent doesn't just check if things are running —
it tracks what changed, indexes research files, generates summaries,
and maintains situational awareness across sessions.

Config in tools_config.json:
{
    "agent_ops": {
        "enabled": true,
        "watch_paths": ["."],
        "reports_dir": "data_dev/agent/reports",
        "watch_state_file": "data_dev/agent/watch_state.json",
        "research_index_file": "data_dev/agent/research_index.json",
        "max_report_age_days": 30
    }
}
"""

import json
import hashlib
import os
import subprocess
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional

from . import BaseTool


def _hash_file(path: Path) -> Optional[str]:
    """Quick SHA256 of a file's contents."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except Exception:
        return None


def _file_meta(path: Path) -> dict:
    """Get file metadata without reading full contents."""
    try:
        stat = path.stat()
        return {
            "path": str(path),
            "size": stat.st_size,
            "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            "hash": _hash_file(path),
        }
    except Exception:
        return {"path": str(path), "error": "inaccessible"}


# Research file extensions we care about
RESEARCH_EXTENSIONS = {
    ".py", ".tex", ".md", ".txt", ".json", ".csv",
    ".pdf", ".ipynb", ".html", ".log", ".dat", ".toml", ".yaml", ".yml",
}

# Directories to skip
SKIP_DIRS = {
    "__pycache__", ".git", "node_modules", ".venv", "venv",
    ".mypy_cache", ".pytest_cache", "dist", "build", ".tox",
}


class AgentOpsTool(BaseTool):
    name = "agent_ops"
    description = (
        "Autonomous agent operations — file watching, change tracking, "
        "research indexing, report generation, and system digests. "
        "This is the agent's situational awareness engine."
    )
    actions = {
        "scan": "Scan watched directories for changes since last check. Params: paths (optional list, uses config default)",
        "digest": "Generate a full system digest — health + changes + research status. No params.",
        "report": "Write a report to disk. Params: title, content, tags (optional list)",
        "reports": "List stored reports. Params: limit (default 20), tag (optional filter)",
        "read_report": "Read a stored report. Params: filename",
        "index_research": "Scan and index all TOE research files. Params: root (optional, default ~/Desktop/TOE)",
        "research_status": "Get current research index summary. No params.",
        "diff_since": "Show git changes since a date or commit. Params: since (date string or commit hash), path (optional)",
        "snapshot": "Take a point-in-time system snapshot (processes, ports, disk, servers). No params.",
    }

    def __init__(self, config: dict = None):
        super().__init__(config)
        self._reports_dir = Path(self.config.get("reports_dir", "data_dev/agent/reports"))
        self._watch_state_file = Path(self.config.get("watch_state_file", "data_dev/agent/watch_state.json"))
        self._research_index_file = Path(self.config.get("research_index_file", "data_dev/agent/research_index.json"))

    def is_configured(self) -> bool:
        return True  # No external credentials needed

    def execute(self, action: str, params: dict = None) -> dict:
        params = params or {}
        dispatch = {
            "scan": self._scan,
            "digest": self._digest,
            "report": self._write_report,
            "reports": self._list_reports,
            "read_report": self._read_report,
            "index_research": self._index_research,
            "research_status": self._research_status,
            "diff_since": self._diff_since,
            "snapshot": self._snapshot,
        }
        handler = dispatch.get(action)
        if not handler:
            return {"ok": False, "error": f"Unknown action: {action}"}
        try:
            return handler(params)
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── File Watching ────────────────────────────────────────────

    def _load_watch_state(self) -> dict:
        """Load previous scan state from disk."""
        if self._watch_state_file.exists():
            try:
                return json.loads(self._watch_state_file.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {"files": {}, "last_scan": None}

    def _save_watch_state(self, state: dict):
        """Persist scan state to disk."""
        self._watch_state_file.parent.mkdir(parents=True, exist_ok=True)
        self._watch_state_file.write_text(
            json.dumps(state, indent=2, default=str),
            encoding="utf-8"
        )

    def _scan_directory(self, root: Path) -> dict:
        """Recursively scan a directory, returning file metadata."""
        files = {}
        if not root.exists():
            return files

        for dirpath, dirnames, filenames in os.walk(root):
            # Prune skipped directories
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]

            for fname in filenames:
                fpath = Path(dirpath) / fname
                if fpath.suffix.lower() in RESEARCH_EXTENSIONS or fname in {
                    "Makefile", "Dockerfile", "requirements.txt", "pyproject.toml",
                }:
                    meta = _file_meta(fpath)
                    files[str(fpath)] = meta
        return files

    def _scan(self, params: dict) -> dict:
        """Scan watched paths and diff against last known state."""
        watch_paths = params.get("paths") or self.config.get(
            "watch_paths", ["."]
        )

        prev_state = self._load_watch_state()
        prev_files = prev_state.get("files", {})
        last_scan = prev_state.get("last_scan")

        # Scan all paths
        current_files = {}
        for wp in watch_paths:
            root = Path(wp).expanduser()
            current_files.update(self._scan_directory(root))

        # Diff
        prev_keys = set(prev_files.keys())
        curr_keys = set(current_files.keys())

        added = curr_keys - prev_keys
        removed = prev_keys - curr_keys
        modified = set()

        for key in prev_keys & curr_keys:
            old_hash = prev_files[key].get("hash")
            new_hash = current_files[key].get("hash")
            if old_hash and new_hash and old_hash != new_hash:
                modified.add(key)

        # Save new state
        new_state = {
            "files": current_files,
            "last_scan": datetime.now().isoformat(),
            "total_files": len(current_files),
        }
        self._save_watch_state(new_state)

        changes = {
            "added": [{"path": p, **current_files[p]} for p in sorted(added)],
            "removed": [{"path": p} for p in sorted(removed)],
            "modified": [{"path": p, **current_files[p]} for p in sorted(modified)],
        }
        total_changes = len(added) + len(removed) + len(modified)

        return {
            "ok": True,
            "last_scan": last_scan,
            "current_scan": new_state["last_scan"],
            "total_files_tracked": len(current_files),
            "total_changes": total_changes,
            "changes": changes,
            "first_scan": last_scan is None,
        }

    # ── Reports ──────────────────────────────────────────────────

    def _write_report(self, params: dict) -> dict:
        """Write a report to disk."""
        title = params.get("title")
        content = params.get("content")
        tags = params.get("tags", [])

        if not title or not content:
            return {"ok": False, "error": "Missing required params: 'title', 'content'"}

        self._reports_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        slug = title.lower().replace(" ", "_")[:40]
        filename = f"{timestamp}_{slug}.md"
        filepath = self._reports_dir / filename

        report = f"""---
title: {title}
date: {datetime.now().isoformat()}
tags: {json.dumps(tags)}
---

# {title}

{content}
"""
        filepath.write_text(report, encoding="utf-8")

        return {
            "ok": True,
            "filename": filename,
            "path": str(filepath),
            "title": title,
        }

    def _list_reports(self, params: dict) -> dict:
        """List stored reports."""
        limit = params.get("limit", 20)
        tag_filter = params.get("tag")

        if not self._reports_dir.exists():
            return {"ok": True, "reports": [], "total": 0}

        reports = []
        for f in sorted(self._reports_dir.glob("*.md"), reverse=True):
            try:
                text = f.read_text(encoding="utf-8")
                # Parse frontmatter
                if text.startswith("---"):
                    end = text.index("---", 3)
                    front = text[3:end].strip()
                    meta = {}
                    for line in front.split("\n"):
                        if ":" in line:
                            k, v = line.split(":", 1)
                            meta[k.strip()] = v.strip()

                    if tag_filter:
                        tags = json.loads(meta.get("tags", "[]"))
                        if tag_filter not in tags:
                            continue

                    reports.append({
                        "filename": f.name,
                        "title": meta.get("title", f.stem),
                        "date": meta.get("date", ""),
                        "tags": json.loads(meta.get("tags", "[]")),
                        "size": f.stat().st_size,
                    })
            except Exception:
                reports.append({"filename": f.name, "title": f.stem, "error": "parse_failed"})

        return {"ok": True, "reports": reports[:limit], "total": len(reports)}

    def _read_report(self, params: dict) -> dict:
        """Read a stored report."""
        filename = params.get("filename")
        if not filename:
            return {"ok": False, "error": "Missing required param: 'filename'"}

        filepath = self._reports_dir / filename
        if not filepath.exists():
            return {"ok": False, "error": f"Report not found: {filename}"}

        content = filepath.read_text(encoding="utf-8")
        return {"ok": True, "filename": filename, "content": content[:20000]}

    # ── Research Index ───────────────────────────────────────────

    def _index_research(self, params: dict) -> dict:
        """Scan and index all TOE research files."""
        root = Path(params.get("root", ".")).expanduser()

        if not root.exists():
            return {"ok": False, "error": f"Root path does not exist: {root}"}

        index = {
            "root": str(root),
            "indexed_at": datetime.now().isoformat(),
            "categories": {},
            "files": [],
            "stats": {"total_files": 0, "total_size": 0, "by_extension": {}},
        }

        # Categorize by directory structure
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            rel_dir = str(Path(dirpath).relative_to(root))

            for fname in filenames:
                fpath = Path(dirpath) / fname
                ext = fpath.suffix.lower()
                if ext not in RESEARCH_EXTENSIONS and fname not in {
                    "Makefile", "Dockerfile", "requirements.txt",
                }:
                    continue

                try:
                    stat = fpath.stat()
                    entry = {
                        "path": str(fpath),
                        "relative": str(fpath.relative_to(root)),
                        "name": fname,
                        "ext": ext,
                        "size": stat.st_size,
                        "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                        "category": rel_dir.split("/")[0] if "/" in rel_dir or rel_dir != "." else "root",
                    }
                    index["files"].append(entry)
                    index["stats"]["total_files"] += 1
                    index["stats"]["total_size"] += stat.st_size
                    index["stats"]["by_extension"][ext] = index["stats"]["by_extension"].get(ext, 0) + 1

                    # Group by category
                    cat = entry["category"]
                    if cat not in index["categories"]:
                        index["categories"][cat] = {"count": 0, "size": 0, "files": []}
                    index["categories"][cat]["count"] += 1
                    index["categories"][cat]["size"] += stat.st_size
                    index["categories"][cat]["files"].append(entry["relative"])
                except Exception:
                    continue

        # Save index
        self._research_index_file.parent.mkdir(parents=True, exist_ok=True)
        self._research_index_file.write_text(
            json.dumps(index, indent=2, default=str),
            encoding="utf-8"
        )

        # Summary without full file list
        summary = {
            "ok": True,
            "root": str(root),
            "total_files": index["stats"]["total_files"],
            "total_size_mb": round(index["stats"]["total_size"] / (1024 * 1024), 2),
            "by_extension": index["stats"]["by_extension"],
            "categories": {
                k: {"count": v["count"], "size_kb": round(v["size"] / 1024, 1)}
                for k, v in index["categories"].items()
            },
            "index_path": str(self._research_index_file),
        }

        return summary

    def _research_status(self, params: dict) -> dict:
        """Get current research index summary."""
        if not self._research_index_file.exists():
            return {"ok": True, "indexed": False, "message": "No research index yet. Run index_research first."}

        try:
            index = json.loads(self._research_index_file.read_text(encoding="utf-8"))
            return {
                "ok": True,
                "indexed": True,
                "indexed_at": index.get("indexed_at"),
                "total_files": index["stats"]["total_files"],
                "total_size_mb": round(index["stats"]["total_size"] / (1024 * 1024), 2),
                "by_extension": index["stats"]["by_extension"],
                "categories": {
                    k: {"count": v["count"]}
                    for k, v in index["categories"].items()
                },
            }
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── Git Integration ──────────────────────────────────────────

    def _diff_since(self, params: dict) -> dict:
        """Show git changes since a date or commit."""
        since = params.get("since")
        path = params.get("path", ".")

        if not since:
            return {"ok": False, "error": "Missing required param: 'since' (date or commit hash)"}

        try:
            # Try as a date first
            cmd = ["git", "log", "--oneline", f"--since={since}", "--", path]
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=10,
                cwd=str(Path(__file__).parent.parent)
            )
            commits = result.stdout.strip().split("\n") if result.stdout.strip() else []

            # Get diffstat
            diff_cmd = ["git", "diff", f"--stat", f"HEAD~{max(len(commits), 1)}", "--", path]
            diff_result = subprocess.run(
                diff_cmd, capture_output=True, text=True, timeout=10,
                cwd=str(Path(__file__).parent.parent)
            )

            return {
                "ok": True,
                "since": since,
                "commits": commits[:50],
                "commit_count": len(commits),
                "diffstat": diff_result.stdout.strip()[:5000] if diff_result.stdout else "",
            }
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── System Snapshot ──────────────────────────────────────────

    def _snapshot(self, params: dict) -> dict:
        """Take a comprehensive system snapshot."""
        snapshot = {
            "timestamp": datetime.now().isoformat(),
            "checks": {},
        }

        # Listening ports (primary health signal)
        harness_ports = []
        try:
            result = subprocess.run(
                ["ss", "-tlnp"],
                capture_output=True, text=True, timeout=5
            )
            lines = result.stdout.strip().split("\n")[1:]  # skip header
            harness_ports = [l for l in lines if ":8080" in l or ":8081" in l]
            snapshot["checks"]["ports"] = {
                "harness_ports": harness_ports,
                "total_listening": len(lines),
            }
        except Exception:
            snapshot["checks"]["ports"] = {"error": "ss failed"}

        # Server processes (secondary — try multiple patterns)
        try:
            procs = []
            for pattern in ["uvicorn.*server", "python.*server.py", "python3.*server"]:
                result = subprocess.run(
                    ["pgrep", "-af", pattern],
                    capture_output=True, text=True, timeout=5
                )
                if result.stdout.strip():
                    procs.extend([l for l in result.stdout.strip().split("\n") if l and "pgrep" not in l])
            # Deduplicate by PID
            seen_pids = set()
            unique_procs = []
            for p in procs:
                pid = p.split()[0] if p else ""
                if pid and pid not in seen_pids:
                    seen_pids.add(pid)
                    unique_procs.append(p)
            snapshot["checks"]["servers"] = {
                "count": len(unique_procs),
                "processes": unique_procs,
            }
        except Exception:
            snapshot["checks"]["servers"] = {"count": 0, "error": "pgrep failed"}

        # Disk
        try:
            import shutil
            usage = shutil.disk_usage("/")
            snapshot["checks"]["disk"] = {
                "percent_used": round(usage.used / usage.total * 100, 1),
                "free_gb": round(usage.free / (1024**3), 2),
            }
        except Exception:
            snapshot["checks"]["disk"] = {"error": "disk check failed"}

        # Data directories
        harness_root = Path(__file__).parent.parent
        for name in ["data", "data_dev"]:
            p = harness_root / name
            if p.exists():
                try:
                    result = subprocess.run(
                        ["du", "-sh", str(p)],
                        capture_output=True, text=True, timeout=10
                    )
                    snapshot["checks"][f"{name}_size"] = result.stdout.strip().split("\t")[0]
                except Exception:
                    snapshot["checks"][f"{name}_size"] = "?"

        # Git status
        try:
            result = subprocess.run(
                ["git", "status", "--porcelain"],
                capture_output=True, text=True, timeout=5,
                cwd=str(harness_root)
            )
            changes = [l for l in result.stdout.strip().split("\n") if l]
            snapshot["checks"]["git"] = {
                "uncommitted_changes": len(changes),
                "files": changes[:20],
            }
        except Exception:
            snapshot["checks"]["git"] = {"error": "git failed"}

        # Memory usage
        try:
            result = subprocess.run(
                ["free", "-h"],
                capture_output=True, text=True, timeout=5
            )
            lines = result.stdout.strip().split("\n")
            if len(lines) >= 2:
                snapshot["checks"]["memory"] = lines[1]
        except Exception:
            pass

        # Overall health — ports listening is the primary signal
        ports_up = len(harness_ports) > 0
        servers_up = snapshot["checks"].get("servers", {}).get("count", 0) > 0
        disk_ok = snapshot["checks"].get("disk", {}).get("percent_used", 100) < 90
        snapshot["healthy"] = (ports_up or servers_up) and disk_ok

        return {"ok": True, "snapshot": snapshot}

    # ── Digest ───────────────────────────────────────────────────

    def _digest(self, params: dict) -> dict:
        """Generate a full system digest combining all checks."""
        digest = {
            "timestamp": datetime.now().isoformat(),
            "sections": {},
        }

        # 1. System snapshot
        snap = self._snapshot({})
        digest["sections"]["system"] = snap.get("snapshot", {}).get("checks", {})
        digest["healthy"] = snap.get("snapshot", {}).get("healthy", False)

        # 2. File changes
        scan = self._scan({})
        digest["sections"]["file_changes"] = {
            "total_changes": scan.get("total_changes", 0),
            "added": len(scan.get("changes", {}).get("added", [])),
            "modified": len(scan.get("changes", {}).get("modified", [])),
            "removed": len(scan.get("changes", {}).get("removed", [])),
            "first_scan": scan.get("first_scan", False),
            "details": scan.get("changes", {}),
        }

        # 3. Research index status
        research = self._research_status({})
        digest["sections"]["research"] = research

        # 4. Recent reports
        reports = self._list_reports({"limit": 5})
        digest["sections"]["recent_reports"] = reports.get("reports", [])

        return {"ok": True, "digest": digest}
