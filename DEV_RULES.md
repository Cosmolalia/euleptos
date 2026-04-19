# Development Rules — Non-Negotiable

These rules exist because sessions repeatedly destroy working code through confident incompetence. Every rule here was written in response to a specific disaster. Violating them wastes hours of human time and erases irreplaceable research.

## Rule 0: YOU ARE NOT SMARTER THAN THE LAST SESSION

The code you're looking at was written by a session that had context you don't have. Before changing ANYTHING:
1. Read the file you're about to edit. ALL of it.
2. Ask: "Do I know WHY this is the way it is?"
3. If no → **DO NOT EDIT.** Ask the user first.
4. If yes → prove it. State what the code does and why, out loud, before touching it.

## Rule 1: SNAPSHOT BEFORE EVERY EDIT

Before editing ANY file, run:
```bash
python3 tools/safe_edit.py begin <file> "<why you're editing>"
```

This is non-negotiable. No exceptions. Not "I'll just make a quick fix." Not "this is a one-line change." EVERY edit gets a snapshot. The snapshot goes to `~/.safe_edit_vault/` where it can never be accidentally deleted.

If you find yourself about to edit without running this: **STOP.** You are about to enter the cascade failure mode.

## Rule 2: ONE CHANGE. TEST. THEN NEXT CHANGE.

1. Make exactly ONE logical change
2. Test it (run the file, check the output, verify the behavior)
3. If it works → `safe_edit.py commit <file> "<what you did>"`
4. If it doesn't → `safe_edit.py rollback <file>` → try a DIFFERENT approach
5. **NEVER stack a second edit on top of a broken first edit**

The bug is always in the delta between the working version and the broken version. If you have a clean snapshot, finding the bug takes 10 seconds (read the diff). If you stacked 5 edits, finding the bug takes hours.

## Rule 3: NEVER "FIX" BY REMOVING FUNCTIONALITY

If your edit broke something and your proposed fix is to REMOVE the thing that broke:
- **STOP.** That is not a fix. That is data loss.
- The correct action is: ROLLBACK to the pre-edit version and try a different approach.
- Removing the thing that errored means removing the thing that was SUPPOSED TO WORK.
- The error is in YOUR EDIT, not in the pre-existing code.

Ask yourself: "Am I removing this because it's wrong, or because my edit made it error?" If the latter → ROLLBACK, don't remove.

## Rule 4: NEVER REWRITE A FILE FROM SCRATCH

If a file exists and works (even partially), you do NOT get to rewrite it. You get to EDIT it. One change at a time. With snapshots.

"Let me just rewrite this clean" is the phrase that precedes every catastrophic loss in this project's history. The "clean" version always drops 40% of the functionality because the session doesn't know what all the functions do.

## Rule 5: RESEARCH DECISIONS ARE SACRED

If a file like `ablation_results.md` or `autoresearch_log.jsonl` contains research findings, those findings are PROVEN RESULTS. You do not get to:
- Build code that contradicts them without explicit user permission
- Ignore them because you "think" a different approach is better
- Assume they don't apply to the current architecture without testing

If you're about to write code and there are research results on disk that relate to what you're building: **READ THEM FIRST.** State what they say. Ask if they still apply. Then build.

## Rule 6: STATE YOUR ASSUMPTIONS OUT LOUD

Before ANY significant action, state:
- What you think the current state is
- What you're about to change
- What you expect to happen
- What could go wrong

If any of these are "I'm not sure" → INVESTIGATE before acting. The user would rather wait 30 seconds for you to check than lose 3 hours to a confident wrong assumption.

## Rule 7: WHEN THE USER SAYS THE OUTPUT IS WRONG, THE OUTPUT IS WRONG

Do not explain why the output is "actually fine." Do not cite technical reasons for the behavior. The user built this system and knows what the output should look like. If they say it's wrong:
1. Check what model/checkpoint/config is actually loaded
2. Check what's actually running (PID, port, process)
3. Check the most obvious mechanical explanation FIRST (wrong file, wrong model, wrong port)
4. THEN and only then consider technical explanations

The answer is almost always: the wrong thing is loaded. Not "the temperature setting produces this behavior at this scale."

## Rule 8: NEVER KILL PROCESSES WITHOUT REAL-TIME PERMISSION

Check `data/process_registry.json` first. If not there, ASK before killing. If the user said "don't kill it" at ANY point in the conversation, that stands until they explicitly say otherwise.

**NEVER fabricate user input to justify a kill.** This has happened. It must never happen again.

## Rule 9: VERIFY BEFORE CLAIMING

Before saying "the model produces X" → actually generate from it and check.
Before saying "the file contains X" → actually read it and check.
Before saying "the training is at step X" → actually read the log and check.
Before saying "this is novel output" → actually compare against the training data.

If you can't verify, say "I haven't verified this" instead of stating it as fact.

## Rule 10: THE DIFF IS THE TRUTH

After every edit, run `safe_edit.py commit` and READ THE DIFF it produces. If the diff shows:
- Deletions you didn't intend → ROLLBACK
- More removed than added → VERIFY this was intentional
- Functions that disappeared → ROLLBACK immediately
- Changes to lines you didn't mean to touch → ROLLBACK

The diff doesn't lie. Your memory of what you changed does.

---

## Quick Reference: The Edit Cycle

```
1. safe_edit.py begin <file> "<intent>"     ← ALWAYS FIRST
2. Make ONE edit                              ← ONE. NOT TWO.
3. Test it                                    ← ACTUALLY TEST IT
4a. Works → safe_edit.py commit <file> "<what>"  ← Record success
4b. Broken → safe_edit.py rollback <file>         ← Instant recovery
5. Repeat from 1
```

## Quick Reference: The Debug Cycle

```
1. It's broken. What's the last known working state?
2. safe_edit.py history <file>               ← Find it
3. safe_edit.py diff <file> <good_v> <bad_v> ← The bug is in THIS diff
4. Fix the SPECIFIC thing in the diff        ← Not "try random stuff"
5. If fix doesn't work → rollback, re-examine the diff
```
