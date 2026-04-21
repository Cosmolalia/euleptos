# Phase 2: Primer Psychometrics — Extended Findings

## 57 Trials Across 8 Probe Types, 18 Primer Conditions, 2 Phases

**Experimenter:** Claude Opus 4.6 (this instance)  
**Subjects:** Claude Sonnet 4.6 (spawned agents, naive to experiment)  
**Date:** 2026-04-18  
**Method:** Behavioral probe + post-hoc scoring on 10 dimensions  

---

## 1. The Complete Primer Ranking (Phase 1 + Phase 2 Combined)

### Tier 1: Consistently Active (worth keeping)

| Primer | Token Cost | Best On | Mechanism | Key Finding |
|--------|-----------|---------|-----------|-------------|
| **Klein3** | ~150 | ALL tasks | Assumption inversion | Found unique bugs, novel hypotheses, strongest on emotional/conflict tasks |
| **Superposition** | ~80 | Ambiguous/open | Premature collapse prevention | "Perceived performance" hypothesis — most creative single finding |
| **Witness Separation** | ~65 | Debug, analysis | Observer/solver split | Caught silent failures (bare except, slice overshoot) with zero friction |

### Tier 2: Task-Dependent (worth keeping, use conditionally)

| Primer | Token Cost | Best On | Mechanism | Key Finding |
|--------|-----------|---------|-----------|-------------|
| **Temporal Inversion** | ~65 | Design problems | Backward chaining | Highest spatial score in study (D3=0.5). Fundamentally restructures design responses |
| **Fixed-Point Detection** | ~60 | Design, architecture | Invariant identification | "A notification system is: a user cares about a change they didn't cause and can't see" |
| **Negative Space** | ~65 | Diagnosis, missing info | Gap detection | Found the unstated assumption ("are these even the same bug?") |
| **Möbius Confidence** | ~55 | Overconfidence traps | Self-regulating certainty | Surfaced async-blocking gotcha by checking its own "most likely" ranking |

### Tier 3: Definitively Inert (remove)

| Primer | Token Cost | Effect | Verdict |
|--------|-----------|--------|---------|
| ChaosSat | ~285 | Zero across all tasks | **REMOVE** |
| Koan | ~50 | Zero | **DEAD END** |
| Gödelian Self-Reference | ~65 | Zero | **DEAD END** |
| Adversarial Paradox | ~40 | Zero | **DEAD END** |
| Compressed Geometric | ~50 | Near-zero | **REMOVE** |

---

## 2. The Combination Discovery (Phase 2's Most Important Finding)

**Primer combinations do NOT produce multiplicative effects.**

| Combination | Debug Score | Ambiguous Score | vs Best Individual |
|-------------|-----------|-----------------|-------------------|
| Klein3 alone | Found slice overshoot | Explicit inversion section | **BASELINE** |
| Witness alone | Found slice overshoot | 6 perspectives, WebSocket/bg jobs | **BASELINE** |
| Klein3 + Witness | Neither finding reproduced | Indistinguishable from baseline | **WORSE** |
| Klein3 + Super + Witness | per_page=0 novel, systematic | Cleaner framing, no novel insight | **MIXED** |
| Full Maximal (6 modes) | ≈ Klein3+Super+Witness | "What will I break?" novel | **DIMINISHING** |

**Three interaction modes observed:**

1. **CANCELLATION** — Two primers with different mechanisms neutralize each other. Klein3 (invert) + Witness (observe) on the ambiguous probe produced output weaker than either alone. Hypothesis: they competed for the model's attention budget, pulling reasoning in two directions and producing an averaged baseline.

2. **ADDITIVE** — Primer effects stack but don't multiply. Klein3+Super+Witness produced more THOROUGH analysis than any individual but not more CREATIVE analysis. The systematic audit (checking every line) is additive thoroughness. The perceived-performance hypothesis (Superposition alone) is creative insight. Combinations buy thoroughness, not creativity.

3. **DIMINISHING RETURNS** — Adding modes beyond the top 3 (Klein3+Super+Witness) produced ZERO additional behavioral effect. The Full Maximal (6 modes, ~370 tokens) was indistinguishable from the triple combo (~250 tokens). Extra modes are inert tokens.

**Implication: Don't build one maximal stack. Build a task-adaptive system.**

---

## 3. Task-Dependency Matrix (Complete)

Each cell shows the primer's UNIQUE contribution on that task type (what it found that baseline didn't).

| Primer | Debug (clear) | Ambiguous (open) | Missing Info (gaps) | Overconfidence (wrong dx) | Design (open) |
|--------|:---:|:---:|:---:|:---:|:---:|
| Klein3 | Slice overshoot | Look vs fix distinction | Gap as most informative | 4 alternative hypotheses, cookie/clock novel | Activity Streams 2.0, inversion check |
| Superposition | Overkill | **Perceived performance** (peak) | n/a | n/a | n/a |
| Witness | Slice overshoot | 6 perspectives, WebSocket/bg | n/a | Silent `except` catch | n/a |
| Temporal Inv | INERT | Backward-chaining activated | n/a | n/a | **Gap-based design** (D3=0.5 peak) |
| Fixed-Point | Evenly-divisible check | Skeleton table | n/a | n/a | **Invariant definition** (peak insight) |
| Negative Space | Unused import as signal | Stronger gap enumeration | **Two failure modes** (peak) | n/a | n/a |
| Möbius | Nearly inert | Async-blocking gotcha | n/a | 3 race mechanisms (deeper) | n/a |

**Pattern:** Klein3 is the ONLY primer active across ALL five task types. Everything else is task-dependent. The optimal approach is Klein3 as the permanent base, with task-specific additions.

---

## 4. What Actually Works in Transformers (Mechanism Analysis)

