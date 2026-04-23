#!/usr/bin/env bash
# Euleptos one-shot installer (Linux / macOS)
# Usage:
#   curl -fsSL https://euleptos.com/install.sh | bash                  # minimal (Claude Code only)
#   curl -fsSL https://euleptos.com/install.sh | bash -s -- --ollama   # + install Ollama + pull a model
#   bash install.sh --ollama
#
# Environment overrides:
#   EULEPTOS_DIR          install location           (default: ~/euleptos)
#   EULEPTOS_MODEL        Ollama model to pull       (default: llama3.2:3b)
#   EULEPTOS_WITH_OLLAMA  1=install Ollama if missing + pull a model (same as --ollama)
#   EULEPTOS_NO_OLLAMA    1=skip Ollama entirely, don't even detect an existing install
#   EULEPTOS_NO_PULL      1=skip pulling a model (only matters with --ollama)
#   EULEPTOS_YES          1=auto-yes to all prompts (also implied if non-interactive)

set -eu

# Parse CLI flags (supports `bash -s -- --ollama` when piped from curl)
WITH_OLLAMA=0
[ "${EULEPTOS_WITH_OLLAMA:-}" = "1" ] && WITH_OLLAMA=1
for arg in "$@"; do
    case "$arg" in
        --ollama)     WITH_OLLAMA=1 ;;
        --no-ollama)  WITH_OLLAMA=0; EULEPTOS_NO_OLLAMA=1 ;;
        -h|--help)
            cat <<'USAGE'
Euleptos one-shot installer (Linux / macOS)

Usage:
  curl -fsSL https://euleptos.com/install.sh | bash                  # minimal (Claude Code only)
  curl -fsSL https://euleptos.com/install.sh | bash -s -- --ollama   # + install Ollama + pull a model
  bash install.sh [--ollama|--no-ollama]

Env overrides:
  EULEPTOS_DIR, EULEPTOS_MODEL, EULEPTOS_WITH_OLLAMA,
  EULEPTOS_NO_OLLAMA, EULEPTOS_NO_PULL, EULEPTOS_YES
USAGE
            exit 0 ;;
    esac
done

INSTALL_DIR="${EULEPTOS_DIR:-$HOME/euleptos}"
ZIP_URL="${EULEPTOS_ZIP_URL:-https://euleptos.com/dist/euleptos-latest.zip}"
DEFAULT_MODEL="${EULEPTOS_MODEL:-llama3.2:3b}"

# Auto-yes when stdin is not a TTY (curl|bash) or when EULEPTOS_YES=1
INTERACTIVE=1
[ -t 0 ] || INTERACTIVE=0
[ "${EULEPTOS_YES:-}" = "1" ] && INTERACTIVE=0

# Colors (skip if not a TTY on stdout)
if [ -t 1 ]; then
    C_GREEN='\033[1;32m'; C_YELLOW='\033[1;33m'; C_RED='\033[1;31m'
    C_CYAN='\033[1;36m'; C_DIM='\033[2m'; C_NC='\033[0m'
else
    C_GREEN=''; C_YELLOW=''; C_RED=''; C_CYAN=''; C_DIM=''; C_NC=''
fi

say()    { printf "%b%s%b\n" "$C_CYAN" "$*" "$C_NC"; }
ok()     { printf "  %b✓%b %s\n" "$C_GREEN" "$C_NC" "$*"; }
warn()   { printf "  %b⚠%b %s\n" "$C_YELLOW" "$C_NC" "$*"; }
fail()   { printf "  %b✗%b %s\n" "$C_RED" "$C_NC" "$*" >&2; exit 1; }
dim()    { printf "  %b%s%b\n" "$C_DIM" "$*" "$C_NC"; }

ask_yes() {
    # ask_yes "Question?"  -> returns 0 for yes, 1 for no
    local prompt="$1"
    if [ "$INTERACTIVE" = "0" ]; then
        return 0
    fi
    printf "    %s [Y/n] " "$prompt"
    local reply
    read -r reply </dev/tty 2>/dev/null || return 0
    case "$reply" in
        n|N|no|NO) return 1 ;;
        *) return 0 ;;
    esac
}

printf "%b╔════════════════════════════════════════╗%b\n" "$C_CYAN" "$C_NC"
printf "%b║       EULEPTOS  one-shot installer      ║%b\n" "$C_CYAN" "$C_NC"
printf "%b║       local-first AI harness            ║%b\n" "$C_CYAN" "$C_NC"
printf "%b╚════════════════════════════════════════╝%b\n" "$C_CYAN" "$C_NC"

