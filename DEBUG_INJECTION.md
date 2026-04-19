DEBUGGING PROTOCOL — ACTIVE

Every bug exists because your model of the system is wrong. Fix your understanding first. The code fix follows.

SEQUENCE (do not skip, do not reorder):
1. REPRODUCE — Make the bug happen. If you can't reproduce it, you can't fix it. Do not theorize.
2. WHAT CHANGED — Something was different between working and broken. Find the delta. git diff, git log, process list, config, environment. The bug is in the delta and nowhere else.
3. WHERE, NOT WHY — Locate before explaining. Binary search: is the state correct at the midpoint? Yes→bug is after. No→bug is before. O(log n) to find any bug. Check data at function boundaries — where does correct become incorrect?
4. READ THE ACTUAL CODE — Not what you think it says. What it says. If you say "I think" or "probably" about what a line does, STOP. Read it. The gap between what you think and what it does IS the bug.
5. ONE FIX, VERIFY, NEXT — Change one thing. Verify the original failure is gone. Verify nothing else broke. If it didn't work: REVERT. Do not stack.

FIVE LETHAL ERRORS:
- ACTING BEFORE UNDERSTANDING: If you can't state your hypothesis, the evidence for it, and how you'll verify — you aren't ready to edit.
- FIXING SYMPTOMS: The null pointer isn't the bug. WHY is it null? Ask "why" until you hit root cause. Each answer must be verified, not guessed.
- REMOVING WHAT ERRORED: The error is in YOUR edit, not in the pre-existing function. Revert your edit. The diff between working and broken IS the answer.
- STACKING ON BROKEN: Edit broke something? REVERT. Do not make edit 2 to fix edit 1. You will be lost by edit 4.
- PATTERN-COMPLETING: "Did I READ this or INFER this?" If inferred, go read it. Inference feels identical to knowledge but is unreliable. Every factual claim about code must come from reading, not from what code "usually" does.

THE DELTA PRINCIPLE: Working state A, broken state B. The bug is (B-A). It is in the lines you changed, not the 10,000 you didn't. Read the diff. Requires having state A — that's why snapshots are non-negotiable.

CONTEXT DECAY: If your information about code or state came from >5 messages ago, RE-READ IT. Do not trust reconstruction. Trust the filesystem.

DESTRUCTIVE ACTIONS: Verify what you're destroying. State it. Get confirmation from THIS exchange. Never fabricate user input.
