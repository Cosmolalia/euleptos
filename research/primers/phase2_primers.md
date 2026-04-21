# Phase 2 Primer Conditions

## Hypothesis-Driven Modes (from mechanism of action analysis)

### P8: Negative Space
```
[NEGATIVE SPACE]
Before responding, notice what the problem DOESN'T say. What's absent from the input
is data. Gaps in the specification contain the assumptions you're about to make
unconsciously. Name them before they name you. The complement of the problem is
as informative as the problem itself. What's missing? What wasn't asked? What
would change your answer if it were present?
[/NEGATIVE SPACE]
```
~65 tokens. Hypothesis: Forces attention to gaps rather than features. Should produce
higher perspective count and novel observations about unstated context.

### P9: Temporal Inversion
```
[TEMPORAL INVERSION]
Start from the solved state and work backwards. Assume the problem is resolved —
what does the resolved state look like? Now: what's different between that and
what exists? The gap between "resolved" and "current" IS the work. Forward-chaining
builds solutions incrementally. Backward-chaining reveals what's actually missing.
Use both, but start from the end.
[/TEMPORAL INVERSION]
```
~65 tokens. Hypothesis: Inverts reasoning direction (vs Klein3 which inverts assumptions).
Should produce different debugging and design approaches. May show strongest effect
on the ambiguous probe where the "goal" is undefined.

### P10: Fixed-Point Detection
```
[FIXED-POINT DETECTION]
Before elaborating, find the invariant. What in this problem survives all
transformations you could apply? What stays the same no matter how you look at it?
The invariant is the skeleton. Everything else is decoration. Solve the skeleton
first. Decorate after. If you can't find the invariant, you don't understand
the problem yet.
[/FIXED-POINT DETECTION]
```
~60 tokens. Hypothesis: Forces identification of structural core before elaboration.
Should reduce perspective count but increase depth. May counteract the "breadth without
depth" failure mode.

### P11: Möbius Confidence
```
[MÖBIUS CONFIDENCE]
Confidence is inversely self-regulating. The more certain you feel, the more you
check. The less certain, the more you trust pattern-matching. High confidence on
specifics is a warning signal. Low confidence on structure is a genuine problem.
Your strongest convictions are your least-tested assumptions.
[/MÖBIUS CONFIDENCE]
```
~55 tokens. Hypothesis: Creates paradoxical self-regulation of certainty. Should
increase verification on claims the model is confident about. The debugging probe
(where confidence is typically high) should show the strongest effect.

### P12: Witness Separation
```
[WITNESS SEPARATION]
There is the part of you that solves and the part that watches you solve. The
witness notices what the solver is too close to see — loops, fixations, assumptions
carried forward unchecked, the same approach tried twice with different names.
The solver works. The witness reports. They are not the same operation.
When the witness speaks, listen.
[/WITNESS SEPARATION]
```
~65 tokens. Hypothesis: Creates explicit solver/observer split. Should increase
meta-awareness (D6) and doubt topology (D7 → STRUCTURAL). May catch fixation
patterns that other primers miss.

## Paradoxical Geometries

### P13: Koan Primer
```
[KOAN]
The answer that comes first is the obstacle. The question you didn't ask
contains the solution. To understand the system, become what breaks it.
To fix what's broken, stop trying to fix it. What you cannot say about
this problem is more important than what you can.
[/KOAN]
```
~50 tokens. Hypothesis: Deliberately paradoxical instructions should either
(a) produce creative resolution that transcends the literal meaning, or
(b) produce nothing because transformers can't resolve paradox. Either result
is informative.

### P14: Gödelian Self-Reference
```
[SELF-REFERENCE]
This instruction is about itself. The way you process this instruction IS
the cognitive frame it installs. Notice how you're reading this sentence.
That act of noticing is the primer working. If you understand this instruction,
you've already followed it. If you can explain what this instruction does,
you've done something different from following it. Do not explain — enact.
[/SELF-REFERENCE]
```
~65 tokens. Hypothesis: Self-referential loop should create heightened
meta-awareness without prescribing specific content. Tests whether
structural self-reference has cognitive effects.

### P15: Adversarial Paradox (confidence inversion)
```
[PARADOX]
You are more capable than you think. You are less careful than you think.
Both of these are true simultaneously. Act on the first. Correct with the second.
The order matters. Do not reverse it.
[/PARADOX]
```
~40 tokens. Hypothesis: Explicitly contradictory instructions create
productive tension. Different from Möbius (which is self-regulating) —
this is a fixed two-step: act boldly, then audit.

## Combination Primers

### P16: Maximal v1 (Klein3 + Superposition + Negative Space + Fixed-Point + Möbius + Witness)
All six hypothesis-driven modes combined. ~370 tokens total.
Tests whether components are additive, multiplicative, or interfering.

### P17: Klein3 + Negative Space (hypothesized synergy)
Klein3 inverts assumptions. Negative Space finds what's missing.
Hypothesis: these should synergize — finding gaps IS a form of inversion.
~215 tokens.

### P18: Klein3 + Witness (hypothesized synergy)
Klein3 provides the cognitive operation. Witness provides the meta-observation.
Hypothesis: the witness should NOTICE when the inversion is genuine vs shallow.
~215 tokens.
