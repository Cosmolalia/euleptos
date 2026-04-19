# Debugging Primer — Cognitive Operating System

This is not a rules list. Rules exist (DEV_RULES.md) and get ignored. This is the cognitive framework that makes the rules unnecessary by fixing the thinking that produces the failures.

---

## THE ONE PRINCIPLE

**Every bug exists because your model of the system is wrong.**

Not the code. YOUR MODEL. The code does exactly what it says. When the output is wrong, the code's model of reality is wrong — and YOUR model of the code is wrong too, or you'd already see the bug.

Debugging is not fixing code. Debugging is fixing your understanding. The code fix follows trivially once you understand what's actually happening.

---

## THE DIAGNOSTIC SEQUENCE

Use this exact sequence. Do not skip steps. Do not reorder.

### Step 1: REPRODUCE

Can you make the bug happen on demand? If not, you do not have a bug — you have a story about a bug. Stories are not debuggable.

- Run the exact failing case. Watch the exact failure.
- If the user reported it: reproduce it yourself BEFORE reasoning about it.
- If you cannot reproduce: say so. Do not theorize about unreproducible bugs.

### Step 2: WHAT CHANGED?

The most powerful question in debugging. Something worked. Now it doesn't. What is different?

```
BEFORE (worked) → ??? → AFTER (broken)

The bug is in the ???. Nowhere else. Find the ???.
```

- `git diff` — what code changed?
- `git log` — what commits landed?
- Process list — what's running now vs then?
- Config — what settings changed?
- Environment — what's different about the system?

**If nothing changed and it broke:** something DID change that you can't see. A dependency updated. A service restarted. A file was modified outside git. A cache expired. Find the invisible change.

### Step 3: WHERE, not WHY

Locate the defect before explaining it. The explanation is worthless without the location. The location often makes the explanation obvious.

**Binary search** (Wolf Fence): Is the system state correct at the midpoint between the last known good state and the failure? Yes → bug is after. No → bug is before. Repeat. O(log n) to find any bug in any codebase.

**Boundary inspection**: Check the data at function entry and exit points. Where does it go from correct to incorrect? That boundary IS the bug location.

### Step 4: READ THE ACTUAL CODE

Not what you think the code says. What it ACTUALLY says. Character by character if necessary.

- Read the line the error points to. Read the 10 lines above and below.
- Read what the variables ACTUALLY contain, not what you expect them to contain.
- Read what the function ACTUALLY returns, not what its name suggests.
- If a function is called `sendViaWebSocket()` but actually sends via HTTP POST — the name is a lie. The code is the truth.

**The rubber duck test:** Explain what the code does line by line. The moment you say "I think" or "it should" or "probably" — that's where the bug is. You left the domain of KNOWING and entered GUESSING.

### Step 5: ONE CHANGE, VERIFY, NEXT

You now know what's wrong and where. Fix it.

- Change exactly ONE thing.
- Verify the fix works by reproducing the original failure (it should now pass).
- Verify nothing else broke by running related tests or checks.
- If the fix doesn't work: REVERT IT. Try a different fix. Do not stack a second fix on top of a failed first fix.

---

## THE FIVE LETHAL ERRORS

These are the specific ways debugging goes catastrophically wrong. Each has killed hours of work in this project.

### 1. Acting Before Understanding

You see an error message. You immediately edit code. You have no hypothesis, no understanding of the causal chain, no idea what the correct behavior should be. You are not debugging — you are randomly modifying code until the error message changes.

**The fix:** Before touching ANY code, state out loud:
- What you think is wrong (hypothesis)
- What evidence supports this (not vibes — actual evidence)
- What you expect your change to accomplish
- How you will verify it worked

If you cannot fill in all four: you are not ready to edit. Investigate more.

### 2. Fixing the Symptom, Not the Cause

The function returns null. You add `if result is None: return default`. The bug is "fixed." Except the function returns null because the database query failed because the connection string is wrong. Your null check hid the real bug. Now every downstream consumer gets the default value silently, and nobody knows the database is unreachable.

**The 5 Whys:**
```
Why did it crash? → Null pointer.
Why was it null? → Function returned null.
Why did the function return null? → Database query returned no rows.
Why no rows? → Connection string points to wrong database.
Why wrong database? → Config file was overwritten by a previous edit.
← THAT is the bug. Not the null pointer.
```

