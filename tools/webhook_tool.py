"""
Webhook Tool — Inbound event receiver and notification queue.

Exposes a catch-all webhook endpoint on the harness. External services
(Twilio, email forwarding, Stripe, GitHub, custom scripts) POST events here.
Events go into a queue the agent can read on wake-up.

This tool doesn't register a FastAPI route itself — the harness wires
/api/webhooks/incoming into this tool's receive() method.

Config in tools_config.json:
{
    "webhook": {
        "enabled": true,
        "secret": "optional-shared-secret-for-verification",
        "max_queue_size": 500,
        "auto_ack": false
    }
}
"""

import json
import hashlib
import hmac
from pathlib import Path
from datetime import datetime
from typing import Optional

from . import BaseTool


class WebhookTool(BaseTool):
    name = "webhook"
    description = (
        "Inbound webhook receiver. External services POST events here. "
        "Events are queued for the agent to read, process, and acknowledge. "
        "Use this for receiving SMS replies, email notifications, CI/CD events, "
        "or any callback-based integration."
    )
    actions = {
        "queue": "Read queued events. Params: limit (default 20), source (optional filter), unread_only (bool, default true)",
        "ack": "Acknowledge/dismiss an event. Params: event_id",
        "ack_all": "Acknowledge all events. Params: source (optional — ack only from this source)",
        "count": "Count queued events. Params: source (optional), unread_only (bool, default true)",
        "sources": "List all event sources seen. No params.",
        "clear": "Clear old acknowledged events. Params: older_than_hours (default 24)",
    }

    def __init__(self, config: dict = None):
        super().__init__(config)
        self._queue_path: Optional[Path] = None
        self._events: list[dict] = []

    def _ensure_queue(self):
        """Load event queue from disk."""
        if self._queue_path is None:
            # Will be set by harness integration
            from pathlib import Path
            self._queue_path = Path("data_dev/agent/webhook_events.jsonl")

        if not self._events and self._queue_path.exists():
            try:
                with open(self._queue_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            self._events.append(json.loads(line))
            except Exception:
                pass

    def _save_queue(self):
        """Persist event queue to disk."""
        if self._queue_path:
            self._queue_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._queue_path, "w", encoding="utf-8") as f:
                for event in self._events:
                    f.write(json.dumps(event, default=str) + "\n")

    def receive(self, source: str, payload: dict, headers: dict = None) -> dict:
        """
        Called by the harness when an external webhook hits /api/webhooks/incoming.
        NOT called by the agent — this is the inbound receiver.
        """
        self._ensure_queue()

        # Optional signature verification
        secret = self.config.get("secret")
        if secret and headers:
            sig_header = headers.get("x-webhook-signature") or headers.get("x-hub-signature-256")
            if sig_header:
                body_str = json.dumps(payload, sort_keys=True)
                expected = hmac.new(secret.encode(), body_str.encode(), hashlib.sha256).hexdigest()
                if not hmac.compare_digest(sig_header, f"sha256={expected}"):
                    return {"ok": False, "error": "Invalid webhook signature"}

        event = {
            "id": hashlib.sha256(f"{datetime.now().isoformat()}{source}{json.dumps(payload, default=str)}".encode()).hexdigest()[:16],
            "source": source,
            "payload": payload,
            "received_at": datetime.now().isoformat(),
            "acknowledged": False,
        }

        self._events.append(event)

        # Trim queue
        max_size = self.config.get("max_queue_size", 500)
        if len(self._events) > max_size:
            # Remove oldest acknowledged events first
            acked = [e for e in self._events if e.get("acknowledged")]
            if acked:
                self._events = [e for e in self._events if not e.get("acknowledged")] + acked[-(max_size // 2):]

        self._save_queue()
        return {"ok": True, "event_id": event["id"]}

    def execute(self, action: str, params: dict = None) -> dict:
        params = params or {}
        self._ensure_queue()

        if action == "queue":
            return self._get_queue(params)
        elif action == "ack":
            return self._ack(params)
        elif action == "ack_all":
            return self._ack_all(params)
        elif action == "count":
            return self._count(params)
        elif action == "sources":
            return self._sources()
        elif action == "clear":
            return self._clear(params)

        return {"ok": False, "error": f"Unknown action: {action}"}

    def _get_queue(self, params: dict) -> dict:
        limit = params.get("limit", 20)
        source = params.get("source")
        unread_only = params.get("unread_only", True)

        events = self._events
        if source:
            events = [e for e in events if e.get("source") == source]
        if unread_only:
            events = [e for e in events if not e.get("acknowledged")]

        # Newest first
        events = list(reversed(events[-limit:]))

        return {"ok": True, "events": events, "total": len(events)}

    def _ack(self, params: dict) -> dict:
        event_id = params.get("event_id")
        if not event_id:
            return {"ok": False, "error": "Missing required param: 'event_id'"}

        for event in self._events:
            if event["id"] == event_id:
                event["acknowledged"] = True
                event["acked_at"] = datetime.now().isoformat()
                self._save_queue()
                return {"ok": True, "event_id": event_id}

        return {"ok": False, "error": f"Event '{event_id}' not found"}

    def _ack_all(self, params: dict) -> dict:
        source = params.get("source")
        count = 0
        now = datetime.now().isoformat()

        for event in self._events:
            if not event.get("acknowledged"):
                if source is None or event.get("source") == source:
                    event["acknowledged"] = True
                    event["acked_at"] = now
                    count += 1

        self._save_queue()
        return {"ok": True, "acknowledged": count}

    def _count(self, params: dict) -> dict:
        source = params.get("source")
        unread_only = params.get("unread_only", True)

        events = self._events
        if source:
            events = [e for e in events if e.get("source") == source]
        if unread_only:
            events = [e for e in events if not e.get("acknowledged")]

        return {"ok": True, "count": len(events)}

    def _sources(self) -> dict:
        sources = {}
        for event in self._events:
            src = event.get("source", "unknown")
            sources.setdefault(src, {"total": 0, "unread": 0})
            sources[src]["total"] += 1
            if not event.get("acknowledged"):
                sources[src]["unread"] += 1

        return {"ok": True, "sources": sources}

    def _clear(self, params: dict) -> dict:
        hours = params.get("older_than_hours", 24)
        cutoff = datetime.now().timestamp() - (hours * 3600)
        before = len(self._events)

        self._events = [
            e for e in self._events
            if not e.get("acknowledged") or
               datetime.fromisoformat(e.get("acked_at", e["received_at"])).timestamp() > cutoff
        ]

        removed = before - len(self._events)
        self._save_queue()
        return {"ok": True, "removed": removed, "remaining": len(self._events)}
