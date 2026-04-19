"""
Agent Tool Registry — self-describing, config-driven, fully logged.

Each tool is a Python class inheriting from BaseTool. The registry auto-discovers
tools in this package, manages per-tool config/credentials, and provides a unified
execute() interface that the agent scheduler calls.

Architecture preference: minimal ceremony, maximum introspection. Every tool call
gets logged. Every tool can describe its own capabilities at runtime. Config lives
in one JSON file so the human can edit it with a text editor.
"""

import json
import importlib
import pkgutil
from pathlib import Path
from datetime import datetime
from typing import Any, Optional

# ── Base Tool ────────────────────────────────────────────────

class BaseTool:
    """
    Base class for all agent tools.

    Subclass this and define:
      - name: str           — unique tool identifier (e.g. "email")
      - description: str    — what this tool does (shown to agent)
      - actions: dict       — {action_name: description} of available operations
      - execute(action, params) — do the thing, return result dict
    """
    name: str = "base"
    description: str = "Base tool — do not use directly"
    actions: dict = {}

    def __init__(self, config: dict = None):
        self.config = config or {}
        self._enabled = self.config.get("enabled", True)

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool):
        self._enabled = value

    def execute(self, action: str, params: dict = None) -> dict:
        """Execute an action. Override in subclass."""
        return {"error": f"Tool '{self.name}' does not implement execute()"}

    def describe(self) -> dict:
        """Self-description for the agent to understand capabilities."""
        return {
            "name": self.name,
            "description": self.description,
            "enabled": self.enabled,
            "actions": self.actions,
            "configured": self.is_configured(),
        }

    def is_configured(self) -> bool:
        """Override to check if required credentials/config are present."""
        return True

    def validate_action(self, action: str) -> Optional[str]:
        """Return error string if action is invalid, None if ok."""
        if action not in self.actions:
            available = ", ".join(self.actions.keys()) if self.actions else "none"
            return f"Unknown action '{action}' for tool '{self.name}'. Available: {available}"
        return None


# ── Tool Registry ────────────────────────────────────────────

