# Euleptos Harness — Project Instructions

Euleptos is a Claude harness: a local chat interface with persistent sessions, artifacts, multi-user auth, voice/video rooms, and a cognition-primer layer.

**The harness is the container. The primers are what make it work.**

## Cognition Primers — What Makes This Different

This harness ships with research-validated cognition primers that are loaded into every session. They were developed through 57-trial ablation studies (see `experiments/primer_psychometrics/` in the source repo) and have persisted across Claude 3.0 → 4.6.

- `GEOMETRIC_COGNITION` — Klein3 triple-pass cognition (direct / invert / combine), superposition protocol, witness separation. The default cognitive frame.
- `CODING_PRIMER` + `CODING_INJECTION.md` — engineering discipline for when coding-mode is active.
- `DEBUG_PRIMER` + `DEBUG_INJECTION.md` — systematic bug-finding protocol.
- `PERSONA_REANCHOR.md` — relational/identity anchor (optional).

These are visible, editable text files — not baked-in prompts. See the primer ablation findings for which are tier-1 (always active) vs tier-2 (conditional).

## MANDATORY: Read DEV_RULES.md BEFORE Editing Any Code

Before editing ANY file, read `DEV_RULES.md`. Its rules exist because previous sessions repeatedly destroyed working code through confident incompetence:

- **Snapshot before every edit** using `tools/safe_edit.py`
- **One change, test, then next change** — never stack edits
- **Never "fix" by removing functionality** — rollback instead
- **Never rewrite a file from scratch** — edit incrementally
- **When the user says output is wrong, it's wrong** — check mechanical basics first

Snapshot command: `python3 tools/safe_edit.py begin <file> "<intent>"` before EVERY edit.

## Dev vs Prod

- `server_dev.py` = sandbox, edit here first, runs on port 8081
- `server.py` = prod, runs on port 8080, only sync FROM dev after testing
- **Prod must NEVER lead dev.**

## Never Do Without Asking

- Kill any running process (check `data/process_registry.json` first)
- Revert code
- Restart servers
- Modify other users' credentials or data
