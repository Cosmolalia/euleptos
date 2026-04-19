# Coding Mode Addendum — Snapshot Lifecycle

Paste the block below at the end of the existing `[CODING MODE ACTIVE]` /
`CODING PROTOCOL` text in your per-chat coding-mode settings. It adds the
snapshot-lifecycle instructions without replacing any of the existing
engineering-discipline text.

---

```
AUTO-SNAPSHOT LIFECYCLE — ACTIVE IN THIS CHAT

This chat has coding mode on, which means every Edit / Write you do here is
auto-snapshotted to ~/.claude_auto_snapshots/ before the edit applies, scoped
to this session only. Other chats without coding mode are completely untouched.

ON YOUR FIRST ACTION IN EACH TURN (before any Edit/Write):
- Extract your session_id from the most recent "UserPromptSubmit hook success"
  line in your prompt context. Format: "session:XXXXXXXX".
- Run: mkdir -p ~/.claude_active_sessions && touch ~/.claude_active_sessions/<session_id>.flag
- This refreshes the gate. Without an active flag, the PreToolUse hook is a no-op.

STATUS NUDGE (show on every turn where you made edits):
- Emit a one-line status after your response, e.g.:
  📸 snapshots: server_dev.py (3 this session), voice_proxy.py (1)
- This keeps the fact that snapshots are recording visible to the user so
  they remember to toggle coding mode off when done.

IF AN EDIT INTRODUCES A NEW BUG:
- REVERT FIRST, then diagnose. Two bugs in flight is undefined state.
- Command: python3 tools/claude_hooks/claude_snapshot.py revert <file>
- To see what would be reverted before committing:
  python3 .../claude_snapshot.py diff <file>
- To see history:
  python3 .../claude_snapshot.py list <file>

THE RULE, NOT THE MORALITY:
Edit E introduced unexpected behavior B' somewhere else in the system?
Revert E before touching B'. Always. You cannot debug a system that has
two of your changes both misbehaving — you will stack fixes on top of
broken fixes and make it worse. The snapshot infrastructure exists so
"revert first" is always cheap. Use it.
```

---

## Shorter alternative (if the full text is too long for the settings field)

```
AUTO-SNAPSHOT ACTIVE: This chat is coding-mode. First action each turn:
  mkdir -p ~/.claude_active_sessions && touch ~/.claude_active_sessions/<session_id>.flag
(session_id from "UserPromptSubmit hook success" line).

If an edit introduces a bug elsewhere: REVERT FIRST, then diagnose. Command:
  python3 tools/claude_hooks/claude_snapshot.py revert <file>
Also: list, diff, status subcommands. Two bugs in flight is undefined state.

Emit a one-line status after edits so the user sees snapshot activity.
```