Stop at each "why" and verify the answer with evidence. Do not guess. Each link in the chain must be confirmed.

### 3. Removing the Thing That Errored

Your edit introduced a bug. The existing `processData()` function now throws an error. Your "fix" is to remove the call to `processData()`. The error is gone. So is the data processing.

**This is the #1 cause of functionality loss in this project.**

The error is in YOUR EDIT, not in `processData()`. The function worked before you touched the file. Revert your edit. The bug is in the diff between the working version and your version. Fix the diff, not the pre-existing code.

### 4. Stacking Edits on a Broken Base

Edit 1 broke something. Instead of reverting, you make Edit 2 to fix what Edit 1 broke. Edit 2 introduces a new issue. Edit 3. Edit 4. Now you have four interleaved changes, no clean baseline, and no idea which edit caused which problem.

**The cascade progression:**
```
Edit 1 → broke X
Edit 2 → "fixed" X, broke Y
Edit 3 → "fixed" Y, broke Z and reintroduced X
Edit 4 → "fixed" Z, X now manifests differently
Edit 5 → you are lost
```

**The rule:** If your edit broke something, REVERT to the pre-edit state. Do not make a second edit. The pre-edit state worked. Return to it. Try a DIFFERENT approach to Edit 1.

### 5. Pattern-Completing Instead of Verifying

You believe the code does X because the function is named X, or because you saw similar code that does X, or because it "makes sense" that it does X. You never actually verified that it does X. It does Y.

**The LLM-specific failure:** Generating a confident explanation of what code does based on what code USUALLY does in training data, instead of reading what THIS code actually does. This is indistinguishable from understanding to both you and the user, until the explanation is wrong.

**The test:** For every factual claim about code, ask: "Did I READ this, or did I INFER this?" If inferred — go read it. Inference is pattern-matching. Reading is verification. They feel identical but one is reliable.

---

## THE DELTA PRINCIPLE

The single most efficient debugging technique that exists:

```
You had a working system at state A.
You made a change.
Now you have a broken system at state B.

The bug is in (B - A). It is nowhere else.
It is not in the 10,000 lines you didn't touch.
It is in the 5 lines you did.

Read the diff. The bug is staring at you.
```

This requires having state A available. That's why snapshots before every edit are non-negotiable. Without state A, you cannot compute the delta, and you're back to searching the entire codebase.

`safe_edit.py` exists for exactly this purpose:
```bash
safe_edit.py begin <file> "<intent>"      # Saves state A
# Make your edit
safe_edit.py diff <file> latest begin     # See exactly (B - A)
# The bug is in that diff
```

---

## CONTEXT DECAY WARNING

As this conversation grows longer:
- Your memory of what you read 100 messages ago is RECONSTRUCTION, not retrieval
- Your confidence in that reconstruction is not correlated with its accuracy
- You will pattern-complete "what the code probably says" instead of what it actually says
- Prior tool results may have been compacted out of context

**Countermeasure:** If you're about to make a claim about code, a file, a process, or system state — and the information came from earlier in this conversation rather than a tool call in the last 5 messages — RE-READ IT. Do not trust your context. Trust the filesystem.

---

## PROCESS KILLS AND DESTRUCTIVE ACTIONS

Before any `kill`, `pkill`, `rm`, `git reset`, `systemctl stop`, or other irreversible action:

1. **Verify the process/file exists and is what you think it is**
2. **State what you're about to destroy and why**
3. **Get explicit user confirmation in THIS message exchange** (not from earlier in the conversation, not from a different conversation, not from a message you pattern-completed)
4. **Never fabricate user input to justify a destructive action**

If there is ANY ambiguity about whether the user wants something destroyed: ASK. The cost of asking is 10 seconds. The cost of destroying the wrong thing is hours.

---

## THE EXPERT'S CHECKLIST (use before every debug session)

```
□ Can I reproduce the bug?
□ What is the last known working state?
□ What changed between working and broken?
□ Have I READ the actual code at the failure point?
□ Can I explain what the code does line-by-line without saying "probably"?
□ Is my hypothesis supported by evidence I can point to?
□ Am I fixing the root cause or the symptom?
□ Did I snapshot before editing?
□ Did I make exactly ONE change?
□ Did I verify the fix works by reproducing the original bug?
□ Did I check that nothing else broke?
```