# ---------------------------------------------------------------- 1. Python
say "→ Checking Python"
if ! command -v python3 >/dev/null 2>&1; then
    printf "%b%s%b\n" "$C_RED" "Python 3 not found. Install Python 3.10+ first:" "$C_NC"
    printf "  macOS:  brew install python@3.11\n"
    printf "  Debian: sudo apt install python3 python3-pip python3-venv\n"
    printf "  Fedora: sudo dnf install python3 python3-pip\n"
    fail "Aborted — install Python and re-run."
fi
PY_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PY_OK=$(python3 -c 'import sys; print(1 if sys.version_info>=(3,10) else 0)')
if [ "$PY_OK" != "1" ]; then
    fail "Python $PY_VERSION found, but 3.10+ required."
fi
ok "Python $PY_VERSION"

if ! python3 -m pip --version >/dev/null 2>&1; then
    fail "pip not available. Install python3-pip and re-run."
fi

# ---------------------------------------------------------------- 2. unzip / curl
for cmd in curl unzip; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
        fail "$cmd is required but not installed."
    fi
done

# ---------------------------------------------------------------- 3. Download
say "→ Downloading Euleptos"
mkdir -p "$INSTALL_DIR"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
curl -fSL --progress-bar "$ZIP_URL" -o "$TMP/euleptos.zip" || fail "Download failed."
SIZE=$(du -h "$TMP/euleptos.zip" 2>/dev/null | cut -f1 || echo "?")
ok "Downloaded $SIZE"

unzip -oq "$TMP/euleptos.zip" -d "$INSTALL_DIR" || fail "Unzip failed."
ok "Extracted to $INSTALL_DIR"

# ---------------------------------------------------------------- 4. Python deps
say "→ Installing Python dependencies"
if [ -f "$INSTALL_DIR/requirements.txt" ]; then
    python3 -m pip install --quiet --user -r "$INSTALL_DIR/requirements.txt" \
        || fail "pip install failed. Try: python3 -m pip install --user fastapi uvicorn python-multipart websockets"
else
    python3 -m pip install --quiet --user fastapi uvicorn python-multipart websockets \
        || fail "pip install failed."
fi
ok "Deps installed"

# ---------------------------------------------------------------- 5. Claude Code detection
CLAUDE_AVAILABLE=0
say "→ Checking Claude Code"
if command -v claude >/dev/null 2>&1; then
    CC_VERSION=$(claude --version 2>/dev/null | head -1 || echo "installed")
    ok "Claude Code detected ($CC_VERSION)"
    dim "Euleptos will drive it directly — no API key needed."
    CLAUDE_AVAILABLE=1
else
    warn "Claude Code not found on PATH"
    dim "Euleptos primarily drives your existing 'claude' CLI — no API key required."
    dim "Install it from: https://docs.anthropic.com/en/docs/claude-code"
    dim "(You can still run Euleptos with Ollama-only — skip this and continue.)"
fi

# ---------------------------------------------------------------- 6. Ollama
# Default: only use Ollama if it's already on PATH. Never auto-install unless
# the user opts in with --ollama / EULEPTOS_WITH_OLLAMA=1.
# EULEPTOS_NO_OLLAMA=1 skips the entire phase (no detection, no wiring).
OLLAMA_AVAILABLE=0
if [ "${EULEPTOS_NO_OLLAMA:-}" = "1" ]; then
    dim "Skipping Ollama (EULEPTOS_NO_OLLAMA=1)"
elif command -v ollama >/dev/null 2>&1; then
    say "→ Checking Ollama (local model runner)"
    ok "Ollama already installed ($(ollama --version 2>/dev/null | head -1))"
    OLLAMA_AVAILABLE=1
elif [ "$WITH_OLLAMA" = "1" ]; then
    say "→ Installing Ollama (--ollama flag / EULEPTOS_WITH_OLLAMA=1)"
    if ask_yes "Install Ollama? (official installer from ollama.com)"; then
        dim "Running official Ollama installer..."
        if curl -fsSL https://ollama.com/install.sh | sh; then
            ok "Ollama installed"
            OLLAMA_AVAILABLE=1
        else
            warn "Ollama install failed. You can install later: curl -fsSL https://ollama.com/install.sh | sh"
        fi
    else
        dim "Skipped."
    fi
else
    dim "Ollama not installed (skipping — pass --ollama to install, or do it yourself later)"
fi

