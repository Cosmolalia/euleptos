# Euleptos

**The well-grasped instance.** A local-first Claude Code harness with rolling context, persistent artifacts, and a baked-in geometric cognition primer.

Euleptos runs on your machine. Your conversations, artifacts, and context live in local files you own — no cloud database, no hidden telemetry. It's a thin FastAPI wrapper around Anthropic's Claude API that adds the things Claude Code needs to feel continuous across sessions: rolling context windows, snapshot-before-edit discipline, artifact persistence, and a Klein3 reasoning primer that's been tuned across ~60 trials for real-world effectiveness.

---

## What's in the box

- **FastAPI server** (`server.py`) — HTTP + WebSocket, serves the chat UI and proxies to Anthropic
- **Rolling context** — per-session message history, automatic compression as you approach the context limit
- **Artifact store** — files Claude creates show up in a panel, persist across sessions, can be downloaded or edited
- **Safe-edit discipline** (`tools/safe_edit.py`) — snapshot every file before Claude touches it, rollback any edit in one command
- **Klein3 cognitive primer** (`geometric_prompt_optimized.md`) — ~500-token prompt that has Claude hold direct + inverted reasoning in superposition before collapsing on evidence. Catches confident-wrong answers.
- **Coding mode / Debug mode** — stackable prompt injections that activate engineering discipline (one change at a time, read-not-infer, delta principle)
- **Admin panel** (`static/admin.html`) — inspect sessions, manage artifacts, swap models, tune the primer stack

---

## Install

Requires **Python 3.10+** and an Anthropic API key.

```bash
git clone https://github.com/Cosmolalia/euleptos.git
cd euleptos
pip install -r requirements.txt       # or: pip install fastapi uvicorn anthropic python-multipart websockets
cp .env.example .env                  # then edit .env — set ANTHROPIC_API_KEY
python3 server.py
```

Open `http://localhost:8080`.

**Windows:** double-click `start_dist.bat` after editing `.env`.

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