class ToolRegistry:
    """
    Discovers, configures, and manages all agent tools.

    Config file layout (tools_config.json):
    {
        "email": {"enabled": true, "smtp_host": "...", ...},
        "browser": {"enabled": true, ...},
        ...
    }
    """

    def __init__(self, config_path: Path, log_path: Path):
        self.config_path = config_path
        self.log_path = log_path
        self._tools: dict[str, BaseTool] = {}
        self._config: dict = {}
        self._load_config()
        self._discover_tools()

    def _load_config(self):
        """Load tool configs from disk."""
        if self.config_path.exists():
            try:
                self._config = json.loads(self.config_path.read_text(encoding="utf-8"))
            except Exception:
                self._config = {}
        else:
            self._config = {}

    def _save_config(self):
        """Persist tool configs to disk."""
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(
            json.dumps(self._config, indent=2, default=str),
            encoding="utf-8"
        )

    def _discover_tools(self):
        """Auto-discover all tool modules in this package."""
        package_dir = Path(__file__).parent
        for _, module_name, _ in pkgutil.iter_modules([str(package_dir)]):
            if module_name.startswith("_"):
                continue
            try:
                module = importlib.import_module(f".{module_name}", package="tools")
                # Look for classes that inherit from BaseTool
                for attr_name in dir(module):
                    attr = getattr(module, attr_name)
                    if (isinstance(attr, type)
                        and issubclass(attr, BaseTool)
                        and attr is not BaseTool):
                        tool_config = self._config.get(attr.name, {})
                        instance = attr(config=tool_config)
                        self._tools[instance.name] = instance
            except Exception as e:
                print(f"[TOOLS] Error loading tool module '{module_name}': {e}", flush=True)

    def execute(self, tool_name: str, action: str, params: dict = None) -> dict:
        """
        Execute a tool action. Returns result dict with at minimum:
          {"ok": bool, ...result data or error...}

        Every call is logged to disk.
        """
        start = datetime.now()
        params = params or {}

        # Resolve tool
        tool = self._tools.get(tool_name)
        if not tool:
            result = {"ok": False, "error": f"Tool '{tool_name}' not found. Available: {list(self._tools.keys())}"}
            self._log(tool_name, action, params, result, start)
            return result

        if not tool.enabled:
            result = {"ok": False, "error": f"Tool '{tool_name}' is disabled. Enable it in tools config."}
            self._log(tool_name, action, params, result, start)
            return result

        # Validate action
        err = tool.validate_action(action)
        if err:
            result = {"ok": False, "error": err}
            self._log(tool_name, action, params, result, start)
            return result

        # Execute
        try:
            result = tool.execute(action, params)
            if "ok" not in result:
                result["ok"] = True
        except Exception as e:
            result = {"ok": False, "error": str(e), "exception": type(e).__name__}

        self._log(tool_name, action, params, result, start)
        return result

    def _log(self, tool_name: str, action: str, params: dict, result: dict, start: datetime):
        """Append execution log entry."""
        duration = (datetime.now() - start).total_seconds()
        # Sanitize params — never log raw passwords/tokens
        safe_params = {k: ("***" if any(s in k.lower() for s in ["password", "token", "secret", "key"]) else v)
                       for k, v in (params or {}).items()}

        entry = {
            "timestamp": start.isoformat(),
            "tool": tool_name,
            "action": action,
            "params": safe_params,
            "ok": result.get("ok", False),
            "duration": round(duration, 3),
            "error": result.get("error"),
            "result_preview": str(result)[:500] if result.get("ok") else None,
        }
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        except Exception as e:
            print(f"[TOOLS] Log write error: {e}", flush=True)

    def list_tools(self) -> list[dict]:
        """Return descriptions of all registered tools."""
        return [tool.describe() for tool in self._tools.values()]

    def get_tool(self, name: str) -> Optional[BaseTool]:
        """Get a tool instance by name."""
        return self._tools.get(name)

    def update_tool_config(self, tool_name: str, updates: dict):
        """Update config for a specific tool and reinitialize it."""
        self._config.setdefault(tool_name, {}).update(updates)
        self._save_config()
        # Reinitialize the tool with new config
        tool = self._tools.get(tool_name)
        if tool:
            tool.config = self._config[tool_name]
            tool._enabled = tool.config.get("enabled", True)

    def get_config(self) -> dict:
        """Return full tool config (with secrets masked)."""
        masked = {}
        for tool_name, cfg in self._config.items():
            masked[tool_name] = {}
            for k, v in cfg.items():
                if any(s in k.lower() for s in ["password", "token", "secret", "key"]):
                    masked[tool_name][k] = "***" if v else None
                else:
                    masked[tool_name][k] = v
        return masked

    def get_agent_tool_description(self) -> str:
        """
        Generate a text block the agent reads on wake-up so it knows what tools
        it has and how to call them. This is my preference — the agent should
        always know exactly what it can do, not guess.
        """
        lines = ["[AVAILABLE TOOLS]"]
        for tool in self._tools.values():
            status = "ENABLED" if tool.enabled else "DISABLED"
            configured = "configured" if tool.is_configured() else "NOT CONFIGURED"
            lines.append(f"\n## {tool.name} ({status}, {configured})")
            lines.append(f"   {tool.description}")
            if tool.actions:
                lines.append("   Actions:")
                for action_name, action_desc in tool.actions.items():
                    lines.append(f"     - {action_name}: {action_desc}")
        lines.append("\n[/AVAILABLE TOOLS]")
        lines.append("")
        lines.append("To use a tool, write a JSON block in your response like:")
        lines.append('```tool_call')
        lines.append('{"tool": "email", "action": "send", "params": {"to": "...", "subject": "...", "body": "..."}}')
        lines.append('```')
        lines.append("The harness will intercept this, execute the tool, and inject the result.")
        return "\n".join(lines)

    def get_log(self, limit: int = 50) -> list[dict]:
        """Return recent tool execution log entries."""
        entries = []
        if self.log_path.exists():
            with open(self.log_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            entries.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass
        return list(reversed(entries[-limit:]))