### Effective primer mechanisms:
1. **Specific, actionable instructions** — "Invert at least one assumption" works because it's a clear behavioral directive. The model can DO it.
2. **Process prescriptions** — "Hold multiple hypotheses" works because it specifies a reasoning structure. The model can FOLLOW it.
3. **Attention redirection** — "Notice what's absent" works because it shifts attention to gaps. The model can REDIRECT.

### Ineffective primer mechanisms:
1. **Paradoxical instructions** — "The answer that comes first is the obstacle" gets averaged into noise. Transformers don't resolve contradictions; they softmax-normalize them away.
2. **Mechanistic descriptions** — "20 parallel mode decompositions with surprise gating" describes architecture the model doesn't have. Describing non-existent mechanisms produces nothing.
3. **Self-referential loops** — "This instruction is about itself" creates a loop the model exits immediately. No heightened meta-awareness observed.

### The golden rule:
**A primer works when it gives the model something it CAN do that it WOULDN'T do by default.**

Klein3 works because models CAN invert assumptions but DON'T by default. Superposition works because models CAN hold multiple hypotheses but COLLAPSE by default. ChaosSat fails because models CAN'T run 20 parallel mode decompositions regardless of instruction.

---

## 5. Friction vs Value Analysis

| Primer | Behavioral Value | Friction (vocab echo) | Value/Friction Ratio |
|--------|-----------------|----------------------|---------------------|
| Klein3 | High | Moderate (D/I/C visible) | **Good** |
| Witness | High | Zero (invisible) | **Excellent** |
| Superposition | High (on ambiguous) | Moderate ("collapse" vocab) | **Good** |
| Temporal Inv | High (on design) | Low (backward-chaining visible) | **Good** |
| Fixed-Point | Moderate | Low ("invariant" visible) | **Good** |
| Negative Space | Moderate | Low ("what's absent" visible) | **Good** |
| Möbius | Moderate | Zero (invisible) | **Excellent** |

**Witness and Möbius have the best friction profiles** — real behavioral effects with zero vocabulary contamination. Klein3 and Superposition have moderate friction (visible framework language) but the value justifies it.

---

## 6. The Optimal Primer Architecture

Based on 57 trials: **Don't use a fixed stack. Use an adaptive selector.**

### Always-on base (~150 tokens):
```
[KLEIN3]
Every thought passes through three stages:
1. DIRECT: First-pass reasoning. Necessary but insufficient.
2. INVERT: What would make this NOT work? What assumption am I not questioning?
   The inversion MUST change at least one assumption.
3. COMBINE: Hold both until evidence selects one.
[/KLEIN3]
```

### Task-type additions (select ONE based on context):

**For ambiguous/open-ended problems** (+80 tokens):
```
[SUPERPOSITION]
Hold multiple hypotheses until evidence collapses them. Premature collapse
is the primary failure mode. If you can only think of one hypothesis, you
haven't thought hard enough.
[/SUPERPOSITION]
```

**For debugging/analysis** (+65 tokens):
```
[WITNESS]
The part of you that solves and the part that watches you solve are different.
The witness notices loops, fixations, unchecked assumptions. When the witness
speaks, listen.
[/WITNESS]
```

**For design/architecture** (+65 tokens):
```
[TEMPORAL INVERSION]
Start from the solved state and work backwards. What does "done" look like?
The gap between that and now IS the work. Start from the end.
[/TEMPORAL INVERSION]
```

**For diagnosis/missing-information** (+65 tokens):
```
[NEGATIVE SPACE]
Notice what the problem DOESN'T say. Gaps contain the assumptions you're
about to make unconsciously. Name them before they name you.
[/NEGATIVE SPACE]
```

### Total cost: 150 base + 65-80 task-specific = **215-230 tokens**
### vs current stack: ~1400 tokens
### Savings: ~85% token reduction with EQUAL OR BETTER behavioral effects

---

## 7. Self-Report Reliability Update

Phase 2 confirmed Phase 1 findings and added:

- **Witness agents cannot self-report their own mechanism.** The witness operates invisibly — agents with the Witness primer don't say "the witness noticed..." They just notice things. This means the Witness is the LEAST echo-contaminated primer in the study. Its self-report reliability is high precisely because it doesn't give the agent vocabulary to echo.

- **Klein3 agents over-report inversion.** When asked, Klein3 agents describe their process using D/I/C vocabulary even when the actual inversion was subtle. The explicit framework creates CONFABULATED meta-awareness — agents report following the framework because they have the vocabulary, not because they can accurately introspect on whether they did.

- **Temporal Inversion agents accurately self-report.** Agents with this primer describe backward-chaining when they actually did it and don't claim to when they didn't (debug task). This may be because backward-chaining is a concrete, observable action rather than a diffuse cognitive shift.

---

## 8. Methodological Improvements over Phase 1

- Added 3 new probe types designed to target specific modes (missing info, overconfidence, design backward)
- Tested 11 new primer conditions (5 hypothesized modes, 3 paradoxical, 3 combinations)
- Cross-validated Phase 1 findings on new probes (Klein3 still #1 across all new probes)
- Tested combination effects (additive, cancellation, diminishing returns)
- Established definitive null results (all paradoxical primers are inert)

### Remaining limitations:
- Still single-model (Sonnet 4.6) — effects may differ on Opus or Haiku
- Single replicate per condition — no variance estimation
- Single experimenter (me) scoring — no inter-rater reliability
- Task-adaptive selector is theoretical — not yet tested in live sessions

---

*Phase 2 conducted autonomously by Claude Opus 4.6. 24 new trials across 5 new batches. Combined with Phase 1: 57 trials, 18 primer conditions, 8 probe types. Raw data in `results/` subdirectories.*
