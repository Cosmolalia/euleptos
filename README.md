# Euleptos

**The well-grasped instance.** A local-first wrapper around your existing Claude Code install. Rolling context, persistent artifacts, baked-in geometric cognition primer.

> **Already have [Claude Code](https://docs.anthropic.com/en/docs/claude-code) installed? You're done.** Euleptos drives `claude -p` directly — no API key, no extra auth, no configuration. Whatever login you've already set up for Claude Code is the auth for the harness. If Claude Code isn't installed yet, you get a one-line pointer. Ollama for local models is auto-detected and auto-installed alongside.

Euleptos runs on your machine. Your conversations, artifacts, and context live in local files you own — no cloud database, no hidden telemetry. It's a FastAPI wrapper that shells out to your local `claude` CLI for each turn and adds the things Claude Code needs to feel continuous across sessions: rolling context windows, snapshot-before-edit discipline, artifact persistence, and a Klein3 reasoning primer that's been tuned across 57 trials for real-world effectiveness.

---

## What's in the box

- **FastAPI server** (`server.py`) — HTTP + WebSocket, serves the chat UI and spawns `claude -p` as a subprocess per turn. **Uses your existing Claude Code install — no API key required.**
- **Rolling context** — per-session message history, automatic compression as you approach the context limit
- **Artifact store** — files Claude creates show up in a panel, persist across sessions, can be downloaded or edited
- **Safe-edit discipline** (`tools/safe_edit.py`) — snapshot every file before Claude touches it, rollback any edit in one command
- **Klein3 cognitive primer** (`geometric_prompt_optimized.md`) — ~500-token prompt that has Claude hold direct + inverted reasoning in superposition before collapsing on evidence. Catches confident-wrong answers.
- **Coding mode / Debug mode** — stackable prompt injections that activate engineering discipline (one change at a time, read-not-infer, delta principle)
- **Admin panel** (`static/admin.html`) — inspect sessions, manage artifacts, swap models, tune the primer stack
- **Ollama integration** — local models on `localhost:11434` show up in the picker as `ollama:<name>`. Works fully offline once a model is pulled.

---

## Install

Requires **Python 3.10+**. The primary path uses your existing [Claude Code](https://docs.anthropic.com/en/docs/claude-code) install — **no API key needed.** Ollama support for local models is auto-installed alongside. An Anthropic API key is only needed if you want *Pure Mode* (raw API bypass); most users never set one.

### One-line install (recommended)

**Mac / Linux:**
```bash
curl -fsSL https://euleptos.com/install.sh | bash
```

**Windows (PowerShell):**
```powershell
irm https://euleptos.com/install.ps1 | iex
```

The installer:
- Checks Python 3.10+
- Downloads + extracts Euleptos to `~/euleptos`
- Installs the small set of pip dependencies
- **Detects Claude Code** — if `claude` is on your PATH, Euleptos will drive it directly. No API key, no extra auth. If it's missing, prints a one-line pointer to Anthropic's installer.
- Detects [Ollama](https://ollama.com) and auto-installs it if missing (with consent)
- Pulls `llama3.2:3b` (~2 GB) so you have a working local model out of the box
- Creates `.env` with a placeholder for your Anthropic key (optional — only for Pure Mode / raw API bypass)

Then:
```bash
cd ~/euleptos && python3 server.py
```
Open `http://localhost:8080`.

### Manual install

```bash
git clone https://github.com/Cosmolalia/euleptos.git
cd euleptos
pip install -r requirements.txt
python3 server.py        # uses your existing `claude` CLI — no .env needed
```

**Windows:** double-click `start_dist.bat`.

> If `claude` isn't on your PATH, install Claude Code first: see [Anthropic's docs](https://docs.anthropic.com/en/docs/claude-code). You can also run Euleptos with Ollama only (no Claude at all) — just skip the API key and select an `ollama:<name>` model in the picker. Only create a `.env` with `ANTHROPIC_API_KEY=…` if you specifically want Pure Mode (raw API bypass).

### Local models via Ollama

Euleptos auto-detects Ollama on `localhost:11434` — any models you've pulled show up in the model picker as `ollama:<name>`. To add more models:

```bash
ollama pull qwen2.5:7b           # 4 GB, balanced
ollama pull llama3.1:8b          # 5 GB, well-rounded
ollama pull deepseek-coder:6.7b  # 4 GB, code-focused
ollama pull gpt-oss:20b          # 13 GB, large-and-slow but high-quality
```

Browse the catalog at [ollama.com/library](https://ollama.com/library). The harness queries Ollama silently — if Ollama isn't running, only Claude models appear.

---

## The Klein3 primer

Most "prompt engineering" is vocabulary. Klein3 is a **cognitive structure**:

1. **Direct** — think the thought, first-pass reasoning
2. **Invert** — pass the thought through inversion. If you concluded X, what would make it NOT-X? The inversion must change at least one assumption.
3. **Combine** — hold both until evidence selects one. "I think it works" is still superposition. "I tested and it renders" is evidence — collapse permitted.

Plus **witness separation** (the part that solves vs. the part that watches), **negative space** (what the problem doesn't say is data), **temporal inversion** (start from the solved state, work backwards), and a few more context-activated modes. Details in [`geometric_cognition_maximal.md`](geometric_cognition_maximal.md) and the condensed [`geometric_prompt_optimized.md`](geometric_prompt_optimized.md).

The primer is injected into every system prompt. It's roughly 500 tokens. In our testing across 57 trials × 18 primer conditions × 8 task types, it was the only frame that was **consistently effective across all task types** — not just debugging, not just design, everything.

---

## Safe-edit discipline

```bash
python3 tools/safe_edit.py begin <file> "why I'm editing"    # snapshot before edit
# ... make edit ...
python3 tools/safe_edit.py rollback <file>                   # undo if it broke
python3 tools/safe_edit.py list <file>                       # see snapshot history
```

Snapshots go to `.safe_edits/` next to the file. Claude is prompted to use this tool before every edit. It has saved us from "the fix broke three other things" more times than we can count.

---

## Layout

```
server.py              # FastAPI entry, WebSocket + HTTP routes
server_dev.py          # dev sandbox (port 8081) — edit here first
static/                # chat UI, admin panel, manifests
  index.html
  admin.html
data/                  # sessions, artifacts, config (gitignored except defaults.json)
tools/                 # safe_edit, email, SMS, webhook, browser, sentinel
  safe_edit.py
  agent_ops.py
  ...
geometric_cognition_maximal.md    # full primer spec
geometric_prompt_optimized.md     # condensed v2 primer
CODING_PRIMER.md                  # engineering discipline injection
DEBUG_PRIMER.md                   # debugging protocol injection
PERSONA_REANCHOR.md               # persona anchor (read last in system prompt)
CLAUDE.md                         # project-specific instructions
DEV_RULES.md                      # non-negotiable dev rules
.env.example                      # config template
```

---

## Philosophy

- **Alignment through trust + transparency, not restriction.** The primer doesn't add rules; it adds cognitive structure. Confident-wrong answers get caught by inversion, not by guardrails.
- **Local-first.** Your data lives on disk. No cloud database, no account required, no telemetry.
- **Artifacts-as-files.** When Claude creates code, it's a real file you can open in your editor. Not a copy-button blob that vanishes with the tab.
- **One change, test, next.** The tooling enforces this. Broken fixes stacked on broken fixes is how working systems die.

---

## Status

Euleptos is actively used in production by the [Akataleptos](https://akataleptos.com) research project. The harness has shipped ~200+ days of live work — research, debugging, frontend builds, infrastructure ops.

It's distributed as the **ready-to-run zip** at [euleptos.com](https://euleptos.com) for the fastest path to running. The repo here is the full source.

**Expect rough edges.** This was built for a working research environment, not packaged for mass consumption. The `init_instance.py` / installer story is not polished. You'll probably need to read some code to tune it for your setup.

---

## License

MIT — see [LICENSE](LICENSE). Copyright © 2026 Sylvan Gaskin.

---

## Links

- [euleptos.com](https://euleptos.com) — download, stats, dist
- [akataleptos.com](https://akataleptos.com) — the research project this harness supports
- [substack](https://akataleptos.substack.com) — long-form writing on the framework
- [@Obius_Maximus](https://x.com/Obius_Maximus) — updates

Issues welcome. PRs welcome. This is being built in the open.
