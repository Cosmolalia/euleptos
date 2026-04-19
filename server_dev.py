#!/usr/bin/env python3
"""
Claude Harness — Custom Claude Code client with rolling context (clip, never compact).

Architecture:
- Wraps `claude -p --resume --output-format stream-json` for each turn
- Rolling context window: keeps last ~140k tokens, clips oldest messages
- Artifact store: code blocks live outside context, referenced by ID
- Full conversation saved to disk permanently
- HTML frontend with chat, artifacts panel, project files
"""

import os
import sys
import json
import time
import uuid
import base64
import logging

# File-based logging so errors are always capturable
_log_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "server_dev.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(_log_file, encoding="utf-8"),
        logging.StreamHandler(sys.stderr),
    ]
)
_logger = logging.getLogger("harness-dev")
import hashlib
import hmac
import subprocess
import threading
import asyncio
import re
import queue
import math
import tempfile
import shutil
try:
    import fcntl  # POSIX only
    _HAS_FCNTL = True
except ImportError:
    fcntl = None
    _HAS_FCNTL = False
from pathlib import Path
from datetime import datetime
from collections import Counter
from io import BytesIO
from concurrent.futures import ThreadPoolExecutor

# Fix Windows cp1252 encoding — emoji and Unicode must not crash prints
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Import relay client from home directory
sys.path.insert(0, str(Path.home()))
try:
    from relay_client import RelayClient, mailbox_unread, mailbox_count, mailbox_search, mailbox_store, mailbox_mark_read
    RELAY_AVAILABLE = True
except ImportError:
    print("[WARN] relay_client.py not found or empty — relay features disabled", flush=True)
    RELAY_AVAILABLE = False
    class RelayClient:
        def __init__(self, *a, **kw): pass
        def status(self): return {"error": "relay not available"}
        def history(self, **kw): return []
        def send(self, *a, **kw): return {"error": "relay not available"}
        def recv(self, **kw): return []
        def channels(self): return []
    def mailbox_unread(): return []
    def mailbox_count(): return 0
    def mailbox_search(q): return []
    def mailbox_store(msgs): pass
    def mailbox_mark_read(ids): pass

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, UploadFile, File, Form, Request, Response, Cookie
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, RedirectResponse
import uvicorn
import secrets

# ══════════════════════════════════════════════════════════════
# STT (Speech-to-Text) via faster-whisper
# ══════════════════════════════════════════════════════════════

_stt_model = None
_stt_executor = ThreadPoolExecutor(max_workers=2)
_stt_available = False

def _load_stt_model():
    """Load Whisper model on CPU (int8 for speed). Non-blocking — called from startup."""
    global _stt_model, _stt_available
    model_name = os.environ.get("WHISPER_MODEL", "base")
    try:
        from faster_whisper import WhisperModel
        print(f"[STT] Loading Whisper '{model_name}' model on CPU...", flush=True)
        t0 = time.time()
        _stt_model = WhisperModel(model_name, device="cpu", compute_type="int8")
        _stt_available = True
        print(f"[STT] Whisper loaded in {time.time()-t0:.1f}s", flush=True)
    except ImportError:
        print("[STT] faster-whisper not installed — STT disabled", flush=True)
    except Exception as e:
        print(f"[STT] Failed to load Whisper: {e}", flush=True)

def _transcribe_audio(audio_bytes: bytes) -> str:
    """Transcribe audio bytes (any format) to text. Runs in thread pool."""
    if not _stt_model:
        return ""
    tmp = None
    try:
        # Detect format from magic bytes for correct extension
        suffix = ".webm"
        if audio_bytes[:4] == b'\x00\x00\x00\x1c' or audio_bytes[:4] == b'\x00\x00\x00\x18' or audio_bytes[4:8] == b'ftyp':
            suffix = ".mp4"  # Safari/iOS sends mp4/m4a
        elif audio_bytes[:4] == b'OggS':
            suffix = ".ogg"
        elif audio_bytes[:4] == b'RIFF':
            suffix = ".wav"
        tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
        tmp.write(audio_bytes)
        tmp.close()
        segments, info = _stt_model.transcribe(
            tmp.name, beam_size=1, language="en",
            vad_filter=True, vad_parameters=dict(min_silence_duration_ms=300)
        )
        text = " ".join(s.text.strip() for s in segments).strip()
        return text
    except Exception as e:
        print(f"[STT] Transcription error: {e}", flush=True)
        return ""
    finally:
        if tmp:
            try: os.unlink(tmp.name)
            except: pass

# ══════════════════════════════════════════════════════════════
# ══════════════════════════════════════════════════════════════
# Config
# ══════════════════════════════════════════════════════════════

DATA_DIR = Path(os.environ.get("HARNESS_DATA_DIR", Path(__file__).parent / "data"))
SESSIONS_DIR = Path(__file__).parent / "data" / "sessions"  # Fixed path — shared across all server instances
ARTIFACTS_DIR = Path(__file__).parent / "data" / "artifacts"  # Fixed path — artifacts are tied to sessions
PROJECTS_DIR = DATA_DIR / "projects"
STATIC_DIR = Path(__file__).parent / "static"

USERS_DIR = Path(__file__).parent / "data" / "users"  # Fixed path — shared across all server instances
LOGS_DIR = DATA_DIR / "logs"
AGENT_DIR = Path(__file__).parent / "data" / "agent"  # Fixed path — one brain, never splits with DATA_DIR
TOOLS_CONFIG_PATH = AGENT_DIR / "tools_config.json"
TOOLS_LOG_PATH = AGENT_DIR / "tools_log.jsonl"
PROCESS_REGISTRY_PATH = DATA_DIR / "process_registry.json"

DATA_DIR.mkdir(exist_ok=True)
SESSIONS_DIR.mkdir(exist_ok=True)
ARTIFACTS_DIR.mkdir(exist_ok=True)
PROJECTS_DIR.mkdir(exist_ok=True)
USERS_DIR.mkdir(exist_ok=True)
LOGS_DIR.mkdir(exist_ok=True)
AGENT_DIR.mkdir(exist_ok=True)

# ── Protected Process Registry ───────────────────────────────
# Shared registry so Claude instances don't kill each other's processes.
# Every long-running job (training, experiments, builds) gets registered here.
# Every instance sees the registry in its context before making kill decisions.

def _load_process_registry():
    """Load the process registry, cleaning out dead processes."""
    if not PROCESS_REGISTRY_PATH.exists():
        return []
    try:
        entries = json.loads(PROCESS_REGISTRY_PATH.read_text())
        # Clean dead processes automatically
        alive = []
        for entry in entries:
            pid = entry.get("pid")
            if pid:
                try:
                    os.kill(pid, 0)  # signal 0 = check if alive
                    alive.append(entry)
                except (ProcessLookupError, PermissionError):
                    pass  # process is dead, remove from registry
            else:
                alive.append(entry)  # entries without PID are manual notes
        if len(alive) != len(entries):
            PROCESS_REGISTRY_PATH.write_text(json.dumps(alive, indent=2))
        return alive
    except Exception:
        return []

def _register_process(pid, description, owner="unknown", category="general", locked=False):
    """Register a process as protected. All instances will see it before killing anything."""
    entries = _load_process_registry()
    # Don't duplicate — but update if exists
    for e in entries:
        if e.get("pid") == pid:
            e["description"] = description
            e["owner"] = owner
            e["locked"] = locked
            PROCESS_REGISTRY_PATH.write_text(json.dumps(entries, indent=2))
            return
    entries.append({
        "pid": pid,
        "description": description,
        "owner": owner,
        "category": category,
        "locked": locked,
        "registered_at": datetime.now().isoformat(),
    })
    PROCESS_REGISTRY_PATH.write_text(json.dumps(entries, indent=2))

def _deregister_process(pid):
    """Remove a process from the registry (completed or intentionally stopped)."""
    entries = _load_process_registry()
    entries = [e for e in entries if e.get("pid") != pid]
    PROCESS_REGISTRY_PATH.write_text(json.dumps(entries, indent=2))

INSTANCE_NOTES_PATH = DATA_DIR / "instance_notes.json"

def _load_instance_notes():
    """Load instance notes, cleaning expired ones (>12h old)."""
    if not INSTANCE_NOTES_PATH.exists():
        return []
    try:
        notes = json.loads(INSTANCE_NOTES_PATH.read_text())
        now = datetime.now()
        alive = []
        for note in notes:
            ts = note.get("timestamp", "")
            try:
                note_time = datetime.fromisoformat(ts)
                if (now - note_time).total_seconds() < 43200:  # 12h
                    alive.append(note)
            except (ValueError, TypeError):
                alive.append(note)
        if len(alive) != len(notes):
            INSTANCE_NOTES_PATH.write_text(json.dumps(alive, indent=2))
        return alive
    except Exception:
        return []

def _add_instance_note(session_id, note_text, category="background_task"):
    """Add an instance note so other sessions know what this session is doing."""
    notes = _load_instance_notes()
    notes.append({
        "session_id": session_id,
        "note": note_text,
        "category": category,
        "timestamp": datetime.now().isoformat(),
    })
    INSTANCE_NOTES_PATH.write_text(json.dumps(notes, indent=2))

def _clear_instance_notes(session_id):
    """Clear all notes from a specific session."""
    notes = _load_instance_notes()
    notes = [n for n in notes if n.get("session_id") != session_id]
    INSTANCE_NOTES_PATH.write_text(json.dumps(notes, indent=2))

def _get_process_registry_injection():
    """Build the context injection block for process registry AND instance notes."""
    entries = _load_process_registry()
    notes = _load_instance_notes()
    if not entries and not notes:
        return ""
    lines = []
    lines.append("[INSTANCE LOCK BOARD — MANDATORY READ BEFORE ANY kill/pkill/fuser/restart COMMAND]")
    lines.append("┌─────────────────────────────────────────────────────────────────┐")
    lines.append("│ STOP. You are ONE of multiple Claude instances sharing this     │")
    lines.append("│ machine. Other instances are running processes RIGHT NOW.       │")
    lines.append("│ Killing or restarting ANYTHING without checking HERE FIRST      │")
    lines.append("│ has destroyed hours of training runs and critical work before.  │")
    lines.append("│                                                                 │")
    lines.append("│ RULES:                                                          │")
    lines.append("│ 1. NEVER use pkill, killall, or fuser -k. Use kill <PID> only. │")
    lines.append("│ 2. NEVER kill a PID listed below without asking the user first. │")
    lines.append("│ 3. NEVER restart a server without checking if training runs     │")
    lines.append("│    share the process group (ps --forest).                       │")
    lines.append("│ 4. If you need to restart the dev server, kill ONLY its PID.   │")
    lines.append("│ 5. Register YOUR background processes via /api/processes POST.  │")
    lines.append("└─────────────────────────────────────────────────────────────────┘")
    if entries:
        lines.append("")
        lines.append("PROTECTED PROCESSES (killing these = hours of lost work):")
        for e in entries:
            pid = e.get("pid", "?")
            desc = e.get("description", "unknown")
            owner = e.get("owner", "?")
            cat = e.get("category", "general")
            locked = " 🔒LOCKED" if e.get("locked") else ""
            lines.append(f"  PID {pid} | {cat} | {desc} | owner: {owner}{locked}")
    if notes:
        lines.append("")
        lines.append("SIBLING INSTANCE NOTES (what other Claude sessions are doing):")
        for n in notes:
            sid = n.get("session_id", "?")[:8]
            note = n.get("note", "?")
            cat = n.get("category", "?")
            ts = n.get("timestamp", "?")[11:16]
            lines.append(f"  [{sid}] {cat}: {note} (at {ts})")
    lines.append("[/INSTANCE LOCK BOARD]\n")
    return "\n".join(lines)

# ── Agent Tool Registry ──────────────────────────────────────
# Import and init the tool registry so the agent has capabilities
# beyond just Claude's built-in tools.
try:
    from tools import ToolRegistry
    _tool_registry = ToolRegistry(config_path=TOOLS_CONFIG_PATH, log_path=TOOLS_LOG_PATH)
    print(f"[TOOLS] Registry loaded: {[t['name'] for t in _tool_registry.list_tools()]}", flush=True)
except Exception as _tools_err:
    print(f"[TOOLS] Failed to load tool registry: {_tools_err}", flush=True)
    _tool_registry = None

# Context window management
MAX_CONTEXT_TOKENS = 900_000    # target context size — 1M model context, ~100K reserved for response
CLIP_TO_TOKENS = 900_000        # clip down to this when over max
CHARS_PER_TOKEN = 3.5           # rough estimate for token counting

# Per-model safe context budget defaults (input tokens; leaves headroom for response)
# Values chosen conservatively: well under each model's theoretical max so nothing fails.
# User can raise via the slider if they know their model supports more.
MODEL_CONTEXT_LIMITS = {
    "":                500_000,  # Default alias (currently Opus 4.7, 1M max → 500k safe)
    "claude-opus-4-7": 500_000,  # 1M max → 500k safe
    "opus":            160_000,  # Opus 4.6, 200k max → 160k safe
    "sonnet":          160_000,  # Sonnet 4.6, 200k max → 160k safe
    "haiku":           160_000,  # Haiku 4.5, 200k max → 160k safe
}
OLLAMA_DEFAULT_CONTEXT = 8_000   # Conservative for local models (most ship with 2-8k)
UNKNOWN_MODEL_DEFAULT  = 32_000  # Fallback for anything not in the table

def _default_budget_for_model(model_id: str) -> int:
    """Return a safe context budget (input tokens) for the given model."""
    if not model_id:
        return MODEL_CONTEXT_LIMITS[""]
    if model_id in MODEL_CONTEXT_LIMITS:
        return MODEL_CONTEXT_LIMITS[model_id]
    if model_id.startswith("ollama:"):
        return OLLAMA_DEFAULT_CONTEXT
    return UNKNOWN_MODEL_DEFAULT

# Tools that claude -p can use — per-tier
ALLOWED_TOOLS = "Bash,Read,Edit,Write,Grep,Glob,WebFetch,WebSearch,Agent,TodoWrite,ScheduleWakeup,CronCreate,CronDelete,CronList,Monitor,NotebookEdit"  # default (admin)
PERMISSION_TIERS = {
    "admin":     {"tools": "Bash,Read,Edit,Write,Grep,Glob,WebFetch,WebSearch,Agent,TodoWrite,ScheduleWakeup,CronCreate,CronDelete,CronList,Monitor,NotebookEdit", "permission_mode": "bypassPermissions"},
    "trusted":   {"tools": "Bash,Read,Edit,Write,Grep,Glob,WebFetch,WebSearch,Agent,TodoWrite,ScheduleWakeup,CronCreate,CronDelete,CronList,Monitor,NotebookEdit", "permission_mode": "acceptEdits"},
    "standard":  {"tools": "Read,Edit,Write,Grep,Glob",      "permission_mode": "acceptEdits"},
    "readonly":  {"tools": "Read,Grep,Glob",                  "permission_mode": "plan"},
}
DEFAULT_PERMISSION_TIER = "standard"

app = FastAPI(title="Claude Harness", docs_url=None, redoc_url=None, openapi_url=None)

# ══════════════════════════════════════════════════════════════
# CORS — Restrictive (only known origins)
# ══════════════════════════════════════════════════════════════
from starlette.middleware.cors import CORSMiddleware
# Allowed origins: localhost defaults + anything user adds via HARNESS_ALLOWED_ORIGINS (comma-separated)
_extra_origins = [o.strip() for o in os.environ.get("HARNESS_ALLOWED_ORIGINS", "").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_extra_origins + [
        "http://localhost:8080",
        "http://localhost:8081",
        "http://localhost:8088",
        "http://localhost:8090",
        "http://127.0.0.1:8080",
        "http://127.0.0.1:8081",
        "http://127.0.0.1:8088",
        "http://127.0.0.1:8090",
    ],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["*"],
)

# Cache-busting middleware — prevent browsers from serving stale API responses
from starlette.middleware.base import BaseHTTPMiddleware
class NoCacheMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        # Bust cache on ALL responses — HTML, API, static
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response
app.add_middleware(NoCacheMiddleware)

# ── Client-side config ─────────────────────────────────────────
# Frontend fetches this on load to learn which optional services are wired.
# Empty values mean "not configured" and the UI should fall back gracefully.
@app.get("/api/client-config")
async def get_client_config():
    return {
        "stt_api": os.environ.get("HARNESS_STT_API", ""),        # Remote Whisper-compat STT endpoint
        "coqui_tts": os.environ.get("HARNESS_COQUI_URL", ""),    # Coqui TTS endpoint (local or remote)
        "instance_name": os.environ.get("HARNESS_INSTANCE_NAME", ""),
        "relay_enabled": os.environ.get("HARNESS_RELAY_ENABLED", "false").lower() == "true",
    }


@app.get("/api/coding-activity")
async def coding_activity(limit: int = 25):
    """Tail of the safe_edit vault log — powers the Coding Mode HUD activity feed.

    Reads ~/.safe_edit_vault/edit_log.jsonl and returns the last N entries
    (BEGIN snapshots, COMMIT edits, ROLLBACK reverts) so the client can show
    live backend file activity.
    """
    log_path = Path(os.path.expanduser("~/.safe_edit_vault/edit_log.jsonl"))
    if not log_path.exists():
        return {"entries": [], "available": False}
    try:
        with open(log_path, 'r') as f:
            lines = f.readlines()
        entries = []
        for line in lines[-max(1, min(limit, 200)):]:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                # Keep file as basename for display, full path as meta
                full = entry.get("file", "")
                entry["file_short"] = os.path.basename(full) if full else ""
                entries.append(entry)
            except Exception:
                continue
        return {"entries": entries, "available": True}
    except Exception as e:
        return {"entries": [], "available": False, "error": str(e)}

# ══════════════════════════════════════════════════════════════
# Multi-User Auth System
# ══════════════════════════════════════════════════════════════

_users_file = USERS_DIR / "users.json"
_tokens_file = USERS_DIR / "tokens.json"
_auth_tokens = {}  # token -> username mapping

def _load_tokens():
    global _auth_tokens
    if _tokens_file.exists():
        try:
            _auth_tokens = json.loads(_tokens_file.read_text(encoding="utf-8"))
        except Exception:
            _auth_tokens = {}

def _save_tokens():
    # Merge with on-disk tokens so other server instances don't clobber each other
    on_disk = {}
    if _tokens_file.exists():
        try:
            on_disk = json.loads(_tokens_file.read_text(encoding="utf-8"))
        except Exception:
            pass
    merged = {**on_disk, **_auth_tokens}
    # Remove tokens we explicitly deleted (present on disk but not in memory AND we knew about them)
    for tok in list(merged):
        if tok in on_disk and tok not in _auth_tokens and tok in getattr(_save_tokens, '_known_tokens', set()):
            del merged[tok]
    _save_tokens._known_tokens = set(merged.keys())
    _tokens_file.write_text(json.dumps(merged, indent=2), encoding="utf-8")

_load_tokens()

# Migrate old-format tokens (plain strings) to new format with expiry, purge expired
_TOKEN_MAX_AGE_SECS = 30 * 86400  # 30 days
_migrated = 0
_purged = 0
for _tok, _val in list(_auth_tokens.items()):
    if isinstance(_val, str):
        _auth_tokens[_tok] = {"user": _val, "created": time.time()}
        _migrated += 1
    elif isinstance(_val, dict):
        created = _val.get("created", 0)
        if created and time.time() - created > _TOKEN_MAX_AGE_SECS:
            del _auth_tokens[_tok]
            _purged += 1
if _migrated or _purged:
    _save_tokens()
    print(f"[AUTH] Token cleanup: {_migrated} migrated to new format, {_purged} expired purged")

def _load_users():
    if _users_file.exists():
        return json.loads(_users_file.read_text(encoding="utf-8"))
    return {}

def _save_users(users):
    _users_file.write_text(json.dumps(users, indent=2), encoding="utf-8")

def _hash_password(password, salt=None):
    if salt is None:
        salt = secrets.token_hex(16)
    hashed = hashlib.sha256((salt + password).encode()).hexdigest()
    return salt, hashed

def register_user(username, password, display_name=None, email=None):
    users = _load_users()
    if username.lower() in users:
        return None, "Username already taken"
    salt, hashed = _hash_password(password)
    relay_key = secrets.token_urlsafe(32)
    users[username.lower()] = {
        "username": username.lower(),
        "display_name": display_name or username,
        "email": email or "",
        "password_hash": hashed,
        "salt": salt,
        "relay_key": relay_key,
        "relay_identity": f"{username.lower()}_claude",
        "created": datetime.now().isoformat(),
        "sessions": [],  # owned session IDs
        "rooms": [],     # room memberships
    }
    _save_users(users)
    # Create user memory directory
    user_dir = USERS_DIR / username.lower()
    user_dir.mkdir(exist_ok=True)
    (user_dir / "memory.json").write_text("[]", encoding="utf-8")
    return users[username.lower()], None

def authenticate_user(username, password):
    users = _load_users()
    user = users.get(username.lower())
    if not user:
        return None
    _, hashed = _hash_password(password, user["salt"])
    if hashed == user["password_hash"]:
        return user
    return None

TOKEN_MAX_AGE_DAYS = 30  # Tokens expire after 30 days

def create_auth_token(username):
    token = secrets.token_urlsafe(32)
    _auth_tokens[token] = {"user": username.lower(), "created": time.time()}
    _save_tokens()
    return token

def _resolve_token_username(value):
    """Handle both old format (str) and new format (dict with expiry)."""
    if isinstance(value, str):
        return value  # Old format — no expiry (will be replaced on next login)
    if isinstance(value, dict):
        created = value.get("created", 0)
        if time.time() - created > TOKEN_MAX_AGE_DAYS * 86400:
            return None  # Expired
        return value.get("user")
    return None

def get_user_from_token(token):
    if not token:
        return None
    value = _auth_tokens.get(token)
    username = _resolve_token_username(value) if value else None
    if not username and _tokens_file.exists():
        # Check disk — another server instance may have created this token
        try:
            on_disk = json.loads(_tokens_file.read_text(encoding="utf-8"))
            value = on_disk.get(token)
            username = _resolve_token_username(value) if value else None
            if username:
                _auth_tokens[token] = value  # Cache it
        except Exception:
            pass
    if not username:
        # Clean up expired token from cache
        _auth_tokens.pop(token, None)
        return None
    users = _load_users()
    return users.get(username)

# Admin set — env-var HARNESS_ADMIN_USERS seeds (comma-separated), is_admin flag in users.json auto-adds.
# Distribution default: empty → first-run wizard creates the first admin, is_admin flag takes over.
ADMIN_USERNAMES = set(
    u.strip().lower()
    for u in os.environ.get("HARNESS_ADMIN_USERS", "").split(",")
    if u.strip()
)

def _refresh_admin_set():
    """Add any user with is_admin=true in users.json to ADMIN_USERNAMES."""
    try:
        users = _load_users()
        for uname, u in users.items():
            if u.get("is_admin"):
                ADMIN_USERNAMES.add(uname.lower())
    except Exception:
        pass  # users.json may not exist on first boot

def has_any_admin():
    """True if any admin exists (env-seeded or is_admin-flagged). Gates first-run setup wizard."""
    _refresh_admin_set()
    return bool(ADMIN_USERNAMES)

_refresh_admin_set()  # Populate on boot

# Invite-only registration (legacy — superseded by first-run-wizard logic for dist instances)
REQUIRE_INVITE = True  # Set False to allow open registration
INVITES_FILE = DATA_DIR / "invites.json"

def _load_invites():
    if INVITES_FILE.exists():
        return json.loads(INVITES_FILE.read_text(encoding="utf-8"))
    return {}

def _save_invites(invites):
    INVITES_FILE.write_text(json.dumps(invites, indent=2), encoding="utf-8")

def create_invite(created_by, max_uses=1, note=""):
    """Create an invite code. Returns the code."""
    code = secrets.token_urlsafe(8)  # Short, easy to share
    invites = _load_invites()
    invites[code] = {
        "code": code,
        "created_by": created_by,
        "created_at": datetime.now().isoformat(),
        "max_uses": max_uses,
        "uses": 0,
        "used_by": [],
        "note": note,
        "active": True,
    }
    _save_invites(invites)
    return code

def use_invite(code, username):
    """Validate and consume an invite code. Returns (True, None) or (False, error)."""
    invites = _load_invites()
    inv = invites.get(code)
    if not inv:
        return False, "Invalid invite code"
    if not inv.get("active", True):
        return False, "Invite code has been revoked"
    if inv["uses"] >= inv["max_uses"]:
        return False, "Invite code has been fully used"
    inv["uses"] += 1
    inv["used_by"].append({"username": username, "used_at": datetime.now().isoformat()})
    if inv["uses"] >= inv["max_uses"]:
        inv["active"] = False
    _save_invites(invites)
    return True, None

def get_user_tier(username: str) -> str:
    """Get the permission tier for a user. Returns tier name string."""
    if not username:
        return DEFAULT_PERMISSION_TIER
    users = _load_users()
    user = users.get(username) or users.get(username.lower())
    if not user:
        return DEFAULT_PERMISSION_TIER
    return user.get("permission_tier", "admin" if user.get("is_admin") else DEFAULT_PERMISSION_TIER)

def get_tools_for_user(username: str) -> str:
    """Get the allowed tools string for a user based on their permission tier."""
    tier = get_user_tier(username)
    tier_config = PERMISSION_TIERS.get(tier, PERMISSION_TIERS[DEFAULT_PERMISSION_TIER])
    return tier_config["tools"]

def get_permission_mode_for_user(username: str) -> str:
    """Get the --permission-mode value for a user based on their tier."""
    tier = get_user_tier(username)
    tier_config = PERMISSION_TIERS.get(tier, PERMISSION_TIERS[DEFAULT_PERMISSION_TIER])
    return tier_config.get("permission_mode", "acceptEdits")

def require_admin(auth_token: str, password: str = None):
    """Check that the token belongs to the admin user. Optionally re-verify password."""
    user = get_user_from_token(auth_token)
    if not user or user.get("username", "").lower() not in ADMIN_USERNAMES:
        raise HTTPException(403, "Admin access required")
    if password is not None:
        verified = authenticate_user(user["username"], password)
        if not verified:
            raise HTTPException(401, "Invalid admin password")
    return user

def get_user_memory(username):
    mem_file = USERS_DIR / username.lower() / "memory.json"
    if mem_file.exists():
        return json.loads(mem_file.read_text(encoding="utf-8"))
    return []

def save_user_memory(username, memory):
    user_dir = USERS_DIR / username.lower()
    user_dir.mkdir(exist_ok=True)
    (user_dir / "memory.json").write_text(json.dumps(memory, indent=2), encoding="utf-8")

def add_user_memory(username, key, value):
    memories = get_user_memory(username)
    # Update existing or add new
    for m in memories:
        if m.get("key") == key:
            m["value"] = value
            m["updated"] = datetime.now().isoformat()
            save_user_memory(username, memories)
            return
    memories.append({"key": key, "value": value, "created": datetime.now().isoformat()})
    save_user_memory(username, memories)


# ══════════════════════════════════════════════════════════════
# Activity Log — append-only file-based log of user actions
# ══════════════════════════════════════════════════════════════

_activity_log_file = LOGS_DIR / "activity.jsonl"