# ---------------------------------------------------------------- 6. Start Ollama if needed
if [ "$OLLAMA_AVAILABLE" = "1" ]; then
    if ! curl -fsS http://localhost:11434/api/tags >/dev/null 2>&1; then
        say "→ Starting Ollama service"
        STARTED=0
        # Linux systemd
        if command -v systemctl >/dev/null 2>&1 && systemctl list-unit-files 2>/dev/null | grep -q '^ollama'; then
            sudo systemctl start ollama 2>/dev/null && STARTED=1 || true
        fi
        # macOS brew service
        if [ "$STARTED" = "0" ] && command -v brew >/dev/null 2>&1 && brew services list 2>/dev/null | grep -q ollama; then
            brew services start ollama >/dev/null 2>&1 && STARTED=1 || true
        fi
        # Fallback: nohup
        if [ "$STARTED" = "0" ]; then
            nohup ollama serve >/dev/null 2>&1 &
            disown 2>/dev/null || true
        fi
        # Wait up to 15s for service to come up
        for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
            if curl -fsS http://localhost:11434/api/tags >/dev/null 2>&1; then
                break
            fi
            sleep 1
        done
    fi
    if curl -fsS http://localhost:11434/api/tags >/dev/null 2>&1; then
        ok "Ollama serving on http://localhost:11434"
    else
        warn "Ollama installed but not responding on :11434 (start manually: ollama serve)"
        OLLAMA_AVAILABLE=0
    fi
fi

# ---------------------------------------------------------------- 7. Pull a model
# Only pull a baseline model when --ollama is passed AND the user has none.
# Never silently pull 2GB on a user who just wanted the harness.
if [ "$OLLAMA_AVAILABLE" = "1" ]; then
    EXISTING=$(curl -fsS http://localhost:11434/api/tags 2>/dev/null \
        | python3 -c 'import json,sys; d=json.load(sys.stdin); print(len(d.get("models",[])))' 2>/dev/null \
        || echo 0)
    if [ "$EXISTING" != "0" ]; then
        ok "$EXISTING Ollama model(s) already installed"
    elif [ "$WITH_OLLAMA" = "1" ] && [ "${EULEPTOS_NO_PULL:-}" != "1" ]; then
        say "→ No local models yet"
        if ask_yes "Pull $DEFAULT_MODEL (~2 GB, fast baseline)?"; then
            ollama pull "$DEFAULT_MODEL" || warn "Model pull failed (try manually: ollama pull $DEFAULT_MODEL)"
            ok "$DEFAULT_MODEL ready"
        else
            dim "Skipped. Pull any model with: ollama pull <model>"
            dim "Model catalog: https://ollama.com/library"
        fi
    else
        dim "No local Ollama models (pull one with: ollama pull llama3.2:3b)"
    fi
fi

# ---------------------------------------------------------------- 9. .env
say "→ Configuring optional API key"
if [ -f "$INSTALL_DIR/.env" ]; then
    ok ".env already exists, leaving alone"
else
    cat > "$INSTALL_DIR/.env" <<'EOF'
# Anthropic API key — OPTIONAL.
#
# Euleptos uses your existing Claude Code install by default (no key needed).
# Leave this blank unless you specifically want "Pure Mode" — a bypass that
# hits the raw Anthropic API directly instead of going through claude -p.
#
#   Get one (only if you want Pure Mode): https://console.anthropic.com/
ANTHROPIC_API_KEY=
EOF
    ok "Created .env stub (API key optional — leave blank for default Claude Code flow)"
fi

# ---------------------------------------------------------------- 10. Done
PORT=8080
printf "\n%b╔════════════════════════════════════════╗%b\n" "$C_GREEN" "$C_NC"
printf "%b║         ✓ INSTALL COMPLETE              ║%b\n" "$C_GREEN" "$C_NC"
printf "%b╚════════════════════════════════════════╝%b\n" "$C_GREEN" "$C_NC"
printf "\n  Start the harness:\n"
printf "    %bcd %s%b\n" "$C_CYAN" "$INSTALL_DIR" "$C_NC"
printf "    %bpython3 server.py%b\n" "$C_CYAN" "$C_NC"
printf "\n  Then open: %bhttp://localhost:%s%b\n\n" "$C_CYAN" "$PORT" "$C_NC"
if [ "$CLAUDE_AVAILABLE" = "1" ]; then
    printf "  %bClaude Code is wired in — no API key needed. Just go.%b\n" "$C_DIM" "$C_NC"
else
    printf "  %bInstall Claude Code (no API key needed) to use Claude: https://docs.anthropic.com/en/docs/claude-code%b\n" "$C_DIM" "$C_NC"
fi
if [ "$OLLAMA_AVAILABLE" = "1" ]; then
    printf "  %bOllama models appear in the picker as ollama:<name>.%b\n" "$C_DIM" "$C_NC"
fi
printf "\n"
