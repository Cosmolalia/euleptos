"""
System Tool — Structured system operations.

Rather than raw shell commands, this gives the agent clean, logged actions
for common system tasks. Everything returns structured data.

Config in tools_config.json:
{
    "system": {
        "enabled": true,
        "allowed_commands": ["ls", "df", "ps", "uptime", "curl"],
        "blocked_paths": ["/etc/shadow", "/root"],
        "max_output_chars": 50000
    }
}
"""

import os
import subprocess
import shutil
import platform
from pathlib import Path
from datetime import datetime

from . import BaseTool


class SystemTool(BaseTool):
    name = "system"
    description = (
        "Structured system operations — disk, processes, health checks, "
        "HTTP requests, file operations. Provides clean interfaces to common "
        "admin tasks with structured return data."
    )
    actions = {
        "disk": "Check disk usage. Params: path (default '/')",
        "processes": "List running processes. Params: filter (optional grep string), limit (default 20)",
        "uptime": "System uptime and load. No params.",
        "http": "Make an HTTP request. Params: url, method (default GET), headers (optional dict), body (optional)",
        "env": "Get environment variable. Params: name",
        "file_info": "Get file/directory info. Params: path",
        "ports": "List listening network ports. Params: filter (optional)",
        "health": "Run a quick health check on the harness. No params.",
    }

    def execute(self, action: str, params: dict = None) -> dict:
        params = params or {}
        dispatch = {
            "disk": self._disk,
            "processes": self._processes,
            "uptime": self._uptime,
            "http": self._http,
            "env": self._env,
            "file_info": self._file_info,
            "ports": self._ports,
            "health": self._health,
        }
        handler = dispatch.get(action)
        if not handler:
            return {"ok": False, "error": f"Unknown action: {action}"}
        try:
            return handler(params)
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _disk(self, params: dict) -> dict:
        path = params.get("path", "/")
        usage = shutil.disk_usage(path)
        return {
            "ok": True,
            "path": path,
            "total_gb": round(usage.total / (1024**3), 2),
            "used_gb": round(usage.used / (1024**3), 2),
            "free_gb": round(usage.free / (1024**3), 2),
            "percent_used": round(usage.used / usage.total * 100, 1),
        }

    def _processes(self, params: dict) -> dict:
        limit = params.get("limit", 20)
        filter_str = params.get("filter")

        result = subprocess.run(
            ["ps", "aux", "--sort=-%mem"],
            capture_output=True, text=True, timeout=10
        )
        lines = result.stdout.strip().split("\n")
        header = lines[0] if lines else ""
        processes = lines[1:] if len(lines) > 1 else []

        if filter_str:
            processes = [p for p in processes if filter_str.lower() in p.lower()]

        return {
            "ok": True,
            "header": header,
            "processes": processes[:limit],
            "total_shown": min(len(processes), limit),
            "total_matched": len(processes),
        }

    def _uptime(self, params: dict) -> dict:
        result = subprocess.run(["uptime"], capture_output=True, text=True, timeout=5)
        return {
            "ok": True,
            "uptime": result.stdout.strip(),
            "platform": platform.platform(),
            "python": platform.python_version(),
        }

    def _http(self, params: dict) -> dict:
        url = params.get("url")
        if not url:
            return {"ok": False, "error": "Missing required param: 'url'"}

        method = params.get("method", "GET").upper()
        headers = params.get("headers", {})
        body = params.get("body")

        cmd = ["curl", "-s", "-w", "\n%{http_code}", "-X", method]
        for k, v in headers.items():
            cmd.extend(["-H", f"{k}: {v}"])
        if body:
            cmd.extend(["-d", body])
        cmd.append(url)

        max_chars = self.config.get("max_output_chars", 50000)

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            output = result.stdout
            lines = output.rsplit("\n", 1)
            response_body = lines[0] if len(lines) > 1 else output
            status_code = int(lines[-1]) if len(lines) > 1 and lines[-1].isdigit() else None

            truncated = len(response_body) > max_chars
            response_body = response_body[:max_chars]

            return {
                "ok": True,
                "status_code": status_code,
                "body": response_body,
                "truncated": truncated,
                "method": method,
                "url": url,
            }
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": f"HTTP request timed out: {url}"}

    def _env(self, params: dict) -> dict:
        name = params.get("name")
        if not name:
            return {"ok": False, "error": "Missing required param: 'name'"}

        # Block sensitive env vars
        blocked = {"password", "secret", "token", "key", "credential"}
        if any(b in name.lower() for b in blocked):
            return {"ok": False, "error": f"Blocked: env var '{name}' may contain secrets"}

        value = os.environ.get(name)
        return {"ok": True, "name": name, "value": value, "exists": value is not None}

    def _file_info(self, params: dict) -> dict:
        path_str = params.get("path")
        if not path_str:
            return {"ok": False, "error": "Missing required param: 'path'"}

        # Block sensitive paths
        blocked = self.config.get("blocked_paths", ["/etc/shadow", "/root"])
        for bp in blocked:
            if path_str.startswith(bp):
                return {"ok": False, "error": f"Blocked path: {path_str}"}

        path = Path(path_str)
        if not path.exists():
            return {"ok": False, "error": f"Path does not exist: {path_str}"}

        stat = path.stat()
        info = {
            "ok": True,
            "path": str(path.resolve()),
            "exists": True,
            "is_file": path.is_file(),
            "is_dir": path.is_dir(),
            "size_bytes": stat.st_size,
            "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            "created": datetime.fromtimestamp(stat.st_ctime).isoformat(),
        }

        if path.is_dir():
            try:
                contents = list(path.iterdir())
                info["item_count"] = len(contents)
                info["items"] = [{"name": p.name, "is_dir": p.is_dir()} for p in sorted(contents)[:50]]
            except PermissionError:
                info["item_count"] = -1
                info["error"] = "Permission denied listing directory"

        return info

    def _ports(self, params: dict) -> dict:
        filter_str = params.get("filter")

        result = subprocess.run(
            ["ss", "-tlnp"],
            capture_output=True, text=True, timeout=10
        )
        lines = result.stdout.strip().split("\n")
        header = lines[0] if lines else ""
        ports = lines[1:] if len(lines) > 1 else []

        if filter_str:
            ports = [p for p in ports if filter_str in p]

        return {"ok": True, "header": header, "ports": ports}

    def _health(self, params: dict) -> dict:
        """Quick health check for the harness system."""
        checks = {}

        # Check server processes
        result = subprocess.run(
            ["pgrep", "-f", "uvicorn.*server"],
            capture_output=True, text=True, timeout=5
        )
        pids = result.stdout.strip().split("\n") if result.stdout.strip() else []
        checks["server_pids"] = pids
        checks["servers_running"] = len(pids)

        # Check disk
        usage = shutil.disk_usage("/")
        checks["disk_percent"] = round(usage.used / usage.total * 100, 1)
        checks["disk_free_gb"] = round(usage.free / (1024**3), 2)

        # Check data dirs
        for name in ["data", "data_dev"]:
            p = Path(__file__).parent.parent / name
            if p.exists():
                result = subprocess.run(
                    ["du", "-sh", str(p)],
                    capture_output=True, text=True, timeout=10
                )
                checks[f"{name}_size"] = result.stdout.strip().split("\t")[0] if result.stdout else "?"

        # Overall health
        healthy = checks["servers_running"] > 0 and checks["disk_percent"] < 90
        checks["healthy"] = healthy

        return {"ok": True, "checks": checks}