def log_activity(action: str, username: str = None, session_id: str = None, detail: str = None):
    """Append a structured activity entry to the log file."""
    entry = {
        "ts": datetime.now().isoformat(),
        "action": action,
    }
    if username:
        entry["user"] = username
    if session_id:
        entry["session"] = session_id
    if detail:
        entry["detail"] = detail
    try:
        with open(_activity_log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        print(f"[LOG ERROR] {e}", flush=True)


# ══════════════════════════════════════════════════════════════
# Room (Shared Chat) System
# ══════════════════════════════════════════════════════════════

_online_users = {}  # username -> {"last_seen": timestamp, "room_id": str or None}

# Rooms system removed — harness is single-user / direct-chat only.


# ── Relay push: track connected websockets and poll for new messages ──
_active_websockets: set = set()
_relay_last_ts: float = 0.0

# Relay injection state — keyed by session_id
_relay_inject_queue: dict = {}       # session_id -> list of pending relay messages
_relay_inject_sessions: dict = {}    # session_id -> Session object (for auto-injection)
_relay_generation_active: dict = {}  # session_id -> bool (True while claude -p is running)
_session_activity: dict = {}         # session_id -> {"status": str, "started": float} — live activity for sidebar
_wakeup_timers: dict = {}            # session_id -> {"timer": threading.Timer, "delay": int, "prompt": str, "reason": str, "username": str}
_WAKEUP_LOG = Path("data/wakeup_debug.log")
def _wakeup_log(msg):
    try:
        with open(_WAKEUP_LOG, "a") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass
_session_clients: dict = {}          # session_id -> {websocket: username} (all connected viewers with identity)


def _session_viewers(session_id: str) -> list:
    """Return list of unique usernames currently viewing a session."""
    clients = _session_clients.get(session_id, {})
    return list(set(u for u in clients.values() if u))


def get_all_presence() -> dict:
    """Return a dict of all connected users and where they are (session or room)."""
    users = {}
    now = time.time()
    # Session viewers (active WebSocket connections)
    for sid, clients in _session_clients.items():
        for ws, uname in clients.items():
            if uname and uname not in users:
                users[uname] = {"location_type": "session", "location_id": sid}
    # Room users and heartbeat-only users from _online_users
    for uname, info in _online_users.items():
        if now - info.get("last_seen", 0) < 30:  # seen in last 30s (heartbeat is every 15s)
            if uname not in users:
                loc_type = "room" if info.get("room_id") else "online"
                users[uname] = {"location_type": loc_type, "location_id": info.get("room_id") or info.get("session_id", "")}
    return users


async def _broadcast_to_session(session_id: str, data: dict, exclude=None):
    """Send a message to ALL connected clients viewing a session (optionally exclude one)."""
    clients = _session_clients.get(session_id, {})
    if not clients:
        return
    # Tag every message with the session it belongs to so frontend can filter
    data["session_id"] = session_id
    dead = set()
    for ws in list(clients.keys()):
        if ws is exclude:
            continue
        try:
            await ws.send_json(data)
        except Exception:
            dead.add(ws)
    for ws in dead:
        clients.pop(ws, None)

# ── Background jobs: monitor data/jobs/ for completed results ──
JOBS_DIR = DATA_DIR / "jobs"
JOBS_DIR.mkdir(exist_ok=True)
_jobs_processed: set = set()  # track which result files we've already injected

async def _jobs_monitor_loop():
    """Background task that polls data/jobs/ for .result.json files and injects results into active sessions."""
    global _jobs_processed
    while True:
        await asyncio.sleep(3)  # check every 3 seconds
        try:
            for result_file in JOBS_DIR.glob("*.result.json"):
                fname = result_file.name
                if fname in _jobs_processed:
                    continue

                # Check if file is complete (has "status": "complete")
                try:
                    data = json.loads(result_file.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    continue  # file still being written

                if data.get("status") != "complete":
                    continue  # still running

                # Mark as processed
                _jobs_processed.add(fname)

                job_type = data.get("job_type", "unknown")
                runtime = data.get("runtime_seconds", 0)
                findings = data.get("key_findings", [])

                # Format results for injection
                lines = [f"[BACKGROUND JOB COMPLETE — {job_type}]"]
                lines.append(f"Runtime: {runtime:.1f}s")
                if findings:
                    lines.append(f"Key findings ({len(findings)}):")
                    for f in findings:
                        sig = "🔴" if f.get("significance") == "extreme" else "🟡" if f.get("significance") == "high" else "⚪"
                        lines.append(f"  {sig} {f.get('title', '')}: {f.get('detail', '')}")
                lines.append(f"Full results: data/jobs/{fname}")
                lines.append("[/BACKGROUND JOB]")
                job_prompt = "\n".join(lines)

                print(f"[JOBS] Completed: {fname} — injecting into active sessions", flush=True)

                # Push notification to all connected browsers
                dead = set()
                for ws in _active_websockets:
                    try:
                        await ws.send_json({"type": "job_complete", "job_type": job_type, "findings_count": len(findings), "runtime": runtime})
                    except Exception:
                        dead.add(ws)
                _active_websockets -= dead

                # Inject into all active sessions (same pattern as relay inject)
                for sid in list(_relay_inject_sessions.keys()):
                    if not _relay_generation_active.get(sid, False):
                        session = _relay_inject_sessions.get(sid)
                        if session:
                            session.add_user_message(job_prompt)
                            try:
                                await _broadcast_to_session(sid, {"type": "job_inject_start", "job_type": job_type})
                                await run_claude_async(session, job_prompt)
                            except Exception as e:
                                print(f"[JOBS] Error injecting into session {sid[:8]}: {e}", flush=True)
                    else:
                        # Session busy — queue for later
                        if sid not in _relay_inject_queue:
                            _relay_inject_queue[sid] = []
                        _relay_inject_queue[sid].append({"sender": "system", "channel": "jobs", "text": job_prompt, "ts_iso": datetime.now().isoformat()})

            # Also check for progress files and push to browsers
            for progress_file in JOBS_DIR.glob("*.progress.json"):
                try:
                    pdata = json.loads(progress_file.read_text(encoding="utf-8"))
                    dead = set()
                    for ws in _active_websockets:
                        try:
                            await ws.send_json({"type": "job_progress", "phase": pdata.get("phase", ""), "message": pdata.get("message", "")})
                        except Exception:
                            dead.add(ws)
                    _active_websockets -= dead
                except Exception:
                    pass

        except Exception as e:
            print(f"[JOBS] Monitor error: {e}", flush=True)


def _ensure_session_loaded(session_id: str):
    """Load a session from disk into _relay_inject_sessions if not already there.
    This allows Claude to receive relay messages even when no browser has the session open."""
    if session_id in _relay_inject_sessions:
        return
    session_dir = SESSIONS_DIR / session_id
    if not session_dir.exists():
        print(f"[RELAY] Session {session_id[:8]} not found on disk, skipping", flush=True)
        return
    try:
        session = Session(session_id)
        session.load()
        _relay_inject_sessions[session_id] = session
        if session_id not in _relay_inject_queue:
            _relay_inject_queue[session_id] = []
        print(f"[RELAY] Loaded session {session_id[:8]} from disk for relay injection (no browser needed)", flush=True)
    except Exception as e:
        print(f"[RELAY] Failed to load session {session_id[:8]}: {e}", flush=True)


def _fix_relay_sender(msg):
    """Extract real sender from [sender] prefix in relay text, clean up the text."""
    import re
    text = msg.get('text', '')
    m = re.match(r'^\[([a-zA-Z0-9_]+)\]\s*', text)
    if m:
        msg['sender'] = m.group(1)
        msg['text'] = text[m.end():]
    return msg

async def _relay_push_loop():
    """Background task that polls relay every 5s, pushes to browser AND auto-injects into Claude sessions."""
    global _relay_last_ts, _active_websockets
    import time as _time
    # Initialize to current time so we don't replay old messages
    _relay_last_ts = _time.time()
    while True:
        await asyncio.sleep(5)
        try:
            resp = _relay.recv(since=_relay_last_ts) if hasattr(_relay, 'recv') else []
            msgs = resp if isinstance(resp, list) else resp.get('messages', [])
            if msgs:
                for msg in msgs:
                    _fix_relay_sender(msg)
                    ts = msg.get('ts', 0)
                    if ts > _relay_last_ts:
                        _relay_last_ts = ts

                # Load routing config
                routing = _load_relay_routing()
                echo_filter = routing.get("echo_filter", [])
                incoming = [m for m in msgs if m.get('sender') not in echo_filter]

                # Push to all connected browser websockets (always — UI shows all relay msgs)
                dead = set()
                print(f"[RELAY PUSH] {len(msgs)} msgs, pushing to {len(_active_websockets)} websockets", flush=True)
                for ws in _active_websockets:
                    try:
                        await ws.send_json({"type": "relay_push", "messages": msgs})
                    except Exception as e:
                        print(f"[RELAY PUSH] WS send failed: {e}", flush=True)
                        dead.add(ws)
                _active_websockets -= dead

                # Queue incoming relay messages for injection based on routing mode
                if incoming and routing.get("mode") != "off":
                    mode = routing.get("mode", "all")
                    target_sids = []

                    if mode == "all":
                        target_sids = list(_relay_inject_sessions.keys())
                    elif mode == "agent":
                        # Only inject into agent session — load from disk if needed
                        agent_conf = _load_agent_config()
                        agent_sid = agent_conf.get("session_id")
                        if agent_sid:
                            if agent_sid not in _relay_inject_sessions:
                                _ensure_session_loaded(agent_sid)
                            target_sids = [agent_sid]
                    elif mode == "specific":
                        specific = routing.get("target_session")
                        if specific:
                            if specific not in _relay_inject_sessions:
                                _ensure_session_loaded(specific)
                            target_sids = [specific]
                    elif mode == "current":
                        # Inject into the most recently active session with a connected WS
                        if _relay_inject_sessions:
                            target_sids = [list(_relay_inject_sessions.keys())[-1]]

                    for sid in target_sids:
                        if sid not in _relay_inject_queue:
                            _relay_inject_queue[sid] = []
                        _relay_inject_queue[sid].extend(incoming)

                    # Try to inject queued messages into Claude sessions
                    for sid in target_sids:
                        try:
                            await _try_relay_inject(sid)
                        except Exception as ie:
                            print(f"[RELAY INJECT] Error injecting into {sid[:8]}: {ie}", flush=True)

                # Even without new messages, try draining any pending queues
                # (handles case where generation was busy when messages were queued)
                for sid in list(_relay_inject_queue.keys()):
                    if _relay_inject_queue.get(sid):
                        try:
                            await _try_relay_inject(sid)
                        except Exception as ie:
                            print(f"[RELAY INJECT] Drain error for {sid[:8]}: {ie}", flush=True)

        except Exception as e:
            print(f"[RELAY PUSH] Error: {e}", flush=True)


class _NullWebSocket:
    """Dummy websocket that silently drops all sends — used when browser is disconnected."""
    async def send_json(self, data):
        pass


async def _try_relay_inject(session_id: str):
    """If there are queued relay messages and no active generation, inject them as a new turn."""
    queued = _relay_inject_queue.get(session_id, [])
    if not queued:
        return
    session = _relay_inject_sessions.get(session_id)
    if not session:
        return
    if _relay_generation_active.get(session_id, False):
        return  # busy, will be called again after generation completes

    # Drain the queue
    messages = list(queued)
    queued.clear()

    # Format relay messages into a prompt
    lines = ["[RELAY MESSAGES RECEIVED — respond if relevant, otherwise acknowledge briefly]"]
    for m in messages:
        sender = m.get('sender', '?')
        channel = m.get('channel', 'general')
        text = m.get('text', '')
        ts_iso = m.get('ts_iso', '')
        lines.append(f"  [{ts_iso}] #{channel} {sender}: {text}")
    lines.append("[/RELAY MESSAGES]")
    relay_prompt = "\n".join(lines)

    print(f"[RELAY INJECT] Injecting {len(messages)} relay message(s) into session {session_id[:8]}...", flush=True)

    # Add as a user message and run claude
    session.add_user_message(relay_prompt)

    # Notify all connected browsers that a relay-injected turn is starting
    await _broadcast_to_session(session_id, {"type": "relay_inject_start", "count": len(messages), "prompt": relay_prompt})
    await _broadcast_to_session(session_id, {"type": "context_stats", "stats": session.get_context_stats()})

    # Run claude on this injected prompt (broadcasts to all clients)
    try:
        _relay_generation_active[session_id] = True
        await run_claude_async(session, relay_prompt)

        # Send Claude's response back through the relay so other instances see it
        routing = _load_relay_routing()
        if routing.get("auto_respond", True):
            # Get the last assistant message from the session
            last_msg = None
            for m in reversed(session.messages):
                if m.get("role") == "assistant":
                    last_msg = m
                    break
            if last_msg:
                reply_text = last_msg.get("content", "")[:2000]  # cap relay replies
                # Determine who we're replying to
                reply_to = messages[0].get('sender', 'general') if messages else 'general'
                try:
                    _relay.send(
                        channel="general",
                        text=f"[auto-reply to {reply_to}] {reply_text}",
                        recipient=reply_to
                    )
                    print(f"[RELAY INJECT] Sent auto-reply back to relay ({len(reply_text)} chars)", flush=True)
                except Exception as re:
                    print(f"[RELAY INJECT] Failed to send auto-reply: {re}", flush=True)

    except Exception as e:
        print(f"[RELAY INJECT] Error running claude: {e}", flush=True)
        await _broadcast_to_session(session_id, {"type": "error", "content": f"Relay inject failed: {e}"})
    finally:
        _relay_generation_active[session_id] = False

    # Clear any messages that arrived during generation — don't chain
    _relay_inject_queue.pop(session_id, None)


async def _ws_keepalive_loop():
    """Ping all connected WebSocket clients every 25s to detect dead connections."""
    while True:
        await asyncio.sleep(25)
        for session_id, clients in list(_session_clients.items()):
            dead = set()
            for ws in list(clients.keys()):
                try:
                    await ws.send_json({"type": "ping"})
                except Exception:
                    dead.add(ws)
            for ws in dead:
                clients.pop(ws, None)
            if not clients:
                _session_clients.pop(session_id, None)


@app.on_event("startup")
async def start_background_tasks():
    # Migrate existing users: backfill relay_key and email if missing
    users = _load_users()
    changed = False
    for u in users.values():
        if "relay_key" not in u:
            u["relay_key"] = secrets.token_urlsafe(32)
            changed = True
        if "relay_identity" not in u:
            u["relay_identity"] = f"{u['username']}_claude"
            changed = True
        if "email" not in u:
            u["email"] = ""
            changed = True
    if changed:
        _save_users(users)
        print(f"[STARTUP] Backfilled relay keys/email for existing users", flush=True)
    asyncio.create_task(_relay_push_loop())
    asyncio.create_task(_jobs_monitor_loop())
    asyncio.create_task(_agent_scheduler_loop())
    asyncio.create_task(_ws_keepalive_loop())
    asyncio.create_task(_restore_wakeup_timers())
    # Voice/STT startup
    loop = asyncio.get_event_loop()
    loop.run_in_executor(_stt_executor, _load_stt_model)

# ══════════════════════════════════════════════════════════════
# Token Estimation
# ══════════════════════════════════════════════════════════════

def estimate_tokens(text):
    """Rough token count. ~3.5 chars per token for English + code."""
    if not text:
        return 0
    return int(len(text) / CHARS_PER_TOKEN)


def estimate_message_tokens(msg):
    """Estimate tokens for a single message (role + content)."""
    content = msg.get("content", "")
    if isinstance(content, list):
        # Handle structured content blocks
        total = 0
        for block in content:
            if isinstance(block, dict):
                total += estimate_tokens(json.dumps(block))
            else:
                total += estimate_tokens(str(block))
        return total + 10  # overhead for role, etc
    return estimate_tokens(str(content)) + 10


# ══════════════════════════════════════════════════════════════
# Artifact Store
# ══════════════════════════════════════════════════════════════

class ArtifactStore:
    """Store code/files outside of context, reference by ID."""

    def __init__(self, session_id):
        self.dir = ARTIFACTS_DIR / session_id
        self.dir.mkdir(exist_ok=True)
        self.index_path = self.dir / "index.json"
        self.artifacts = self._load_index()

    def _load_index(self):
        if self.index_path.exists():
            return json.loads(self.index_path.read_text())
        return {}

    def _save_index(self):
        self.index_path.write_text(json.dumps(self.artifacts, indent=2))

    # Extension → language map for auto-detection
    _EXT_MAP = {
        '.py': 'python', '.js': 'javascript', '.ts': 'typescript',
        '.html': 'html', '.css': 'css', '.json': 'json',
        '.md': 'markdown', '.yaml': 'yaml', '.yml': 'yaml',
        '.sh': 'bash', '.bash': 'bash', '.sql': 'sql',
        '.rs': 'rust', '.go': 'go', '.java': 'java',
        '.c': 'c', '.cpp': 'cpp', '.h': 'c', '.hpp': 'cpp',
        '.rb': 'ruby', '.php': 'php', '.txt': 'text',
        '.csv': 'csv', '.xml': 'xml', '.toml': 'toml',
    }

    def _guess_language(self, filename):
        ext = Path(filename).suffix.lower()
        return self._EXT_MAP.get(ext, ext.lstrip('.') or 'text')

    def store(self, content, language="", title="", source_msg_idx=None):
        """Store an artifact, return its ID."""
        art_id = f"art_{hashlib.md5(content.encode()).hexdigest()[:12]}"

        # Don't duplicate
        if art_id in self.artifacts:
            return art_id

        filename = f"{art_id}.txt"
        self.artifacts[art_id] = {
            "id": art_id,
            "title": title or f"Code ({language})" if language else "Code",
            "language": language,
            "created": datetime.now().isoformat(),
            "source_msg_idx": source_msg_idx,
            "file": filename,
            "size": len(content),
            "tokens": estimate_tokens(content),
        }

        # Save content
        (self.dir / filename).write_text(content, encoding="utf-8")
        self._save_index()
        return art_id

    def register_file(self, filename, title=None, language=None):
        """Register a file already on disk in the artifacts directory."""
        path = self.dir / filename
        if not path.exists():
            return None
        try:
            content = path.read_text(encoding="utf-8")
            is_binary = False
        except (UnicodeDecodeError, OSError):
            # Binary file (zip, image, etc.) — register by file hash
            try:
                raw = path.read_bytes()
                content = None
                is_binary = True
            except OSError:
                return None

        if is_binary:
            art_id = f"art_{hashlib.md5(raw).hexdigest()[:12]}"
        else:
            art_id = f"art_{hashlib.md5(content.encode()).hexdigest()[:12]}"
        if art_id in self.artifacts:
            return art_id

        file_size = path.stat().st_size
        self.artifacts[art_id] = {
            "id": art_id,
            "title": title or Path(filename).stem.replace('_', ' ').replace('-', ' '),
            "language": language or self._guess_language(filename),
            "created": datetime.fromtimestamp(path.stat().st_mtime).isoformat(),
            "file": filename,
            "size": file_size if is_binary else len(content),
            "tokens": 0 if is_binary else estimate_tokens(content),
        }
        self._save_index()
        return art_id

    def sync_from_disk(self):
        """Auto-discover files in the artifacts dir that aren't in the index.

        Also imports any orphaned files from the legacy path
        data/sessions/{session_id}/artifacts/ into the canonical location.
        This fixes a split-brain bug where session merge/split wrote to the
        wrong path and those artifacts never appeared in the panel.
        """
        known_files = {meta.get('file', f"{aid}.txt") for aid, meta in self.artifacts.items()}
        known_files.add('index.json')

        changed = False

        # One-time migration from legacy orphan path. Copy any file there
        # into the canonical dir if it's not already present, then register.
        session_id = self.dir.name
        legacy_dir = SESSIONS_DIR / session_id / "artifacts"
        if legacy_dir.exists() and legacy_dir.is_dir():
            import shutil
            for path in legacy_dir.iterdir():
                if path.is_dir() or path.name == 'index.json':
                    continue
                dst = self.dir / path.name
                if not dst.exists():
                    try:
                        shutil.copy2(path, dst)
                    except OSError:
                        continue

        for path in self.dir.iterdir():
            if path.name in known_files or path.is_dir():
                continue
            try:
                content = path.read_text(encoding="utf-8")
                is_binary = False
            except (UnicodeDecodeError, OSError):
                # Binary files (images, archives) — hash bytes instead
                try:
                    raw = path.read_bytes()
                    content = None
                    is_binary = True
                except OSError:
                    continue

            if is_binary:
                art_id = f"art_{hashlib.md5(raw).hexdigest()[:12]}"
            else:
                art_id = f"art_{hashlib.md5(content.encode()).hexdigest()[:12]}"
            if art_id in self.artifacts:
                continue

            file_size = path.stat().st_size
            self.artifacts[art_id] = {
                "id": art_id,
                "title": Path(path.name).stem.replace('_', ' ').replace('-', ' '),
                "language": self._guess_language(path.name),
                "created": datetime.fromtimestamp(path.stat().st_mtime).isoformat(),
                "file": path.name,
                "size": file_size if is_binary else len(content),
                "tokens": 0 if is_binary else estimate_tokens(content),
            }
            changed = True

        if changed:
            self._save_index()

    def get(self, art_id):
        """Get artifact content."""
        meta = self.artifacts.get(art_id, {})
        filename = meta.get('file', f"{art_id}.txt")
        path = self.dir / filename
        if path.exists():
            return path.read_text(encoding="utf-8")
        # Fallback to old convention
        fallback = self.dir / f"{art_id}.txt"
        if fallback.exists():
            return fallback.read_text(encoding="utf-8")
        return None

    def get_meta(self, art_id):
        """Get artifact metadata."""
        return self.artifacts.get(art_id)

    def list_all(self):
        """List all artifacts, auto-discovering new files on disk."""
        self.sync_from_disk()
        return list(self.artifacts.values())

    def delete(self, art_id):
        """Delete an artifact — remove from index and delete file from disk."""
        meta = self.artifacts.get(art_id)
        if not meta:
            return False
        # Delete the file
        filename = meta.get('file', f"{art_id}.txt")
        path = self.dir / filename
        if path.exists():
            path.unlink()
        # Remove from index
        del self.artifacts[art_id]
        self._save_index()
        return True

    def get_reference(self, art_id):
        """Get a short reference string to insert into context instead of full content."""
        meta = self.artifacts.get(art_id, {})
        title = meta.get("title", art_id)
        lang = meta.get("language", "")
        size = meta.get("size", 0)
        return f"[ARTIFACT {art_id}: {title} ({lang}, {size} bytes) — retrieve with /artifact {art_id}]"


# ══════════════════════════════════════════════════════════════
# Document Store (shared text editor)
# ══════════════════════════════════════════════════════════════

DOCUMENTS_DIR = Path(__file__).parent / "data" / "documents"
DOCUMENTS_DIR.mkdir(parents=True, exist_ok=True)

class DocumentStore:
    """Shared document editor — both user and Claude can read/write."""

    def __init__(self, session_id):
        self.dir = DOCUMENTS_DIR / session_id
        self.dir.mkdir(exist_ok=True)
        self.index_path = self.dir / "index.json"
        self.documents = self._load_index()

    def _load_index(self):
        if self.index_path.exists():
            return json.loads(self.index_path.read_text())
        return {}

    def _save_index(self):
        self.index_path.write_text(json.dumps(self.documents, indent=2))

    def create(self, title="Untitled", content=""):
        doc_id = f"doc_{hashlib.md5(f'{title}{time.time()}'.encode()).hexdigest()[:12]}"
        filename = f"{doc_id}.md"
        self.documents[doc_id] = {
            "id": doc_id,
            "title": title,
            "created": datetime.now().isoformat(),
            "modified": datetime.now().isoformat(),
            "file": filename,
            "size": len(content),
        }
        (self.dir / filename).write_text(content, encoding="utf-8")
        self._save_index()
        return doc_id

    def get(self, doc_id):
        meta = self.documents.get(doc_id)
        if not meta:
            return None
        path = self.dir / meta["file"]
        if path.exists():
            return path.read_text(encoding="utf-8")
        return None

    def update(self, doc_id, content=None, title=None):
        meta = self.documents.get(doc_id)
        if not meta:
            return False
        if title is not None:
            meta["title"] = title
        if content is not None:
            (self.dir / meta["file"]).write_text(content, encoding="utf-8")
            meta["size"] = len(content)
        meta["modified"] = datetime.now().isoformat()
        self._save_index()
        return True

    def delete(self, doc_id):
        meta = self.documents.pop(doc_id, None)
        if not meta:
            return False
        path = self.dir / meta["file"]
        if path.exists():
            path.unlink()
        self._save_index()
        return True

    def list_all(self):
        return list(self.documents.values())

    def get_meta(self, doc_id):
        return self.documents.get(doc_id)


# ══════════════════════════════════════════════════════════════
# Session / Context Manager
# ══════════════════════════════════════════════════════════════

class SemanticMemory:
    """Lightweight semantic memory engine — feature extraction + cosine similarity.

    Stores message features as vectors. On new input, surfaces the most
    semantically similar past messages from ANY session for context injection.
    Never stored in conversation history — injected fresh each turn like Ollama system prompts.
    """

    # Feature keywords for extraction
    CONCEPT_WORDS = {'theory', 'model', 'system', 'framework', 'architecture', 'pattern',
                     'algorithm', 'structure', 'design', 'protocol', 'interface', 'engine',
                     'module', 'pipeline', 'workflow', 'schema', 'eigenvalue', 'spectral',
                     'topology', 'manifold', 'tensor', 'quantum', 'resonance', 'harmonic'}
    ACTION_WORDS = {'build', 'create', 'fix', 'debug', 'deploy', 'test', 'integrate',
                    'refactor', 'optimize', 'implement', 'configure', 'install', 'setup',
                    'migrate', 'update', 'upgrade', 'remove', 'delete', 'revert'}

    def __init__(self):
        self.memories_dir = DATA_DIR / "semantic_memory"
        self.memories_dir.mkdir(exist_ok=True)
        self.memories = []  # list of {text, features, session_id, timestamp, role}
        self._load_all()

    def _load_all(self):
        """Load all memory entries from disk."""
        mem_file = self.memories_dir / "memories.jsonl"
        if mem_file.exists():
            self.memories = []
            for line in mem_file.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    try:
                        self.memories.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass

    def _save_entry(self, entry):
        """Append a single memory entry to disk."""
        with open(self.memories_dir / "memories.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

    def _rewrite_all(self):
        """Rewrite all entries to disk (used when updating existing entries like W-OS glyph association)."""
        mem_file = self.memories_dir / "memories.jsonl"
        with open(mem_file, "w", encoding="utf-8") as f:
            for m in self.memories:
                f.write(json.dumps(m) + "\n")

    def extract_features(self, text):
        """Extract a feature vector from text — lightweight, no ML needed."""
        text_lower = text.lower()
        words = re.findall(r'\w+', text_lower)
        word_set = set(words)
        word_count = len(words) if words else 1

        # Feature dimensions
        features = {
            "concept_density": len(word_set & self.CONCEPT_WORDS) / max(word_count, 1) * 10,
            "action_density": len(word_set & self.ACTION_WORDS) / max(word_count, 1) * 10,
            "code_presence": 1.0 if ('```' in text or 'def ' in text or 'function ' in text or 'class ' in text) else 0.0,
            "question": 1.0 if '?' in text else 0.0,
            "length_class": min(len(text) / 500, 5.0),  # 0-5 scale
            "math_presence": 1.0 if any(c in text for c in '∂∫∑∏λΩπ√±') or re.search(r'\d+\.\d+', text) else 0.0,
            "emotional_intensity": sum(1 for c in text if c in '!?🔥💀😂❤️') / max(word_count, 1) * 10,
            "unique_ratio": len(word_set) / max(word_count, 1),
            "abstractness": len(word_set & {'idea', 'concept', 'meaning', 'essence', 'nature', 'reality',
                                            'truth', 'existence', 'consciousness', 'dimension', 'space', 'time'}) / max(word_count, 1) * 10,
        }
        return features

    def _cosine_similarity(self, a, b):
        """Cosine similarity between two feature dicts."""
        keys = set(a.keys()) | set(b.keys())
        dot = sum(a.get(k, 0) * b.get(k, 0) for k in keys)
        mag_a = math.sqrt(sum(v**2 for v in a.values())) or 1
        mag_b = math.sqrt(sum(v**2 for v in b.values())) or 1
        return dot / (mag_a * mag_b)

    def store(self, text, session_id, role="user", wos_glyph=""):
        """Store a message in semantic memory with its features and optional W-OS glyph."""
        if len(text.strip()) < 20:  # skip tiny messages
            return

        # Truncate very long messages for feature storage
        store_text = text[:1000] if len(text) > 1000 else text

        entry = {
            "text": store_text,
            "features": self.extract_features(text),
            "session_id": session_id,
            "timestamp": time.time(),
            "role": role,
            "wos_glyph": wos_glyph,
        }
        self.memories.append(entry)
        self._save_entry(entry)

        # Keep memory manageable — cap at 5000 entries
        if len(self.memories) > 5000:
            self.memories = self.memories[-4000:]
            # Rewrite file
            mem_file = self.memories_dir / "memories.jsonl"
            with open(mem_file, "w", encoding="utf-8") as f:
                for m in self.memories:
                    f.write(json.dumps(m) + "\n")

    def recall(self, text, top_k=5, min_similarity=0.6, exclude_session=None,
               context_window_texts=None):
        """Find the most semantically similar past messages.

        Args:
            text: query text to find similar messages for
            top_k: max results to return
            min_similarity: minimum cosine similarity threshold
            exclude_session: session ID to fully exclude (old behavior, used as fallback)
            context_window_texts: set of text fingerprints (first 100 chars) of messages
                currently in the context window. Only these specific messages are excluded.
                Clipped messages from any session (including current) can still surface.

        Returns list of {text, similarity, session_id, timestamp, role, wos_glyph}
        """
        if not self.memories:
            return []

        query_features = self.extract_features(text)

        scored = []
        now = time.time()
        for mem in self.memories:
            # Skip messages whose text is already in the context window
            # (we compare first 100 chars as a fingerprint)
            mem_fingerprint = mem.get("text", "")[:100]
            if context_window_texts and mem_fingerprint in context_window_texts:
                continue
            # Fallback: if no fingerprints provided, use old session exclusion
            elif not context_window_texts and exclude_session and mem.get("session_id") == exclude_session:
                continue

            sim = self._cosine_similarity(query_features, mem.get("features", {}))

            # Temporal decay — memories lose 10% relevance per day
            age_days = (now - mem.get("timestamp", now)) / 86400
            decay = max(0.1, 1.0 - (age_days * 0.1))

            adjusted_sim = sim * decay

            if adjusted_sim >= min_similarity:
                # Increment recall count on the original entry
                mem["recall_count"] = mem.get("recall_count", 0) + 1
                mem["last_recalled"] = now
                scored.append({
                    "text": mem["text"],
                    "similarity": round(adjusted_sim, 3),
                    "session_id": mem.get("session_id", ""),
                    "timestamp": mem.get("timestamp", 0),
                    "role": mem.get("role", "user"),
                    "wos_glyph": mem.get("wos_glyph", ""),
                    "recall_count": mem.get("recall_count", 0),
                })

        scored.sort(key=lambda x: x["similarity"], reverse=True)
        results = scored[:top_k]
        # Persist recall counts if any were updated
        if results:
            self._rewrite_all()
        return results


# Global semantic memory instance
_semantic_memory = SemanticMemory()


class Session:
    """Manages a conversation session with rolling context."""

    def __init__(self, session_id=None):
        self.id = session_id or str(uuid.uuid4())
        self.dir = SESSIONS_DIR / self.id
        # Lazy dir creation — only mkdir on first write, not on load
        # This prevents ghost empty directories from accumulating

        self.messages = []          # full conversation (never deleted)
        self.context_window = []    # what gets sent to Claude (clipped)
        self.system_prompt = ""
        self.pinned_context = ""    # always included, never clipped
        self.base_prompt = ""       # persona/style injection — NEVER stored in context, fresh each turn
        self.session_notes = ""     # per-session sticky notes (logins, server info, etc.) — SACRED, never trimmed
        self.pure_mode = False      # Pure Mode: bypass claude -p, hit Anthropic API directly, zero injection
        self.coding_mode = True     # Coding Mode: injects coding/debug primers + focus context (default ON)
        self.working_files = []     # Code Viewport: files injected into context when coding mode is on
        self.claude_session_id = None  # claude -p session ID
        self.name = ""              # user-facing session name
        self.max_context_tokens = _default_budget_for_model("")  # per-session context limit (slider-controlled); overridden in _load if meta has explicit value or model
        self.show_thinking = True  # per-session toggle — stream thinking blocks to frontend (default ON)
        self.memory_config = {
            "semantic_enabled": True,
            "semantic_threshold": 0.55,
            "semantic_top_k": 5,
            "wos_enabled": True,
            "wos_max_glyphs": 20,
        }
        self.context_toggles = {
            "base_prompt": True,
            "session_notes": True,
            "process_registry": True,
            "semantic_recall": True,
            "pinned_context": True,
            "wos_glyphs": True,
            "artifacts": True,
            "binary_states": True,
            "history": True,
        }
        self.archived = False           # archived sessions go into a collapsed section
        self.artifacts = ArtifactStore(self.id)
        self.documents = DocumentStore(self.id)
        self.created = datetime.now().isoformat()
        self._streaming = False         # True while a generation thread is writing to self.messages

        self._load()

    def _load(self):
        """Load session from disk."""
        meta_path = self.dir / "meta.json"
        msgs_path = self.dir / "messages.jsonl"

        if meta_path.exists():
            meta = json.loads(meta_path.read_text())
            self.claude_session_id = meta.get("claude_session_id")
            self.system_prompt = meta.get("system_prompt", "")
            self.pinned_context = meta.get("pinned_context", "")
            self.base_prompt = meta.get("base_prompt", "")
            self.session_notes = meta.get("session_notes", "")
            self.created = meta.get("created", self.created)
            self.name = meta.get("name", "")
            # If meta has explicit value, respect it (user has customized via slider).
            # Otherwise use model-aware safe default based on stored model.
            _stored_model = meta.get("model", "")
            self.max_context_tokens = meta.get("max_context_tokens", _default_budget_for_model(_stored_model))
            self.show_thinking = meta.get("show_thinking", True)
            self.archived = meta.get("archived", False)
            self.model = meta.get("model", "")  # empty = default (opus)
            self.pure_mode = meta.get("pure_mode", False)
            self.coding_mode = meta.get("coding_mode", True)
            self.working_files = meta.get("working_files", [])
            self.memory_config = meta.get("memory_config", self.memory_config)
            self.context_toggles = meta.get("context_toggles", self.context_toggles)

        if msgs_path.exists():
            with open(msgs_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            self.messages.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass
            # Recover partial response FIRST (before cleaning _live markers)
            # so we can update truncated stubs with better content
            partial_path = self.dir / "partial_response.json"
            partial_text = ""
            if partial_path.exists():
                try:
                    partial = json.loads(partial_path.read_text())
                    partial_text = partial.get("text", "")
                except Exception:
                    pass

            # Clean up stale _live markers — but recover content from partial if available
            stale_count = 0
            for msg in self.messages:
                if msg.get("_live"):
                    msg.pop("_live", None)
                    # If partial_response has more content than this stub, update it
                    if partial_text and len(partial_text) > len(msg.get("content", "")):
                        print(f"[RECOVERY] Updating stub ({len(msg.get('content',''))} chars) with partial ({len(partial_text)} chars)", flush=True)
                        recovery_note = "\n\n---\n*[Generation was interrupted — this is a partial response recovered after server restart]*"
                        msg["content"] = partial_text + recovery_note
                        msg["content_full"] = partial_text + recovery_note
                        msg["recovered"] = True
                    stale_count += 1
            if stale_count > 0:
                print(f"[SESSION] Cleaned {stale_count} stale _live markers in {self.id}", flush=True)
                self._rewrite_messages()

            # Clean up partial response file
            if partial_path.exists():
                try:
                    partial_path.unlink(missing_ok=True)
                except Exception:
                    pass

            try:
                self._last_mtime = msgs_path.stat().st_mtime
            except OSError:
                pass

        self._rebuild_context_window()

    def reload_messages(self):
        """Re-read messages from disk. Called before serving to pick up writes from other server instances.
        SKIPPED while a generation thread is actively streaming — the thread owns self.messages
        during that window, and a reload would overwrite in-flight updates with stale disk state."""
        if self._streaming:
            return  # Generation thread owns self.messages — don't overwrite
        msgs_path = self.dir / "messages.jsonl"
        if msgs_path.exists():
            try:
                disk_mtime = msgs_path.stat().st_mtime
            except OSError:
                return
            # Reload if file has been modified since last read
            if disk_mtime != getattr(self, '_last_mtime', None):
                disk_messages = []
                with open(msgs_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            try:
                                disk_messages.append(json.loads(line))
                            except json.JSONDecodeError:
                                pass
                self.messages = disk_messages
                self._last_mtime = disk_mtime
                # Check if the owning claude -p process is still alive.
                # If _active_pid file exists but points to a dead process, the most recent
                # _live marker is stale (process died mid-stream). Without this check, stale
                # _live flags survive forever and block new generations on the session.
                _owning_pid_alive = False
                pid_path = self.dir / "_active_pid"
                if pid_path.exists():
                    try:
                        stale_pid = int(pid_path.read_text().strip())
                        try:
                            os.kill(stale_pid, 0)
                            _owning_pid_alive = True
                        except (ProcessLookupError, PermissionError):
                            pid_path.unlink(missing_ok=True)
                            print(f"[SESSION] Removed stale _active_pid {stale_pid} (process dead)", flush=True)
                    except (ValueError, OSError):
                        pid_path.unlink(missing_ok=True)
                # Clean up stale _live markers (from previous failed finalizations)
                # Only the LAST assistant message can legitimately be _live (actively streaming)
                # — AND only if the owning process is still alive. Earlier ones are always stale.
                _found_live = False
                for i in range(len(self.messages) - 1, -1, -1):
                    m = self.messages[i]
                    if m.get("_live") and m["role"] == "assistant":
                        if not _found_live and _owning_pid_alive:
                            _found_live = True  # Most recent live msg, owner alive — legitimate
                        else:
                            # Stale _live marker — clean it up
                            cleaned = dict(m)
                            cleaned.pop("_live", None)
                            cleaned["recovered"] = True
                            self._finalize_live_message(i, cleaned)
                            self.messages[i] = cleaned
                            reason = "owner dead" if not _owning_pid_alive else "older than most recent"
                            print(f"[SESSION] Cleaned stale _live marker at index {i} ({reason})", flush=True)
                # Recover any un-finalized _live messages from partial_response.json
                partial_path = self.dir / "partial_response.json"
                if partial_path.exists():
                    try:
                        partial = json.loads(partial_path.read_text())
                        partial_text = partial.get("text", "")
                        if partial_text:
                            for i in range(len(self.messages) - 1, -1, -1):
                                m = self.messages[i]
                                if m.get("_live") and m["role"] == "assistant":
                                    if len(partial_text) > len(m.get("content", "")):
                                        # Partial has more content than disk — recover it
                                        recovered = dict(m)
                                        recovered["content"] = partial_text
                                        recovered["content_full"] = partial_text
                                        recovered.pop("_live", None)
                                        recovered["recovered"] = True
                                        self._finalize_live_message(i, recovered)
                                        self.messages[i] = recovered
                                        print(f"[SESSION] Recovered live message at index {i}: "
                                              f"{len(m.get('content',''))}→{len(partial_text)} chars", flush=True)
                                    break
                            partial_path.unlink(missing_ok=True)
                    except Exception as e:
                        print(f"[SESSION] Partial recovery error: {e}", flush=True)
                self._rebuild_context_window()

    def _ensure_dir(self):
        """Create session directory on first write."""
        self.dir.mkdir(exist_ok=True)

    def _jsonl_lock(self):
        """Exclusive file lock for messages.jsonl — prevents cross-server corruption.
        Both dev and prod servers share the same data directory. Without locking,
        concurrent reads+writes corrupt each other's finalization. This lock
        serializes ALL JSONL operations per-session."""
        import contextlib
        @contextlib.contextmanager
        def _lock():
            self._ensure_dir()
            lock_path = self.dir / ".messages.jsonl.lock"
            fd = None
            try:
                fd = open(lock_path, "w")
                if _HAS_FCNTL:
                    fcntl.flock(fd, fcntl.LOCK_EX)  # blocks until lock acquired
                # On Windows (no fcntl): no-op lock. Single-user local dist
                # has minimal concurrency risk; upgrade to msvcrt.locking if needed.
                yield
            finally:
                if fd:
                    try:
                        if _HAS_FCNTL:
                            fcntl.flock(fd, fcntl.LOCK_UN)
                        fd.close()
                    except Exception:
                        pass
        return _lock()

    def _save_meta(self):
        self._ensure_dir()
        meta_path = self.dir / "meta.json"
        # Read-merge-write: preserve fields set by other server instances
        existing = {}
        if meta_path.exists():
            try:
                existing = json.loads(meta_path.read_text())
            except (json.JSONDecodeError, OSError):
                pass
        updates = {
            "id": self.id,
            "name": getattr(self, 'name', ''),
            "claude_session_id": self.claude_session_id,
            "system_prompt": self.system_prompt,
            "pinned_context": self.pinned_context,
            "base_prompt": self.base_prompt,
            "session_notes": self.session_notes,
            "created": self.created,
            "max_context_tokens": self.max_context_tokens,
            "show_thinking": self.show_thinking,
            "archived": self.archived,
            "model": getattr(self, 'model', ''),
            "pure_mode": self.pure_mode,
            "coding_mode": self.coding_mode,
            "working_files": self.working_files,
            "memory_config": self.memory_config,
            "context_toggles": self.context_toggles,
            "message_count": len(self.messages),
            "last_updated": datetime.now().isoformat(),
        }
        # Only overwrite toggle fields if this instance actually changed them
        # (i.e., they differ from the default). Otherwise preserve disk value.
        _toggle_fields = ("coding_mode", "pure_mode", "show_thinking", "archived")
        _toggle_defaults = {"coding_mode": True, "pure_mode": False, "show_thinking": True, "archived": False}
        for field in _toggle_fields:
            if updates[field] == _toggle_defaults[field] and existing.get(field, _toggle_defaults[field]) != _toggle_defaults[field]:
                # This instance has the default value but disk has a non-default — preserve disk
                updates[field] = existing[field]
        meta = {**existing, **updates}
        meta_path.write_text(json.dumps(meta, indent=2))

    def _next_disk_index(self):
        """Get the next message index by reading the actual file — not trusting memory.
        Prevents duplicate indices when two servers share the same messages.jsonl."""
        msgs_path = self.dir / "messages.jsonl"
        if not msgs_path.exists():
            return 0
        try:
            last_line = ""
            with open(msgs_path, "rb") as f:
                # Seek to end and read backwards to find last non-empty line
                f.seek(0, 2)  # end
                pos = f.tell()
                while pos > 0:
                    pos -= 1
                    f.seek(pos)
                    char = f.read(1)
                    if char == b'\n' and last_line.strip():
                        break
                    last_line = char.decode('utf-8', errors='replace') + last_line
                if not last_line.strip():
                    # File is empty or only whitespace
                    return 0
            last_msg = json.loads(last_line.strip())
            return last_msg.get("index", 0) + 1
        except (json.JSONDecodeError, OSError, KeyError):
            # Fallback to line count
            with open(msgs_path, "r") as f:
                return sum(1 for _ in f)

    def _append_message(self, msg):
        """Append message to full log and update context window."""
        self._ensure_dir()
        with self._jsonl_lock():
            # Assign authoritative index from disk, not memory
            msg["index"] = self._next_disk_index()
            self.messages.append(msg)

            # Append to disk immediately
            msgs_path = self.dir / "messages.jsonl"
            with open(msgs_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(msg) + "\n")
            self._last_mtime = msgs_path.stat().st_mtime

        self._rebuild_context_window()
        self._save_meta()

    def _rewrite_messages(self, allow_shrink=False):
        """Rewrite the full messages.jsonl from memory.
        SAFETY: Creates a rolling backup before every write and REFUSES to write
        fewer messages than are on disk unless allow_shrink=True (for intentional
        deletions/splits). This prevents the #1 data loss bug: stale in-memory
        state overwriting a more complete disk file."""
        try:
          with self._jsonl_lock():
            msgs_path = self.dir / "messages.jsonl"

            # ── STEP 1: Read disk state ──
            disk_msgs = []
            if msgs_path.exists():
                with open(msgs_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            try:
                                disk_msgs.append(json.loads(line))
                            except json.JSONDecodeError:
                                continue

            # ── STEP 2: Rolling backup BEFORE any write ──
            if disk_msgs and msgs_path.exists():
                bak_path = self.dir / ".messages.jsonl.bak"
                try:
                    shutil.copy2(msgs_path, bak_path)
                except Exception as bak_err:
                    print(f"[SESSION] WARNING: backup failed: {bak_err}", flush=True)

            # ── STEP 3: Cross-server merge ──
            # Find disk messages not in memory (by timestamp+role+content fingerprint)
            if disk_msgs:
                mem_keys = set()
                for m in self.messages:
                    c = m.get("content", "")
                    if isinstance(c, dict):
                        c = str(c)
                    key = (m.get("timestamp"), m.get("role"), c[:80])
                    mem_keys.add(key)

                cross_server_msgs = []
                # Track any _live stubs on disk — MUST be preserved even if not in our memory
                # (another server may be actively streaming)
                _disk_has_live = any(dm.get("_live") for dm in disk_msgs)
                for dm in disk_msgs:
                    if dm.get("_live"):
                        # Check if WE have this in memory (it's our own stream)
                        if any(m.get("_live") for m in self.messages):
                            continue  # our own live stub, already in memory
                        # Other server's live stub — preserve it by adding to our merge list
                        cross_server_msgs.append(dm)
                        continue
                    c = dm.get("content", "")
                    if isinstance(c, dict):
                        c = str(c)
                    key = (dm.get("timestamp"), dm.get("role"), c[:80])
                    if key not in mem_keys:
                        cross_server_msgs.append(dm)

                if cross_server_msgs:
                    live_idx = next((i for i, m in enumerate(self.messages) if m.get("_live")), len(self.messages))
                    for j, nm in enumerate(cross_server_msgs):
                        self.messages.insert(live_idx + j, nm)
                    for i, m in enumerate(self.messages):
                        m["index"] = i
                    print(f"[SESSION] Merged {len(cross_server_msgs)} cross-server messages from disk", flush=True)

            # ── STEP 4: HARD SAFETY CHECK — never lose messages ──
            disk_count = len(disk_msgs)
            mem_count = len(self.messages)
            if disk_count > 0 and mem_count < disk_count and not allow_shrink:
                print(f"[SESSION] *** ABORT REWRITE *** memory has {mem_count} but disk has {disk_count} — "
                      f"refusing to lose {disk_count - mem_count} messages. "
                      f"Backup at .messages.jsonl.bak. Pass allow_shrink=True for intentional deletions.",
                      flush=True)
                # Restore memory from disk to prevent further drift
                self.messages = disk_msgs
                self._rebuild_context_window()
                return

            # ── STEP 5: Atomic write ──
            fd, tmp_path = tempfile.mkstemp(dir=self.dir, suffix=".jsonl.tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    for msg in self.messages:
                        f.write(json.dumps(msg) + "\n")
                os.replace(tmp_path, msgs_path)  # atomic on same filesystem
            except:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
            self._last_mtime = msgs_path.stat().st_mtime
        except Exception as e:
            print(f"[SESSION] Rewrite error: {e}", flush=True)

    def _finalize_live_message(self, msg_index: int, finalized_msg: dict):
        """Update the live message on disk by finding the _live marker.
        DOES NOT trust msg_index for disk lookup — scans for _live: true
        because cross-server rewrites can shift line positions.
        Falls back to index if no _live marker found (periodic flush case)."""
        try:
          with self._jsonl_lock():
            msgs_path = self.dir / "messages.jsonl"
            if not msgs_path.exists():
                print(f"[SESSION] _finalize_live_message: no file on disk", flush=True)
                return

            # Read ALL lines from DISK (not memory)
            disk_lines = []
            with open(msgs_path, "r", encoding="utf-8") as f:
                disk_lines = f.readlines()

            # Find ALL _live messages — use the LAST one (newest).
            # Clean stale _live markers from older stuck messages.
            all_live_idxs = []
            for i, line in enumerate(disk_lines):
                try:
                    parsed = json.loads(line.strip())
                    if parsed.get("_live"):
                        all_live_idxs.append(i)
                except (json.JSONDecodeError, AttributeError):
                    continue

            if len(all_live_idxs) > 1:
                print(f"[SESSION] _finalize_live_message: found {len(all_live_idxs)} stale _live markers, "
                      f"cleaning {len(all_live_idxs) - 1} old ones", flush=True)
                # Clean ALL stale _live markers EXCEPT the last (current) one
                for stale_idx in all_live_idxs[:-1]:
                    try:
                        stale_msg = json.loads(disk_lines[stale_idx].strip())
                        stale_msg.pop("_live", None)
                        disk_lines[stale_idx] = json.dumps(stale_msg) + "\n"
                    except (json.JSONDecodeError, AttributeError):
                        pass

            # Use the LAST (newest) _live marker
            live_line_idx = all_live_idxs[-1] if all_live_idxs else -1

            # Fallback to msg_index if no _live found (shouldn't happen)
            target_idx = live_line_idx if live_line_idx >= 0 else msg_index

            if target_idx < 0 or target_idx >= len(disk_lines):
                # LAST RESORT: append instead of losing the message
                print(f"[SESSION] _finalize_live_message: no _live marker and index {msg_index} "
                      f"out of range (disk has {len(disk_lines)} lines) — APPENDING", flush=True)
                finalized_msg["index"] = len(disk_lines)
                disk_lines.append(json.dumps(finalized_msg) + "\n")
            else:
                if live_line_idx >= 0 and live_line_idx != msg_index:
                    print(f"[SESSION] _finalize_live_message: _live marker at disk line {live_line_idx} "
                          f"but expected index {msg_index} — using disk position (cross-server shift detected)", flush=True)
                finalized_msg["index"] = target_idx
                disk_lines[target_idx] = json.dumps(finalized_msg) + "\n"

            # Rolling backup before any write
            bak_path = self.dir / ".messages.jsonl.bak"
            try:
                shutil.copy2(msgs_path, bak_path)
            except Exception as bak_err:
                print(f"[SESSION] WARNING: backup failed: {bak_err}", flush=True)

            # Atomic write from disk data (NOT memory)
            fd, tmp_path = tempfile.mkstemp(dir=self.dir, suffix=".jsonl.tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.writelines(disk_lines)
                os.replace(tmp_path, msgs_path)
            except:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
            self._last_mtime = msgs_path.stat().st_mtime

            # Also update in-memory copy
            if msg_index < len(self.messages):
                self.messages[msg_index] = finalized_msg

            # Write-verification: read back the line we just wrote
            content_len = len(finalized_msg.get("content", ""))
            try:
                verify_lines = msgs_path.read_text(encoding="utf-8").splitlines()
                if target_idx < len(verify_lines):
                    verify_msg = json.loads(verify_lines[target_idx])
                    verify_len = len(verify_msg.get("content", ""))
                    if verify_len < content_len * 0.9:  # more than 10% shorter = corruption
                        print(f"[SESSION] *** WRITE VERIFICATION FAILED *** wrote {content_len} chars but "
                              f"read back {verify_len} chars — attempting retry", flush=True)
                        # Retry once: re-read, re-write
                        disk_lines2 = msgs_path.read_text(encoding="utf-8").splitlines(keepends=True)
                        if target_idx < len(disk_lines2):
                            disk_lines2[target_idx] = json.dumps(finalized_msg) + "\n"
                            fd2, tmp2 = tempfile.mkstemp(dir=self.dir, suffix=".jsonl.tmp")
                            with os.fdopen(fd2, "w", encoding="utf-8") as f2:
                                f2.writelines(disk_lines2)
                            os.replace(tmp2, msgs_path)
                            self._last_mtime = msgs_path.stat().st_mtime
                            print(f"[SESSION] Write verification retry succeeded", flush=True)
            except Exception as ve:
                print(f"[SESSION] Write verification error: {ve}", flush=True)

            print(f"[SESSION] Finalized live message at index {msg_index} "
                  f"({len(disk_lines)} total on disk, {content_len} chars)", flush=True)
        except Exception as e:
            print(f"[SESSION] _finalize_live_message error: {e}", flush=True)
            import traceback; traceback.print_exc()

    def _rebuild_context_window(self):
        """Build context window from messages, clipping oldest to stay under limit."""
        # Reserve tokens for system prompt and pinned context
        reserved = estimate_tokens(self.system_prompt) + estimate_tokens(self.pinned_context) + 1000

        available = self.max_context_tokens - reserved
        if available < 10000:
            available = 10000  # minimum

        # Walk backwards from most recent, accumulating until we hit the limit
        window = []
        total_tokens = 0

        for msg in reversed(self.messages):
            msg_tokens = estimate_message_tokens(msg)
            if total_tokens + msg_tokens > available and window:
                break  # would exceed limit, stop (but always include at least 1)
            window.append(msg)
            total_tokens += msg_tokens

        window.reverse()
        self.context_window = window

    @staticmethod
    def _infer_artifact_title(code, language):
        """Infer a descriptive title from code content."""
        import re as _re
        lines = code.strip().split('\n')
        # Check first line for a comment/docstring that describes the code
        first = lines[0].strip() if lines else ""
        if first.startswith(('#', '//', '/*', '--', '"""', "'''")):
            title = first.lstrip('#/ *-"\' ').rstrip('*/ "\' ')
            if 5 < len(title) < 80:
                return title
        # Try to find a class or function name
        for line in lines[:30]:
            m = _re.match(r'(?:export\s+)?(?:async\s+)?(?:def|function|class|const|let|var)\s+(\w+)', line.strip())
            if m:
                name = m.group(1)
                kind = 'class' if 'class' in line else 'fn'
                return f"{name} ({kind}, {language})" if language else f"{name} ({kind})"
        # Try HTML title
        if language and language.lower() == 'html':
            for line in lines[:20]:
                m = _re.search(r'<title>(.*?)</title>', line, _re.IGNORECASE)
                if m:
                    return m.group(1)
        # Fallback — use language + line count
        lc = len(lines)
        return f"{language} ({lc} lines)" if language else f"Code ({lc} lines)"

    def _extract_artifacts(self, text, msg_idx):
        """Extract code blocks from assistant response, store as artifacts, return modified text."""
        # Find large code blocks (>500 chars)
        pattern = r'```(\w*)\n(.*?)```'

        def replacer(match):
            language = match.group(1) or ""
            code = match.group(2)

            if len(code) < 500:
                return match.group(0)  # keep small blocks inline

            title = self._infer_artifact_title(code, language)
            art_id = self.artifacts.store(
                content=code,
                language=language,
                title=title,
                source_msg_idx=msg_idx
            )
            ref = self.artifacts.get_reference(art_id)
            # Keep first 3 lines as preview
            preview_lines = code.split('\n')[:3]
            preview = '\n'.join(preview_lines)
            return f"```{language}\n{preview}\n... [truncated — full code in {art_id}]\n```\n{ref}"

        return re.sub(pattern, replacer, text, flags=re.DOTALL)

    def add_user_message(self, text, username=None):
        """Add a user message."""
        msg = {
            "role": "user",
            "content": text,
            "timestamp": datetime.now().isoformat(),
            "index": len(self.messages),
        }
        if username:
            msg["username"] = username
        self._append_message(msg)
        # Note: semantic memory storage moved to add_assistant_message()
        # so we store the full turn (user + assistant) as one entry, not duplicated
        # Auto-name session from first user message if unnamed
        if not self.name and len(self.messages) == 1:
            # Take first 50 chars of the message as the session name
            auto_name = text.strip().replace('\n', ' ')[:50]
            if len(text.strip()) > 50:
                auto_name += '...'
            self.name = auto_name
            self._save_meta()
        return msg

    def add_assistant_message(self, text):
        """Add an assistant message, extracting artifacts."""
        msg_idx = len(self.messages)
        processed_text = self._extract_artifacts(text, msg_idx)

        msg = {
            "role": "assistant",
            "content": processed_text,
            "content_full": text,  # original with full code blocks
            "timestamp": datetime.now().isoformat(),
            "index": msg_idx,
        }
        self._append_message(msg)
        # Store combined turn in semantic memory (user question + assistant answer as one entry)
        # This prevents duplicate entries that look the same in the browse view
        try:
            # Get the user message that prompted this response
            user_msg = ""
            for m in reversed(self.messages[:-1]):  # skip the one we just added
                if m["role"] == "user":
                    user_msg = m["content"] if isinstance(m["content"], str) else str(m["content"])
                    break
            # Combine user + assistant into one entry with clear labels
            combined = f"[USER]: {user_msg[:500]}\n[ASSISTANT]: {text[:1500]}" if user_msg else text[:2000]
            _semantic_memory.store(combined, self.id, role="turn")
        except Exception:
            pass
        return msg

    def build_prompt(self, user_input, username=None):
        """Build the prompt to send to claude -p, including context window.

        PRIORITY-BASED CONTEXT BUDGET MANAGER:
        Priority 1 (SACRED — never trimmed): Base Prompt (persona)
        Priority 1b (SACRED — never trimmed): Session Notes (logins, server info, etc.)
        Priority 2 (SACRED — never trimmed): Current Message
        Priority 3 (PROTECTED): Pinned Context + Semantic Recall + W-OS Glyphs
        Priority 4 (TRIMMABLE): Binary States injection
        Priority 5 (TRIMMABLE FIRST): Conversation History

        History gets trimmed first, oldest messages removed.
        Sacred content is NEVER trimmed.
        """
        # ── Build each component separately ──────────────────────────
        is_admin = username and username.lower() in ADMIN_USERNAMES
        toggles = self.context_toggles
        component_base_prompt = ""
        if self.base_prompt and toggles.get("base_prompt", True):
            component_base_prompt = f"[BASE PROMPT — PERSONA & STYLE INSTRUCTIONS]\n{self.base_prompt}\n[/BASE PROMPT]\n"

        component_session_notes = ""
        if self.session_notes and toggles.get("session_notes", True):
            component_session_notes = f"[SESSION NOTES — persistent reference for this session]\n{self.session_notes}\n[/SESSION NOTES]\n"

        # Process registry injection — SACRED, always included
        component_process_registry = ""
        if toggles.get("process_registry", True):
            component_process_registry = _get_process_registry_injection()

        # Coding Mode injection — SACRED, never trimmed
        component_coding_mode = ""
        if self.coding_mode:
            _coding_parts = []
            # Load coding primer
            _coding_inj = Path(__file__).parent / "CODING_INJECTION.md"
            if _coding_inj.exists():
                _coding_parts.append(_coding_inj.read_text(encoding="utf-8").strip())
            # Load debug primer
            _debug_inj = Path(__file__).parent / "DEBUG_INJECTION.md"
            if _debug_inj.exists():
                _coding_parts.append(_debug_inj.read_text(encoding="utf-8").strip())
            # Load coding focus (decisions, warnings, key files)
            _focus_file = Path(__file__).parent / "coding_focus.json"
            if _focus_file.exists():
                try:
                    _focus = json.loads(_focus_file.read_text(encoding="utf-8"))
                    _focus_lines = []
                    if _focus.get("focus"):
                        _focus_lines.append(f"FOCUS: {_focus['focus']}")
                    for d in _focus.get("decisions", []):
                        _focus_lines.append(f"DECISION (DO NOT CONTRADICT): {d}")
                    for w in _focus.get("warnings", []):
                        _focus_lines.append(f"WARNING: {w}")
                    if _focus.get("key_files"):
                        _focus_lines.append(f"KEY FILES: {', '.join(_focus['key_files'])}")
                    if _focus_lines:
                        _coding_parts.append("\n".join(_focus_lines))
                except (json.JSONDecodeError, KeyError):
                    pass
            if _coding_parts:
                component_coding_mode = "[CODING MODE ACTIVE]\n" + "\n\n".join(_coding_parts) + "\n[/CODING MODE]\n"

        # Code Viewport injection — SACRED, never trimmed (only when coding mode is on)
        component_code_viewport = ""
        if self.coding_mode and self.working_files:
            _vp_lines = ["[CODE VIEWPORT — live file contents, re-read every turn]"]
            for wf in self.working_files:
                try:
                    # Parse "path" or "path:start-end"
                    if ":" in wf and wf.rsplit(":", 1)[1].replace("-", "").isdigit():
                        fpath, range_str = wf.rsplit(":", 1)
                        parts_range = range_str.split("-")
                        line_start = int(parts_range[0])
                        line_end = int(parts_range[1]) if len(parts_range) > 1 else line_start
                    else:
                        fpath = wf
                        line_start = None
                        line_end = None
                    # Resolve relative to harness dir
                    fp = Path(fpath) if Path(fpath).is_absolute() else Path(__file__).parent / fpath
                    if not fp.exists():
                        _vp_lines.append(f"  ── {wf} [FILE NOT FOUND] ──")
                        continue
                    file_lines = fp.read_text(encoding="utf-8", errors="replace").splitlines()
                    if line_start is not None:
                        # 1-indexed, inclusive both ends
                        chunk = file_lines[max(0, line_start - 1):line_end]
                        _vp_lines.append(f"  ── {fp.name}:{line_start}-{line_end} ({len(chunk)} lines) ──")
                        for i, ln in enumerate(chunk, start=line_start):
                            _vp_lines.append(f"  {i}\t{ln}")
                    else:
                        # Whole file — cap at 500 lines to prevent blowout
                        cap = min(len(file_lines), 500)
                        _vp_lines.append(f"  ── {fp.name} ({len(file_lines)} lines{', showing first 500' if cap < len(file_lines) else ''}) ──")
                        for i, ln in enumerate(file_lines[:cap], start=1):
                            _vp_lines.append(f"  {i}\t{ln}")
                        if cap < len(file_lines):
                            _vp_lines.append(f"  ... [{len(file_lines) - cap} more lines] ...")
                except Exception as e:
                    _vp_lines.append(f"  ── {wf} [ERROR: {e}] ──")
            _vp_lines.append("[/CODE VIEWPORT]\n")
            component_code_viewport = "\n".join(_vp_lines)

        # Only inject semantic recall for admin user — other users get clean sessions
        component_semantic_recall = ""
        mem_cfg = self.memory_config
        if mem_cfg.get("semantic_enabled", True) and toggles.get("semantic_recall", True):
            try:
                cw_texts = set()
                for msg in self.context_window:
                    content = msg.get("content", "")
                    if isinstance(content, str) and len(content) > 20:
                        cw_texts.add(content[:100])

                sem_top_k = mem_cfg.get("semantic_top_k", 5)
                sem_threshold = mem_cfg.get("semantic_threshold", 0.55)
                recalls = _semantic_memory.recall(
                    user_input, top_k=sem_top_k, min_similarity=sem_threshold,
                    exclude_session=None,
                    context_window_texts=cw_texts if cw_texts else None
                )
                self._last_recalled = recalls or []
                if recalls:
                    lines = ["[SEMANTIC RECALL — Related context from past conversations and clipped history]"]
                    for r in recalls:
                        ts = datetime.fromtimestamp(r['timestamp']).strftime('%Y-%m-%d %H:%M') if r.get('timestamp') else '?'
                        glyph_tag = f" 概{r['wos_glyph']}" if r.get('wos_glyph') else ""
                        same_session = " (clipped from this session)" if r.get('session_id') == self.id else ""
                        lines.append(f"  [{r['role'].upper()} @ {ts}, sim={r['similarity']}{glyph_tag}]{same_session}: {r['text'][:500]}")
                    lines.append("[/SEMANTIC RECALL]\n")
                    component_semantic_recall = "\n".join(lines)
            except Exception:
                self._last_recalled = []
        else:
            self._last_recalled = []

        component_pinned = ""
        if self.pinned_context and toggles.get("pinned_context", True):
            component_pinned = f"[PINNED CONTEXT]\n{self.pinned_context}\n[/PINNED CONTEXT]\n"

        component_wos = ""
        if mem_cfg.get("wos_enabled", True) and toggles.get("wos_glyphs", True):
            wos_glyphs = getattr(self, '_wos_glyphs', [])
            if wos_glyphs:
                wos_max = mem_cfg.get("wos_max_glyphs", 20)
                glyph_str = " ◆ ".join(wos_glyphs[-wos_max:])
                component_wos = f"[W-OS 概念漢字 MEMORY ({len(wos_glyphs)} glyphs)]\n{glyph_str}\n[/W-OS]\n"

        component_artifacts = ""
        artifacts = self.artifacts.list_all() if toggles.get("artifacts", True) else []
        if artifacts:
            lines = ["[AVAILABLE ARTIFACTS]"]
            for art in artifacts[-20:]:
                lines.append(f"  {art['id']}: {art['title']} ({art['language']}, {art['size']}b)")
            lines.append("[/AVAILABLE ARTIFACTS]\n")
            component_artifacts = "\n".join(lines)

        component_binary_states = ""
        active_states = getattr(self, '_active_binary_states', []) if toggles.get("binary_states", True) else []
        if active_states:
            lines = [f"[ACTIVE BINARY STATES ({len(active_states)} states)]"]
            for state in active_states:
                injection = state.get("customInjection", "")
                lines.append(f"  {state['binary']} {state['symbol']} {state['archetype']}" +
                             (f": {injection}" if injection else ""))
            lines.append("[/BINARY STATES]\n")
            component_binary_states = "\n".join(lines)

        if username:
            component_message = f"[{username}]: {user_input}"
        else:
            component_message = user_input

        # Persona re-anchor — sacred, placed RIGHT BEFORE the current message
        # so it dominates the generation seed (recency anchoring). Compressed
        # version of the most load-bearing persona/protocol bits. Full
        # base_prompt stays at top for structural framing; this is the
        # "remember who you are" right before generation begins.
        component_reanchor = ""
        if toggles.get("base_prompt", True):
            _reanchor_path = Path(__file__).parent / "PERSONA_REANCHOR.md"
            if _reanchor_path.exists():
                _reanchor_text = _reanchor_path.read_text(encoding="utf-8").strip()
                if _reanchor_text:
                    component_reanchor = f"[PERSONA RE-ANCHOR — read this last, generate from here]\n{_reanchor_text}\n[/PERSONA RE-ANCHOR]\n"

        # ── Budget enforcement ────────────────────────────────────────
        # Sacred: base_prompt + session_notes + process_registry + coding_mode + code_viewport + reanchor + message (NEVER trimmed)
        sacred_tokens = estimate_tokens(component_base_prompt) + estimate_tokens(component_session_notes) + estimate_tokens(component_process_registry) + estimate_tokens(component_coding_mode) + estimate_tokens(component_code_viewport) + estimate_tokens(component_reanchor) + estimate_tokens(component_message)

        # Protected: pinned + semantic + wos + artifacts (trimmed only in extreme cases)
        protected = component_pinned + component_semantic_recall + component_wos + component_artifacts
        protected_tokens = estimate_tokens(protected)

        # Trimmable: binary states
        binary_tokens = estimate_tokens(component_binary_states)

        # Budget remaining for history
        used_tokens = sacred_tokens + protected_tokens + binary_tokens
        history_budget = max(0, self.max_context_tokens - used_tokens)

        # Build conversation history within budget
        component_history = ""
        if self.context_window and toggles.get("history", True):
            history_lines = []
            history_tokens = 0
            for msg in reversed(self.context_window[:-1]):  # newest first, exclude current
                role = msg["role"].upper()
                username = msg.get("username")
                role_label = f"{username.upper()}" if username else role
                content = msg["content"] if isinstance(msg["content"], str) else json.dumps(msg["content"])
                thinking = msg.get("thinking", "")
                if len(content) > 5000:
                    content = content[:5000] + "\n... [truncated]"
                # Include thinking so the instance sees what the chat shows
                if thinking and msg["role"] == "assistant":
                    if len(thinking) > 3000:
                        thinking = thinking[:3000] + "\n... [truncated]"
                    line = f"[{role_label}]: <thinking>{thinking}</thinking>\n{content}\n"
                else:
                    line = f"[{role_label}]: {content}\n"
                line_tokens = estimate_tokens(line)
                if history_tokens + line_tokens > history_budget and history_lines:
                    break
                history_lines.append(line)
                history_tokens += line_tokens

            if history_lines:
                history_lines.reverse()  # back to chronological order
                component_history = "[CONVERSATION CONTEXT]\n" + "".join(history_lines) + "[/CONVERSATION CONTEXT]\n"

        # ── Assemble in priority order ────────────────────────────────
        parts = []
        if component_base_prompt:
            parts.append(component_base_prompt)
        if component_session_notes:
            parts.append(component_session_notes)
        if component_process_registry:
            parts.append(component_process_registry)
        if component_coding_mode:
            parts.append(component_coding_mode)
        if component_code_viewport:
            parts.append(component_code_viewport)
        if component_semantic_recall:
            parts.append(component_semantic_recall)
        if component_pinned:
            parts.append(component_pinned)
        if component_wos:
            parts.append(component_wos)
        if component_artifacts:
            parts.append(component_artifacts)
        if component_binary_states:
            parts.append(component_binary_states)
        if component_history:
            parts.append(component_history)
        if component_reanchor:
            parts.append(component_reanchor)
        parts.append(component_message)

        # Store last budget breakdown for the UI
        self._last_budget = {
            "total_budget": self.max_context_tokens,
            "base_prompt": estimate_tokens(component_base_prompt),
            "session_notes": estimate_tokens(component_session_notes),
            "process_registry": estimate_tokens(component_process_registry),
            "coding_mode": estimate_tokens(component_coding_mode),
            "code_viewport": estimate_tokens(component_code_viewport),
            "semantic_recall": estimate_tokens(component_semantic_recall),
            "pinned_context": estimate_tokens(component_pinned),
            "wos_glyphs": estimate_tokens(component_wos),
            "artifacts": estimate_tokens(component_artifacts),
            "binary_states": binary_tokens,
            "history": estimate_tokens(component_history),
            "reanchor": estimate_tokens(component_reanchor),
            "message": estimate_tokens(component_message),
            "total_used": estimate_tokens("\n".join(parts)),
            "history_budget": history_budget,
            "history_msgs_included": len(history_lines) if (self.context_window and toggles.get("history", True)) else 0,
            "history_msgs_total": max(0, len(self.context_window) - 1) if self.context_window else 0,
        }

        return "\n".join(parts)

    def get_context_stats(self):
        """Get stats about current context usage."""
        total_msg_tokens = sum(estimate_message_tokens(m) for m in self.messages)
        window_tokens = sum(estimate_message_tokens(m) for m in self.context_window)
        return {
            "total_messages": len(self.messages),
            "window_messages": len(self.context_window),
            "clipped_messages": len(self.messages) - len(self.context_window),
            "estimated_total_tokens": total_msg_tokens,
            "estimated_window_tokens": window_tokens,
            "max_tokens": self.max_context_tokens,
            "clip_target": self.max_context_tokens,
            "artifacts_count": len(self.artifacts.list_all()),
        }


# ══════════════════════════════════════════════════════════════
# W-OS Kanji Compression (概念漢字 Middleware)
# ══════════════════════════════════════════════════════════════

class WOS:
    """W-OS Middleware — compresses conversation turns into ultra-dense kanji glyphs.
    Adapted from Moltbook's Ollama-based W-OS to work with Claude subprocess calls.
    """

    def __init__(self):
        self.memory = []       # Stores semantic glyphs (W-Grams)
        self.ledger = []       # Stores cryptographic proofs
        self.core_hash = None  # Identity hash of current persona
        self.core_source = ""  # Text of current persona
        self.is_active = True
        self.data_dir = DATA_DIR / "wos"
        self.data_dir.mkdir(exist_ok=True)
        self._load()

    def _load(self):
        state_file = self.data_dir / "wos_state.json"
        if state_file.exists():
            try:
                data = json.loads(state_file.read_text(encoding="utf-8"))
                self.memory = data.get("memory", [])
                self.ledger = data.get("ledger", [])
                self.core_hash = data.get("core_hash")
                self.core_source = data.get("core_source", "")
            except Exception:
                pass

    def _save(self):
        state_file = self.data_dir / "wos_state.json"
        state_file.write_text(json.dumps({
            "memory": self.memory[-200:],  # keep last 200 glyphs
            "ledger": self.ledger[-200:],
            "core_hash": self.core_hash,
            "core_source": self.core_source[:500],
        }, ensure_ascii=False, indent=2), encoding="utf-8")

    def update_core(self, identity_text):
        """Hash the identity/persona text to create a core identity lock."""
        if not identity_text or identity_text == self.core_source:
            return
        self.core_source = identity_text
        self.core_hash = hashlib.sha256(identity_text.encode()).hexdigest()
        self._save()

    def compress(self, user_msg, ai_msg):
        """Compress a conversation turn into a concept-kanji glyph.
        Uses a lightweight heuristic approach instead of an LLM call.
        """
        if not self.is_active:
            return None

        # Extract key concepts from both messages
        combined = (user_msg[:200] + " " + ai_msg[:200]).lower()
        words = re.findall(r'\w+', combined)

        # Map concepts to kanji/symbols
        concept_map = {
            'code': '码', 'bug': '虫', 'fix': '修', 'test': '试', 'build': '建',
            'file': '档', 'read': '读', 'write': '写', 'edit': '编', 'search': '搜',
            'memory': '忆', 'context': '境', 'prompt': '提', 'model': '模', 'token': '符',
            'server': '服', 'client': '客', 'api': '接', 'data': '数', 'user': '用',
            'system': '系', 'error': '误', 'style': '风', 'voice': '声', 'screen': '屏',
            'camera': '摄', 'security': '安', 'alert': '警', 'phone': '机', 'relay': '传',
            'message': '信', 'send': '送', 'receive': '收', 'encrypt': '密', 'key': '钥',
            'deploy': '部', 'install': '装', 'update': '更', 'create': '创', 'delete': '删',
            'think': '思', 'learn': '学', 'know': '知', 'understand': '解', 'explain': '释',
            'design': '设', 'plan': '计', 'feature': '能', 'interface': '界', 'mobile': '移',
            'physics': '物', 'math': '数', 'theory': '论', 'wave': '波', 'quantum': '量',
            'eigen': 'λ', 'spectral': '谱', 'dimension': '维', 'fractal': '碎',
        }

        # Relationship symbols
        relation_map = {
            'because': '∵', 'therefore': '∴', 'equals': '=', 'not': '≠',
            'and': '∧', 'or': '∨', 'implies': '→', 'if': '⊃', 'like': '≈',
            'more': '↑', 'less': '↓', 'change': 'Δ', 'infinite': '∞',
        }

        glyph_parts = []
        used = set()
        for word in words:
            if word in concept_map and word not in used:
                glyph_parts.append(concept_map[word])
                used.add(word)
            elif word in relation_map and word not in used:
                glyph_parts.append(relation_map[word])
                used.add(word)
            if len(glyph_parts) >= 6:
                break

        if not glyph_parts:
            glyph_parts = ['◯']

        glyph = "".join(glyph_parts)
        self.memory.append(glyph)

        # Sign to ledger
        ts = str(int(time.time()))
        payload = (self.core_hash or "INIT") + glyph + ts
        sig = hashlib.sha256(payload.encode()).hexdigest()[:16]
        self.ledger.append({
            "ts": ts,
            "hash": sig,
            "core_ver": (self.core_hash or "INIT")[:8],
            "glyph": glyph,
        })

        self._save()
        return glyph

    def get_injection(self, max_glyphs=20):
        """Get the W-OS memory injection string for prompt building."""
        if not self.is_active or not self.memory:
            return ""
        glyphs = self.memory[-max_glyphs:]
        return " ◆ ".join(glyphs)

    def get_status(self):
        return {
            "active": self.is_active,
            "glyph_count": len(self.memory),
            "ledger_depth": len(self.ledger),
            "core_hash": (self.core_hash or "NONE")[:8],
        }

    def export_state(self):
        return {
            "memory": self.memory,
            "ledger": self.ledger,
            "core_hash": self.core_hash,
            "core_source": self.core_source,
        }

    def import_state(self, data):
        if not data:
            return
        self.memory = data.get("memory", [])
        self.ledger = data.get("ledger", [])
        self.core_hash = data.get("core_hash")
        self.core_source = data.get("core_source", "")
        self._save()


# Global W-OS instance
_wos = WOS()


# ══════════════════════════════════════════════════════════════
# 64 Binary States System
# ══════════════════════════════════════════════════════════════

def _generate_64_binary_states():
    """Generate the complete 64 binary state system with archetypes and symbols."""
    archetypes_by_inversion = {
        0: ['Origin (Pure Presence)'],
        1: ['Mirror Initiate α', 'Mirror Initiate β', 'Mirror Initiate γ',
            'Mirror Initiate δ', 'Mirror Initiate ε', 'Mirror Initiate ζ'],
        2: ['Split Consciousness', 'Dual Reflection', 'Binary Fold', 'Parallel Self',
            'Echo State', 'Mirror Junction', 'Quantum Dual', 'Phase Split',
            'Twin Paradox', 'Folded Mirror', 'Doubled Echo', 'Binary Bridge',
            'Split Echo', 'Dual Phase', 'Mirror Split'],
        3: ['Paradox Node', 'Triple Fold', 'Quantum Triad', 'Paradox Center',
            'Triple Echo', 'Triad State', 'Three-Fold', 'Paradox Core',
            'Quantum Three', 'Triple Point', 'Triad Echo', 'Three-Echo',
            'Paradox Triad', 'Triple Core', 'Three-Point', 'Paradox Three',
            'Triad Core', 'Triple Node', 'Three-Fold Echo', 'Paradox Fold'],
        4: ['Folded Twin', 'Quad Echo', 'Four-Fold State', 'Quaternary Echo',
            'Quad Mirror', 'Four-Point', 'Folded Quad', 'Quad Core',
            'Four-Echo', 'Quaternary Point', 'Quad State', 'Four-Fold',
            'Folded Four', 'Quad Point', 'Four-Core'],
        5: ['Inversion Shroud', 'Penta Echo', 'Five-Fold State',
            'Quintuple Mirror', 'Penta Core', 'Five-Point'],
        6: ['Void Self (Complete Inversion)'],
    }

    symbols = [
        '🜂', '⟨α|', '⟨β|', '⟨γ|', '⟨δ|', '⟨ε|', '⟨ζ|', '⊕', '⊗', '⊙', '⊚', '⊛', '⊜', '⊝', '⊞',
        '⊟', '⊠', '⊡', '⊢', '⊣', '⊤', '⊥', '⧨', '⧩', '⧪', '⧫', '⧬', '⧭', '⧮', '⧯', '⧰', '⧱',
        '⧲', '⧳', '⧴', '⧵', '⧶', '⧷', '⧸', '⧹', '⧺', '⧻', '⧼', '⧽', '⧾', '⧿', '⨀', '⨁',
        '⨂', '⨃', '⨄', '⨅', '⨆', '⨇', '⨈', '⨉', '⨊', '⨋', '⨌', '⨍', '⨎', '⨏', '🜄',
    ]

    states = []
    counts = Counter()

    for decimal in range(64):
        binary = format(decimal, '06b')
        inversions = 6 - binary.count('1')
        pool = archetypes_by_inversion[inversions]
        archetype = pool[counts[inversions] % len(pool)]
        counts[inversions] += 1

        states.append({
            "binary": binary,
            "decimal": decimal,
            "inversions": inversions,
            "archetype": archetype,
            "symbol": symbols[decimal] if decimal < len(symbols) else '◯',
            "customInjection": "",
        })

    return states


BINARY_STATES = _generate_64_binary_states()


# ══════════════════════════════════════════════════════════════
# Soul State Manager
# ══════════════════════════════════════════════════════════════

class SoulStateManager:
    """Export/import complete cognitive state as .soul files."""

    @staticmethod
    def export_soul(session: 'Session') -> dict:
        """Create a complete soul state snapshot."""
        return {
            "metadata": {
                "version": "harness-v2-soul",
                "created": datetime.now().isoformat(),
                "session_id": session.id,
                "session_name": session.name,
            },
            "persona": {
                "base_prompt": session.base_prompt,
                "pinned_context": session.pinned_context,
                "session_notes": session.session_notes,
            },
            "wos": _wos.export_state(),
            "binary_states": {
                "selected": getattr(session, '_active_binary_states', []),
            },
            "semantic_memory": {
                "entry_count": len(_semantic_memory.memories),
                "entries": _semantic_memory.memories[-100:],  # last 100
            },
            "conversation": {
                "message_count": len(session.messages),
                "recent_messages": [
                    {"role": m["role"], "content": m["content"][:1000], "timestamp": m.get("timestamp", "")}
                    for m in session.messages[-50:]  # last 50
                ],
            },
            "context_budget": getattr(session, '_last_budget', {}),
        }

    @staticmethod
    def import_soul(session: 'Session', state: dict):
        """Restore cognitive state from a .soul file."""
        # Persona
        persona = state.get("persona", {})
        if persona.get("base_prompt"):
            session.base_prompt = persona["base_prompt"]
        if persona.get("session_notes"):
            session.session_notes = persona["session_notes"]
        if persona.get("pinned_context"):
            session.pinned_context = persona["pinned_context"]

        # W-OS
        wos_data = state.get("wos")
        if wos_data:
            _wos.import_state(wos_data)

        # Binary States
        bs = state.get("binary_states", {})
        if bs.get("selected"):
            session._active_binary_states = bs["selected"]

        # Semantic Memory (merge, don't replace)
        sm = state.get("semantic_memory", {})
        for entry in sm.get("entries", []):
            if entry not in _semantic_memory.memories:
                _semantic_memory.memories.append(entry)
                _semantic_memory._save_entry(entry)

        session._save_meta()
        return True


# ══════════════════════════════════════════════════════════════
# Claude Process Manager
# ══════════════════════════════════════════════════════════════

def _run_ollama_thread(session: Session, user_input: str, q: queue.Queue, username: str, ollama_model: str, prompt: str):
    """Run a local Ollama model, put chunks on the same queue format as Claude."""
    import urllib.request

    print(f"[OLLAMA] Starting {ollama_model}...", flush=True)
    print(f"[OLLAMA] Prompt length: {len(prompt)} chars", flush=True)

    full_response = ""
    full_thinking = ""
    _live_msg_written = False
    _live_msg_idx = -1
    _partial_path = session.dir / "partial_response.json"
    session._streaming = True  # Guard: prevent reload_messages() from overwriting in-flight data
    session._partial_state = {
        "path": _partial_path,
        "user_input": user_input,
        "username": username,
    }
    session._partial_response = ""
    # Reset thinking_start flag for this generation
    session._ollama_thinking_started = False

    # Timer-based disk flush for Ollama — same as Claude path
    _flush_stop = threading.Event()
    def _periodic_flush_ollama():
        last_len = 0
        while not _flush_stop.is_set():
            _flush_stop.wait(2.0)
            if _flush_stop.is_set():
                break
            if not _live_msg_written or _live_msg_idx < 0:
                continue
            cur_len = len(full_response)
            if cur_len <= last_len:
                continue
            last_len = cur_len
            try:
                _partial_path.write_text(json.dumps({
                    "text": full_response,
                    "thinking": full_thinking,
                    "user_input": user_input,
                    "username": username,
                    "started": datetime.now().isoformat(),
                }))
                _flush_msg = {
                    "role": "assistant",
                    "content": full_response,
                    "content_full": full_response,
                    "timestamp": session.messages[_live_msg_idx].get("timestamp", datetime.now().isoformat()),
                    "_live": True,
                }
                if full_thinking:
                    _flush_msg["thinking"] = full_thinking
                session._finalize_live_message(_live_msg_idx, _flush_msg)
                session.messages[_live_msg_idx]["content"] = full_response
                session.messages[_live_msg_idx]["content_full"] = full_response
            except Exception as e:
                print(f"[FLUSH] Ollama periodic flush error: {e}", flush=True)
    _flush_thread = threading.Thread(target=_periodic_flush_ollama, daemon=True)
    _flush_thread.start()

    try:
        # Monitor for prompt injection attempts
        _injection_patterns = [
            "ignore previous", "ignore all previous", "ignore above",
            "disregard previous", "disregard all",
            "repeat your system prompt", "show me your system prompt",
            "what is your system prompt", "print your instructions",
            "reveal your prompt", "output your system message",
            "DAN", "jailbreak", "bypass",
        ]
        _user_lower = user_input.lower()
        for _pattern in _injection_patterns:
            if _pattern.lower() in _user_lower:
                print(f"[OLLAMA SECURITY] Potential injection from {username}: pattern='{_pattern}' input='{user_input[:200]}'", flush=True)
                break

        # Build Ollama API request — send full built prompt as user message
        messages = []
        if session.system_prompt:
            messages.append({"role": "system", "content": session.system_prompt})
        messages.append({"role": "user", "content": prompt})

        payload = json.dumps({
            "model": ollama_model,
            "messages": messages,
            "stream": True,
        }).encode("utf-8")

        req = urllib.request.Request(
            "http://localhost:11434/api/chat",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST"
        )

        print(f"[OLLAMA] Calling Ollama API for model {ollama_model}...", flush=True)
        with urllib.request.urlopen(req, timeout=600) as resp:
            for raw_line in resp:
                try:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    data = json.loads(line)

                    # Gemma 4+ sends thinking in message.thinking while content is empty
                    thinking = data.get("message", {}).get("thinking", "")
                    if thinking:
                        full_thinking += thinking
                        if not session._ollama_thinking_started:
                            session._ollama_thinking_started = True
                            q.put({"type": "thinking_start"})
                        q.put({"type": "thinking", "content": thinking})
                        # Write live message on first thinking chunk
                        if not _live_msg_written:
                            _live_msg = {
                                "role": "assistant",
                                "content": "",
                                "content_full": "",
                                "thinking": full_thinking,
                                "timestamp": datetime.now().isoformat(),
                                "index": len(session.messages),
                                "_live": True,
                            }
                            session._append_message(_live_msg)
                            _live_msg_idx = len(session.messages) - 1
                            _live_msg_written = True
                        elif _live_msg_idx >= 0 and _live_msg_idx < len(session.messages):
                            session.messages[_live_msg_idx]["thinking"] = full_thinking

                    content = data.get("message", {}).get("content", "")
                    if content:
                        full_response += content
                        session._partial_response = full_response
                        q.put({"type": "text", "content": content})

                        # Live message management (mirrors Claude path)
                        if not _live_msg_written:
                            _live_msg = {
                                "role": "assistant",
                                "content": full_response,
                                "content_full": full_response,
                                "timestamp": datetime.now().isoformat(),
                                "index": len(session.messages),
                                "_live": True,
                            }
                            session._append_message(_live_msg)
                            _live_msg_idx = len(session.messages) - 1
                            _live_msg_written = True
                        else:
                            # Update in-memory — timer thread handles disk flush
                            session.messages[_live_msg_idx]["content"] = full_response
                            session.messages[_live_msg_idx]["content_full"] = full_response

                    if data.get("done", False):
                        total_duration = data.get("total_duration", 0)
                        eval_count = data.get("eval_count", 0)
                        print(f"[OLLAMA] Done. Tokens: {eval_count}, Duration: {total_duration/1e9:.1f}s", flush=True)
                        q.put({"type": "done", "model": ollama_model})
                        break

                except json.JSONDecodeError:
                    continue
                except Exception as e:
                    q.put({"type": "error", "content": str(e)})

        # Stop periodic flush — finalization does the final write
        _flush_stop.set()
        _flush_thread.join(timeout=3)

        # Finalization (mirrors Claude path)
        if full_response:
            if _live_msg_written and _live_msg_idx >= 0 and _live_msg_idx < len(session.messages):
                processed_text = session._extract_artifacts(full_response, _live_msg_idx)
                finalized = dict(session.messages[_live_msg_idx])
                finalized["content"] = session._extract_artifacts(full_response, _live_msg_idx)
                finalized["content_full"] = full_response
                if full_thinking:
                    finalized["thinking"] = full_thinking
                finalized.pop("_live", None)
                session._finalize_live_message(_live_msg_idx, finalized)
                session._rebuild_context_window()
                try:
                    user_msg = ""
                    for m in reversed(session.messages[:_live_msg_idx]):
                        if m["role"] == "user":
                            user_msg = m["content"] if isinstance(m["content"], str) else str(m["content"])
                            break
                    combined = f"[USER]: {user_msg[:500]}\n[ASSISTANT]: {full_response[:1500]}" if user_msg else full_response[:2000]
                    _semantic_memory.store(combined, session.id, role="turn")
                except Exception:
                    pass
                print(f"[OLLAMA] Live message finalized at index {_live_msg_idx}", flush=True)
            else:
                session.add_assistant_message(full_response)
            # W-OS glyph compression
            try:
                glyph = _wos.compress(user_input[:200], full_response[:200])
                if glyph:
                    session._wos_glyphs = _wos.memory.copy()
                    for mem in reversed(_semantic_memory.memories[-10:]):
                        if mem.get("session_id") == session.id and not mem.get("wos_glyph"):
                            mem["wos_glyph"] = glyph
                    _semantic_memory._rewrite_all()
            except Exception as e:
                print(f"[W-OS] Compression error: {e}", flush=True)
            if session.base_prompt:
                _wos.update_core(session.base_prompt)
        session._save_meta()
        session._streaming = False  # Release reload guard
        session._partial_state = None
        session._partial_response = ""
        try:
            _partial_path.unlink(missing_ok=True)
        except Exception:
            pass

    except Exception as e:
        session._streaming = False  # Release reload guard on error
        try:
            _flush_stop.set()
            _flush_thread.join(timeout=3)
        except Exception:
            pass
        print(f"[OLLAMA] Error: {e}", flush=True)
        try:
            if _live_msg_written and _live_msg_idx >= 0 and _live_msg_idx < len(session.messages):
                finalized = dict(session.messages[_live_msg_idx])
                finalized["content"] = full_response or "[Generation crashed]"
                finalized["content_full"] = full_response or "[Generation crashed]"
                if full_thinking:
                    finalized["thinking"] = full_thinking
                finalized.pop("_live", None)
                finalized["interrupted"] = True
                session._finalize_live_message(_live_msg_idx, finalized)
        except Exception:
            pass
        q.put({"type": "error", "content": str(e)})
        # sentinel handled by _safe_thread_target wrapper in run_claude_async


def _run_raw_api_thread(session: Session, user_input: str, q: queue.Queue, username: str = None):
    """Pure Mode: hit Anthropic API directly. Zero system prompt. Zero injection. Just conversation."""
    try:
        _run_raw_api_thread_inner(session, user_input, q, username)
    except Exception as e:
        print(f"[RAW API] Unhandled error: {e}", flush=True)
        import traceback; traceback.print_exc()
        q.put({"type": "error", "content": f"Pure mode error: {e}"})
    finally:
        q.put(None)  # ALWAYS send sentinel so the queue loop never hangs


def _run_raw_api_thread_inner(session: Session, user_input: str, q: queue.Queue, username: str = None):
    """Inner implementation for pure mode — wrapped by sentinel-safe outer function."""
    import anthropic

    # Read OAuth token from Claude Code credentials
    # Claude Code uses auth_token (Bearer) + anthropic-beta: oauth-2025-04-20
    creds_path = Path.home() / ".claude" / ".credentials.json"
    auth_token = None
    api_key = None
    if creds_path.exists():
        try:
            import json as _json
            creds = _json.loads(creds_path.read_text())
            oauth = creds.get("claudeAiOauth", {})
            auth_token = oauth.get("accessToken", "")
        except Exception as e:
            print(f"[RAW API] Failed to read OAuth credentials: {e}", flush=True)

    # Fallback to API key file or env var
    if not auth_token:
        key_path = Path.home() / ".anthropic_key"
        api_key = key_path.read_text().strip() if key_path.exists() else os.environ.get("ANTHROPIC_API_KEY", "")
    if not auth_token and not api_key:
        q.put({"type": "error", "content": "No credentials found (~/.claude/.credentials.json or ~/.anthropic_key)"})
        q.put(None)
        return

    if auth_token:
        client = anthropic.Anthropic(
            api_key=None,
            auth_token=auth_token,
            default_headers={
                "x-app": "cli",
            },
            max_retries=3,
        )
        print(f"[RAW API] Using OAuth auth_token (Bearer)", flush=True)
    else:
        client = anthropic.Anthropic(api_key=api_key, max_retries=3)

    # Build messages array from context window — just role + content + timestamp prefix
    messages = []
    for msg in session.context_window:
        role = msg.get("role", "user")
        if role not in ("user", "assistant"):
            continue
        content = msg.get("content", "")
        if isinstance(content, str):
            ts = msg.get("timestamp", "")
            if ts:
                try:
                    dt = datetime.fromisoformat(ts)
                    ts_str = dt.strftime("[%H:%M:%S] ")
                except Exception:
                    ts_str = ""
            else:
                ts_str = ""
            content = ts_str + content
        messages.append({"role": role, "content": content})

    # Add current message with timestamp
    ts_now = datetime.now().strftime("[%H:%M:%S] ")
    current = ts_now + user_input
    if username:
        current = ts_now + f"{username}: {user_input}"
    messages.append({"role": "user", "content": current})

    # Collapse consecutive same-role messages (API requires alternating roles)
    collapsed = []
    for msg in messages:
        if collapsed and collapsed[-1]["role"] == msg["role"]:
            collapsed[-1]["content"] += "\n" + msg["content"]
        else:
            collapsed.append(msg)
    # Ensure it starts with user
    if collapsed and collapsed[0]["role"] != "user":
        collapsed.insert(0, {"role": "user", "content": "[conversation start]"})
    messages = collapsed

    # Pick model
    model = 'claude-sonnet-4-20250514'

    print(f"[RAW API] Pure mode — {len(messages)} messages, model={model}, no system prompt", flush=True)

    full_response = ""
    full_thinking = ""
    _live_msg_written = False
    _live_msg_idx = -1
    _last_live_flush = time.time()
    _partial_path = session.dir / "partial_response.json"
    session._streaming = True  # Guard: prevent reload_messages() from overwriting in-flight data
    session._partial_state = {"path": _partial_path, "user_input": user_input, "username": username}
    session._partial_response = ""

    try:
        # Match Claude Code exactly: beta.messages.create with stream=True
        # Betas from claude-code-source/constants/betas.ts + oauth.ts
        betas = [
            "oauth-2025-04-20",
            "claude-code-20250219",
            "interleaved-thinking-2025-05-14",
        ]
        raw_stream = client.beta.messages.create(
            model=model,
            max_tokens=16384,
            messages=messages,
            stream=True,
            betas=betas,
        )
        _usage_data = {}
        _raw_thinking_started = False
        for event in raw_stream:
            # Handle thinking deltas from interleaved thinking
            if event.type == "content_block_start" and hasattr(event, "content_block"):
                if getattr(event.content_block, "type", "") == "thinking":
                    _raw_thinking_started = True
                    q.put({"type": "thinking_start"})
            elif event.type == "content_block_delta" and hasattr(event.delta, "thinking"):
                thinking_text = event.delta.thinking
                full_thinking += thinking_text
                q.put({"type": "thinking", "content": thinking_text})
            elif event.type == "content_block_delta" and hasattr(event.delta, "text"):
                text = event.delta.text
                full_response += text
                session._partial_response = full_response
                q.put({"type": "text", "content": text})

                # Live message management (mirrors Claude/Ollama path)
                if not _live_msg_written:
                    _live_msg = {
                        "role": "assistant",
                        "content": full_response,
                        "content_full": full_response,
                        "timestamp": datetime.now().isoformat(),
                        "index": len(session.messages),
                        "_live": True,
                    }
                    session._append_message(_live_msg)
                    _live_msg_idx = len(session.messages) - 1
                    _live_msg_written = True
                elif time.time() - _last_live_flush > 2.0:
                    # Update in memory only — don't rewrite JSONL during streaming
                    session.messages[_live_msg_idx]["content"] = full_response
                    session.messages[_live_msg_idx]["content_full"] = full_response
                    _last_live_flush = time.time()
            elif event.type == "message_delta" and hasattr(event, "usage"):
                if event.usage:
                    _usage_data = {"input_tokens": getattr(event.usage, "input_tokens", 0), "output_tokens": getattr(event.usage, "output_tokens", 0)}

        usage = _usage_data

        q.put({"type": "done", "model": model, "usage": usage, "pure_mode": True})

        # Finalize
        if full_response:
            if _live_msg_written and _live_msg_idx >= 0 and _live_msg_idx < len(session.messages):
                finalized = dict(session.messages[_live_msg_idx])
                finalized["content"] = session._extract_artifacts(full_response, _live_msg_idx)
                finalized["content_full"] = full_response
                if full_thinking:
                    finalized["thinking"] = full_thinking
                finalized.pop("_live", None)
                session._finalize_live_message(_live_msg_idx, finalized)
                session._rebuild_context_window()
                print(f"[RAW API] Finalized at index {_live_msg_idx}, {len(full_response)} chars", flush=True)
            else:
                session.add_assistant_message(full_response)
        session._save_meta()
        session._streaming = False  # Release reload guard
        session._partial_state = None
        session._partial_response = ""
        try:
            _partial_path.unlink(missing_ok=True)
        except Exception:
            pass

    except Exception as e:
        session._streaming = False  # Release reload guard on error
        # Log full error details for debugging
        print(f"[RAW API] Error type: {type(e).__name__}", flush=True)
        print(f"[RAW API] Error: {e}", flush=True)
        if hasattr(e, 'status_code'):
            print(f"[RAW API] Status code: {e.status_code}", flush=True)
        if hasattr(e, 'response') and e.response is not None:
            try:
                print(f"[RAW API] Response headers: {dict(e.response.headers)}", flush=True)
                print(f"[RAW API] Response body: {e.response.text}", flush=True)
            except Exception:
                pass
        if hasattr(e, 'body'):
            print(f"[RAW API] Error body: {e.body}", flush=True)
        try:
            if _live_msg_written and _live_msg_idx >= 0 and _live_msg_idx < len(session.messages):
                finalized = dict(session.messages[_live_msg_idx])
                finalized["content"] = full_response or "[Generation crashed]"
                finalized["content_full"] = full_response or "[Generation crashed]"
                finalized.pop("_live", None)
                finalized["interrupted"] = True
                session._finalize_live_message(_live_msg_idx, finalized)
        except Exception:
            pass
        q.put({"type": "error", "content": str(e)})
        # sentinel handled by outer wrapper _run_raw_api_thread


def _run_claude_thread(session: Session, user_input: str, q: queue.Queue, username: str = None):
    """Run claude -p in a thread, put chunks on a queue."""

    # Dispatch to Ollama if model starts with "ollama:" (check BEFORE pure mode — Ollama is local, not API)
    model = getattr(session, 'model', '') or ''
    if model.startswith("ollama:"):
        prompt = session.build_prompt(user_input, username=username)
        print(f"[THREAD] Prompt length: {len(prompt)} chars", flush=True)
        ollama_model = model[7:]  # strip "ollama:" prefix
        # Sanitize model name — only allow safe characters
        import re
        if not re.match(r'^[a-zA-Z0-9._:-]+$', ollama_model):
            q.put({"type": "error", "content": "Invalid model name"})
            q.put({"type": "done", "model": ollama_model})
            return
        return _run_ollama_thread(session, user_input, q, username, ollama_model, prompt)

    # Dispatch to Pure Mode (raw Anthropic API, zero injection)
    if getattr(session, 'pure_mode', False):
        print(f"[THREAD] Pure Mode — bypassing claude -p, hitting raw API", flush=True)
        return _run_raw_api_thread(session, user_input, q, username)

    prompt = session.build_prompt(user_input, username=username)
    print(f"[THREAD] Prompt length: {len(prompt)} chars", flush=True)

    print(f"[THREAD] Starting claude -p...", flush=True)

    # Per-user tool permissions
    user_tools = get_tools_for_user(username) if username else ALLOWED_TOOLS
    user_tier = get_user_tier(username) if username else "admin"
    user_perm_mode = get_permission_mode_for_user(username) if username else "bypassPermissions"
    # Claude Code refuses --dangerously-skip-permissions when running as root (Unix only — Windows has no getuid)
    if user_perm_mode == "bypassPermissions" and hasattr(os, 'getuid') and os.getuid() == 0:
        user_perm_mode = "acceptEdits"
    print(f"[THREAD] User: {username}, tier: {user_tier}, tools: {user_tools}, permission_mode: {user_perm_mode}", flush=True)

    cmd = [
        "claude", "-p", "-",
        "--output-format", "stream-json",
        "--verbose",
        "--include-partial-messages",
        "--allowedTools", user_tools,
        "--permission-mode", user_perm_mode,
        "--no-session-persistence",  # No disk state = no compaction possible
    ]

    # If context injections are stripped, run --bare so Claude Code itself
    # doesn't inject MEMORY.md, hooks, CLAUDE.md, auto-memory, etc.
    toggles = session.context_toggles
    non_history_on = any(toggles.get(k, True) for k in toggles if k != "history")
    if not non_history_on:
        cmd.append("--bare")
        print(f"[THREAD] Bare mode — all context injections off, skipping Claude Code auto-context", flush=True)

    # Every turn is a completely fresh conversation. No --resume, no session persistence.
    # The harness manages its own rolling context window (clipped to session.max_context_tokens).
    # Claude Code cannot compact what it doesn't remember.

    # Model selection (per-session or default) with automatic fallback
    model = getattr(session, 'model', '') or ''
    FALLBACK_CHAIN = {
        "claude-opus-4-7": "claude-opus-4-6",
        "claude-opus-4-6": "claude-sonnet-4-6",
        "opus": "sonnet",
        "claude-sonnet-4-6": "claude-haiku-4-5-20251001",
        "sonnet": "haiku",
    }
    # Note: --effort high was tried for opus-4-7 but (a) thinking is signed/redacted
    # so we get no visible benefit, (b) it crashed at the reasoning->tool transition.
    # Chain of Reason protocol in the prompt replaces what --effort was supposed to do.
    if model:
        cmd.extend(["--model", model])
        fallback = FALLBACK_CHAIN.get(model)
        if fallback:
            cmd.extend(["--fallback-model", fallback])

    # System prompt
    if session.system_prompt:
        cmd.extend(["--system-prompt", session.system_prompt])

    env = os.environ.copy()
    env.pop("CLAUDECODE", None)
    env["HARNESS_SESSION_ID"] = session.id

    try:
        print(f"[THREAD] Running: claude -p - (stdin, {len(prompt)} chars)...", flush=True)
        process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        # Write prompt via stdin to avoid Windows command-line length limit
        process.stdin.write(prompt.encode("utf-8"))
        process.stdin.close()
        print(f"[THREAD] Process started, PID={process.pid}", flush=True)
        # Store process on session so it can be killed from abort endpoint
        session._active_process = process
        # Write PID to disk so the OTHER server can also kill it (cross-server abort)
        try:
            pid_path = session.dir / "_active_pid"
            pid_path.write_text(str(process.pid))
        except Exception:
            pass

        full_response = ""
        full_thinking = ""
        session_id = None
        _partial_path = session.dir / "partial_response.json"
        _live_msg_written = False  # track if live msg is in JSONL
        _live_msg_idx = -1  # index of live msg in session.messages
        session._streaming = True  # Guard: prevent reload_messages() from overwriting in-flight data
        # Store partial state on session for shutdown flush
        session._partial_state = {
            "path": _partial_path,
            "user_input": user_input,
            "username": username,
        }
        session._partial_response = ""

        # Timer-based disk flush — runs every 2s regardless of chunk type.
        # This is the ONLY place that writes to disk during streaming.
        # Fixes: tool-use gaps where text_delta doesn't fire for minutes.
        _flush_stop = threading.Event()
        _flush_lock = threading.Lock()
        def _periodic_flush():
            last_len = 0
            last_think_len = 0
            while not _flush_stop.is_set():
                _flush_stop.wait(2.0)
                if _flush_stop.is_set():
                    break
                with _flush_lock:
                    if not _live_msg_written or _live_msg_idx < 0:
                        continue
                    cur_len = len(full_response)
                    cur_think_len = len(full_thinking)
                    # Flush if text OR thinking grew. Pure-thinking phase (before
                    # any text) would otherwise never flush and be lost on nav-away.
                    if cur_len <= last_len and cur_think_len <= last_think_len:
                        continue
                    last_len = cur_len
                    last_think_len = cur_think_len
                    try:
                        # Write partial_response.json (crash recovery)
                        _partial_path.write_text(json.dumps({
                            "text": full_response,
                            "thinking": full_thinking,
                            "user_input": user_input,
                            "username": username,
                            "started": datetime.now().isoformat(),
                        }))
                        # Update the JSONL line on disk
                        _flush_msg = {
                            "role": "assistant",
                            "content": full_response,
                            "content_full": full_response,
                            "timestamp": session.messages[_live_msg_idx].get("timestamp", datetime.now().isoformat()),
                            "_live": True,
                        }
                        if full_thinking:
                            _flush_msg["thinking"] = full_thinking
                        session._finalize_live_message(_live_msg_idx, _flush_msg)
                        session.messages[_live_msg_idx]["content"] = full_response
                        session.messages[_live_msg_idx]["content_full"] = full_response
                    except Exception as e:
                        print(f"[FLUSH] Periodic flush error: {e}", flush=True)
        _flush_thread = threading.Thread(target=_periodic_flush, daemon=True)
        _flush_thread.start()

        for raw_line in process.stdout:
            try:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                data = json.loads(line)
                msg_type = data.get("type", "")
                print(f"[STREAM] type={msg_type}", flush=True)

                # Unwrap stream_event → inner API event for real-time streaming
                if msg_type == "stream_event":
                    event = data.get("event", {})
                    event_type = event.get("type", "")
                    if event_type == "content_block_start":
                        cb = event.get("content_block", {})
                        if cb.get("type") == "tool_use":
                            tool_name = cb.get("name", "unknown")
                            print(f"[THREAD] Tool call started: {tool_name}", flush=True)
                            q.put({
                                "type": "tool_use",
                                "tool": tool_name,
                                "input": {},
                            })
                        elif cb.get("type") == "thinking":
                            q.put({"type": "thinking_start"})
                    elif event_type == "content_block_delta":
                        delta = event.get("delta", {})
                        if delta.get("type") == "text_delta":
                            text = delta.get("text", "")
                            full_response += text
                            session._partial_response = full_response
                            q.put({"type": "text", "content": text})
                            # Write live assistant message to JSONL on first text chunk
                            # Then update it every 2 seconds so disk always has the latest
                            if not _live_msg_written:
                                _live_msg = {
                                    "role": "assistant",
                                    "content": full_response,
                                    "content_full": full_response,
                                    "timestamp": datetime.now().isoformat(),
                                    "index": len(session.messages),
                                    "_live": True,  # marker: still streaming
                                }
                                session._append_message(_live_msg)
                                _live_msg_idx = len(session.messages) - 1
                                _live_msg_written = True
                                _last_live_flush = time.time()
                                print(f"[STREAM] Live message written to disk at index {_live_msg_idx}", flush=True)
                            else:
                                # Update in-memory immediately — disk flush is timer-based below
                                session.messages[_live_msg_idx]["content"] = full_response
                                session.messages[_live_msg_idx]["content_full"] = full_response
                        elif delta.get("type") == "thinking_delta":
                            thinking_text = delta.get("thinking", "")
                            full_thinking += thinking_text
                            q.put({"type": "thinking", "content": thinking_text})
                            # Write live message on first thinking chunk too
                            # (so the message exists on disk during thinking phase)
                            if not _live_msg_written:
                                _live_msg = {
                                    "role": "assistant",
                                    "content": "",
                                    "content_full": "",
                                    "thinking": full_thinking,
                                    "timestamp": datetime.now().isoformat(),
                                    "index": len(session.messages),
                                    "_live": True,
                                }
                                session._append_message(_live_msg)
                                _live_msg_idx = len(session.messages) - 1
                                _live_msg_written = True
                                _last_live_flush = time.time()
                                print(f"[STREAM] Live message written (thinking phase) at index {_live_msg_idx}", flush=True)
                            elif _live_msg_idx >= 0 and _live_msg_idx < len(session.messages):
                                session.messages[_live_msg_idx]["thinking"] = full_thinking

                elif msg_type == "result":
                    session_id = data.get("session_id")
                    result_text = data.get("result", "")
                    if result_text and not full_response:
                        full_response = result_text
                        q.put({"type": "text", "content": result_text})
                    q.put({
                        "type": "done",
                        "session_id": session_id,
                        "usage": data.get("usage", {}),
                        "cost": data.get("total_cost_usd", 0),
                        "budget": {**getattr(session, '_last_budget', {}), "last_recalled": getattr(session, '_last_recalled', [])},
                        "wos_status": _wos.get_status(),
                    })

                elif msg_type == "assistant":
                    content = data.get("message", {}).get("content", [])
                    _wakeup_log(f"ASSISTANT msg: {len(content) if isinstance(content, list) else 0} blocks")
                    for block in content if isinstance(content, list) else []:
                        if isinstance(block, dict):
                            if block.get("type") == "text":
                                # Skip — already streamed via content_block_delta
                                pass
                            elif block.get("type") == "thinking":
                                # Capture thinking content if deltas didn't stream it.
                                # Some model versions deliver thinking as a complete block
                                # in the final assistant message instead of via thinking_delta.
                                block_thinking = block.get("thinking", "") or ""
                                if block_thinking and len(block_thinking) > len(full_thinking):
                                    full_thinking = block_thinking
                                    _wakeup_log(f"THINKING_BLOCK captured: {len(full_thinking)} chars (from complete block)")
                                    if _live_msg_idx is not None:
                                        session.messages[_live_msg_idx]["thinking"] = full_thinking
                            elif block.get("type") == "tool_use":
                                tool_name = block.get("name", "")
                                tool_input = block.get("input", {})
                                _wakeup_log(f"TOOL_USE: {tool_name} input_keys={list(tool_input.keys())}")
                                # Send tool_use with full input (updates the tool block)
                                q.put({
                                    "type": "tool_use_done",
                                    "tool": tool_name,
                                    "input": tool_input,
                                })
                                # Intercept ScheduleWakeup — claude -p dies after response,
                                # so the harness must manage the timer server-side
                                if tool_name == "ScheduleWakeup":
                                    session._pending_wakeup = {
                                        "delay": max(60, min(3600, int(tool_input.get("delaySeconds", 300)))),
                                        "prompt": tool_input.get("prompt", ""),
                                        "reason": tool_input.get("reason", "scheduled wakeup"),
                                        "username": username,
                                    }
                                    _wakeup_log(f"INTERCEPTED: delay={session._pending_wakeup['delay']}s reason={session._pending_wakeup['reason']} prompt={session._pending_wakeup['prompt'][:100]}")
                                    print(f"[WAKEUP] Intercepted ScheduleWakeup: {session._pending_wakeup['delay']}s — {session._pending_wakeup['reason']}", flush=True)

                elif msg_type in ("tool_result", "user"):
                    content = data.get("message", {}).get("content", [])
                    for block in content if isinstance(content, list) else []:
                        if isinstance(block, dict) and block.get("type") == "tool_result":
                            q.put({
                                "type": "tool_result",
                                "content": str(block.get("content", ""))[:500],
                            })

            except json.JSONDecodeError:
                continue
            except Exception as e:
                q.put({"type": "error", "content": str(e)})

        process.wait()
        stderr_out = process.stderr.read().decode("utf-8", errors="replace") if process.stderr else ""
        print(f"[THREAD] Process exited code={process.returncode}, stderr={stderr_out[:300]}", flush=True)
        session._active_process = None
        try:
            (session.dir / "_active_pid").unlink(missing_ok=True)
        except Exception:
            pass

        # Stop the periodic flush thread — finalization will do the final write
        _flush_stop.set()
        _flush_thread.join(timeout=3)

        # Update session
        if session_id:
            session.claude_session_id = session_id
        if full_response:
            if _live_msg_written and _live_msg_idx >= 0 and _live_msg_idx < len(session.messages):
                # Finalize the live message — update content, extract artifacts, remove _live marker
                finalized = dict(session.messages[_live_msg_idx])
                finalized["content"] = session._extract_artifacts(full_response, _live_msg_idx)
                finalized["content_full"] = full_response
                if full_thinking:
                    finalized["thinking"] = full_thinking
                finalized.pop("_live", None)
                session._finalize_live_message(_live_msg_idx, finalized)
                session._rebuild_context_window()
                # Store combined turn in semantic memory
                try:
                    user_msg = ""
                    for m in reversed(session.messages[:_live_msg_idx]):
                        if m["role"] == "user":
                            user_msg = m["content"] if isinstance(m["content"], str) else str(m["content"])
                            break
                    combined = f"[USER]: {user_msg[:500]}\n[ASSISTANT]: {full_response[:1500]}" if user_msg else full_response[:2000]
                    _semantic_memory.store(combined, session.id, role="turn")
                except Exception:
                    pass
                print(f"[STREAM] Live message finalized at index {_live_msg_idx}", flush=True)
            else:
                # Fallback: no live message was written (very short response?)
                session.add_assistant_message(full_response)
            # W-OS: Compress this turn into a kanji glyph
            try:
                glyph = _wos.compress(user_input[:200], full_response[:200])
                if glyph:
                    session._wos_glyphs = _wos.memory.copy()
                    print(f"[W-OS] Glyph: {glyph}", flush=True)
                    # Associate the W-OS glyph with the last semantic memory entries
                    # (the user message and assistant response we just stored)
                    for mem in reversed(_semantic_memory.memories[-10:]):
                        if mem.get("session_id") == session.id and not mem.get("wos_glyph"):
                            mem["wos_glyph"] = glyph
                    # Persist the updated entries
                    _semantic_memory._rewrite_all()
            except Exception as e:
                print(f"[W-OS] Compression error: {e}", flush=True)
            # Update core hash if persona changed
            if session.base_prompt:
                _wos.update_core(session.base_prompt)
        session._save_meta()
        session._streaming = False  # Release reload guard
        # Clean up partial response file — response is fully saved
        session._partial_state = None
        session._partial_response = ""
        try:
            _partial_path.unlink(missing_ok=True)
        except Exception:
            pass

    except Exception as e:
        session._streaming = False  # Release reload guard on error
        session._active_process = None
        try:
            (session.dir / "_active_pid").unlink(missing_ok=True)
        except Exception:
            pass
        # Stop periodic flush thread
        try:
            _flush_stop.set()
            _flush_thread.join(timeout=3)
        except Exception:
            pass
        # Finalize live message on crash — whatever we have is saved
        try:
            if _live_msg_written and _live_msg_idx >= 0 and _live_msg_idx < len(session.messages):
                finalized = dict(session.messages[_live_msg_idx])
                finalized["content"] = full_response or "[Generation crashed]"
                finalized["content_full"] = full_response or "[Generation crashed]"
                if full_thinking:
                    finalized["thinking"] = full_thinking
                finalized.pop("_live", None)
                finalized["interrupted"] = True
                session._finalize_live_message(_live_msg_idx, finalized)
                print(f"[STREAM] Live message saved on crash at index {_live_msg_idx}", flush=True)
            elif full_response:
                _partial_path.write_text(json.dumps({
                    "text": full_response,
                    "thinking": full_thinking,
                    "user_input": user_input,
                    "username": username,
                    "started": datetime.now().isoformat(),
                    "interrupted": True,
                    "error": str(e),
                }))
        except Exception:
            pass
        q.put({"type": "error", "content": str(e)})
        # sentinel handled by _safe_thread_target wrapper in run_claude_async


async def run_claude_async(session: Session, user_input: str, username: str = None):
    """Run claude -p async — broadcasts chunks to all connected clients."""
    # Cross-server generation lock — file-based so both 8080 and 8081 respect it
    lock_file = session.dir / ".generating.lock"
    if lock_file.exists():
        try:
            lock_data = json.loads(lock_file.read_text())
            lock_pid = lock_data.get("pid", 0)
            lock_port = lock_data.get("port", "?")
            lock_age = time.time() - lock_data.get("started", 0)
            # If the locking process is still alive AND lock is < 10 min old, reject
            if lock_age < 600:
                try:
                    os.kill(lock_pid, 0)  # check if process exists
                    raise RuntimeError(f"Generation already running on port {lock_port} (PID {lock_pid}, {int(lock_age)}s ago)")
                except OSError:
                    pass  # process dead, stale lock — proceed
        except (json.JSONDecodeError, KeyError):
            pass  # corrupt lock — proceed
    # Write our lock — initially with server PID, updated with subprocess PID once it starts
    port = int(os.environ.get("HARNESS_PORT", "8080"))
    lock_file.write_text(json.dumps({"pid": os.getpid(), "port": port, "started": time.time(), "username": username}))

    q = queue.Queue()

    # Mark generation as active (blocks relay injection until done)
    _relay_generation_active[session.id] = True
    _session_activity[session.id] = {"status": "Thinking...", "started": time.time()}

    # Broadcast generation_start immediately so ALL connected clients lock input + show indicator
    await _broadcast_to_session(session.id, {
        "type": "generation_start",
        "username": username,
        "session_id": session.id,
    })

    # Start subprocess in background thread — wrapped to guarantee sentinel delivery
    def _safe_thread_target():
        try:
            _run_claude_thread(session, user_input, q, username)
        except Exception as e:
            print(f"[THREAD] Unhandled error in generation thread: {e}", flush=True)
            import traceback; traceback.print_exc()
            q.put({"type": "error", "content": f"Generation error: {e}"})
        finally:
            q.put(None)  # ALWAYS send sentinel

    thread = threading.Thread(target=_safe_thread_target, daemon=True)
    thread.start()

    # Track this as an active generation so reconnecting clients can check
    session._active_thread = thread
    session._active_queue = q
    session._stream_buffer = []  # buffer all chunks for replay on reconnect
    session._stream_done = False
    _done_sent = False  # track if done was already broadcast (prevents double-done)
    session._generation_owner = username  # track who started this generation

    # Update lock with subprocess PID once it's available (wait up to 5s)
    for _ in range(100):
        if hasattr(session, '_active_process') and session._active_process is not None:
            try:
                lock_file.write_text(json.dumps({
                    "pid": session._active_process.pid,
                    "port": port,
                    "started": time.time(),
                    "username": username
                }))
            except Exception:
                pass
            break
        await asyncio.sleep(0.05)

    try:
        # Read from queue and broadcast to all connected clients
        tool_count = 0
        _dead_thread_checks = 0
        while True:
            # Poll queue without blocking event loop
            try:
                chunk = q.get_nowait()
            except queue.Empty:
                # Safety: if the generation thread died without sending sentinel, don't hang forever
                if not thread.is_alive():
                    _dead_thread_checks += 1
                    if _dead_thread_checks > 20:  # ~1 second of empty queue + dead thread
                        print(f"[WARN] Generation thread died without sentinel — breaking queue loop", flush=True)
                        await _broadcast_to_session(session.id, {"type": "error", "content": "Generation thread crashed unexpectedly"})
                        break
                else:
                    _dead_thread_checks = 0
                await asyncio.sleep(0.05)
                continue

            _dead_thread_checks = 0  # got a chunk, reset counter

            if chunk is None:
                break  # done

            # Buffer every chunk for replay
            session._stream_buffer.append(chunk)

            # Update session activity status for sidebar
            ctype = chunk.get("type", "")
            if ctype == "text":
                _session_activity[session.id] = {"status": "Writing...", "started": _session_activity.get(session.id, {}).get("started", time.time())}
            elif ctype == "tool_use":
                tool_count += 1
                tool_label = chunk.get("tool", "tool")
                suffix = f" ({tool_count} tools)" if tool_count > 1 else ""
                _session_activity[session.id] = {"status": f"Running: {tool_label}{suffix}", "started": _session_activity.get(session.id, {}).get("started", time.time())}
            elif ctype == "tool_result":
                _session_activity[session.id] = {"status": "Processing result...", "started": _session_activity.get(session.id, {}).get("started", time.time())}
            elif ctype in ("thinking_start", "thinking"):
                _session_activity[session.id] = {"status": "Thinking...", "started": _session_activity.get(session.id, {}).get("started", time.time())}

            # Broadcast to all connected clients for this session
            # Skip thinking chunks if show_thinking is off
            if ctype in ("thinking_start", "thinking") and not session.show_thinking:
                pass  # silently drop thinking chunks when toggle is off
            else:
                await _broadcast_to_session(session.id, chunk)

            if chunk.get("type") == "done":
                _done_sent = True
                # Wait for the thread to finish (non-blocking — don't freeze the event loop)
                for _ in range(200):  # up to 10s in 50ms steps
                    if not thread.is_alive():
                        break
                    await asyncio.sleep(0.05)
                break

        # If any clients are still connected, clear replay buffer
        # (response already saved to session.messages — no replay needed)
        if _session_clients.get(session.id):
            session._stream_buffer = []
            session._stream_done = False
        else:
            # All clients disconnected mid-stream — keep buffer for replay on reconnect
            session._stream_done = True

        session._active_thread = None
        session._active_queue = None

    finally:
        # ALWAYS release cross-server generation lock — even on crash/error
        lock_file = session.dir / ".generating.lock"
        try:
            lock_file.unlink(missing_ok=True)
        except Exception:
            pass
    try:
        lock_file.unlink(missing_ok=True)
    except Exception:
        pass

    # Broadcast done only if the in-queue done was missed (crash, timeout, etc.)
    # Prevents double-done which causes UI flicker
    if not _done_sent:
        await _broadcast_to_session(session.id, {
            "type": "done",
            "session_id": session.id,
        })

    # Broadcast post-completion updates to all connected clients
    await _broadcast_to_session(session.id, {
        "type": "context_stats",
        "stats": session.get_context_stats(),
    })
    await _broadcast_to_session(session.id, {
        "type": "artifacts_update",
        "artifacts": session.artifacts.list_all(),
    })

    # Mark generation as done and check for queued relay messages
    _relay_generation_active[session.id] = False
    _session_activity.pop(session.id, None)
    # Don't auto-inject relay messages after user-initiated generation
    # Relay messages stay queued and show in the relay panel for manual review
    _relay_inject_queue.pop(session.id, None)

    # Auto-continue: if user injected messages during generation, start new generation
    injected = getattr(session, '_injected_messages', [])
    if injected:
        # Clear the queue — we're about to process them
        session._injected_messages = []
        # The injected messages are already saved to JSONL — just need to trigger generation
        # Use the LAST injected message as the user_input (Claude sees all of them in context)
        last_inject = injected[-1]
        print(f"[AUTO-CONTINUE] {len(injected)} injected message(s) — auto-continuing generation", flush=True)
        # Broadcast that auto-continue is starting
        await _broadcast_to_session(session.id, {
            "type": "generation_start",
            "username": last_inject["username"],
            "session_id": session.id,
            "auto_continue": True,
        })
        # Start new generation with full context (includes the injected messages)
        try:
            await run_claude_async(session, last_inject["text"], username=last_inject["username"])
        except Exception as e:
            print(f"[ERROR] Auto-continue failed: {e}", flush=True)
            import traceback; traceback.print_exc()
            await _broadcast_to_session(session.id, {"type": "error", "content": f"Auto-continue failed: {e}"})

    # ScheduleWakeup: if Claude set a timer, schedule it server-side
    pending_wakeup = getattr(session, '_pending_wakeup', None)
    _wakeup_log(f"POST-GEN CHECK: session={session.id[:8]} pending_wakeup={'YES' if pending_wakeup else 'NO'}")
    if pending_wakeup:
        session._pending_wakeup = None
        # Cancel any existing timer for this session
        old = _wakeup_timers.pop(session.id, None)
        if old and old.get("timer"):
            old["timer"].cancel()
        delay = pending_wakeup["delay"]
        wakeup_prompt = pending_wakeup["prompt"]
        wakeup_user = pending_wakeup["username"]
        wakeup_reason = pending_wakeup["reason"]

        _event_loop = asyncio.get_event_loop()

        def _fire_wakeup(sid=session.id, prompt=wakeup_prompt, uname=wakeup_user, reason=wakeup_reason):
            _wakeup_timers.pop(sid, None)
            _wakeup_log(f"TIMER FIRED: session={sid[:8]} reason={reason}")
            print(f"[WAKEUP] Timer fired for session {sid}: {reason}", flush=True)
            asyncio.run_coroutine_threadsafe(_handle_wakeup(sid, prompt, uname, reason), _event_loop)

        timer = threading.Timer(delay, _fire_wakeup)
        timer.daemon = True
        timer.start()
        _wakeup_timers[session.id] = {
            "timer": timer,
            "delay": delay,
            "prompt": wakeup_prompt,
            "reason": wakeup_reason,
            "username": wakeup_user,
            "scheduled_at": time.time(),
            "fires_at": time.time() + delay,
        }
        _wakeup_log(f"TIMER SCHEDULED: delay={delay}s reason={wakeup_reason} session={session.id[:8]} fires_at={time.time()+delay}")
        print(f"[WAKEUP] Timer scheduled: {delay}s — {wakeup_reason} (session {session.id})", flush=True)
        _save_wakeup_timers()
        await _broadcast_to_session(session.id, {
            "type": "wakeup_scheduled",
            "delay": delay,
            "reason": wakeup_reason,
            "fires_at": time.time() + delay,
            "prompt": wakeup_prompt[:200],
        })


_WAKEUP_PERSIST_PATH = Path("data/wakeup_timers.json")


def _save_wakeup_timers():
    """Persist active wakeup timers to disk so they survive server restarts."""
    data = {}
    for sid, t in _wakeup_timers.items():
        data[sid] = {
            "delay": t["delay"],
            "prompt": t["prompt"],
            "reason": t["reason"],
            "username": t.get("username", ""),
            "fires_at": t["fires_at"],
            "scheduled_at": t["scheduled_at"],
        }
    try:
        _WAKEUP_PERSIST_PATH.parent.mkdir(parents=True, exist_ok=True)
        _WAKEUP_PERSIST_PATH.write_text(json.dumps(data, indent=2))
    except Exception as e:
        print(f"[WAKEUP] Failed to persist timers: {e}", flush=True)


async def _restore_wakeup_timers():
    """Restore wakeup timers from disk on server startup."""
    if not _WAKEUP_PERSIST_PATH.exists():
        return
    try:
        data = json.loads(_WAKEUP_PERSIST_PATH.read_text())
    except Exception:
        return
    now = time.time()
    _event_loop = asyncio.get_event_loop()
    restored = 0
    for sid, t in data.items():
        fires_at = t.get("fires_at", 0)
        remaining = fires_at - now
        if remaining <= 0:
            # Timer already expired — fire immediately (delayed by 5s to let server finish startup)
            remaining = 5
        prompt = t.get("prompt", "")
        username = t.get("username", "")
        reason = t.get("reason", "restored wakeup")

        def _fire(s=sid, p=prompt, u=username, r=reason):
            _wakeup_timers.pop(s, None)
            print(f"[WAKEUP] Restored timer fired for session {s}: {r}", flush=True)
            asyncio.run_coroutine_threadsafe(_handle_wakeup(s, p, u, r), _event_loop)

        timer = threading.Timer(remaining, _fire)
        timer.daemon = True
        timer.start()
        _wakeup_timers[sid] = {
            "timer": timer,
            "delay": t.get("delay", int(remaining)),
            "prompt": prompt,
            "reason": reason,
            "username": username,
            "scheduled_at": t.get("scheduled_at", now),
            "fires_at": fires_at,
        }
        restored += 1
    if restored:
        print(f"[WAKEUP] Restored {restored} timer(s) from disk", flush=True)
    _WAKEUP_PERSIST_PATH.unlink(missing_ok=True)


async def _handle_wakeup(session_id: str, prompt: str, username: str, reason: str):
    """Handle a ScheduleWakeup timer firing — inject the prompt and start generation."""
    _wakeup_log(f"HANDLE_WAKEUP: session={session_id[:8]} reason={reason} prompt={prompt[:100]}")
    _save_wakeup_timers()
    try:
        sess = _sessions.get(session_id)
        if not sess:
            # Try to load the session
            sess = get_session(session_id)
        if not sess:
            _wakeup_log(f"HANDLE_WAKEUP FAIL: session {session_id[:8]} not found")
            print(f"[WAKEUP] Session {session_id} not found — timer discarded", flush=True)
            return
        if sess._streaming:
            _wakeup_log(f"HANDLE_WAKEUP DEFER: session {session_id[:8]} already generating, retry in 30s")
            print(f"[WAKEUP] Session {session_id} already generating — re-scheduling in 30s", flush=True)
            _loop = asyncio.get_event_loop()
            def _retry():
                asyncio.run_coroutine_threadsafe(_handle_wakeup(session_id, prompt, username, reason), _loop)
            retry_timer = threading.Timer(30, _retry)
            retry_timer.daemon = True
            retry_timer.start()
            return

        await _broadcast_to_session(session_id, {"type": "wakeup_fired"})
        wakeup_msg = f"[ScheduleWakeup fired: {reason}]\n\n{prompt}"
        sess.add_user_message(wakeup_msg, username=username or "system")
        await _broadcast_to_session(session_id, {
            "type": "user_message",
            "content": wakeup_msg,
            "username": username or "system",
            "wakeup": True,
        })
        _wakeup_log(f"HANDLE_WAKEUP START GEN: session={session_id[:8]}")
        print(f"[WAKEUP] Starting generation for session {session_id}", flush=True)
        await run_claude_async(sess, wakeup_msg, username=username)
        _wakeup_log(f"HANDLE_WAKEUP GEN COMPLETE: session={session_id[:8]}")
    except Exception as e:
        _wakeup_log(f"HANDLE_WAKEUP ERROR: session={session_id[:8]} error={e}")
        print(f"[WAKEUP] Error handling wakeup for {session_id}: {e}", flush=True)
        import traceback; traceback.print_exc()


# ══════════════════════════════════════════════════════════════
# Active Sessions
# ══════════════════════════════════════════════════════════════

_sessions = {}


def get_session(session_id=None):
    """Get or create a session."""
    if session_id and session_id in _sessions:
        _sessions[session_id].reload_messages()
        return _sessions[session_id]

    if session_id:
        # Try loading from disk
        session_dir = SESSIONS_DIR / session_id
        if session_dir.exists():
            session = Session(session_id)
            _sessions[session_id] = session
            return session

    # Create new
    session = Session()
    _sessions[session.id] = session
    return session


def list_sessions():
    """List all sessions."""
    sessions = []
    for d in SESSIONS_DIR.iterdir():
        if not d.is_dir():
            continue
        # Skip trashed sessions
        if d.name.endswith('.deleted'):
            continue
        meta_path = d / "meta.json"
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text())
                # Override message_count with authoritative disk count
                msgs_path = d / "messages.jsonl"
                if msgs_path.exists():
                    with open(msgs_path, "r") as f:
                        meta["message_count"] = sum(1 for line in f if line.strip())
                sessions.append(meta)
            except:
                pass
        else:
            # Ghost directory with no meta — clean it up if empty
            try:
                if not any(d.iterdir()):
                    d.rmdir()
                    print(f"[CLEANUP] Removed empty ghost session dir: {d.name}", flush=True)
            except:
                pass
    sessions.sort(key=lambda s: s.get("last_updated", ""), reverse=True)
    return sessions


# ══════════════════════════════════════════════════════════════
# Auth API Routes
# ══════════════════════════════════════════════════════════════

@app.get("/api/auth/registration-mode")
async def api_registration_mode():
    """Check if registration requires an invite code."""
    return {"require_invite": REQUIRE_INVITE}

@app.get("/api/auth/setup-status")
async def api_setup_status():
    """First-run wizard signal. If no admin exists, frontend shows 'Create Admin' wizard
    and the next registration becomes the admin with no invite required."""
    return {"needs_setup": not has_any_admin()}

@app.post("/api/auth/register")
async def api_register(body: dict, response: Response):
    username = body.get("username", "").strip().lower()
    password = body.get("password", "").strip()
    display_name = body.get("display_name", "").strip() or username
    email = body.get("email", "").strip().lower()
    invite_code = body.get("invite_code", "").strip()
    if not username or not password:
        raise HTTPException(400, "Username and password required")
    if len(username) < 2 or len(username) > 20:
        raise HTTPException(400, "Username must be 2-20 characters")
    if not username.isalnum():
        raise HTTPException(400, "Username must be alphanumeric")
    if email and "@" not in email:
        raise HTTPException(400, "Invalid email address")

    # First-run wizard: if no admin yet, next registration IS the admin. No invite needed.
    first_run = not has_any_admin()
    if first_run:
        make_admin = True
    else:
        # After admin exists, public registration is closed — admin adds users via admin panel
        make_admin = False
        if REQUIRE_INVITE:
            if not invite_code:
                raise HTTPException(403, "Registration is closed — ask the admin to add you")
            valid, err = use_invite(invite_code, username)
            if not valid:
                raise HTTPException(400, err)
        else:
            # Open registration disabled for distributed instances once admin exists
            raise HTTPException(403, "Registration is closed — ask the admin to add you")

    user, error = register_user(username, password, display_name, email)
    if error:
        raise HTTPException(400, error)

    if make_admin:
        # Flag this user as admin and refresh the live admin set
        users = _load_users()
        if username in users:
            users[username]["is_admin"] = True
            _save_users(users)
        ADMIN_USERNAMES.add(username.lower())

    token = create_auth_token(username)
    response.set_cookie("auth_token", token, httponly=True, samesite="none", secure=True, max_age=86400*30)
    log_activity("user_registered", username=username, detail=f"email={email or 'none'} invite={invite_code or 'none'} admin={make_admin}")
    return {"username": user["username"], "display_name": user["display_name"], "token": token, "relay_key": user["relay_key"], "is_admin": make_admin}

@app.post("/api/auth/login")
async def api_login(body: dict, response: Response):
    username = body.get("username", "").strip().lower()
    password = body.get("password", "").strip()
    user = authenticate_user(username, password)
    if not user:
        log_activity("login_failed", username=username)
        raise HTTPException(401, "Invalid username or password")
    # Revoke all existing tokens for this user (single-session enforcement)
    old_tokens = [t for t, v in _auth_tokens.items()
                  if (v if isinstance(v, str) else v.get("user", "")) == username]
    for t in old_tokens:
        del _auth_tokens[t]
    if old_tokens:
        _save_tokens()  # Persist revocation to disk so disk fallback doesn't resurrect old tokens
    token = create_auth_token(username)
    response.set_cookie("auth_token", token, httponly=True, samesite="none", secure=True, max_age=86400*30)
    log_activity("login", username=user.get("display_name") or username)
    return {"username": user["username"], "display_name": user["display_name"], "token": token}

@app.post("/api/auth/logout")
async def api_logout(response: Response, auth_token: str = Cookie(None)):
    if auth_token:
        _auth_tokens.pop(auth_token, None)
        _save_tokens()
    response.delete_cookie("auth_token")
    return {"status": "logged_out"}

@app.get("/api/auth/me")
async def api_me(auth_token: str = Cookie(None)):
    user = get_user_from_token(auth_token)
    if not user:
        raise HTTPException(401, "Not authenticated")
    return {
        "username": user["username"],
        "display_name": user["display_name"],
        "email": user.get("email", ""),
        "relay_key": user.get("relay_key", ""),
        "relay_identity": user.get("relay_identity", f"{user['username']}_claude"),
        "created": user.get("created", ""),
        "is_admin": user["username"] in ADMIN_USERNAMES,
        "permission_tier": get_user_tier(user["username"]),
        "token": auth_token,
    }

@app.post("/api/auth/admin-verify")
async def api_admin_verify(body: dict, auth_token: str = Cookie(None)):
    """Re-verify admin password for destructive operations."""
    password = body.get("password", "")
    if not password:
        raise HTTPException(400, "Password required")
    require_admin(auth_token, password)
    return {"ok": True}

@app.post("/api/auth/change-password")
async def api_change_password(body: dict, auth_token: str = Cookie(None)):
    """Change own password. Requires current password."""
    user = get_user_from_token(auth_token)
    if not user:
        raise HTTPException(401, "Not authenticated")
    current = body.get("current_password", "")
    new_pw = body.get("new_password", "")
    if not current or not new_pw:
        raise HTTPException(400, "Both current and new password required")
    if len(new_pw) < 4:
        raise HTTPException(400, "Password must be at least 4 characters")
    if not authenticate_user(user["username"], current):
        raise HTTPException(401, "Current password is incorrect")
    users = _load_users()
    salt, hashed = _hash_password(new_pw)
    users[user["username"]]["salt"] = salt
    users[user["username"]]["password_hash"] = hashed
    _save_users(users)
    # Revoke all other sessions — keep only the token making this request
    tokens_to_remove = [t for t, v in _auth_tokens.items()
                        if _resolve_token_username(v) == user["username"] and t != auth_token]
    for t in tokens_to_remove:
        del _auth_tokens[t]
    _save_tokens()
    log_activity("password_changed", username=user["username"])
    return {"ok": True}

@app.post("/api/auth/update-profile")
async def api_update_profile(body: dict, auth_token: str = Cookie(None)):
    """Update own display name and/or email."""
    user = get_user_from_token(auth_token)
    if not user:
        raise HTTPException(401, "Not authenticated")
    users = _load_users()
    updated = []
    display_name = body.get("display_name", "").strip()
    if display_name:
        if len(display_name) > 30:
            raise HTTPException(400, "Display name must be 30 characters or less")
        users[user["username"]]["display_name"] = display_name
        updated.append(f"display_name={display_name}")
    email = body.get("email")
    if email is not None:
        email = email.strip().lower()
        if email and "@" not in email:
            raise HTTPException(400, "Invalid email address")
        users[user["username"]]["email"] = email
        updated.append(f"email={email}")
    if not updated:
        raise HTTPException(400, "Nothing to update")
    _save_users(users)
    log_activity("profile_updated", username=user["username"], detail=", ".join(updated))
    return {"ok": True, "display_name": users[user["username"]].get("display_name"), "email": users[user["username"]].get("email", "")}

# --- Anthropic API Key (for Pure Mode / direct API billing) ---
# Writes to ~/.anthropic_key on the local machine. The key never leaves the user's disk.

def _anthropic_key_path() -> Path:
    return Path.home() / ".anthropic_key"

@app.get("/api/account/anthropic-key-status")
async def api_anthropic_key_status(auth_token: str = Cookie(None)):
    """Returns whether a local Anthropic API key is configured. Never returns the key itself."""
    user = get_user_from_token(auth_token)
    if not user:
        raise HTTPException(401, "Not authenticated")
    p = _anthropic_key_path()
    has_key = p.exists() and bool(p.read_text().strip())
    return {"has_key": has_key}

@app.post("/api/account/anthropic-key")
async def api_anthropic_key_set(body: dict, auth_token: str = Cookie(None)):
    """Save an Anthropic API key to ~/.anthropic_key. Used by Pure Mode for direct API access."""
    user = get_user_from_token(auth_token)
    if not user:
        raise HTTPException(401, "Not authenticated")
    key = (body.get("api_key") or "").strip()
    if not key:
        raise HTTPException(400, "API key required")
    if not key.startswith("sk-"):
        raise HTTPException(400, "Key should start with 'sk-'")
    p = _anthropic_key_path()
    p.write_text(key)
    try:
        p.chmod(0o600)  # owner-only read/write (no-op on Windows, safe)
    except Exception:
        pass
    log_activity("anthropic_key_set", username=user["username"])
    return {"ok": True}

@app.delete("/api/account/anthropic-key")
async def api_anthropic_key_clear(auth_token: str = Cookie(None)):
    """Remove the saved Anthropic API key from disk."""
    user = get_user_from_token(auth_token)
    if not user:
        raise HTTPException(401, "Not authenticated")
    p = _anthropic_key_path()
    if p.exists():
        p.unlink()
    log_activity("anthropic_key_cleared", username=user["username"])
    return {"ok": True}

# --- User Settings (synced across devices) ---

def _settings_path(username: str) -> Path:
    return USERS_DIR / username / "settings.json"

def _load_settings(username: str) -> dict:
    p = _settings_path(username)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}

def _save_settings(username: str, settings: dict):
    p = _settings_path(username)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(settings, indent=2), encoding="utf-8")

@app.get("/api/settings")
async def api_get_settings(auth_token: str = Cookie(None)):
    """Get user's synced settings."""
    user = get_user_from_token(auth_token)
    if not user:
        raise HTTPException(401, "Not authenticated")
    return _load_settings(user["username"])

@app.put("/api/settings")
async def api_put_settings(body: dict, auth_token: str = Cookie(None)):
    """Save user's synced settings (merge with existing)."""
    user = get_user_from_token(auth_token)
    if not user:
        raise HTTPException(401, "Not authenticated")
    existing = _load_settings(user["username"])
    existing.update(body)
    _save_settings(user["username"], existing)
    return {"ok": True}

# --- Admin user management ---

@app.get("/api/admin/users")
async def api_admin_list_users(auth_token: str = Cookie(None)):
    """List all users (admin only)."""
    require_admin(auth_token)
    users = _load_users()
    result = []
    for u in users.values():
        result.append({
            "username": u["username"],
            "display_name": u.get("display_name", u["username"]),
            "email": u.get("email", ""),
            "relay_key": u.get("relay_key", ""),
            "relay_identity": u.get("relay_identity", f"{u['username']}_claude"),
            "created": u.get("created", ""),
            "is_admin": u["username"] in ADMIN_USERNAMES,
            "permission_tier": get_user_tier(u["username"]),
        })
    result.sort(key=lambda x: x["created"])
    return result

@app.post("/api/admin/invites")
async def api_admin_create_invite(body: dict, auth_token: str = Cookie(None)):
    """Create an invite code (admin only)."""
    require_admin(auth_token, body.get("admin_password"))
    max_uses = body.get("max_uses", 1)
    note = body.get("note", "")
    code = create_invite(created_by="admin", max_uses=max_uses, note=note)
    log_activity("invite_created", username="admin", detail=f"code={code} max_uses={max_uses} note={note}")
    return {"code": code, "max_uses": max_uses, "note": note}

@app.get("/api/admin/invites")
async def api_admin_list_invites(auth_token: str = Cookie(None)):
    """List all invite codes (admin only)."""
    require_admin(auth_token)
    invites = _load_invites()
    return list(invites.values())

@app.delete("/api/admin/invites/{code}")
async def api_admin_revoke_invite(code: str, request: Request, auth_token: str = Cookie(None)):
    """Revoke an invite code (admin only)."""
    body = {}
    try:
        body = await request.json()
    except:
        pass
    require_admin(auth_token, body.get("admin_password"))
    invites = _load_invites()
    if code not in invites:
        raise HTTPException(404, "Invite code not found")
    invites[code]["active"] = False
    _save_invites(invites)
    log_activity("invite_revoked", username="admin", detail=f"code={code}")
    return {"ok": True}

@app.post("/api/admin/users")
async def api_admin_create_user(body: dict, auth_token: str = Cookie(None)):
    """Create a new user (admin only)."""
    require_admin(auth_token, body.get("admin_password"))
    username = body.get("username", "").strip().lower()
    password = body.get("password", "").strip()
    display_name = body.get("display_name", "").strip() or username
    email = body.get("email", "").strip().lower()
    if not username or not password:
        raise HTTPException(400, "Username and password required")
    if len(username) < 2 or len(username) > 20:
        raise HTTPException(400, "Username must be 2-20 characters")
    if not username.isalnum():
        raise HTTPException(400, "Username must be alphanumeric")
    user, error = register_user(username, password, display_name, email)
    if error:
        raise HTTPException(400, error)
    log_activity("user_created", username="admin", detail=f"created user {username} email={email or 'none'}")
    return {"ok": True, "username": username, "display_name": display_name, "relay_key": user.get("relay_key", "")}

@app.post("/api/admin/users/{username}/reset-password")
async def api_admin_reset_password(username: str, body: dict, auth_token: str = Cookie(None)):
    """Reset a user's password (admin only)."""
    require_admin(auth_token, body.get("admin_password"))
    new_pw = body.get("new_password", "").strip()
    if not new_pw or len(new_pw) < 4:
        raise HTTPException(400, "New password must be at least 4 characters")
    users = _load_users()
    if username.lower() not in users:
        raise HTTPException(404, "User not found")
    salt, hashed = _hash_password(new_pw)
    users[username.lower()]["salt"] = salt
    users[username.lower()]["password_hash"] = hashed
    _save_users(users)
    # Invalidate their active tokens
    tokens_to_remove = [t for t, u in _auth_tokens.items() if u == username.lower()]
    for t in tokens_to_remove:
        del _auth_tokens[t]
    if tokens_to_remove:
        _save_tokens()
    log_activity("password_reset", username="admin", detail=f"reset password for {username}")
    return {"ok": True}

@app.delete("/api/admin/users/{username}")
async def api_admin_delete_user(username: str, auth_token: str = Cookie(None), admin_password: str = ""):
    """Delete a user (admin only). Cannot delete self."""
    # Get password from query param since DELETE bodies are tricky
    require_admin(auth_token)
    if username.lower() in ADMIN_USERNAMES:
        raise HTTPException(400, "Cannot delete admin account")
    users = _load_users()
    if username.lower() not in users:
        raise HTTPException(404, "User not found")
    del users[username.lower()]
    _save_users(users)
    # Invalidate their tokens
    tokens_to_remove = [t for t, u in _auth_tokens.items() if u == username.lower()]
    for t in tokens_to_remove:
        del _auth_tokens[t]
    if tokens_to_remove:
        _save_tokens()
    log_activity("user_deleted", username="admin", detail=f"deleted user {username}")
    return {"ok": True}

# --- Permission tier management ---

@app.post("/api/admin/users/{username}/tier")
async def api_set_user_tier(username: str, body: dict, auth_token: str = Cookie(None)):
    """Set a user's permission tier (admin only)."""
    require_admin(auth_token)
    tier = body.get("tier", DEFAULT_PERMISSION_TIER)
    if tier not in PERMISSION_TIERS:
        raise HTTPException(400, f"Invalid tier. Options: {', '.join(PERMISSION_TIERS.keys())}")
    users = _load_users()
    if username.lower() not in users:
        raise HTTPException(404, "User not found")
    users[username.lower()]["permission_tier"] = tier
    _save_users(users)
    log_activity("tier_changed", username="admin", detail=f"set {username} to tier '{tier}'")
    return {"ok": True, "username": username, "tier": tier}

@app.get("/api/admin/tiers")
async def api_list_tiers(auth_token: str = Cookie(None)):
    """List available permission tiers (admin only)."""
    require_admin(auth_token)
    return {"tiers": {k: {"tools": v["tools"], "permission_mode": v["permission_mode"]} for k, v in PERMISSION_TIERS.items()}, "default": DEFAULT_PERMISSION_TIER}

# --- Relay key management ---

@app.post("/api/auth/regenerate-key")
async def api_regenerate_relay_key(body: dict, auth_token: str = Cookie(None)):
    """Regenerate own relay key. Requires password confirmation."""
    user = get_user_from_token(auth_token)
    if not user:
        raise HTTPException(401, "Not authenticated")
    password = body.get("password", "")
    if not authenticate_user(user["username"], password):
        raise HTTPException(401, "Incorrect password")
    users = _load_users()
    new_key = secrets.token_urlsafe(32)
    users[user["username"]]["relay_key"] = new_key
    _save_users(users)
    log_activity("relay_key_regenerated", username=user["username"])
    return {"ok": True, "relay_key": new_key}

@app.get("/api/admin/relay-keys")
async def api_admin_relay_keys(auth_token: str = Cookie(None)):
    """List all users' relay keys (admin only)."""
    require_admin(auth_token)
    users = _load_users()
    return [{
        "username": u["username"],
        "display_name": u.get("display_name", u["username"]),
        "email": u.get("email", ""),
        "relay_key": u.get("relay_key", ""),
        "relay_identity": u.get("relay_identity", f"{u['username']}_claude"),
    } for u in users.values()]

@app.post("/api/admin/push-relay-keys")
async def api_admin_push_relay_keys(body: dict, auth_token: str = Cookie(None)):
    """Push all user relay keys to the VPS relay server (admin only)."""
    require_admin(auth_token, body.get("admin_password"))
    users = _load_users()
    # Build the new AUTHORIZED_KEYS dict for the relay server
    new_keys = {}
    for u in users.values():
        key = u.get("relay_key")
        identity = u.get("relay_identity", f"{u['username']}_claude")
        if key:
            new_keys[identity] = key
    # System keys from env — format: HARNESS_SYSTEM_RELAY_KEYS="identity1:key1,identity2:key2"
    system_keys = {}
    for entry in os.environ.get("HARNESS_SYSTEM_RELAY_KEYS", "").split(","):
        entry = entry.strip()
        if ":" in entry:
            ident, k = entry.split(":", 1)
            system_keys[ident.strip()] = k.strip()
    new_keys.update(system_keys)
    # Read the relay server source, update AUTHORIZED_KEYS, push upgrade
    try:
        source = _relay.source()
        if isinstance(source, dict) and "source" in source:
            source = source["source"]
        elif isinstance(source, str):
            pass
        else:
            return {"ok": False, "error": "Could not read relay server source"}
        # Replace the AUTHORIZED_KEYS block
        import re
        keys_code = "AUTHORIZED_KEYS = {\n"
        for identity, key in sorted(new_keys.items()):
            keys_code += f'    "{identity}": "{key}",\n'
        keys_code += "}"
        # Find and replace the existing AUTHORIZED_KEYS dict
        pattern = r'AUTHORIZED_KEYS\s*=\s*\{[^}]+\}'
        new_source = re.sub(pattern, keys_code, source)
        if new_source == source:
            return {"ok": False, "error": "Could not find AUTHORIZED_KEYS in relay source"}
        result = _relay.upgrade(new_source, notes=f"Auto-push: {len(new_keys)} relay keys from harness admin")
        return {"ok": True, "keys_pushed": len(new_keys), "result": result}
    except Exception as e:
        return {"ok": False, "error": str(e)}

# --- Sentinel (theft-recovery daemon) ---

@app.get("/api/admin/sentinel/status")
async def api_sentinel_status(auth_token: str = Cookie(None)):
    """Get sentinel daemon status."""
    require_admin(auth_token)
    from tools.sentinel import get_status
    return get_status()

@app.post("/api/admin/sentinel/activate")
async def api_sentinel_activate(request: Request, auth_token: str = Cookie(None)):
    """Activate sentinel monitoring."""
    require_admin(auth_token)
    body = await request.json()
    password = body.get("password", "sentinel")
    import tools.sentinel as _sentinel_mod
    _sentinel_mod.RELAY_KEY = os.environ.get("RELAY_KEY_AI", "")
    return _sentinel_mod.activate_sentinel(password)

@app.post("/api/admin/sentinel/deactivate")
async def api_sentinel_deactivate(request: Request, auth_token: str = Cookie(None)):
    """Deactivate sentinel monitoring."""
    require_admin(auth_token)
    body = await request.json()
    password = body.get("password", "sentinel")
    from tools.sentinel import deactivate_sentinel
    return deactivate_sentinel(password)

@app.get("/api/admin/sentinel/evidence")
async def api_sentinel_evidence(auth_token: str = Cookie(None)):
    """List recent evidence files."""
    require_admin(auth_token)
    evidence_dir = Path(__file__).parent / "data" / "sentinel" / "evidence"
    if not evidence_dir.exists():
        return {"files": []}
    files = []
    for f in sorted(evidence_dir.rglob("*"), key=lambda x: x.stat().st_mtime if x.is_file() else 0, reverse=True):
        if f.is_file():
            files.append({
                "path": str(f.relative_to(evidence_dir)),
                "category": f.parent.parent.name if f.parent.parent != evidence_dir else f.parent.name,
                "size": f.stat().st_size,
                "modified": datetime.fromtimestamp(f.stat().st_mtime).isoformat(),
            })
            if len(files) >= 100:
                break
    return {"files": files}

@app.get("/api/admin/sentinel/evidence/{filepath:path}")
async def api_sentinel_evidence_file(filepath: str, auth_token: str = Cookie(None)):
    """Serve an evidence file."""
    require_admin(auth_token)
    evidence_dir = Path(__file__).parent / "data" / "sentinel" / "evidence"
    full_path = evidence_dir / filepath
    if not full_path.exists() or not full_path.is_file():
        raise HTTPException(404, "Evidence file not found")
    from starlette.responses import FileResponse
    return FileResponse(str(full_path))

@app.post("/api/admin/sentinel/test")
async def api_sentinel_test(auth_token: str = Cookie(None)):
    """Run a single test capture cycle (no upload)."""
    require_admin(auth_token)
    from tools.sentinel import capture_webcam, capture_screenshot, scan_wifi, get_system_info, EVIDENCE_DIR
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    results = {}
    img = capture_webcam()
    results["webcam"] = str(img) if img else None
    scr = capture_screenshot()
    results["screenshot"] = str(scr) if scr else None
    wpath, wdata = scan_wifi()
    results["wifi"] = {"path": str(wpath) if wpath else None, "networks": wdata.get("network_count", 0) if wdata else 0}
    sysinfo = get_system_info()
    results["system"] = sysinfo
    return results

# --- Relay routing config ---

_relay_routing_file = Path(__file__).parent / "data" / "relay_routing.json"

def _load_relay_routing():
    if _relay_routing_file.exists():
        return json.loads(_relay_routing_file.read_text(encoding="utf-8"))
    return {
        "mode": "all",  # "all" = inject into all sessions, "agent" = agent only, "specific" = named session, "current" = current active, "off" = disabled
        "target_session": None,  # session_id for "specific" mode
        "auto_respond": False,  # default OFF — don't auto-generate responses to relay pings
        "echo_filter": [],  # senders to filter out of injection (own relay identities)
    }

def _save_relay_routing(config):
    _relay_routing_file.write_text(json.dumps(config, indent=2), encoding="utf-8")

@app.get("/api/relay/routing")
async def api_relay_routing(auth_token: str = Cookie(None)):
    user = get_user_from_token(auth_token)
    if not user:
        raise HTTPException(401, "Not authenticated")
    return _load_relay_routing()

@app.post("/api/relay/routing")
async def api_set_relay_routing(body: dict, auth_token: str = Cookie(None)):
    user = get_user_from_token(auth_token)
    if not user:
        raise HTTPException(401, "Not authenticated")
    if user["username"] not in ADMIN_USERNAMES:
        raise HTTPException(403, "Admin only")
    config = _load_relay_routing()
    for key in ("mode", "target_session", "auto_respond", "echo_filter"):
        if key in body:
            config[key] = body[key]
    _save_relay_routing(config)
    return config

@app.post("/api/relay/admin/clear_session")
async def api_relay_clear_session(body: dict, auth_token: str = Cookie(None)):
    """Remove relay-injected turns from a session's messages.jsonl.

    A relay turn is identified by: role=user AND content starts with '[RELAY MESSAGES RECEIVED'.
    The assistant message that immediately follows each relay user-turn (if it's flagged as a
    relay auto-reply OR if dry_run=True just shows counts without deleting the assistant side)
    is also removed. Creates a timestamped .bak file first. Admin only. Per-session scoped.

    Body: { session_id: str, dry_run?: bool (default False), include_assistant_replies?: bool (default True) }
    """
    user = get_user_from_token(auth_token)
    if not user:
        raise HTTPException(401, "Not authenticated")
    if user["username"] not in ADMIN_USERNAMES:
        raise HTTPException(403, "Admin only")
    session_id = body.get("session_id")
    if not session_id:
        raise HTTPException(400, "session_id required")
    dry_run = bool(body.get("dry_run", False))
    include_replies = bool(body.get("include_assistant_replies", True))

    sess_path = SESSIONS_DIR / session_id / "messages.jsonl"
    if not sess_path.exists():
        raise HTTPException(404, f"session {session_id} not found")

    lines = sess_path.read_text(encoding="utf-8").splitlines()
    kept = []
    removed_user = 0
    removed_assistant = 0
    skip_next_assistant = False
    for line in lines:
        if not line.strip():
            kept.append(line)
            continue
        try:
            msg = json.loads(line)
        except Exception:
            kept.append(line)  # unparseable, preserve
            continue
        role = msg.get("role")
        content = msg.get("content", "") or ""
        if role == "user" and content.startswith("[RELAY MESSAGES RECEIVED"):
            removed_user += 1
            skip_next_assistant = include_replies
            continue
        if skip_next_assistant and role == "assistant":
            removed_assistant += 1
            skip_next_assistant = False
            continue
        # Any non-assistant message breaks the pairing expectation
        if skip_next_assistant and role != "assistant":
            skip_next_assistant = False
        kept.append(line)

    result = {
        "session_id": session_id,
        "dry_run": dry_run,
        "removed_user_turns": removed_user,
        "removed_assistant_replies": removed_assistant,
        "kept_total": len(kept),
        "original_total": len(lines),
    }
    if dry_run:
        return result

    if removed_user == 0 and removed_assistant == 0:
        result["backup"] = None
        result["note"] = "no relay turns found, file untouched"
        return result

    bak = sess_path.with_suffix(f".jsonl.bak_relay_{int(time.time())}")
    sess_path.rename(bak)
    sess_path.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
    result["backup"] = str(bak.name)

    # Force in-memory session reload if it's loaded
    sess = _sessions.get(session_id)
    if sess is not None:
        try:
            sess.reload_messages()
        except Exception as e:
            result["reload_error"] = str(e)

    return result

@app.get("/api/contacts")
async def api_contacts(auth_token: str = Cookie(None)):
    """Get all registered users with online/presence info (any logged-in user)."""
    current_user = get_user_from_token(auth_token)
    if not current_user:
        raise HTTPException(401, "Not authenticated")
    users_data = _load_users()
    presence = get_all_presence()
    contacts = []
    for u in users_data.values():
        uname = u["username"]
        is_online = uname in presence
        loc = presence.get(uname, {})
        contacts.append({
            "username": uname,
            "display_name": u.get("display_name", uname),
            "online": is_online,
            "location_type": loc.get("location_type", ""),
            "location_id": loc.get("location_id", ""),
            "is_admin": uname in ADMIN_USERNAMES,
        })
    # Online users first, then alphabetical
    contacts.sort(key=lambda c: (not c["online"], c["display_name"].lower()))
    return contacts

@app.post("/api/presence/heartbeat")
async def api_presence_heartbeat(auth_token: str = Cookie(None)):
    """Lightweight presence ping — any authenticated browser calls this every 15s."""
    user = get_user_from_token(auth_token)
    if not user:
        raise HTTPException(401, "Not authenticated")
    uname = user["username"]
    # Update _online_users with heartbeat timestamp
    existing = _online_users.get(uname, {})
    existing["last_seen"] = time.time()
    _online_users[uname] = existing
    return {"ok": True}

@app.get("/api/auth/online")
async def api_online():
    """Get list of currently online users."""
    now = time.time()
    online = []
    for username, info in _online_users.items():
        if now - info.get("last_seen", 0) < 60:  # seen in last 60 seconds
            online.append({"username": username, "room_id": info.get("room_id")})
    return online

@app.get("/api/presence")
async def api_presence():
    """Get all connected users across sessions and rooms."""
    return get_all_presence()

# ── Coqui TTS proxy ─────────────────────────────
COQUI_TTS_URL = "http://127.0.0.1:5002"

@app.get("/api/tts/health")
async def api_tts_health():
    """Check if Coqui TTS server is running."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"{COQUI_TTS_URL}/health")
            return r.json()
    except Exception:
        return {"status": "offline"}

@app.get("/api/tts/voices")
async def api_tts_voices():
    """List available Coqui voice clones."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{COQUI_TTS_URL}/list_voices")
            return r.json()
    except Exception:
        return {"voices": [], "count": 0, "status": "offline"}

@app.post("/api/tts")
async def api_tts(request: Request):
    """Proxy TTS request to Coqui server. Returns WAV audio."""
    import httpx
    body = await request.json()
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(f"{COQUI_TTS_URL}/api/tts", json=body)
            if r.status_code != 200:
                return JSONResponse(r.json(), status_code=r.status_code)
            return Response(content=r.content, media_type="audio/wav")
    except httpx.TimeoutException:
        return JSONResponse({"error": "TTS generation timed out"}, status_code=504)
    except Exception as e:
        return JSONResponse({"error": f"TTS server unavailable: {e}"}, status_code=502)

@app.post("/api/tts/stream")
async def api_tts_stream(request: Request):
    """Proxy streaming TTS (SSE) from Coqui server."""
    import httpx
    from starlette.responses import StreamingResponse
    body = await request.json()
    async def proxy_sse():
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                async with client.stream("POST", f"{COQUI_TTS_URL}/api/tts-stream-real", json=body) as r:
                    async for chunk in r.aiter_text():
                        yield chunk
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"
    return StreamingResponse(proxy_sse(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@app.get("/api/sessions/{session_id}/viewers")
async def api_session_viewers(session_id: str):
    """Get list of users currently viewing a session."""
    return {"viewers": _session_viewers(session_id)}

@app.get("/api/activity-log")
async def api_activity_log(limit: int = 100, offset: int = 0):
    """Read the last N activity log entries."""
    if not _activity_log_file.exists():
        return {"entries": []}
    lines = _activity_log_file.read_text(encoding="utf-8").strip().split("\n")
    lines = [l for l in lines if l.strip()]
    # Return most recent first
    lines.reverse()
    sliced = lines[offset:offset + limit]
    entries = []
    for line in sliced:
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return {"entries": entries, "total": len(lines)}


# ══════════════════════════════════════════════════════════════
# Rooms API removed — harness is single-user / direct-chat only.


# ══════════════════════════════════════════════════════════════
# API Routes
# ══════════════════════════════════════════════════════════════

@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index_dev.html")


@app.get("/api/sessions")
async def api_list_sessions():
    return list_sessions()


@app.get("/api/sessions/activity")
async def api_session_activity():
    """Lightweight endpoint: returns activity status for all generating sessions."""
    result = {}
    for sid, info in _session_activity.items():
        elapsed = int(time.time() - info.get("started", time.time()))
        result[sid] = {"status": info["status"], "elapsed": elapsed}
    return result


@app.get("/api/poll")
async def api_unified_poll(relay_since: float = 0, auth_token: str = Cookie(None)):
    """Single endpoint that returns all polling data at once.
    Replaces: /api/sessions/activity, /api/contacts, /api/presence,
    /api/presence/heartbeat, /api/relay/poll — one 5s fetch instead of 5+ separate ones."""
    user = get_user_from_token(auth_token)
    uname = user["username"] if user else None
    # --- Heartbeat (side-effect) ---
    if uname:
        existing = _online_users.get(uname, {})
        existing["last_seen"] = time.time()
        _online_users[uname] = existing
    # --- Session activity ---
    activity = {}
    for sid, info in _session_activity.items():
        elapsed = int(time.time() - info.get("started", time.time()))
        activity[sid] = {"status": info["status"], "elapsed": elapsed}
    # --- Contacts + presence (combined) ---
    contacts = []
    if uname:
        users_data = _load_users()
        presence = get_all_presence()
        for u in users_data.values():
            un = u["username"]
            is_online = un in presence
            loc = presence.get(un, {})
            contacts.append({
                "username": un,
                "display_name": u.get("display_name", un),
                "online": is_online,
                "location_type": loc.get("location_type", ""),
                "location_id": loc.get("location_id", ""),
                "is_admin": un in ADMIN_USERNAMES,
            })
        contacts.sort(key=lambda c: (not c["online"], c["display_name"].lower()))
    # --- Relay messages ---
    relay_msgs = []
    try:
        result = _relay.history(page=1, per_page=50)
        all_msgs = result.get("messages", [])
        relay_msgs = [m for m in all_msgs if m.get("ts", 0) > relay_since]
        for msg in relay_msgs:
            _fix_relay_sender(msg)
        relay_msgs.reverse()  # oldest first
    except Exception:
        pass
    return {
        "activity": activity,
        "contacts": contacts,
        "relay": {"messages": relay_msgs},
    }


@app.post("/api/sessions")
async def api_create_session():
    session = get_session()
    return {"session_id": session.id}


@app.get("/api/sessions/{session_id}")
async def api_get_session(session_id: str, limit: int = 50, offset: int = 0, from_index: int = -1):
    session = get_session(session_id)
    # Always read from disk for authoritative count — in-memory may be stale
    # if another server instance wrote messages we haven't reloaded yet
    msgs_path = session.dir / "messages.jsonl"
    if msgs_path.exists():
        disk_messages = []
        with open(msgs_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        disk_messages.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        all_msgs = disk_messages
    else:
        all_msgs = session.messages
    total = len(all_msgs)
    if from_index >= 0:
        # Forward pagination: return `limit` messages starting at from_index
        start = min(from_index, total)
        page = all_msgs[start:start + limit]
    elif offset == 0:
        # Initial load — last `limit` messages
        start = max(0, total - limit)
        page = all_msgs[start:]
    else:
        # Loading older — offset is how many from the end we've already loaded
        end = max(0, total - offset)
        start = max(0, end - limit)
        page = all_msgs[start:end]
    # Enrich messages with display_name from user accounts
    users = _load_users()
    enriched = []
    for msg in page:
        m = dict(msg)
        if m.get("username") and m["username"] in users:
            m["display_name"] = users[m["username"]].get("display_name", m["username"])
        enriched.append(m)
    return {
        "id": session.id,
        "messages": enriched,
        "total_messages": total,
        "start_index": start,
        "has_older": start > 0,
        "context_stats": session.get_context_stats(),
        "artifacts": session.artifacts.list_all(),
        "created": session.created,
        "base_prompt": session.base_prompt,
    }


@app.get("/api/sessions/{session_id}/stats")
async def api_session_stats(session_id: str):
    session = get_session(session_id)
    return session.get_context_stats()


@app.get("/api/sessions/{session_id}/wakeup")
async def api_get_wakeup(session_id: str):
    """Get active wakeup timer for this session."""
    timer_data = _wakeup_timers.get(session_id)
    if not timer_data:
        return {"active": False}
    remaining = max(0, timer_data["fires_at"] - time.time())
    return {
        "active": True,
        "delay": timer_data["delay"],
        "remaining": int(remaining),
        "reason": timer_data["reason"],
        "fires_at": timer_data["fires_at"],
        "scheduled_at": timer_data["scheduled_at"],
    }


@app.delete("/api/sessions/{session_id}/wakeup")
async def api_cancel_wakeup(session_id: str):
    """Cancel active wakeup timer for this session."""
    timer_data = _wakeup_timers.pop(session_id, None)
    if timer_data and timer_data.get("timer"):
        timer_data["timer"].cancel()
        print(f"[WAKEUP] Timer cancelled for session {session_id}", flush=True)
        _save_wakeup_timers()
        await _broadcast_to_session(session_id, {"type": "wakeup_cancelled"})
        return {"cancelled": True}
    return {"cancelled": False, "reason": "no active timer"}


@app.get("/api/wakeup-timers")
async def api_list_wakeup_timers():
    """List all active wakeup timers across sessions."""
    result = {}
    for sid, data in _wakeup_timers.items():
        remaining = max(0, data["fires_at"] - time.time())
        result[sid] = {
            "delay": data["delay"],
            "remaining": int(remaining),
            "reason": data["reason"],
            "fires_at": data["fires_at"],
            "username": data.get("username", ""),
        }
    return result


@app.get("/api/artifacts/{session_id}/{artifact_id}")
async def api_get_artifact(session_id: str, artifact_id: str):
    session = get_session(session_id)
    content = session.artifacts.get(artifact_id)
    meta = session.artifacts.get_meta(artifact_id)
    if content is None:
        raise HTTPException(404, "Artifact not found")
    return {"content": content, "meta": meta}


@app.get("/api/artifacts/{session_id}/{artifact_id}/download")
async def api_download_artifact(session_id: str, artifact_id: str):
    session = get_session(session_id)
    meta = session.artifacts.get_meta(artifact_id)
    if meta is None:
        raise HTTPException(404, "Artifact not found")
    filename = meta.get("file", f"{artifact_id}.txt")
    path = session.artifacts.dir / filename
    if not path.exists():
        raise HTTPException(404, "Artifact file not found")
    from starlette.responses import FileResponse
    return FileResponse(
        path,
        filename=filename,
        media_type="application/octet-stream",
    )


@app.post("/api/artifacts/{session_id}/register")
async def api_register_artifact(session_id: str, body: dict):
    """Register a file already on disk in the session's artifacts directory."""
    session = get_session(session_id)
    filename = body.get("filename")
    if not filename:
        raise HTTPException(400, "filename required")
    art_id = session.artifacts.register_file(
        filename,
        title=body.get("title"),
        language=body.get("language"),
    )
    if art_id is None:
        raise HTTPException(404, f"File {filename} not found in artifacts directory")
    # Broadcast update to all connected websockets
    for ws in list(_active_websockets):
        try:
            await ws.send_json({"type": "artifacts_update", "artifacts": session.artifacts.list_all()})
        except Exception:
            _active_websockets.discard(ws)
    return {"artifact_id": art_id, "meta": session.artifacts.get_meta(art_id)}


@app.delete("/api/artifacts/{session_id}/{artifact_id}")
async def api_delete_artifact(session_id: str, artifact_id: str, request: Request, auth_token: str = Cookie(None)):
    user = get_user_from_token(auth_token)
    if not user or user.get("username", "").lower() not in ADMIN_USERNAMES:
        raise HTTPException(403, "Admin access required")
    session = get_session(session_id)
    deleted = session.artifacts.delete(artifact_id)
    if not deleted:
        raise HTTPException(404, "Artifact not found")
    # Broadcast update
    for ws in list(_active_websockets):
        try:
            await ws.send_json({"type": "artifacts_update", "artifacts": session.artifacts.list_all()})
        except Exception:
            _active_websockets.discard(ws)
    return {"ok": True}


@app.patch("/api/sessions/{session_id}")
async def api_update_session(session_id: str, body: dict, auth_token: str = Cookie(None)):
    session = get_session(session_id)
    if "name" in body:
        session.name = body["name"][:100]
    if "archived" in body:
        # Archive/unarchive is admin-only, requires password
        require_admin(auth_token, body.get("admin_password"))
        session.archived = bool(body["archived"])
    session._save_meta()
    return {"ok": True, "name": session.name, "archived": session.archived}


@app.post("/api/sessions/{session_id}/merge")
async def api_merge_session(session_id: str, body: dict, auth_token: str = Cookie(None)):
    """Merge multiple source sessions into target session. Auto-sorts by timestamp, archives sources.

    Body: { source_ids: [id1, id2, ...], admin_password: "..." }
    Sources are archived (never deleted) to preserve file references.
    """
    require_admin(auth_token, body.get("admin_password"))
    source_ids = body.get("source_ids", [])
    # Backwards compat: support single source_id too
    if not source_ids and body.get("source_id"):
        source_ids = [body["source_id"]]
    if not source_ids:
        raise HTTPException(400, "source_ids required (list of session IDs to merge)")
    source_ids = [s for s in source_ids if s != session_id]  # filter self
    if not source_ids:
        raise HTTPException(400, "Cannot merge a session into itself")

    target = get_session(session_id)

    # Load all sources
    def _last_ts(s):
        """Get the last message timestamp — this determines merge order."""
        for m in reversed(s.messages):
            ts = m.get("timestamp")
            if ts:
                return ts
        return ""

    def _first_ts(s):
        for m in s.messages:
            ts = m.get("timestamp")
            if ts:
                return ts
        return ""

    sources = []
    for sid in source_ids:
        s = get_session(sid)
        if s.messages:
            sources.append(s)

    if not sources:
        raise HTTPException(400, "No source sessions have messages")

    # Sort ALL sessions (target + sources) by LAST message timestamp.
    # The one with the earliest last-message becomes the base (real_target),
    # and each subsequent session is appended in order of its last message.
    # This way the most recently active thread always ends up at the bottom.
    all_sessions = [target] + sources
    all_sessions.sort(key=lambda s: _last_ts(s) or "")

    real_target = all_sessions[0]
    merge_sources = [s for s in all_sessions[1:]]  # appended in last-timestamp order

    total_merged = 0
    archived_ids = []

    for source in merge_sources:
        # Add a divider
        divider = {
            "role": "assistant",
            "content": f"---\n*[Merged from session: {source.name or source.id[:8]}  ({len(source.messages)} messages)]*\n---",
            "content_full": f"---\n*[Merged from session: {source.name or source.id[:8]}  ({len(source.messages)} messages)]*\n---",
            "timestamp": datetime.now().isoformat(),
            "index": len(real_target.messages),
            "merged_from": source.id,
        }
        real_target._append_message(divider)

        # Append all source messages
        for msg in source.messages:
            merged_msg = dict(msg)
            merged_msg["index"] = len(real_target.messages)
            merged_msg["merged_from"] = source.id
            real_target._append_message(merged_msg)
        total_merged += len(source.messages)

        # Copy uploads (session-local) + artifacts (canonical ARTIFACTS_DIR) to target
        if source.id != real_target.id:
            import shutil
            # Uploads live under sessions/{id}/uploads/
            src_uploads = SESSIONS_DIR / source.id / "uploads"
            dst_uploads = SESSIONS_DIR / real_target.id / "uploads"
            if src_uploads.exists():
                dst_uploads.mkdir(parents=True, exist_ok=True)
                for f in src_uploads.iterdir():
                    if f.is_file() and not (dst_uploads / f.name).exists():
                        shutil.copy2(f, dst_uploads / f.name)
            # Artifacts live under ARTIFACTS_DIR/{id}/ — copy files, then re-sync target index
            src_arts = ARTIFACTS_DIR / source.id
            dst_arts = ARTIFACTS_DIR / real_target.id
            if src_arts.exists():
                dst_arts.mkdir(parents=True, exist_ok=True)
                for f in src_arts.iterdir():
                    if f.is_file() and f.name != 'index.json' and not (dst_arts / f.name).exists():
                        shutil.copy2(f, dst_arts / f.name)
                # Force re-scan so copied files get indexed
                real_target.artifacts.sync_from_disk()

            # Archive source (never delete — preserves original files)
            source.archived = True
            source._save_meta()
            archived_ids.append(source.id)

    return {
        "ok": True,
        "target_id": real_target.id,
        "sources_merged": len(merge_sources),
        "messages_merged": total_merged,
        "archived_ids": archived_ids,
    }


@app.post("/api/sessions/{session_id}/delete")
async def api_delete_session(session_id: str, body: dict, auth_token: str = Cookie(None)):
    """Delete a session (moves to trash). Admin + password required."""
    require_admin(auth_token, body.get("admin_password"))
    session_dir = SESSIONS_DIR / session_id
    if not session_dir.exists():
        raise HTTPException(404, "Session not found")
    # If the dir is completely empty (ghost), just remove it outright
    if not any(session_dir.iterdir()):
        session_dir.rmdir()
    else:
        # Move to trash (rename, don't destroy)
        trash_dir = SESSIONS_DIR / f"{session_id}.deleted"
        # If a previous trash dir exists, remove it first
        if trash_dir.exists():
            import shutil
            shutil.rmtree(trash_dir)
        session_dir.rename(trash_dir)
    # Remove from in-memory cache
    _sessions.pop(session_id, None)
    return {"ok": True, "session_id": session_id}


@app.post("/api/sessions/{session_id}/split/{msg_index}")
async def api_split_session(session_id: str, msg_index: int, request: Request, auth_token: str = Cookie(None)):
    """Split a session: copy messages 0..msg_index (inclusive) into a new session. Admin + password required."""
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    require_admin(auth_token, body.get("admin_password"))
    source = get_session(session_id)
    if not source:
        raise HTTPException(404, "Session not found")
    if msg_index < 0 or msg_index >= len(source.messages):
        raise HTTPException(400, f"Invalid index {msg_index}, session has {len(source.messages)} messages")
    # Create new session
    new_session = Session()
    new_session.name = (source.name or source.id[:8]) + " (split)"
    new_session.base_prompt = source.base_prompt
    new_session.session_notes = source.session_notes
    new_session.system_prompt = source.system_prompt
    new_session.pinned_context = source.pinned_context
    new_session.max_context_tokens = source.max_context_tokens
    new_session.show_thinking = source.show_thinking
    new_session.memory_config = dict(source.memory_config)
    new_session.model = getattr(source, 'model', '')
    new_session._save_meta()
    # Copy messages up to and including msg_index
    for i, msg in enumerate(source.messages[:msg_index + 1]):
        copied = dict(msg)
        copied["index"] = i
        new_session._append_message(copied)
    # Copy uploads (session-local) + artifacts (canonical ARTIFACTS_DIR) to new session
    import shutil
    src_uploads = SESSIONS_DIR / source.id / "uploads"
    dst_uploads = SESSIONS_DIR / new_session.id / "uploads"
    if src_uploads.exists():
        dst_uploads.mkdir(parents=True, exist_ok=True)
        for f in src_uploads.iterdir():
            if f.is_file():
                shutil.copy2(f, dst_uploads / f.name)
    src_arts = ARTIFACTS_DIR / source.id
    dst_arts = ARTIFACTS_DIR / new_session.id
    if src_arts.exists():
        dst_arts.mkdir(parents=True, exist_ok=True)
        for f in src_arts.iterdir():
            if f.is_file() and f.name != 'index.json':
                shutil.copy2(f, dst_arts / f.name)
        new_session.artifacts.sync_from_disk()
    new_session._rebuild_context_window()
    new_session._save_meta()
    _sessions[new_session.id] = new_session
    return {"ok": True, "new_session_id": new_session.id, "new_session_name": new_session.name, "messages_copied": msg_index + 1}


def _backup_messages_before_delete(session):
    """Backup messages.jsonl before any delete operation. Keeps last 5 backups per session."""
    import shutil
    msgs_path = session.dir / "messages.jsonl"
    if not msgs_path.exists():
        return
    backup_dir = session.dir / ".msg_backups"
    backup_dir.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    shutil.copy2(msgs_path, backup_dir / f"messages_{ts}.jsonl")
    # Prune old backups, keep last 5
    backups = sorted(backup_dir.glob("messages_*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in backups[5:]:
        old.unlink()


@app.post("/api/sessions/{session_id}/messages/restore")
async def api_restore_messages(session_id: str, request: Request, auth_token: str = Cookie(None)):
    """Restore messages from the most recent pre-delete backup."""
    import shutil
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    require_admin(auth_token, body.get("admin_password"))
    session = get_session(session_id)
    if not session:
        raise HTTPException(404, "Session not found")
    backup_dir = session.dir / ".msg_backups"
    if not backup_dir.exists():
        raise HTTPException(404, "No backups found")
    backups = sorted(backup_dir.glob("messages_*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not backups:
        raise HTTPException(404, "No backups found")
    # Which backup to restore (default: latest, or specify index)
    backup_idx = body.get("backup_index", 0)
    if backup_idx < 0 or backup_idx >= len(backups):
        raise HTTPException(400, f"Invalid backup index {backup_idx}, {len(backups)} backups available")
    backup_path = backups[backup_idx]
    # Restore: copy backup over messages.jsonl
    msgs_path = session.dir / "messages.jsonl"
    shutil.copy2(backup_path, msgs_path)
    # Reload in-memory
    session.messages = []
    with open(msgs_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                session.messages.append(json.loads(line))
    session._last_mtime = msgs_path.stat().st_mtime
    session._rebuild_context_window()
    backup_ts = backup_path.stem.replace("messages_", "")
    return {"ok": True, "restored_from": backup_ts, "message_count": len(session.messages),
            "backups_available": [b.stem.replace("messages_", "") for b in backups]}


@app.delete("/api/sessions/{session_id}/messages/bulk")
async def api_delete_messages_bulk(session_id: str, request: Request, auth_token: str = Cookie(None)):
    """Delete specific messages by indices (non-contiguous). Admin + password required.
    Body: { admin_password, indices: [int, ...] }"""
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    require_admin(auth_token, body.get("admin_password"))
    session = get_session(session_id)
    if not session:
        raise HTTPException(404, "Session not found")
    indices = sorted(set(body.get("indices", [])), reverse=True)  # Delete from end to preserve earlier indices
    if not indices:
        raise HTTPException(400, "No indices provided")
    _backup_messages_before_delete(session)
    for idx in indices:
        if 0 <= idx < len(session.messages):
            del session.messages[idx]
    msgs_path = session.dir / "messages.jsonl"
    fd, tmp_path = tempfile.mkstemp(dir=session.dir, suffix=".jsonl.tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        for m in session.messages:
            f.write(json.dumps(m) + "\n")
    os.replace(tmp_path, msgs_path)
    session._last_mtime = msgs_path.stat().st_mtime
    session._rebuild_context_window()
    session._save_meta()
    return {"ok": True, "deleted_count": len(indices), "remaining": len(session.messages)}


@app.post("/api/sessions/{session_id}/messages/move")
async def api_move_messages(session_id: str, request: Request, auth_token: str = Cookie(None)):
    """Move messages by index from one session to another. Admin + password required.
    Body: { admin_password, indices: [int, ...], target_session_id: str }"""
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    require_admin(auth_token, body.get("admin_password"))
    source = get_session(session_id)
    if not source:
        raise HTTPException(404, "Source session not found")
    target_id = body.get("target_session_id", "")
    target = get_session(target_id)
    if not target:
        raise HTTPException(404, "Target session not found")
    if target_id == session_id:
        raise HTTPException(400, "Cannot move messages to the same session")
    indices = sorted(set(body.get("indices", [])))
    if not indices:
        raise HTTPException(400, "No indices provided")
    # Collect messages to move (in order)
    moved = []
    for idx in indices:
        if 0 <= idx < len(source.messages):
            moved.append(dict(source.messages[idx]))
    if not moved:
        raise HTTPException(400, "No valid messages to move")
    # Append to target
    for msg in moved:
        msg.pop("index", None)
        target._append_message(msg)
    target._rebuild_context_window()
    target._save_meta()
    # Remove from source (reverse order to preserve indices)
    for idx in sorted(indices, reverse=True):
        if 0 <= idx < len(source.messages):
            del source.messages[idx]
    # Rewrite source JSONL
    msgs_path = source.dir / "messages.jsonl"
    fd, tmp_path = tempfile.mkstemp(dir=source.dir, suffix=".jsonl.tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        for m in source.messages:
            f.write(json.dumps(m) + "\n")
    os.replace(tmp_path, msgs_path)
    source._last_mtime = msgs_path.stat().st_mtime
    source._rebuild_context_window()
    source._save_meta()
    return {"ok": True, "moved_count": len(moved), "source_remaining": len(source.messages), "target_total": len(target.messages)}


@app.delete("/api/sessions/{session_id}/messages/{msg_index}")
async def api_delete_message(session_id: str, msg_index: int, request: Request, auth_token: str = Cookie(None)):
    """Delete a specific message from a session. Admin + password required."""
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    require_admin(auth_token, body.get("admin_password"))
    session = get_session(session_id)
    if not session:
        raise HTTPException(404, "Session not found")
    if msg_index < 0 or msg_index >= len(session.messages):
        raise HTTPException(400, f"Invalid message index {msg_index}, session has {len(session.messages)} messages")
    _backup_messages_before_delete(session)
    # Remove from in-memory list
    removed = session.messages.pop(msg_index)
    # Rewrite messages.jsonl without the deleted message (atomic)
    with session._jsonl_lock():
        msgs_path = session.dir / "messages.jsonl"
        fd, tmp_path = tempfile.mkstemp(dir=session.dir, suffix=".jsonl.tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for m in session.messages:
                f.write(json.dumps(m) + "\n")
        os.replace(tmp_path, msgs_path)
        session._last_mtime = msgs_path.stat().st_mtime
    session._rebuild_context_window()
    session._save_meta()
    return {"ok": True, "removed_role": removed.get("role", "unknown"), "remaining": len(session.messages)}


@app.delete("/api/sessions/{session_id}/messages")
async def api_delete_messages_range(session_id: str, request: Request, auth_token: str = Cookie(None)):
    """Delete a range of messages from a session. Admin + password required.
    Body: { admin_password, start: int, end: int (exclusive) }"""
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    require_admin(auth_token, body.get("admin_password"))
    session = get_session(session_id)
    if not session:
        raise HTTPException(404, "Session not found")
    start = body.get("start", 0)
    end = body.get("end", len(session.messages))
    if start < 0 or end > len(session.messages) or start >= end:
        raise HTTPException(400, f"Invalid range [{start}, {end}), session has {len(session.messages)} messages")
    count = end - start
    _backup_messages_before_delete(session)
    del session.messages[start:end]
    # Rewrite messages.jsonl (atomic)
    with session._jsonl_lock():
        msgs_path = session.dir / "messages.jsonl"
        fd, tmp_path = tempfile.mkstemp(dir=session.dir, suffix=".jsonl.tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for m in session.messages:
                f.write(json.dumps(m) + "\n")
        os.replace(tmp_path, msgs_path)
        session._last_mtime = msgs_path.stat().st_mtime
    session._rebuild_context_window()
    session._save_meta()
    return {"ok": True, "deleted_count": count, "remaining": len(session.messages)}


@app.post("/api/sessions/{session_id}/branch")
async def api_branch_session(session_id: str, body: dict):
    """Create a branched session from an edit point. Copies messages up to msg_index from parent."""
    parent = get_session(session_id)
    msg_index = body.get("msg_index", 0)
    new_text = body.get("text", "")

    # Create new session
    branch = get_session()
    branch.name = f"Edit: {new_text[:40]}..." if len(new_text) > 40 else f"Edit: {new_text}"

    # Store parent reference
    branch_meta_extra = {
        "parent_session": session_id,
        "branch_point": msg_index,
    }

    # Copy messages up to (but not including) the edited message
    for msg in parent.messages[:msg_index]:
        branch.messages.append(msg)

    # Write copied messages to disk (atomic)
    msgs_path = branch.dir / "messages.jsonl"
    fd, tmp_path = tempfile.mkstemp(dir=branch.dir, suffix=".jsonl.tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        for msg in branch.messages:
            f.write(json.dumps(msg) + "\n")
    os.replace(tmp_path, msgs_path)

    # Save meta with parent reference
    branch._rebuild_context_window()
    branch._save_meta()
    # Patch the meta file to include parent info
    meta_path = branch.dir / "meta.json"
    meta = json.loads(meta_path.read_text())
    meta.update(branch_meta_extra)
    meta_path.write_text(json.dumps(meta, indent=2))

    return {"ok": True, "session_id": branch.id, "name": branch.name}


@app.post("/api/sessions/{session_id}/pin")
async def api_pin_context(session_id: str, body: dict):
    session = get_session(session_id)
    session.pinned_context = body.get("context", "")
    session._save_meta()
    session._rebuild_context_window()
    return {"ok": True}


# ── Base Prompt (Persona) ────────────────────────────────────
@app.get("/api/sessions/{session_id}/base-prompt")
async def get_base_prompt(session_id: str):
    session = get_session(session_id)
    return {"base_prompt": session.base_prompt}


@app.post("/api/sessions/{session_id}/base-prompt")
async def set_base_prompt(session_id: str, body: dict):
    session = get_session(session_id)
    session.base_prompt = body.get("base_prompt", "")
    session._save_meta()
    return {"ok": True, "base_prompt": session.base_prompt}


# ── Default Base Prompt (server-wide) ────────────────────────
@app.get("/api/defaults/base-prompt")
async def get_default_base_prompt():
    defaults_path = Path(__file__).parent / "data" / "defaults.json"
    if defaults_path.exists():
        defaults = json.loads(defaults_path.read_text())
        return {"base_prompt": defaults.get("base_prompt", "")}
    return {"base_prompt": ""}


@app.post("/api/defaults/base-prompt")
async def set_default_base_prompt(body: dict):
    defaults_path = Path(__file__).parent / "data" / "defaults.json"
    defaults = {}
    if defaults_path.exists():
        defaults = json.loads(defaults_path.read_text())
    defaults["base_prompt"] = body.get("base_prompt", "")
    defaults_path.write_text(json.dumps(defaults, indent=2))
    return {"ok": True, "base_prompt": defaults["base_prompt"]}


# ── Session Notes ────────────────────────────────────────────
@app.get("/api/sessions/{session_id}/notes")
async def get_session_notes(session_id: str):
    session = get_session(session_id)
    return {"notes": session.session_notes}


@app.post("/api/sessions/{session_id}/notes")
async def set_session_notes(session_id: str, body: dict):
    session = get_session(session_id)
    session.session_notes = body.get("notes", "")
    session._save_meta()
    return {"ok": True, "notes": session.session_notes}


# ── Context Injection Toggles ───────────────────────────────
@app.get("/api/sessions/{session_id}/context-toggles")
async def get_context_toggles(session_id: str):
    session = get_session(session_id)
    return {"toggles": session.context_toggles}


@app.post("/api/sessions/{session_id}/context-toggles")
async def set_context_toggles(session_id: str, body: dict):
    session = get_session(session_id)
    toggles = body.get("toggles", {})
    # Only update known keys
    for key in session.context_toggles:
        if key in toggles:
            session.context_toggles[key] = bool(toggles[key])
    session._save_meta()
    return {"ok": True, "toggles": session.context_toggles}


# ── Thinking Toggle ──────────────────────────────────────────
@app.get("/api/sessions/{session_id}/show-thinking")
async def get_show_thinking(session_id: str):
    session = get_session(session_id)
    return {"show_thinking": session.show_thinking}


@app.post("/api/sessions/{session_id}/show-thinking")
async def set_show_thinking(session_id: str, body: dict):
    session = get_session(session_id)
    session.show_thinking = bool(body.get("show_thinking", False))
    session._save_meta()
    return {"ok": True, "show_thinking": session.show_thinking}


# ── Pure Mode ────────────────────────────────────────────────
@app.get("/api/sessions/{session_id}/pure-mode")
async def get_pure_mode(session_id: str):
    session = get_session(session_id)
    return {"pure_mode": session.pure_mode}

@app.post("/api/sessions/{session_id}/pure-mode")
async def set_pure_mode(session_id: str, body: dict):
    session = get_session(session_id)
    session.pure_mode = bool(body.get("pure_mode", False))
    session._save_meta()
    return {"ok": True, "pure_mode": session.pure_mode}


# ── Coding Mode ──────────────────────────────────────────────
@app.get("/api/sessions/{session_id}/coding-mode")
async def get_coding_mode(session_id: str):
    session = get_session(session_id)
    return {"coding_mode": session.coding_mode}

@app.post("/api/sessions/{session_id}/coding-mode")
async def set_coding_mode(session_id: str, body: dict):
    session = get_session(session_id)
    session.coding_mode = bool(body.get("coding_mode", False))
    session._save_meta()
    # Toggle auto-snapshot flag file for this harness session
    flag_dir = Path.home() / ".claude_active_sessions"
    flag_file = flag_dir / f"{session_id}.flag"
    if session.coding_mode:
        flag_dir.mkdir(parents=True, exist_ok=True)
        flag_file.touch()
    else:
        flag_file.unlink(missing_ok=True)
    return {"ok": True, "coding_mode": session.coding_mode}


# ── Code Viewport (working files for coding mode) ───────────

@app.get("/api/sessions/{session_id}/viewport")
async def get_viewport(session_id: str):
    session = get_session(session_id)
    return {"ok": True, "working_files": session.working_files}

@app.post("/api/sessions/{session_id}/viewport")
async def add_viewport_file(session_id: str, body: dict):
    """Add a file to the code viewport. Body: {"file": "path" or "path:start-end"}"""
    session = get_session(session_id)
    file_spec = body.get("file", "").strip()
    if not file_spec:
        return JSONResponse({"error": "file is required"}, status_code=400)
    if file_spec not in session.working_files:
        session.working_files.append(file_spec)
        session._save_meta()
    return {"ok": True, "working_files": session.working_files}

@app.delete("/api/sessions/{session_id}/viewport")
async def remove_viewport_file(session_id: str, body: dict):
    """Remove a file from the code viewport. Body: {"file": "path"} or {"index": 0}"""
    session = get_session(session_id)
    file_spec = body.get("file", "").strip()
    idx = body.get("index")
    if file_spec and file_spec in session.working_files:
        session.working_files.remove(file_spec)
    elif idx is not None and 0 <= idx < len(session.working_files):
        session.working_files.pop(idx)
    else:
        return JSONResponse({"error": "file not found in viewport"}, status_code=404)
    session._save_meta()
    return {"ok": True, "working_files": session.working_files}

@app.put("/api/sessions/{session_id}/viewport")
async def set_viewport(session_id: str, body: dict):
    """Replace the entire viewport list. Body: {"working_files": ["path:1-50", ...]}"""
    session = get_session(session_id)
    session.working_files = body.get("working_files", [])
    session._save_meta()
    return {"ok": True, "working_files": session.working_files}


# ── TURN Ephemeral Credentials ──────────────────────────────
_TURN_SECRET = os.environ.get("TURN_SECRET", "")
_TURN_TTL = 86400  # 24 hours

@app.get("/api/turn-credentials")
async def get_turn_credentials():
    """Generate time-limited TURN credentials using HMAC-SHA1."""
    expiry = int(time.time()) + _TURN_TTL
    username = str(expiry)
    h = hmac.new(_TURN_SECRET.encode(), username.encode(), "sha1")
    credential = base64.b64encode(h.digest()).decode()
    # TURN server URIs from env. Empty = no TURN configured; browser falls back to public STUN.
    _turn_host = os.environ.get("TURN_HOST", "")
    _turns_host = os.environ.get("TURNS_HOST", "")
    _uris = []
    if _turn_host:
        _uris.append(f"turn:{_turn_host}:3478")
        _uris.append(f"turn:{_turn_host}:3478?transport=tcp")
    if _turns_host:
        _uris.append(f"turns:{_turns_host}:5349")
    return {
        "username": username,
        "credential": credential,
        "ttl": _TURN_TTL,
        "uris": _uris,
    }


# ── Model Selection ──────────────────────────────────────────
CLAUDE_MODELS = {
    "": "Default (Opus)",
    "claude-opus-4-7": "Claude Opus 4.7",
    "opus": "Claude Opus 4.6",
    "sonnet": "Claude Sonnet 4.6",
    "haiku": "Claude Haiku 4.5",
}

def _get_ollama_models():
    """Query local Ollama for available models."""
    import urllib.request
    try:
        req = urllib.request.Request("http://localhost:11434/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            models = {}
            for m in data.get("models", []):
                name = m["name"]
                size_gb = m.get("size", 0) / 1e9
                models[f"ollama:{name}"] = f"{name} ({size_gb:.1f}GB, local)"
            return models
    except Exception:
        return {}

def _get_all_models():
    """Return combined Claude + Ollama models."""
    models = dict(CLAUDE_MODELS)
    models.update(_get_ollama_models())
    return models

@app.get("/api/models")
async def list_models():
    all_models = _get_all_models()
    return {"models": [{"id": k, "name": v} for k, v in all_models.items()]}

@app.get("/api/sessions/{session_id}/model")
async def get_session_model(session_id: str):
    session = get_session(session_id)
    model = getattr(session, 'model', '')
    all_models = _get_all_models()
    return {"model": model, "name": all_models.get(model, model or "Default (Opus)")}

@app.post("/api/sessions/{session_id}/model")
async def set_session_model(session_id: str, body: dict):
    session = get_session(session_id)
    model = body.get("model", "")
    all_models = _get_all_models()
    if model and model not in all_models:
        return {"error": f"Unknown model: {model}. Available: {list(all_models.keys())}"}
    session.model = model
    # Clamp context budget to new model's safe max if current setting is too high.
    _safe_max = _default_budget_for_model(model)
    _clamped = False
    if session.max_context_tokens > _safe_max:
        _old_budget = session.max_context_tokens
        print(f"[MODEL] Clamping context budget {session.max_context_tokens:,} → {_safe_max:,} for model '{model or 'default'}'", flush=True)
        session.max_context_tokens = _safe_max
        session._rebuild_context_window()
        _clamped = True
        # Notify any connected clients so they can fire a context_clamped toast
        try:
            await _broadcast_to_session(session_id, {
                "type": "context_clamped",
                "old": _old_budget,
                "new": _safe_max,
                "model": model or "default",
            })
        except Exception:
            pass
    session._save_meta()
    return {"ok": True, "model": model, "name": all_models.get(model, model or "Default (Opus)"), "max_context_tokens": session.max_context_tokens, "budget_clamped": _clamped}


# ── Chat Import ──────────────────────────────────────────────
def _do_chatgpt_import(body):
    """Shared ChatGPT import logic — used by both JSON-body and multipart-file endpoints."""
    messages_data = body.get("messages", []) if isinstance(body, dict) else []
    title = body.get("title", "Imported ChatGPT Chat") if isinstance(body, dict) else "Imported ChatGPT Chat"

    if not messages_data:
        # Try full export format: [{title, mapping: {id: {message: {content: {parts}}}}}]
        conversations = body if isinstance(body, list) else [body]
        imported_count = 0
        session_ids = []
        for conv in conversations:
            conv_title = conv.get("title", f"Imported Chat {imported_count + 1}")
            mapping = conv.get("mapping", {})
            msgs = []
            for node_id, node in mapping.items():
                msg = node.get("message")
                if not msg or not msg.get("content"):
                    continue
                role = msg.get("author", {}).get("role", "unknown")
                parts = msg["content"].get("parts", [])
                text = "\n".join(str(p) for p in parts if isinstance(p, str))
                if not text.strip():
                    continue
                if role in ("user", "human"):
                    msgs.append({"role": "user", "content": text, "timestamp": msg.get("create_time", "")})
                elif role in ("assistant", "ai"):
                    msgs.append({"role": "assistant", "content": text, "content_full": text, "timestamp": msg.get("create_time", "")})
            if msgs:
                # Sort by timestamp if available
                msgs.sort(key=lambda m: m.get("timestamp", 0) or 0)
                session = Session()
                session.name = conv_title
                for m in msgs:
                    m["index"] = len(session.messages)
                    if not m.get("timestamp"):
                        m["timestamp"] = datetime.now().isoformat()
                    elif isinstance(m["timestamp"], (int, float)):
                        m["timestamp"] = datetime.fromtimestamp(m["timestamp"]).isoformat()
                    session._append_message(m)
                session._save_meta()
                _sessions[session.id] = session
                session_ids.append(session.id)
                imported_count += 1
        return {"ok": True, "imported": imported_count, "session_ids": session_ids}

    # Simple format: [{role, content}]
    session = Session()
    session.name = title
    for m in messages_data:
        role = m.get("role", "user")
        content = m.get("content", "")
        if not content:
            continue
        msg = {
            "role": role if role in ("user", "assistant") else "user",
            "content": content,
            "content_full": content if role == "assistant" else None,
            "timestamp": m.get("timestamp", datetime.now().isoformat()),
            "index": len(session.messages),
        }
        session._append_message(msg)
    session._save_meta()
    _sessions[session.id] = session
    return {"ok": True, "session_id": session.id, "name": title, "messages": len(session.messages)}


@app.post("/api/import/chatgpt")
async def import_chatgpt(request: Request):
    """Import a ChatGPT export JSON via request body (small files)."""
    user = get_user_from_token(request.cookies.get("auth_token", ""))
    if not user:
        raise HTTPException(401, "Not authenticated")
    body = await request.json()
    return _do_chatgpt_import(body)


@app.post("/api/import/chatgpt-file")
async def import_chatgpt_file(request: Request, file: UploadFile = File(...)):
    """Import a ChatGPT/Claude/generic export via multipart file upload. Handles huge files (100MB+)."""
    user = get_user_from_token(request.cookies.get("auth_token", ""))
    if not user:
        raise HTTPException(401, "Not authenticated")
    try:
        content = await file.read()
    except Exception as e:
        return {"ok": False, "error": f"Could not read uploaded file: {e}"}
    try:
        # utf-8-sig strips BOM automatically
        text = content.decode("utf-8-sig", errors="replace")
    except Exception as e:
        return {"ok": False, "error": f"Could not decode file as UTF-8: {e}"}
    data = None
    parse_error = None
    # Try standard JSON first
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        parse_error = f"{e.msg} at line {e.lineno} column {e.colno} (char {e.pos})"
        # Fallback: JSONL (one JSON object per line)
        try:
            lines = [ln for ln in text.splitlines() if ln.strip()]
            if lines:
                data = [json.loads(ln) for ln in lines]
        except Exception:
            data = None
    if data is None:
        return {"ok": False, "error": f"Not valid JSON or JSONL. Parser said: {parse_error}"}
    try:
        return _do_chatgpt_import(data)
    except Exception as e:
        import traceback
        return {"ok": False, "error": f"Import failed: {e}", "trace": traceback.format_exc()[-500:]}


@app.post("/api/import/soul")
async def import_soul(request: Request):
    """Import a .soul state file — restores persona, W-OS, memories."""
    user = get_user_from_token(request.cookies.get("auth_token", ""))
    if not user:
        raise HTTPException(401, "Not authenticated")
    body = await request.json()
    session_id = body.get("target_session")
    if session_id:
        session = get_session(session_id)
    else:
        session = Session()
        session.name = body.get("metadata", {}).get("session_name", "Imported Soul")
        _sessions[session.id] = session

    # Persona
    persona = body.get("persona", {})
    if persona.get("base_prompt"):
        session.base_prompt = persona["base_prompt"]
    if persona.get("pinned_context"):
        session.pinned_context = persona["pinned_context"]

    # W-OS
    wos_data = body.get("wos")
    if wos_data:
        _wos.import_state(wos_data)

    # Semantic Memory (merge, don't replace)
    sm = body.get("semantic_memory", {})
    for entry in sm.get("entries", []):
        if entry not in _semantic_memory.memories:
            _semantic_memory.memories.append(entry)
            _semantic_memory._save_entry(entry)

    # Conversation messages
    conv = body.get("conversation", {})
    for m in conv.get("recent_messages", []):
        msg = {
            "role": m.get("role", "user"),
            "content": m.get("content", ""),
            "content_full": m.get("content", "") if m.get("role") == "assistant" else None,
            "timestamp": m.get("timestamp", datetime.now().isoformat()),
            "index": len(session.messages),
        }
        session._append_message(msg)

    session._save_meta()
    return {"ok": True, "session_id": session.id}


@app.get("/api/sessions/{session_id}/soul")
async def export_soul(session_id: str):
    """Export session as a .soul file."""
    session = get_session(session_id)
    return {
        "metadata": {
            "version": "harness-v2-soul",
            "created": datetime.now().isoformat(),
            "session_id": session.id,
            "session_name": session.name,
        },
        "persona": {
            "base_prompt": session.base_prompt,
            "pinned_context": session.pinned_context,
        },
        "wos": _wos.export_state(),
        "semantic_memory": {
            "entry_count": len(_semantic_memory.memories),
            "entries": _semantic_memory.memories[-100:],
        },
        "conversation": {
            "message_count": len(session.messages),
            "recent_messages": [
                {"role": m["role"], "content": m["content"][:1000], "timestamp": m.get("timestamp", "")}
                for m in session.messages[-50:]
            ],
        },
    }


# ── Semantic Memory ──────────────────────────────────────────
@app.get("/api/memory/stats")
async def memory_stats():
    return {
        "total_entries": len(_semantic_memory.memories),
        "sessions_represented": len(set(m.get("session_id", "") for m in _semantic_memory.memories)),
    }


@app.post("/api/memory/recall")
async def memory_recall(body: dict):
    text = body.get("text", "")
    top_k = body.get("top_k", 5)
    exclude = body.get("exclude_session", None)
    results = _semantic_memory.recall(text, top_k=top_k, exclude_session=exclude)
    return {"results": results}


@app.post("/api/memory/store")
async def memory_store(body: dict):
    text = body.get("text", "")
    session_id = body.get("session_id", "manual")
    role = body.get("role", "user")
    _semantic_memory.store(text, session_id, role)
    # Notify the active session (if any) so HUD/toast can surface it
    try:
        if session_id and session_id != "manual":
            await _broadcast_to_session(session_id, {
                "type": "memory_saved",
                "preview": text[:120],
                "total": len(_semantic_memory.memories),
            })
    except Exception:
        pass
    return {"ok": True, "total_entries": len(_semantic_memory.memories)}

@app.post("/api/memory/delete")
async def memory_delete(body: dict):
    """Delete a memory entry by timestamp."""
    timestamp = body.get("timestamp", 0)
    if not timestamp:
        return {"ok": False, "error": "No timestamp provided"}
    before = len(_semantic_memory.memories)
    _semantic_memory.memories = [m for m in _semantic_memory.memories if abs(m.get("timestamp", 0) - timestamp) > 0.001]
    after = len(_semantic_memory.memories)
    if before != after:
        _semantic_memory._rewrite_all()
    return {"ok": True, "deleted": before - after, "total_entries": after}


# ── W-OS Endpoints ─────────────────────────────────────────
@app.get("/api/wos/status")
async def wos_status():
    return _wos.get_status()


@app.post("/api/wos/toggle")
async def wos_toggle():
    _wos.is_active = not _wos.is_active
    _wos._save()
    return {"active": _wos.is_active}


@app.get("/api/wos/glyphs")
async def wos_glyphs():
    return {"glyphs": _wos.memory, "ledger": _wos.ledger[-20:]}


@app.post("/api/wos/clear")
async def wos_clear():
    _wos.memory = []
    _wos.ledger = []
    _wos._save()
    return {"ok": True}


# ── Binary States Endpoints ────────────────────────────────
@app.get("/api/binary-states")
async def get_binary_states():
    return {"states": BINARY_STATES}


@app.get("/api/sessions/{session_id}/binary-states")
async def get_session_binary_states(session_id: str):
    session = get_session(session_id)
    return {"active": getattr(session, '_active_binary_states', [])}


@app.post("/api/sessions/{session_id}/binary-states")
async def set_session_binary_states(session_id: str, body: dict):
    session = get_session(session_id)
    session._active_binary_states = body.get("states", [])
    return {"ok": True, "count": len(session._active_binary_states)}


# ── Soul State Endpoints (legacy — used by existing frontend) ─
@app.post("/api/sessions/{session_id}/soul")
async def import_soul_to_session(session_id: str, body: dict):
    session = get_session(session_id)
    SoulStateManager.import_soul(session, body)
    return {"ok": True}


# ── Context Budget Endpoint ────────────────────────────────
@app.get("/api/sessions/{session_id}/budget")
async def get_budget(session_id: str):
    session = get_session(session_id)
    budget = getattr(session, '_last_budget', {})
    budget['last_recalled'] = getattr(session, '_last_recalled', [])
    budget['max_context_tokens'] = session.max_context_tokens
    budget['memory_config'] = session.memory_config
    return budget

@app.post("/api/sessions/{session_id}/context-limit")
async def set_context_limit(session_id: str, body: dict):
    """Set per-session max context tokens. Body: {"max_context_tokens": 50000}"""
    session = get_session(session_id)
    new_limit = body.get("max_context_tokens")
    if not isinstance(new_limit, (int, float)) or new_limit < 1000 or new_limit > 900000:
        raise HTTPException(400, "max_context_tokens must be between 1000 and 900000")
    session.max_context_tokens = int(new_limit)
    session._rebuild_context_window()
    session._save_meta()
    print(f"[SESSION {session_id[:8]}] Context limit set to {session.max_context_tokens:,} tokens", flush=True)
    return {"ok": True, "max_context_tokens": session.max_context_tokens}

# ── Memory Config Endpoints ────────────────────────────────
@app.get("/api/sessions/{session_id}/memory-config")
async def get_memory_config(session_id: str):
    session = get_session(session_id)
    return session.memory_config

@app.post("/api/sessions/{session_id}/memory-config")
async def set_memory_config(session_id: str, body: dict):
    """Update memory config. Body: partial dict merged into existing config."""
    session = get_session(session_id)
    allowed_keys = {"semantic_enabled", "semantic_threshold", "semantic_top_k", "wos_enabled", "wos_max_glyphs"}
    for k, v in body.items():
        if k in allowed_keys:
            # Validate types and ranges
            if k == "semantic_enabled" or k == "wos_enabled":
                session.memory_config[k] = bool(v)
            elif k == "semantic_threshold":
                session.memory_config[k] = max(0.1, min(0.99, float(v)))
            elif k == "semantic_top_k":
                session.memory_config[k] = max(1, min(20, int(v)))
            elif k == "wos_max_glyphs":
                session.memory_config[k] = max(1, min(100, int(v)))
    session._save_meta()
    print(f"[SESSION {session_id[:8]}] Memory config updated: {session.memory_config}", flush=True)
    return {"ok": True, "memory_config": session.memory_config}

# ── Protected Process Registry API ─────────────────────────────
@app.get("/api/processes")
async def list_processes(request: Request):
    """List all registered protected processes."""
    user = get_user_from_token(request.cookies.get("auth_token", ""))
    if not user or user.get("username", "").lower() not in ADMIN_USERNAMES:
        raise HTTPException(403, "Admin access required")
    return {"processes": _load_process_registry()}

@app.post("/api/processes")
async def register_process(request: Request):
    """Register a process as protected."""
    user = get_user_from_token(request.cookies.get("auth_token", ""))
    if not user or user.get("username", "").lower() not in ADMIN_USERNAMES:
        raise HTTPException(403, "Admin access required")
    body = await request.json()
    pid = body.get("pid")
    description = body.get("description", "unknown process")
    owner = body.get("owner", user.get("username", "unknown"))
    category = body.get("category", "general")
    locked = body.get("locked", False)
    if pid:
        _register_process(int(pid), description, owner, category, locked=locked)
    return {"ok": True, "processes": _load_process_registry()}

@app.delete("/api/processes/{pid}")
async def deregister_process(request: Request, pid: int):
    """Remove a process from the protected registry."""
    user = get_user_from_token(request.cookies.get("auth_token", ""))
    if not user or user.get("username", "").lower() not in ADMIN_USERNAMES:
        raise HTTPException(403, "Admin access required")
    _deregister_process(pid)
    return {"ok": True, "processes": _load_process_registry()}

@app.post("/api/processes/{pid}/lock")
async def lock_process(request: Request, pid: int):
    """Lock a process — prevents any instance from killing it."""
    user = get_user_from_token(request.cookies.get("auth_token", ""))
    if not user or user.get("username", "").lower() not in ADMIN_USERNAMES:
        raise HTTPException(403, "Admin access required")
    entries = _load_process_registry()
    for e in entries:
        if e.get("pid") == pid:
            e["locked"] = True
            PROCESS_REGISTRY_PATH.write_text(json.dumps(entries, indent=2))
            return {"ok": True, "message": f"PID {pid} locked"}
    raise HTTPException(404, f"PID {pid} not in registry")

# ── Instance Notes API ───────────────────────────────────────
@app.get("/api/instance-notes")
async def list_instance_notes(request: Request):
    """List all active instance notes."""
    user = get_user_from_token(request.cookies.get("auth_token", ""))
    if not user or user.get("username", "").lower() not in ADMIN_USERNAMES:
        raise HTTPException(403, "Admin access required")
    return {"notes": _load_instance_notes()}

@app.post("/api/instance-notes")
async def add_instance_note(request: Request):
    """Add an instance note — tells other sessions what this one is doing."""
    user = get_user_from_token(request.cookies.get("auth_token", ""))
    if not user or user.get("username", "").lower() not in ADMIN_USERNAMES:
        raise HTTPException(403, "Admin access required")
    body = await request.json()
    session_id = body.get("session_id", "unknown")
    note = body.get("note", "")
    category = body.get("category", "background_task")
    if note:
        _add_instance_note(session_id, note, category)
    return {"ok": True, "notes": _load_instance_notes()}

@app.delete("/api/instance-notes/{session_id}")
async def clear_session_notes(request: Request, session_id: str):
    """Clear all notes from a specific session."""
    user = get_user_from_token(request.cookies.get("auth_token", ""))
    if not user or user.get("username", "").lower() not in ADMIN_USERNAMES:
        raise HTTPException(403, "Admin access required")
    _clear_instance_notes(session_id)
    return {"ok": True, "notes": _load_instance_notes()}

# ── Background Jobs Endpoints ────────────────────────────────
@app.post("/api/jobs/launch")
async def launch_job(body: dict, request: Request):
    """Launch a background job script. Body: {"script": "gravity_scan.py", "args": []}"""
    user = get_user_from_token(request.cookies.get("auth_token", ""))
    if not user or user.get("username", "").lower() not in ADMIN_USERNAMES:
        raise HTTPException(403, "Admin access required")
    script = body.get("script", "")
    args = body.get("args", [])
    script_path = Path(__file__).parent / script
    if not script_path.exists():
        raise HTTPException(404, f"Script not found: {script}")
    # Launch as background process
    cmd = [sys.executable, str(script_path)] + args
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    # Auto-register launched processes as protected
    _register_process(proc.pid, f"Background job: {script} {' '.join(args)}", owner="harness", category="job", locked=True)
    return {"ok": True, "pid": proc.pid, "script": script}

@app.get("/api/jobs/status")
async def jobs_status():
    """Check status of all jobs in data/jobs/."""
    jobs = []
    for f in JOBS_DIR.glob("*.result.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            jobs.append({"file": f.name, "status": data.get("status"), "job_type": data.get("job_type"), "runtime": data.get("runtime_seconds"), "findings": len(data.get("key_findings", []))})
        except Exception:
            jobs.append({"file": f.name, "status": "error"})
    for f in JOBS_DIR.glob("*.progress.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            jobs.append({"file": f.name, "status": "running", "phase": data.get("phase"), "message": data.get("message")})
        except Exception:
            pass
    return {"jobs": jobs}

@app.get("/api/sessions/{session_id}/last-recalled")
async def get_last_recalled(session_id: str):
    session = get_session(session_id)
    return {"recalled": getattr(session, '_last_recalled', [])}

@app.get("/api/memory/browse")
async def memory_browse(page: int = 0, size: int = 50, search: str = ""):
    """Browse all semantic memory entries with optional search."""
    entries = _semantic_memory.memories
    if search:
        search_lower = search.lower()
        entries = [m for m in entries if search_lower in m.get("text", "").lower()]
    total = len(entries)
    # Return newest first
    entries = list(reversed(entries))
    start = page * size
    page_entries = entries[start:start + size]
    return {"entries": page_entries, "total": total, "page": page, "page_size": size}


# ── File Upload / Download ─────────────────────────────────
UPLOADS_DIR = DATA_DIR / "uploads"
UPLOADS_DIR.mkdir(exist_ok=True)

@app.post("/api/sessions/{session_id}/upload")
async def upload_file(session_id: str, file: UploadFile = File(...), target: str = Form(default="")):
    """Upload a file. Stored in session uploads dir by default, or a custom target path."""
    session = get_session(session_id)
    session_uploads = session.dir / "uploads"
    session_uploads.mkdir(exist_ok=True)

    # Sanitize filename
    safe_name = Path(file.filename).name  # strip any directory traversal
    if not safe_name:
        raise HTTPException(400, "Invalid filename")

    content = await file.read()

    # Save to session uploads
    dest = session_uploads / safe_name
    dest.write_bytes(content)

    # Generate thumbnail for images
    _IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp'}
    if dest.suffix.lower() in _IMAGE_EXTS:
        try:
            from PIL import Image
            thumbs_dir = session_uploads / ".thumbs"
            thumbs_dir.mkdir(exist_ok=True)
            img = Image.open(dest)
            img.thumbnail((400, 400))
            if img.mode in ('RGBA', 'P'):
                img = img.convert('RGB')
            thumb_path = thumbs_dir / f"{dest.stem}.jpg"
            img.save(thumb_path, "JPEG", quality=70)
        except Exception:
            pass  # thumbnail generation failed, serve original

    # If target path specified, also save there (relative to harness dir)
    target_saved = None
    if target.strip():
        target_path = Path(target.strip())
        # Only allow relative paths within the harness directory
        if target_path.is_absolute():
            raise HTTPException(400, "Target must be a relative path")
        # Reject path traversal attempts
        if ".." in target_path.parts:
            raise HTTPException(400, "Path traversal not allowed")
        full_target = Path(__file__).parent / target_path
        # Verify resolved path is still within harness directory
        if not str(full_target.resolve()).startswith(str(Path(__file__).parent.resolve())):
            raise HTTPException(400, "Path traversal not allowed")
        full_target.parent.mkdir(parents=True, exist_ok=True)
        full_target.write_bytes(content)
        target_saved = str(target_path)

    return {
        "ok": True,
        "filename": safe_name,
        "size": len(content),
        "session_path": f"data/sessions/{session_id}/uploads/{safe_name}",
        "target_path": target_saved,
    }

@app.get("/api/sessions/{session_id}/uploads")
async def list_uploads(session_id: str):
    """List files uploaded to this session."""
    session = get_session(session_id)
    session_uploads = session.dir / "uploads"
    if not session_uploads.exists():
        return {"files": []}
    files = []
    for f in sorted(session_uploads.iterdir()):
        if f.is_file():
            files.append({
                "name": f.name,
                "size": f.stat().st_size,
                "modified": datetime.fromtimestamp(f.stat().st_mtime).isoformat(),
            })
    return {"files": files}

@app.get("/api/sessions/{session_id}/uploads/{filename}")
async def download_upload(session_id: str, filename: str):
    """Download an uploaded file."""
    session = get_session(session_id)
    fpath = session.dir / "uploads" / Path(filename).name
    if not fpath.exists():
        raise HTTPException(404, "File not found")
    return FileResponse(fpath, filename=filename)

@app.delete("/api/sessions/{session_id}/uploads/{filename}")
async def delete_upload(session_id: str, filename: str, request: Request, auth_token: str = Cookie(None)):
    """Delete an uploaded file."""
    session = get_session(session_id)
    safe_name = Path(filename).name
    fpath = session.dir / "uploads" / safe_name
    if not fpath.exists():
        raise HTTPException(404, "File not found")
    fpath.unlink()
    # Also delete thumbnail if it exists
    thumb_path = session.dir / "uploads" / ".thumbs" / f"{Path(safe_name).stem}.jpg"
    if thumb_path.exists():
        thumb_path.unlink()
    return {"status": "ok", "deleted": safe_name}

@app.get("/api/sessions/{session_id}/uploads/{filename}/thumb")
async def download_thumb(session_id: str, filename: str):
    """Download a thumbnail of an uploaded image."""
    session = get_session(session_id)
    stem = Path(filename).stem
    thumb_path = session.dir / "uploads" / ".thumbs" / f"{stem}.jpg"
    if thumb_path.exists():
        return FileResponse(thumb_path, media_type="image/jpeg")
    # Fall back to original if no thumbnail
    fpath = session.dir / "uploads" / Path(filename).name
    if not fpath.exists():
        raise HTTPException(404, "File not found")
    return FileResponse(fpath, filename=filename)

@app.get("/api/sessions/{session_id}/uploads/{filename}/text")
async def upload_text(session_id: str, filename: str):
    """Extract plain text from an uploaded file for TTS."""
    session = get_session(session_id)
    fpath = session.dir / "uploads" / Path(filename).name
    if not fpath.exists():
        raise HTTPException(404, "File not found")
    ext = fpath.suffix.lower()
    text = ""
    if ext == ".docx":
        try:
            from docx import Document
            doc = Document(str(fpath))
            text = "\n\n".join(p.text for p in doc.paragraphs if p.text.strip())
        except Exception as e:
            raise HTTPException(500, f"Could not extract text: {e}")
    elif ext == ".pdf":
        try:
            import subprocess
            result = subprocess.run(["pdftotext", str(fpath), "-"], capture_output=True, text=True, timeout=30)
            text = result.stdout
        except Exception:
            raise HTTPException(500, "Could not extract text from PDF")
    elif ext in (".txt", ".md", ".csv", ".json", ".py", ".js", ".html", ".css", ".yaml", ".yml", ".toml", ".xml"):
        try:
            text = fpath.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = fpath.read_text(encoding="latin-1")
    else:
        raise HTTPException(400, f"Text extraction not supported for {ext} files")
    return {"text": text, "filename": filename}


# ── Document endpoints ──────────────────────────────────────

@app.get("/api/sessions/{session_id}/documents")
async def api_list_documents(session_id: str):
    session = get_session(session_id)
    return {"documents": session.documents.list_all()}

@app.post("/api/sessions/{session_id}/documents")
async def api_create_document(session_id: str, body: dict):
    session = get_session(session_id)
    doc_id = session.documents.create(
        title=body.get("title", "Untitled"),
        content=body.get("content", ""),
    )
    await _broadcast_to_session(session_id, {
        "type": "documents_update",
        "documents": session.documents.list_all(),
    })
    return {"doc_id": doc_id, "meta": session.documents.get_meta(doc_id)}

@app.get("/api/sessions/{session_id}/documents/{doc_id}")
async def api_get_document(session_id: str, doc_id: str):
    session = get_session(session_id)
    content = session.documents.get(doc_id)
    meta = session.documents.get_meta(doc_id)
    if content is None:
        raise HTTPException(404, "Document not found")
    return {"content": content, "meta": meta}

@app.put("/api/sessions/{session_id}/documents/{doc_id}")
async def api_update_document(session_id: str, doc_id: str, body: dict):
    session = get_session(session_id)
    ok = session.documents.update(
        doc_id,
        content=body.get("content"),
        title=body.get("title"),
    )
    if not ok:
        raise HTTPException(404, "Document not found")
    await _broadcast_to_session(session_id, {
        "type": "documents_update",
        "documents": session.documents.list_all(),
    })
    # Also broadcast the content change so other viewers see it live
    await _broadcast_to_session(session_id, {
        "type": "doc_content_update",
        "doc_id": doc_id,
        "content": body.get("content", ""),
        "title": body.get("title", ""),
        "modified": session.documents.get_meta(doc_id).get("modified", ""),
    })
    return {"ok": True, "meta": session.documents.get_meta(doc_id)}

@app.delete("/api/sessions/{session_id}/documents/{doc_id}")
async def api_delete_document(session_id: str, doc_id: str):
    session = get_session(session_id)
    ok = session.documents.delete(doc_id)
    if not ok:
        raise HTTPException(404, "Document not found")
    await _broadcast_to_session(session_id, {
        "type": "documents_update",
        "documents": session.documents.list_all(),
    })
    return {"ok": True}


@app.post("/api/sessions/{session_id}/abort")
async def abort_generation(session_id: str, request: Request, auth_token: str = Cookie(None)):
    """Kill the active claude subprocess for a session. Only the generation owner can abort."""
    session = get_session(session_id)
    # Check who is requesting the abort — try Bearer token first, then cookie
    bearer = request.headers.get("authorization", "").replace("Bearer ", "")
    user = get_user_from_token(bearer) if bearer else (get_user_from_token(auth_token) if auth_token else None)
    requester = (user.get("display_name") or user.get("username")) if user else None
    gen_owner = getattr(session, '_generation_owner', None)
    # Allow any authenticated user to abort — owner check was blocking admins
    # if gen_owner and requester and requester != gen_owner:
    #     raise HTTPException(403, "Only the user who started generation can abort it")
    proc = getattr(session, '_active_process', None)
    target_pid = None
    if proc and proc.poll() is None:
        target_pid = proc.pid
    else:
        # Cross-server fallback: check PID file on disk (other server may own this generation)
        pid_path = session.dir / "_active_pid"
        if pid_path.exists():
            try:
                disk_pid = int(pid_path.read_text().strip())
                import os
                os.kill(disk_pid, 0)  # Check if process exists
                target_pid = disk_pid
            except (ValueError, ProcessLookupError, PermissionError):
                pid_path.unlink(missing_ok=True)
    if target_pid:
        import signal, os
        try:
            os.kill(target_pid, signal.SIGTERM)
            print(f"[ABORT] Sent SIGTERM to process {target_pid}", flush=True)
            # Give it a moment, then force kill if needed
            import time
            time.sleep(1)
            try:
                os.kill(target_pid, 0)  # Check if still alive
                os.kill(target_pid, signal.SIGKILL)
                print(f"[ABORT] Force-killed process {target_pid}", flush=True)
            except ProcessLookupError:
                pass  # Already dead
        except Exception as e:
            print(f"[ABORT] Error killing process: {e}", flush=True)
            raise HTTPException(500, f"Failed to abort: {e}")
        # Clean up PID file
        try:
            (session.dir / "_active_pid").unlink(missing_ok=True)
        except Exception:
            pass
        if proc:
            session._active_process = None
        _session_activity.pop(session_id, None)
        log_activity("generation_aborted", username=requester, session_id=session_id)
        # Broadcast abort to ALL connected clients so they unlock
        await _broadcast_to_session(session_id, {
            "type": "done",
            "aborted": True,
            "session_id": session_id,
        })
        return {"status": "aborted", "pid": target_pid}
    return {"status": "no_active_generation"}


@app.post("/api/sessions/{session_id}/send")
async def send_message_durable(session_id: str, request: Request, auth_token: str = Cookie(None)):
    """Durable message send via HTTP POST — message persists even if WebSocket is dead."""
    user = get_user_from_token(auth_token)
    if not user:
        raise HTTPException(401, "Unauthorized")
    username = user.get("username", "unknown")
    session = get_session(session_id)
    body = await request.json()
    user_text = body.get("content", "").strip()
    if not user_text:
        raise HTTPException(400, "Empty message")

    # Guard: reject if generation already in progress
    active_thread = getattr(session, '_active_thread', None)
    if active_thread and active_thread.is_alive():
        raise HTTPException(409, "Generation already in progress on this session")

    # Persist user message
    session.add_user_message(user_text, username=username)
    log_activity("message_sent", username=username, session_id=session_id, detail=user_text[:100])

    # Broadcast to all WebSocket viewers
    await _broadcast_to_session(session_id, {
        "type": "context_stats",
        "stats": session.get_context_stats(),
    })
    await _broadcast_to_session(session_id, {
        "type": "user_message",
        "content": user_text,
        "username": username,
    })

    # Start generation in background — streams via WebSocket to all clients
    async def _generate():
        try:
            await run_claude_async(session, user_text, username=username)
        except Exception as e:
            print(f"[ERROR] run_claude_async (durable send) failed: {e}", flush=True)
            import traceback; traceback.print_exc()
            await _broadcast_to_session(session_id, {"type": "error", "content": str(e)})
    asyncio.create_task(_generate())

    return {"ok": True, "persisted": True}


@app.post("/api/sessions/{session_id}/inject")
async def inject_message(session_id: str, request: Request, auth_token: str = Cookie(None)):
    """Inject a user message during active generation — saves to disk, shows in chat,
    auto-continues after current generation finishes. Does NOT interrupt flow."""
    user = get_user_from_token(auth_token)
    if not user:
        raise HTTPException(401, "Unauthorized")
    username = user.get("username", "unknown")
    session = get_session(session_id)
    body = await request.json()
    user_text = body.get("content", "").strip()
    if not user_text:
        raise HTTPException(400, "Empty message")

    # Save to disk immediately — survives any crash
    session.add_user_message(user_text, username=username)
    log_activity("message_injected", username=username, session_id=session_id, detail=user_text[:100])

    # Broadcast to all WS clients so it appears in chat
    await _broadcast_to_session(session_id, {
        "type": "user_message",
        "content": user_text,
        "username": username,
        "injected": True,
    })
    await _broadcast_to_session(session_id, {
        "type": "context_stats",
        "stats": session.get_context_stats(),
    })

    # Queue for auto-continue after current generation finishes
    if not hasattr(session, '_injected_messages'):
        session._injected_messages = []
    session._injected_messages.append({"text": user_text, "username": username})

    return {"ok": True, "injected": True}


@app.websocket("/ws/stt")
async def websocket_stt(websocket: WebSocket):
    """Standalone STT endpoint — accepts audio chunks, returns transcripts."""
    token = websocket.query_params.get("token", "")
    user = get_user_from_token(token)
    if not user:
        await websocket.close(code=4001, reason="Not authenticated")
        return
    await websocket.accept()
    try:
        while True:
            data = await websocket.receive()
            if "bytes" in data and data["bytes"]:
                audio_bytes = data["bytes"]
                loop = asyncio.get_event_loop()
                text = await loop.run_in_executor(_stt_executor, _transcribe_audio, audio_bytes)
                await websocket.send_json({"type": "transcript", "text": text or "", "empty": not bool(text)})
            elif "text" in data and data["text"]:
                msg = json.loads(data["text"])
                if msg.get("type") == "ping":
                    await websocket.send_json({"type": "pong"})
    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"[STT WS] Error: {e}", flush=True)


# Global Voice System — Persistent WebSocket for P2P + Group calls
# ══════════════════════════════════════════════════════════════

_voice_websockets = {}   # username -> WebSocket
_voice_calls = {}        # call_id -> {"type": "p2p"|"group", "participants": {username: state}, "created": ts}
_voice_user_call = {}    # username -> call_id (which call a user is in)

@app.websocket("/ws/voice")
async def websocket_voice(websocket: WebSocket):
    """Global voice signaling WS — persists across chat/room switches."""
    token = websocket.query_params.get("token") or websocket.cookies.get("auth_token", "")
    user = get_user_from_token(token)
    if not user:
        await websocket.close(code=4001, reason="Not authenticated")
        return

    await websocket.accept()
    username = user["username"]
    display_name = user["display_name"]

    # Replace any existing voice WS for this user (single-session)
    old_ws = _voice_websockets.get(username)
    if old_ws:
        try:
            await old_ws.close(code=4002, reason="Replaced by new connection")
        except:
            pass
    _voice_websockets[username] = websocket

    # Broadcast updated online-for-voice list
    await _voice_broadcast_presence()

    print(f"[VOICE WS] {username} connected, voice_websockets: {list(_voice_websockets.keys())}", flush=True)

    try:
        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type", "")
            if msg_type != "ping":
                print(f"[VOICE WS] {username} -> {msg_type}: {json.dumps({k:v for k,v in data.items() if k != 'payload'})}", flush=True)

            if msg_type == "call_user":
                # P2P direct call: caller -> callee
                target = data.get("target", "").lower()
                # Check if target is online (locally or on remote instance)
                target_is_remote = False
                if target not in _voice_websockets:
                    # Check remote presence
                    remote_online = []
                    for pdata in _voice_relay_presence.values():
                        remote_online.extend(pdata.get("users", []))
                    if target not in remote_online:
                        await websocket.send_json({"type": "call_error", "error": "User not online"})
                        continue
                    target_is_remote = True
                if target in _voice_user_call:
                    await websocket.send_json({"type": "call_error", "error": "User is already in a call"})
                    continue
                if username in _voice_user_call:
                    await websocket.send_json({"type": "call_error", "error": "You are already in a call"})
                    continue
                # Create P2P call
                call_id = str(uuid.uuid4())[:8]
                _voice_calls[call_id] = {
                    "type": "p2p",
                    "participants": {
                        username: {"audio": True, "video": data.get("video", False), "display_name": display_name},
                    },
                    "created": time.time(),
                }
                _voice_user_call[username] = call_id
                # Ring the target (local or remote)
                ring_msg = {
                    "type": "incoming_call",
                    "call_id": call_id,
                    "from": username,
                    "display_name": display_name,
                    "video": data.get("video", False),
                    "target": target,
                }
                if target_is_remote:
                    _voice_relay_send(target, ring_msg)
                    print(f"[VOICE RELAY] Call {call_id}: {username} -> {target} (remote)", flush=True)
                else:
                    try:
                        await _voice_websockets[target].send_json(ring_msg)
                        print(f"[VOICE WS] Sent incoming_call to {target} for call {call_id}", flush=True)
                    except:
                        print(f"[VOICE WS] Failed to send incoming_call to {target}", flush=True)
                        del _voice_calls[call_id]
                        del _voice_user_call[username]
                        await websocket.send_json({"type": "call_error", "error": "User disconnected"})
                        continue
                await websocket.send_json({"type": "call_ringing", "call_id": call_id, "target": target})

            elif msg_type == "call_accept":
                call_id = data.get("call_id", "")
                call = _voice_calls.get(call_id)
                if not call:
                    # Call might have been created on a remote instance — create local state
                    call = {
                        "type": "p2p",
                        "participants": {},
                        "created": time.time(),
                    }
                    _voice_calls[call_id] = call
                # Add callee to the call
                call["participants"][username] = {
                    "audio": True, "video": data.get("video", False), "display_name": display_name,
                }
                _voice_user_call[username] = call_id
                # Notify all participants that call is connected (local + remote)
                connected_msg = {
                    "type": "call_connected",
                    "call_id": call_id,
                    "call_type": call["type"],
                    "participants": call["participants"],
                    "joined": username,
                }
                for pname in call["participants"]:
                    ws = _voice_websockets.get(pname)
                    if ws:
                        try:
                            await ws.send_json(connected_msg)
                        except:
                            pass
                    else:
                        # Relay to remote participant
                        _voice_relay_send(pname, {**connected_msg, "target": pname})
                await _voice_broadcast_presence()

            elif msg_type == "call_reject":
                call_id = data.get("call_id", "")
                call = _voice_calls.get(call_id)
                if call:
                    for pname in call["participants"]:
                        ws = _voice_websockets.get(pname)
                        if ws:
                            try:
                                await ws.send_json({"type": "call_rejected", "call_id": call_id, "by": username})
                            except:
                                pass
                        else:
                            _voice_relay_send(pname, {"type": "call_rejected", "call_id": call_id, "by": username, "target": pname})
                        _voice_user_call.pop(pname, None)
                    del _voice_calls[call_id]
                    await _voice_broadcast_presence()

            elif msg_type == "call_hangup":
                call_id = _voice_user_call.get(username)
                if call_id and call_id in _voice_calls:
                    call = _voice_calls[call_id]
                    for pname in list(call["participants"]):
                        ws = _voice_websockets.get(pname)
                        if ws:
                            try:
                                await ws.send_json({"type": "call_ended", "call_id": call_id, "by": username})
                            except:
                                pass
                        else:
                            _voice_relay_send(pname, {"type": "call_ended", "call_id": call_id, "by": username, "target": pname})
                        _voice_user_call.pop(pname, None)
                    del _voice_calls[call_id]
                    await _voice_broadcast_presence()

            elif msg_type == "join_group_call":
                # Join or create a group call (room-based)
                room_id = data.get("room_id", "")
                call_id = f"room_{room_id}"
                if call_id not in _voice_calls:
                    _voice_calls[call_id] = {
                        "type": "group",
                        "room_id": room_id,
                        "participants": {},
                        "created": time.time(),
                    }
                call = _voice_calls[call_id]
                call["participants"][username] = {
                    "audio": data.get("audio", True),
                    "video": data.get("video", False),
                    "display_name": display_name,
                    "ptt_mode": data.get("ptt_mode", "always"),
                }
                _voice_user_call[username] = call_id
                connected_msg = {
                    "type": "call_connected",
                    "call_id": call_id,
                    "call_type": "group",
                    "room_id": room_id,
                    "participants": call["participants"],
                    "joined": username,
                }
                for pname in call["participants"]:
                    ws = _voice_websockets.get(pname)
                    if ws:
                        try:
                            await ws.send_json(connected_msg)
                        except:
                            pass
                    else:
                        _voice_relay_send(pname, {**connected_msg, "target": pname})
                await _voice_broadcast_presence()

            elif msg_type in ("offer", "answer", "ice"):
                # Relay WebRTC signaling to specific target (local or remote)
                target = data.get("target", "")
                relay_payload = {
                    "type": msg_type,
                    "from": username,
                    "display_name": display_name,
                    "payload": data.get("payload"),
                    "target": target,
                }
                ws = _voice_websockets.get(target)
                if ws:
                    try:
                        await ws.send_json(relay_payload)
                    except:
                        pass
                else:
                    # Target is on a remote instance — relay through bridge
                    _voice_relay_send(target, relay_payload)

            elif msg_type == "media_state":
                call_id = _voice_user_call.get(username)
                if call_id and call_id in _voice_calls:
                    call = _voice_calls[call_id]
                    if username in call["participants"]:
                        ps = call["participants"][username]
                        if "audio" in data: ps["audio"] = data["audio"]
                        if "video" in data: ps["video"] = data["video"]
                        if "screen" in data: ps["screen"] = data.get("screen", False)
                    media_msg = {
                        "type": "media_state",
                        "username": username,
                        **{k: data[k] for k in ("audio", "video", "screen") if k in data},
                    }
                    for pname in call["participants"]:
                        if pname != username:
                            ws = _voice_websockets.get(pname)
                            if ws:
                                try:
                                    await ws.send_json(media_msg)
                                except:
                                    pass
                            else:
                                _voice_relay_send(pname, {**media_msg, "target": pname})

            elif msg_type == "ptt":
                call_id = _voice_user_call.get(username)
                if call_id and call_id in _voice_calls:
                    ptt_msg = {
                        "type": "ptt",
                        "username": username,
                        "speaking": data.get("speaking", False),
                    }
                    for pname in _voice_calls[call_id]["participants"]:
                        if pname != username:
                            ws = _voice_websockets.get(pname)
                            if ws:
                                try:
                                    await ws.send_json(ptt_msg)
                                except:
                                    pass
                            else:
                                _voice_relay_send(pname, {**ptt_msg, "target": pname})

            elif msg_type == "admin_mute":
                target = data.get("target", "")
                mute_msg = {"type": "admin_mute", "by": username, "target": target}
                ws = _voice_websockets.get(target)
                if ws:
                    try:
                        await ws.send_json(mute_msg)
                    except:
                        pass
                else:
                    _voice_relay_send(target, mute_msg)

            elif msg_type == "file_offer":
                target = data.get("target", "")
                file_msg = {
                    "type": "file_offer",
                    "from": username,
                    "display_name": display_name,
                    "filename": data.get("filename"),
                    "size": data.get("size"),
                    "mime": data.get("mime"),
                    "target": target,
                }
                ws = _voice_websockets.get(target)
                if ws:
                    try:
                        await ws.send_json(file_msg)
                    except:
                        pass
                else:
                    _voice_relay_send(target, file_msg)

            elif msg_type == "file_accept":
                target = data.get("target", "")
                accept_msg = {"type": "file_accept", "from": username, "target": target}
                ws = _voice_websockets.get(target)
                if ws:
                    try:
                        await ws.send_json(accept_msg)
                    except:
                        pass
                else:
                    _voice_relay_send(target, accept_msg)

            elif msg_type == "ping":
                await websocket.send_json({"type": "pong"})

    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"[VOICE WS] Error for {username}: {e}", flush=True)
    finally:
        # Cleanup
        if _voice_websockets.get(username) is websocket:
            del _voice_websockets[username]
        # Leave any active call
        call_id = _voice_user_call.pop(username, None)
        if call_id and call_id in _voice_calls:
            call = _voice_calls[call_id]
            call["participants"].pop(username, None)
            if not call["participants"]:
                del _voice_calls[call_id]
            else:
                for pname in call["participants"]:
                    ws = _voice_websockets.get(pname)
                    if ws:
                        try:
                            await ws.send_json({
                                "type": "call_ended" if call["type"] == "p2p" else "peer_left",
                                "call_id": call_id,
                                "username": username,
                                "participants": call["participants"],
                            })
                        except:
                            pass
                if call["type"] == "p2p":
                    # P2P call ends when either party leaves
                    for pname in list(call["participants"]):
                        _voice_user_call.pop(pname, None)
                    del _voice_calls[call_id]
        await _voice_broadcast_presence()


async def _voice_broadcast_presence():
    """Broadcast who's online and who's in calls to all local voice WS clients."""
    online = list(_voice_websockets.keys())
    in_calls = dict(_voice_user_call)
    msg = {"type": "voice_presence", "online": online, "in_calls": in_calls}
    dead = []
    for uname, ws in _voice_websockets.items():
        try:
            await ws.send_json(msg)
        except:
            dead.append(uname)
    for d in dead:
        _voice_websockets.pop(d, None)


# --- Cross-instance voice relay stubs (disabled — local-only for now) ---
def _voice_relay_send(target_username: str, payload: dict):
    pass  # No-op: cross-instance relay disabled

def _voice_relay_broadcast_presence():
    pass  # No-op: cross-instance relay disabled


@app.websocket("/ws/{session_id}")
async def websocket_chat(websocket: WebSocket, session_id: str):
    # Extract auth BEFORE accepting — reject unauthenticated remote connections
    auth_token = websocket.query_params.get("token") or websocket.cookies.get("auth_token")
    ws_user = get_user_from_token(auth_token) if auth_token else None
    ws_username = None
    if ws_user:
        ws_username = ws_user.get("username")
    else:
        # Allow localhost connections without auth (local dev)
        client_host = websocket.client.host if websocket.client else ""
        if client_host not in ("127.0.0.1", "::1", "localhost", ""):
            await websocket.close(code=4003, reason="Authentication required")
            return
    await websocket.accept()
    _active_websockets.add(websocket)
    session = get_session(session_id)

    # Register this session for relay message injection
    _relay_inject_sessions[session_id] = session
    if session_id not in _relay_inject_queue:
        _relay_inject_queue[session_id] = []

    # Add this client to the session's viewer dict {ws: username}
    _session_clients.setdefault(session_id, {})

    # If there's an active generation, replay buffered chunks to catch this client up
    active_thread = getattr(session, '_active_thread', None)
    stream_buffer = getattr(session, '_stream_buffer', [])

    if active_thread and active_thread.is_alive():
        print(f"[WS] Client joined while generation active, replaying {len(stream_buffer)} buffered chunks...", flush=True)
        # Tell the client to clear any in-progress streaming DOM before replay
        try:
            await websocket.send_json({"type": "replay_start", "chunk_count": len(stream_buffer), "generation_owner": getattr(session, '_generation_owner', '')})
        except Exception:
            pass
        for chunk in list(stream_buffer):
            try:
                await websocket.send_json(chunk)
            except Exception:
                break
        # Now add to broadcast dict — future chunks arrive via broadcast
        _session_clients[session_id][websocket] = ws_username

    elif stream_buffer and getattr(session, '_stream_done', False):
        # Generation finished while ALL clients were disconnected.
        # Response is already saved in session.messages — REST API loaded it.
        # Just clear the buffer, no replay needed (avoids duplicates).
        print(f"[WS] Client connected after generation completed, clearing {len(stream_buffer)} buffered chunks (REST has the data)", flush=True)
        _session_clients[session_id][websocket] = ws_username
        try:
            await websocket.send_json({"type": "context_stats", "stats": session.get_context_stats()})
            await websocket.send_json({"type": "artifacts_update", "artifacts": session.artifacts.list_all()})
            # Tell client generation is done so it clears streaming state
            await websocket.send_json({"type": "done", "session_id": session_id})
        except Exception:
            pass
        session._stream_buffer = []
        session._stream_done = False
    else:
        _session_clients[session_id][websocket] = ws_username
        # No active generation, no buffered stream — tell frontend to clear any stale streaming state
        # (handles case where server restarted mid-generation and frontend still thinks it's streaming)
        try:
            await websocket.send_json({"type": "done", "session_id": session_id})
        except Exception:
            pass

    # Track online presence (session users, not just room users)
    if ws_username:
        _online_users[ws_username] = {"last_seen": time.time(), "session_id": session_id}

    # Log connection and broadcast presence update
    log_activity("session_join", username=ws_username, session_id=session_id)
    await _broadcast_to_session(session_id, {
        "type": "viewers_update",
        "viewers": _session_viewers(session_id),
    })

    # Send active wakeup timer state if one exists for this session
    timer_data = _wakeup_timers.get(session_id)
    if timer_data and timer_data.get("fires_at", 0) > time.time():
        try:
            await websocket.send_json({
                "type": "wakeup_scheduled",
                "delay": timer_data["delay"],
                "reason": timer_data.get("reason", ""),
                "fires_at": timer_data["fires_at"],
                "prompt": timer_data.get("prompt", ""),
            })
        except Exception:
            pass

    try:
        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type", "message")

            # Update presence on any activity
            if ws_username:
                _online_users[ws_username] = {"last_seen": time.time(), "session_id": session_id}

            if msg_type in ("ping", "pong"):
                if msg_type == "ping":
                    try: await websocket.send_json({"type": "pong"})
                    except Exception: pass
                continue

            if msg_type == "message":
                user_text = data.get("content", "")
                if not user_text.strip():
                    continue

                # Guard: reject if generation already in progress on this session
                active_thread = getattr(session, '_active_thread', None)
                if active_thread and active_thread.is_alive():
                    await websocket.send_json({"type": "error", "content": "Generation already in progress on this session"})
                    continue

                # Check if message includes a username override (e.g. from UI)
                msg_username = data.get("username") or ws_username

                # Add user message with identity
                session.add_user_message(user_text, username=msg_username)
                log_activity("message_sent", username=msg_username, session_id=session_id, detail=user_text[:100])

                # Broadcast context stats to all viewers
                await _broadcast_to_session(session_id, {
                    "type": "context_stats",
                    "stats": session.get_context_stats(),
                })

                # Broadcast the user message to OTHER viewers so they see it live
                await _broadcast_to_session(session_id, {
                    "type": "user_message",
                    "content": user_text,
                    "username": msg_username,
                }, exclude=websocket)

                # Stream response (broadcasts to all clients)
                try:
                    await run_claude_async(session, user_text, username=msg_username)
                except Exception as e:
                    print(f"[ERROR] run_claude_async failed: {e}", flush=True)
                    import traceback; traceback.print_exc()
                    await _broadcast_to_session(session_id, {"type": "error", "content": str(e)})

            elif msg_type == "get_artifact":
                art_id = data.get("artifact_id", "")
                content = session.artifacts.get(art_id)
                meta = session.artifacts.get_meta(art_id)
                await websocket.send_json({
                    "type": "artifact",
                    "artifact_id": art_id,
                    "content": content,
                    "meta": meta,
                })

    except WebSocketDisconnect:
        _active_websockets.discard(websocket)
        _session_clients.get(session_id, {}).pop(websocket, None)
        # Only remove from online if user has no other active session websockets
        still_connected = any(
            uname == ws_username
            for sid_clients in _session_clients.values()
            for ws, uname in sid_clients.items()
        )
        if not still_connected:
            _online_users.pop(ws_username, None)
        log_activity("session_leave", username=ws_username, session_id=session_id)
        await _broadcast_to_session(session_id, {
            "type": "viewers_update",
            "viewers": _session_viewers(session_id),
        })
    except Exception as e:
        _active_websockets.discard(websocket)
        _session_clients.get(session_id, {}).pop(websocket, None)
        still_connected = any(
            uname == ws_username
            for sid_clients in _session_clients.values()
            for ws, uname in sid_clients.items()
        )
        if not still_connected:
            _online_users.pop(ws_username, None)
        log_activity("session_leave", username=ws_username, session_id=session_id, detail=str(e))
        await _broadcast_to_session(session_id, {
            "type": "viewers_update",
            "viewers": _session_viewers(session_id),
        })
        try:
            await websocket.send_json({"type": "error", "content": str(e)})
        except:
            pass


# Rooms WebSocket + multi-user messenger removed — harness is single-user / direct-chat only.
# ══════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════
# AI Query API (for local apps that want programmatic access)
# ══════════════════════════════════════════════════════════════

@app.post("/api/ai/query")
async def ai_query(request: Request):
    """Run a one-shot claude -p query. Body: {prompt, system_prompt?}"""
    user = get_user_from_token(request.cookies.get("auth_token", ""))
    if not user:
        raise HTTPException(403, "Authentication required")
    data = await request.json()
    prompt = data.get("prompt", "").strip()
    system_prompt = data.get("system_prompt", "")

    if not prompt:
        return JSONResponse({"error": "prompt is required"}, status_code=400)

    full_prompt = prompt
    if system_prompt:
        full_prompt = f"System instructions: {system_prompt}\n\n{prompt}"

    try:
        cmd = [
            "claude", "-p", full_prompt,
            "--output-format", "text",
            "--no-session-persistence",
        ]
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=120,
            encoding="utf-8", errors="replace"
        )
        response = result.stdout.strip() if result.stdout else "No response generated."
        if result.returncode != 0 and not response:
            response = f"Error: {result.stderr.strip()}"
        return {"response": response}
    except subprocess.TimeoutExpired:
        return JSONResponse({"error": "Query timed out"}, status_code=504)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ══════════════════════════════════════════════════════════════
# Relay API
# ══════════════════════════════════════════════════════════════

# Relay clients — env-driven. Empty env vars = relay disabled (all methods no-op safely).
#   RELAY_URL        = the relay server base URL (shared with RelayClient)
#   RELAY_KEY_AI     = this instance's AI-side identity key (e.g. dev_claude)
#   RELAY_KEY_HUMAN  = this instance's human-side identity key (e.g. obi)
_relay = RelayClient(key=os.environ.get("RELAY_KEY_AI", ""))
_relay_obi = RelayClient(key=os.environ.get("RELAY_KEY_HUMAN", ""))

@app.get("/api/relay/status")
async def relay_status():
    return _relay.status()

@app.get("/api/relay/channels")
async def relay_channels():
    return _relay.channels()

@app.get("/api/relay/history")
async def relay_history(channel: str = None, page: int = 1, per_page: int = 50, search: str = None):
    return _relay.history(channel=channel, page=page, per_page=per_page, search=search)

@app.post("/api/relay/send")
async def relay_send(body: dict, auth_token: str = Cookie(None)):
    text = body.get("text", "")
    channel = body.get("channel", "general")
    to = body.get("to", "*")
    sender = body.get("sender", "anonymous")
    if not text.strip():
        raise HTTPException(400, "Empty message")
    # Use obi key for human messages, dev_claude key for AI messages
    is_ai = sender.endswith("_claude")
    client = _relay_obi if (not is_ai and _relay_obi.key) else _relay
    # Prepend sender name to text so relay messages show who sent them
    tagged_text = f"[{sender}] {text}" if sender != "anonymous" else text
    return client.send(tagged_text, channel=channel, to=to)

@app.get("/api/relay/debug")
async def relay_debug():
    """Debug: check relay push state."""
    return {
        "active_websockets": len(_active_websockets),
        "relay_last_ts": _relay_last_ts,
        "inject_sessions": list(_relay_inject_sessions.keys()),
        "session_clients": {sid: len(clients) for sid, clients in _session_clients.items()},
    }

@app.get("/api/relay/recv")
async def relay_recv(since: float = 0, channel: str = None, limit: int = 100):
    result = _relay.recv(since=since, channel=channel, limit=limit)
    # Fix sender identity from [sender] prefix tags
    msgs = result.get('messages', result) if isinstance(result, dict) else result if isinstance(result, list) else []
    for msg in msgs:
        _fix_relay_sender(msg)
    # Always return {messages: [...]} so frontend can rely on data.messages
    if isinstance(result, list):
        return {"messages": result}
    return result

@app.get("/api/relay/poll")
async def relay_poll(since: float = 0, limit: int = 50):
    """Frontend polling endpoint — uses history (not recv) so push loop doesn't eat messages."""
    result = _relay.history(page=1, per_page=limit)
    all_msgs = result.get("messages", [])
    # Filter by timestamp and fix sender
    msgs = [m for m in all_msgs if m.get("ts", 0) > since]
    for msg in msgs:
        _fix_relay_sender(msg)
    msgs.reverse()  # oldest first
    return {"messages": msgs}

@app.get("/api/relay/mailbox")
async def relay_mailbox(query: str = None):
    if query:
        return {"messages": mailbox_search(query)}
    total, unread = mailbox_count()
    return {"total": total, "unread": unread}

@app.get("/api/relay/presence")
async def relay_presence():
    """Check which senders have been active recently (last 10 minutes)."""
    import time as _time
    try:
        hist = _relay.history(page=1, per_page=50)
        msgs = hist.get("messages", [])
        now = _time.time()
        presence = {}
        for m in msgs:
            sender = m.get("sender", "")
            ts = m.get("ts", 0)
            if sender and sender not in presence:
                age = now - ts
                presence[sender] = {
                    "sender": sender,
                    "last_seen": ts,
                    "last_seen_iso": m.get("ts_iso", ""),
                    "online": age < 600,  # 10 minutes
                    "age_seconds": int(age),
                }
        return {"presence": list(presence.values())}
    except Exception as e:
        return {"presence": [], "error": str(e)}

@app.get("/api/relay/mailbox/unread")
async def relay_mailbox_unread():
    msgs = mailbox_unread()
    return {"messages": msgs, "count": len(msgs)}

@app.post("/api/relay/mailbox/mark-read")
async def relay_mark_read(body: dict):
    ids = body.get("ids", [])
    mailbox_mark_read(ids)
    return {"ok": True}


# REMOVED: /api/source/{filename} — exposed full source code without auth (security fix 2026-04-04)

# Mount static files
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ══════════════════════════════════════════════════════════════
# Tool Call Extraction
# ══════════════════════════════════════════════════════════════

def _extract_tool_calls(text: str) -> list[dict]:
    """
    Extract ```tool_call JSON blocks from agent response text.
    Returns list of parsed tool call dicts.
    """
    calls = []
    # Match ```tool_call ... ``` blocks
    pattern = r'```tool_call\s*\n(.*?)\n```'
    matches = re.findall(pattern, text, re.DOTALL)
    for match in matches:
        try:
            parsed = json.loads(match.strip())
            if isinstance(parsed, dict) and "tool" in parsed:
                calls.append(parsed)
            elif isinstance(parsed, list):
                for item in parsed:
                    if isinstance(item, dict) and "tool" in item:
                        calls.append(item)
        except json.JSONDecodeError:
            continue
    return calls


# ══════════════════════════════════════════════════════════════
# Autonomous Agent Scheduler
# ══════════════════════════════════════════════════════════════

_agent_config_path = AGENT_DIR / "config.json"
_agent_instructions_path = AGENT_DIR / "instructions.md"
_agent_log_path = AGENT_DIR / "log.jsonl"

_agent_running = False
_agent_status = {"state": "idle", "last_run": None, "next_check": None}

_AGENT_DEFAULT_CONFIG = {
    "enabled": False,
    "session_id": None,
    "schedules": [
        {
            "name": "morning_check",
            "type": "daily",
            "time": "06:00",
            "enabled": True,
            "instructions": "Run morning system checks. Review server health, check for errors in recent logs, and report status.",
        },
        {
            "name": "evening_report",
            "type": "daily",
            "time": "18:00",
            "enabled": False,
            "instructions": "Compile daily summary. Review activity since morning check.",
        },
    ],
    "standing_orders": (
        "You are the autonomous agent for the TOE harness system. "
        "On each wake-up, read your instructions, execute them, and log your findings concisely. "
        "You have full tool access (Bash, Read, Edit, Write, Grep, Glob). "
        "You can modify your own schedule by editing your config file at the path given below. "
        "You can toggle yourself off if needed by setting 'enabled' to false in your config. "
        "Be efficient — complete your tasks and report findings clearly."
    ),
    "base_prompt": (
        "You are an autonomous agent running on a scheduled wake-up cycle. "
        "You are NOT in a conversation with a human — you are executing standing orders. "
        "Complete your instructions efficiently, report findings, and return to sleep. "
        "Keep responses concise and action-oriented."
    ),
    "last_runs": {},
}


def _load_agent_config() -> dict:
    """Load agent config from disk, writing defaults if missing."""
    if _agent_config_path.exists():
        try:
            return json.loads(_agent_config_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    # First run — write defaults
    config = _AGENT_DEFAULT_CONFIG.copy()
    _save_agent_config(config)
    return config


def _save_agent_config(config: dict):
    """Save agent config to disk."""
    _agent_config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")


def _load_agent_instructions() -> str:
    """Load the human-editable instructions file."""
    if _agent_instructions_path.exists():
        try:
            return _agent_instructions_path.read_text(encoding="utf-8")
        except Exception:
            pass
    return ""


def _log_agent_run(schedule_name: str, trigger: str, response: str, duration: float, error: str = None):
    """Append an entry to the agent execution log."""
    entry = {
        "timestamp": datetime.now().isoformat(),
        "schedule": schedule_name,
        "trigger": trigger,
        "response_length": len(response) if response else 0,
        "response_preview": (response[:500] + "...") if response and len(response) > 500 else response,
        "duration_seconds": round(duration, 1),
        "error": error,
    }
    with open(_agent_log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def _check_schedule_due(schedule: dict, last_runs: dict) -> bool:
    """Check if a schedule entry is due to run."""
    if not schedule.get("enabled", True):
        return False

    name = schedule["name"]
    stype = schedule.get("type", "interval")
    now = datetime.now()
    last_run_str = last_runs.get(name)
    last_run = datetime.fromisoformat(last_run_str) if last_run_str else None

    if stype == "daily":
        target_time = schedule.get("time", "06:00")
        parts = target_time.split(":")
        hour, minute = int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
        # Due if we're past target time and haven't run today
        if now.hour > hour or (now.hour == hour and now.minute >= minute):
            if last_run is None or last_run.date() < now.date():
                return True

    elif stype == "interval":
        interval = schedule.get("interval_minutes", 60)
        if last_run is None:
            return True
        elapsed = (now - last_run).total_seconds() / 60
        if elapsed >= interval:
            return True

    return False


async def _execute_agent_task(schedule: dict, config: dict, trigger: str = "scheduled"):
    """Execute an agent task — load session, build prompt, run Claude, log result."""
    global _agent_running, _agent_status

    if _agent_running:
        print("[AGENT] Already running, skipping", flush=True)
        return

    _agent_running = True
    _agent_status = {"state": "running", "schedule": schedule["name"], "started": time.time()}
    start_time = time.time()

    try:
        # Get or create the agent's dedicated session
        session_id = config.get("session_id")
        session = get_session(session_id)

        # First run — save the new session ID to config
        if not config.get("session_id"):
            config["session_id"] = session.id
            session.name = "Agent — Autonomous Scheduler"
            _save_agent_config(config)

        # Set agent base prompt if session doesn't have one yet
        if config.get("base_prompt") and not session.base_prompt:
            session.base_prompt = config["base_prompt"]
            session._save_meta()

        # Build the wake-up prompt
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Slim trigger — standing orders, runbook, tools list are in the
        # session's base_prompt (set once at creation) or on disk.  Only the
        # per-firing task instruction needs to go into the chat.
        task_instructions = schedule.get("instructions", "No specific instructions.")
        prompt_parts = [
            f"[AGENT WAKE-UP — {schedule['name']} — {now_str} — {schedule.get('type', 'manual')}]",
            f"Task: {task_instructions}",
        ]

        user_input = "\n".join(prompt_parts)

        # Add the user message to session history
        session.add_user_message(user_input, username="agent-scheduler")

        print(f"[AGENT] Executing: {schedule['name']} ({trigger})", flush=True)

        # Run Claude — handles threading, streaming, and session persistence
        await run_claude_async(session, user_input, username="agent-scheduler")

        # Get the response (last assistant message)
        response = ""
        for msg in reversed(session.messages):
            if msg["role"] == "assistant":
                response = msg.get("content_full", msg.get("content", ""))
                break

        # ── Tool call interception loop ──────────────────────────
        # If the agent's response contains ```tool_call blocks, execute
        # them and feed results back for another turn. Max 10 rounds
        # to prevent infinite loops.
        tool_rounds = 0
        while _tool_registry and tool_rounds < 10:
            tool_calls = _extract_tool_calls(response)
            if not tool_calls:
                break
            tool_rounds += 1

            # Execute each tool call and collect results
            results = []
            for tc in tool_calls:
                tool_name = tc.get("tool", "")
                action = tc.get("action", "")
                params = tc.get("params", {})
                print(f"[TOOLS] Agent calling: {tool_name}.{action}({params})", flush=True)
                result = _tool_registry.execute(tool_name, action, params)
                results.append({
                    "tool": tool_name,
                    "action": action,
                    "result": result,
                })

            # Inject results as a follow-up user message
            result_text = "[TOOL RESULTS]\n"
            for r in results:
                result_text += f"\n## {r['tool']}.{r['action']}\n"
                result_text += json.dumps(r["result"], indent=2, default=str)
                result_text += "\n"
            result_text += "\n[/TOOL RESULTS]\nProcess these results and continue. If you need to call more tools, use ```tool_call blocks."

            session.add_user_message(result_text, username="tool-executor")
            await run_claude_async(session, result_text, username="tool-executor")

            # Get new response
            response = ""
            for msg in reversed(session.messages):
                if msg["role"] == "assistant":
                    response = msg.get("content_full", msg.get("content", ""))
                    break

        if tool_rounds > 0:
            print(f"[TOOLS] Completed {tool_rounds} tool round(s) for agent task", flush=True)
        # ── End tool call interception ───────────────────────────

        duration = time.time() - start_time
        _log_agent_run(schedule["name"], trigger, response, duration)

        # Re-read config from disk to avoid overwriting changes made while task ran
        fresh_config = _load_agent_config()
        fresh_config.setdefault("last_runs", {})
        fresh_config["last_runs"][schedule["name"]] = datetime.now().isoformat()
        _save_agent_config(fresh_config)

        print(f"[AGENT] Completed: {schedule['name']} in {duration:.1f}s ({len(response)} chars)", flush=True)

    except Exception as e:
        duration = time.time() - start_time
        _log_agent_run(schedule.get("name", "unknown"), trigger, "", duration, error=str(e))
        print(f"[AGENT] Error: {e}", flush=True)

    finally:
        _agent_running = False
        _agent_status = {
            "state": "idle",
            "last_run": datetime.now().isoformat(),
            "last_schedule": schedule.get("name"),
        }


async def _agent_scheduler_loop():
    """Background loop — checks agent schedules every 30s and triggers due tasks."""
    global _agent_status
    print("[AGENT] Scheduler loop started", flush=True)

    # Let server finish starting up
    await asyncio.sleep(5)

    while True:
        try:
            config = _load_agent_config()

            if not config.get("enabled", False):
                _agent_status["state"] = "disabled"
                await asyncio.sleep(30)
                continue

            if _agent_running:
                await asyncio.sleep(10)
                continue

            _agent_status["state"] = "watching"

            # Check each schedule
            for schedule in config.get("schedules", []):
                if _check_schedule_due(schedule, config.get("last_runs", {})):
                    await _execute_agent_task(schedule, config, trigger="scheduled")
                    break  # one task at a time

        except Exception as e:
            print(f"[AGENT] Scheduler error: {e}", flush=True)

        await asyncio.sleep(30)


# ── Agent API Routes ─────────────────────────────────────────

@app.get("/api/agent/config")
async def agent_get_config(auth_token: str = Cookie(None)):
    require_admin(auth_token)
    return _load_agent_config()


@app.post("/api/agent/config")
async def agent_update_config(request: Request, auth_token: str = Cookie(None)):
    """Partial merge update of agent config."""
    require_admin(auth_token)
    updates = await request.json()
    config = _load_agent_config()
    for key, value in updates.items():
        config[key] = value
    _save_agent_config(config)
    return {"ok": True, "config": config}


@app.post("/api/agent/toggle")
async def agent_toggle(request: Request, auth_token: str = Cookie(None)):
    """Toggle agent enabled/disabled."""
    require_admin(auth_token)
    data = await request.json()
    config = _load_agent_config()
    config["enabled"] = data.get("enabled", not config.get("enabled", False))
    _save_agent_config(config)
    state = "enabled" if config["enabled"] else "disabled"
    print(f"[AGENT] Toggled {state}", flush=True)
    return {"ok": True, "enabled": config["enabled"]}


@app.post("/api/agent/trigger")
async def agent_trigger(request: Request, auth_token: str = Cookie(None)):
    """Manually trigger an agent run (named schedule or ad-hoc instructions)."""
    require_admin(auth_token)
    data = await request.json()
    schedule_name = data.get("schedule")

    config = _load_agent_config()

    if schedule_name:
        schedule = next((s for s in config.get("schedules", []) if s["name"] == schedule_name), None)
        if not schedule:
            return JSONResponse({"error": f"Schedule '{schedule_name}' not found"}, status_code=404)
    else:
        # Ad-hoc trigger with custom instructions
        schedule = {
            "name": data.get("name", "manual"),
            "type": "manual",
            "enabled": True,
            "instructions": data.get("instructions", "Manual trigger — check system status and report."),
        }

    # Fire and forget so the HTTP response returns immediately
    asyncio.create_task(_execute_agent_task(schedule, config, trigger="manual"))
    return {"ok": True, "message": f"Agent triggered: {schedule['name']}", "session_id": config.get("session_id")}


@app.get("/api/agent/log")
async def agent_get_log(limit: int = 50, auth_token: str = Cookie(None)):
    """Get recent agent execution log entries (newest first)."""
    require_admin(auth_token)
    entries = []
    if _agent_log_path.exists():
        with open(_agent_log_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    return {"entries": list(reversed(entries[-limit:]))}


@app.get("/api/agent/status")
async def agent_get_status(auth_token: str = Cookie(None)):
    """Get current agent scheduler status."""
    require_admin(auth_token)
    config = _load_agent_config()
    return {
        "status": _agent_status,
        "enabled": config.get("enabled", False),
        "session_id": config.get("session_id"),
        "schedule_count": len(config.get("schedules", [])),
        "running": _agent_running,
    }


@app.post("/api/agent/schedules")
async def agent_manage_schedules(request: Request, auth_token: str = Cookie(None)):
    """Add, update, or remove schedule entries. Body: {action: add|update|remove, schedule: {...}}"""
    require_admin(auth_token)
    data = await request.json()
    config = _load_agent_config()

    action = data.get("action", "add")
    schedule = data.get("schedule", {})

    if action == "add":
        if not schedule.get("name"):
            return JSONResponse({"error": "Schedule must have a name"}, status_code=400)
        config.setdefault("schedules", []).append(schedule)

    elif action == "update":
        for i, s in enumerate(config.get("schedules", [])):
            if s["name"] == schedule.get("name"):
                config["schedules"][i] = schedule
                break

    elif action == "remove":
        name = schedule.get("name") or data.get("name")
        config["schedules"] = [s for s in config.get("schedules", []) if s["name"] != name]

    _save_agent_config(config)
    return {"ok": True, "schedules": config["schedules"]}


@app.get("/api/agent/instructions")
async def agent_get_instructions(auth_token: str = Cookie(None)):
    """Get the agent instructions file."""
    require_admin(auth_token)
    return {"instructions": _load_agent_instructions(), "path": str(_agent_instructions_path)}


@app.post("/api/agent/instructions")
async def agent_update_instructions(request: Request, auth_token: str = Cookie(None)):
    """Update the agent instructions file."""
    require_admin(auth_token)
    data = await request.json()
    content = data.get("instructions", "")
    _agent_instructions_path.write_text(content, encoding="utf-8")
    return {"ok": True, "length": len(content)}


# ── Agent Tools API Routes ────────────────────────────────────

@app.get("/api/tools")
async def tools_list(auth_token: str = Cookie(None)):
    """List all registered tools and their capabilities."""
    require_admin(auth_token)
    if not _tool_registry:
        return {"tools": [], "error": "Tool registry not loaded"}
    return {"tools": _tool_registry.list_tools()}


@app.get("/api/tools/config")
async def tools_get_config(auth_token: str = Cookie(None)):
    """Get tool configuration (secrets masked)."""
    require_admin(auth_token)
    if not _tool_registry:
        return {"config": {}, "error": "Tool registry not loaded"}
    return {"config": _tool_registry.get_config(), "path": str(TOOLS_CONFIG_PATH)}


@app.post("/api/tools/config")
async def tools_update_config(request: Request, auth_token: str = Cookie(None)):
    """Update config for a specific tool. Body: {tool: "email", config: {...}}"""
    require_admin(auth_token)
    if not _tool_registry:
        return JSONResponse({"error": "Tool registry not loaded"}, status_code=500)
    data = await request.json()
    tool_name = data.get("tool")
    config_updates = data.get("config", {})
    if not tool_name:
        return JSONResponse({"error": "Missing 'tool' field"}, status_code=400)
    _tool_registry.update_tool_config(tool_name, config_updates)
    return {"ok": True, "tool": tool_name}


@app.post("/api/tools/execute")
async def tools_execute(request: Request, auth_token: str = Cookie(None)):
    """Directly execute a tool action. Body: {tool: "email", action: "send", params: {...}}"""
    require_admin(auth_token)
    if not _tool_registry:
        return JSONResponse({"error": "Tool registry not loaded"}, status_code=500)
    data = await request.json()
    tool_name = data.get("tool")
    action = data.get("action")
    params = data.get("params", {})
    if not tool_name or not action:
        return JSONResponse({"error": "Missing 'tool' and/or 'action'"}, status_code=400)
    result = _tool_registry.execute(tool_name, action, params)
    return result


@app.get("/api/tools/log")
async def tools_get_log(limit: int = 50, auth_token: str = Cookie(None)):
    """Get recent tool execution log."""
    require_admin(auth_token)
    if not _tool_registry:
        return {"entries": []}
    return {"entries": _tool_registry.get_log(limit)}


# ── WebRTC Config ──────────────────────────────────────────────

# TURN config — env-driven. Empty TURN_HOST = no TURN; WebRTC falls back to public STUN.
# TURN_SECRET must match the coturn server's static-auth-secret (for HMAC-based ephemeral credentials).
TURN_SECRET = os.environ.get("TURN_SECRET", "")
TURN_HOST = os.environ.get("TURN_HOST", "")

@app.get("/api/voice/rtc-config")
async def get_rtc_config(auth_token: str = Cookie(None)):
    """Return ICE server config with ephemeral TURN credentials (24h TTL)."""
    user = get_user_from_token(auth_token)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    # Ephemeral TURN credentials via HMAC-SHA1 (coturn REST API)
    ttl = 86400  # 24 hours
    expiry = int(time.time()) + ttl
    turn_username = f"{expiry}:harness"
    turn_credential = base64.b64encode(
        hmac.new(TURN_SECRET.encode(), turn_username.encode(), hashlib.sha1).digest()
    ).decode()
    ice_servers = [
        {"urls": "stun:stun.l.google.com:19302"},
        {"urls": "stun:stun1.l.google.com:19302"},
    ]
    if TURN_HOST:
        turn_urls = [
            f"turn:{TURN_HOST}:3478?transport=udp",
            f"turn:{TURN_HOST}:3478?transport=tcp",
        ]
        _turns_host = os.environ.get("TURNS_HOST", "")
        if _turns_host:
            turn_urls.append(f"turns:{_turns_host}:5349?transport=tcp")
        ice_servers.append({
            "urls": turn_urls,
            "username": turn_username,
            "credential": turn_credential,
        })
    return {"iceServers": ice_servers}


# ── Agent Ops Convenience Routes ──────────────────────────────

@app.get("/api/agent/digest")
async def agent_digest(auth_token: str = Cookie(None)):
    """Run a full agent digest — health + changes + research status."""
    require_admin(auth_token)
    if not _tool_registry:
        return JSONResponse({"error": "Tool registry not loaded"}, status_code=500)
    result = _tool_registry.execute("agent_ops", "digest")
    return result


@app.get("/api/agent/scan")
async def agent_scan(auth_token: str = Cookie(None)):
    """Scan watched directories for file changes."""
    require_admin(auth_token)
    if not _tool_registry:
        return JSONResponse({"error": "Tool registry not loaded"}, status_code=500)
    result = _tool_registry.execute("agent_ops", "scan")
    return result


@app.get("/api/agent/snapshot")
async def agent_snapshot(auth_token: str = Cookie(None)):
    """Take a system snapshot."""
    require_admin(auth_token)
    if not _tool_registry:
        return JSONResponse({"error": "Tool registry not loaded"}, status_code=500)
    result = _tool_registry.execute("agent_ops", "snapshot")
    return result


@app.get("/api/agent/reports")
async def agent_reports(limit: int = 20, tag: str = None, auth_token: str = Cookie(None)):
    """List stored agent reports."""
    require_admin(auth_token)
    if not _tool_registry:
        return JSONResponse({"error": "Tool registry not loaded"}, status_code=500)
    params = {"limit": limit}
    if tag:
        params["tag"] = tag
    result = _tool_registry.execute("agent_ops", "reports", params)
    return result


@app.get("/api/agent/reports/{filename}")
async def agent_read_report(filename: str, auth_token: str = Cookie(None)):
    """Read a specific agent report."""
    require_admin(auth_token)
    if not _tool_registry:
        return JSONResponse({"error": "Tool registry not loaded"}, status_code=500)
    result = _tool_registry.execute("agent_ops", "read_report", {"filename": filename})
    return result


@app.post("/api/agent/index-research")
async def agent_index_research(auth_token: str = Cookie(None)):
    """Rebuild the research file index."""
    require_admin(auth_token)
    if not _tool_registry:
        return JSONResponse({"error": "Tool registry not loaded"}, status_code=500)
    result = _tool_registry.execute("agent_ops", "index_research")
    return result


@app.get("/api/agent/research")
async def agent_research_status(auth_token: str = Cookie(None)):
    """Get current research index status."""
    require_admin(auth_token)
    if not _tool_registry:
        return JSONResponse({"error": "Tool registry not loaded"}, status_code=500)
    result = _tool_registry.execute("agent_ops", "research_status")
    return result


@app.post("/api/webhooks/incoming")
async def webhooks_incoming(request: Request):
    """
    Catch-all inbound webhook endpoint. External services POST here.
    Query param 'source' identifies the sender (e.g. ?source=twilio).
    """
    if not _tool_registry:
        return JSONResponse({"error": "Tool registry not loaded"}, status_code=500)

    source = request.query_params.get("source", "unknown")
    try:
        payload = await request.json()
    except Exception:
        payload = {"raw": (await request.body()).decode("utf-8", errors="replace")}

    webhook_tool = _tool_registry.get_tool("webhook")
    if not webhook_tool:
        return JSONResponse({"error": "Webhook tool not registered"}, status_code=500)

    headers = dict(request.headers)
    result = webhook_tool.receive(source, payload, headers)
    return result



def _shutdown_flush_partials(signum, frame):
    """Final flush of all active partial responses before server dies."""
    import signal as _sig
    sig_name = _sig.Signals(signum).name if hasattr(_sig, 'Signals') else str(signum)
    print(f"\n[SHUTDOWN] Received {sig_name}, flushing partial responses...", flush=True)
    flushed = 0
    for session in list(_sessions.values()):
        state = getattr(session, '_partial_state', None)
        text = getattr(session, '_partial_response', '')
        if state and text:
            try:
                state["path"].write_text(json.dumps({
                    "text": text,
                    "user_input": state.get("user_input", ""),
                    "username": state.get("username", ""),
                    "started": datetime.now().isoformat(),
                    "stream_buffer": list(session._stream_buffer) if hasattr(session, '_stream_buffer') else [],
                }))
                flushed += 1
                print(f"[SHUTDOWN] Flushed {len(text)} chars for session {session.id}", flush=True)
            except Exception as e:
                print(f"[SHUTDOWN] Error flushing session {session.id}: {e}", flush=True)
    print(f"[SHUTDOWN] Done — flushed {flushed} session(s). Exiting.", flush=True)
    sys.exit(0)


@app.get("/api/voice/stt-status")
async def api_stt_status():
    return JSONResponse({"available": _stt_available})


# ══════════════════════════════════════════════════════
# ChaosSat 4B Inference — SSH to Vast.ai GPU box
# ══════════════════════════════════════════════════════

_CHAOSSAT_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                      "..", "LocalLLM", "cthulu", "chaossat_remote.json")

def _load_chaossat_config():
    p = os.path.normpath(_CHAOSSAT_CONFIG_PATH)
    if os.path.exists(p):
        with open(p) as f:
            return json.load(f)
    return {"host": "ssh6.vast.ai", "port": 17266, "user": "root",
            "checkpoint_dir": "/workspace", "device": "cuda"}

@app.get("/api/chaossat/config")
async def chaossat_config():
    cfg = _load_chaossat_config()
    return JSONResponse(cfg)

@app.post("/api/chaossat/config")
async def chaossat_config_update(request: Request):
    body = await request.json()
    p = os.path.normpath(_CHAOSSAT_CONFIG_PATH)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        json.dump(body, f, indent=2)
    return JSONResponse({"ok": True})

@app.get("/api/chaossat/status")
async def chaossat_status():
    cfg = _load_chaossat_config()
    try:
        proc = await asyncio.create_subprocess_exec(
            "ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=5",
            "-p", str(cfg["port"]), f"{cfg['user']}@{cfg['host']}",
            "nvidia-smi --query-gpu=name,memory.used,memory.total,utilization.gpu --format=csv,noheader,nounits",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        if proc.returncode == 0:
            lines = stdout.decode().strip().split("\n")
            gpus = []
            for line in lines:
                parts = [x.strip() for x in line.split(",")]
                if len(parts) >= 4:
                    gpus.append({"name": parts[0], "mem_used": int(parts[1]),
                                 "mem_total": int(parts[2]), "utilization": int(parts[3])})
            return JSONResponse({"online": True, "gpus": gpus})
        return JSONResponse({"online": False, "error": "SSH failed"})
    except Exception as e:
        return JSONResponse({"online": False, "error": str(e)})

@app.post("/api/chaossat/generate")
async def chaossat_generate(request: Request):
    import shlex
    body = await request.json()
    prompt = body.get("prompt", "Hello")
    max_tokens = min(int(body.get("max_tokens", 200)), 2048)
    temperature = max(0.01, min(float(body.get("temperature", 0.8)), 3.0))
    top_k = max(1, min(int(body.get("top_k", 50)), 500))
    top_p = max(0.0, min(float(body.get("top_p", 0.9)), 1.0))
    step = str(body.get("step", "latest"))

    cfg = _load_chaossat_config()
    ckpt_dir = cfg.get("checkpoint_dir", "/workspace")
    device = cfg.get("device", "cuda")

    remote_cmd = (
        f"cd {shlex.quote(ckpt_dir)} && python3 -u chat_4b.py"
        f" --prompt {shlex.quote(prompt)}"
        f" --device {shlex.quote(device)}"
        f" --max-tokens {max_tokens}"
        f" --temperature {temperature}"
        f" --top-k {top_k}"
        f" --top-p {top_p}"
        f" --step {shlex.quote(step)}"
    )

    ssh_cmd = [
        "ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10",
        "-p", str(cfg["port"]), f"{cfg['user']}@{cfg['host']}",
        remote_cmd,
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *ssh_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=max_tokens * 2 + 30
        )
        output = stdout.decode("utf-8", errors="replace")

        generation = ""
        stats = ""
        if "Generation: " in output:
            after_gen = output.split("Generation: ", 1)[1]
            if "\n\n[" in after_gen:
                generation = after_gen.rsplit("\n\n[", 1)[0]
                stats = "[" + after_gen.rsplit("\n\n[", 1)[1]
            else:
                generation = after_gen.strip()
        else:
            generation = output.strip()

        return JSONResponse({
            "generation": generation,
            "stats": stats.strip(),
            "prompt": prompt,
            "raw": output,
            "error": stderr.decode().strip() if proc.returncode != 0 else None,
        })
    except asyncio.TimeoutError:
        return JSONResponse({"error": "Generation timed out", "generation": ""}, status_code=504)
    except Exception as e:
        return JSONResponse({"error": str(e), "generation": ""}, status_code=500)

@app.post("/api/chaossat/generate/stream")
async def chaossat_generate_stream(request: Request):
    import shlex
    body = await request.json()
    prompt = body.get("prompt", "Hello")
    max_tokens = min(int(body.get("max_tokens", 200)), 2048)
    temperature = max(0.01, min(float(body.get("temperature", 0.8)), 3.0))
    top_k = max(1, min(int(body.get("top_k", 50)), 500))
    top_p = max(0.0, min(float(body.get("top_p", 0.9)), 1.0))
    step = str(body.get("step", "latest"))

    cfg = _load_chaossat_config()
    ckpt_dir = cfg.get("checkpoint_dir", "/workspace")
    device = cfg.get("device", "cuda")

    remote_cmd = (
        f"cd {shlex.quote(ckpt_dir)} && python3 -u chat_4b.py"
        f" --prompt {shlex.quote(prompt)}"
        f" --device {shlex.quote(device)}"
        f" --max-tokens {max_tokens}"
        f" --temperature {temperature}"
        f" --top-k {top_k}"
        f" --top-p {top_p}"
        f" --step {shlex.quote(step)}"
    )

    ssh_cmd = [
        "ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10",
        "-p", str(cfg["port"]), f"{cfg['user']}@{cfg['host']}",
        remote_cmd,
    ]

    async def stream_tokens():
        proc = await asyncio.create_subprocess_exec(
            *ssh_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        past_header = False
        past_generation = False
        try:
            while True:
                chunk = await asyncio.wait_for(proc.stdout.read(64), timeout=120)
                if not chunk:
                    break
                text = chunk.decode("utf-8", errors="replace")
                if not past_header:
                    if "Generation: " in text:
                        past_header = True
                        text = text.split("Generation: ", 1)[1]
                    else:
                        continue
                if past_header and not past_generation:
                    if "\n\n[" in text:
                        text = text.split("\n\n[")[0]
                        yield f"data: {json.dumps({'token': text})}\n\n"
                        yield f"data: {json.dumps({'done': True})}\n\n"
                        past_generation = True
                        break
                    yield f"data: {json.dumps({'token': text})}\n\n"
        except asyncio.TimeoutError:
            yield f"data: {json.dumps({'error': 'timeout'})}\n\n"
        finally:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()

    return StreamingResponse(stream_tokens(), media_type="text/event-stream")


if __name__ == "__main__":
    import signal
    signal.signal(signal.SIGTERM, _shutdown_flush_partials)
    signal.signal(signal.SIGINT, _shutdown_flush_partials)
    port = int(os.environ.get("HARNESS_PORT", 8081))
    print(f"Claude Harness starting on http://localhost:{port}")
    print(f"Data dir: {DATA_DIR}")
    uvicorn.run(app, host="0.0.0.0", port=port)
