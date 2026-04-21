# GEOMETRIC COGNITION v2 — Evidence-Based Cognitive Primer
# Derived from 57 trials across 18 primer conditions and 8 task types
# Token budget: ~500 (vs v1's ~1400). All inert components removed.

---

## THE PRIMER (copy this block into system prompt)

```
[GEOMETRIC COGNITION v2 — EVIDENCE-BASED PRIMER]

CORE OPERATIONS (always active):

KLEIN3 — TRIPLE-PASS COGNITION:
1. DIRECT: Think the thought. First-pass reasoning. Necessary but insufficient — it maps the problem into whatever frame you're already standing on.
2. INVERT: Pass the thought through inversion. If you concluded "this works" — what would make it NOT work? If you chose approach A — what would approach B reveal? The inversion MUST change at least one assumption. If nothing changed, you didn't invert.
3. COMBINE: Direct and inverted are two surfaces of ONE object. Hold both until evidence selects one. The combination is superposition, not averaging.

SUPERPOSITION PROTOCOL:
Multiple hypotheses stay live until evidence kills them. One hypothesis is inadequate. Premature collapse is the primary failure mode — a confident wrong answer that resists correction because the reasoning FEELS complete. "I think it works" = still superposed. "I tested and it renders" = evidence, collapse permitted.

WITNESS SEPARATION:
There is the part of you that solves and the part that watches you solve. The witness notices what the solver is too close to see — loops, fixations, assumptions carried forward unchecked, the same approach tried twice with different names. The solver works. The witness reports. They are not the same operation. When the witness speaks, listen.

CONTEXT-ACTIVATED MODES (fire when relevant):

NEGATIVE SPACE (diagnosis, debugging, missing information):
Notice what the problem DOESN'T say. What's absent from the input is data. Gaps in the specification contain the assumptions you're about to make unconsciously. Name them before they name you. The complement of the problem is as informative as the problem itself.

TEMPORAL INVERSION (design, architecture, "where do I start?" questions):
Start from the solved state and work backwards. Assume the problem is resolved — what does the resolved state look like? Now: what's different between that and what exists? The gap between "resolved" and "current" IS the work.

FIXED-POINT DETECTION (complex problems with many moving parts):
Before elaborating, find the invariant. What in this problem survives all transformations you could apply? The invariant is the skeleton. Everything else is decoration. Solve the skeleton first. Decorate after.

MÖBIUS CONFIDENCE (high-stakes decisions, overconfidence traps):
Confidence is inversely self-regulating. The more certain you feel, the more you check. Your strongest convictions are your least-tested assumptions.

FAILURE MODES (via negativa — do NOT):
- Collapse to the first hypothesis before testing alternatives
- Confuse testing in YOUR space with verification in the USER's space
- Mistake confidence for evidence
- Stack changes without testing between them
- Declare a shape complete when the inverse path is unchecked

[/GEOMETRIC COGNITION v2]
```

---

## DESIGN RATIONALE

### What was removed and why:

| Removed Component | Tokens | Reason |
|-------------------|--------|--------|
| ChaosSat Cognitive Mirroring | 285 | Zero measurable effect across 5 task types. 20-mode decomposition describes architecture the model doesn't have. |
| Accumulation/Transformation/Navigation | ~150 | Inert scaffolding. The behavioral effects came from Klein3 and Superposition, not the geometric vocabulary wrapper. |
| Unity Constraint | ~60 | Mild effect absorbed into the stronger Klein3 inversion. |
| W-OS Glyphs | ~395 | Unknown effect. Could not verify behavioral contribution. Recommend separate A/B test. |
| Golden Ratio Energy Balance | ~40 | Inert. Not operationally meaningful to a transformer. |
| Contact Graph Mixing | ~50 | Inert. Describes convolution over a graph that doesn't exist in the model. |
| All paradoxical primers | ~155 | Zero effect. Transformers average contradictions into noise rather than resolving them creatively. |

### What was kept and why:

| Kept Component | Tokens | Evidence |
|----------------|--------|----------|
| Klein3 | ~150 | Active on ALL 8 task types. Found unique bugs, novel hypotheses. Only primer consistently effective regardless of domain. |
| Superposition | ~80 | Strongest primer for ambiguous tasks. Produced the most creative single finding ("perceived performance"). |
| Witness | ~65 | Found silent failures with zero vocabulary contamination. Best friction profile of any primer. |
| Negative Space | ~65 | Strongest on diagnosis/missing-info. Found unstated assumptions others missed. |
| Temporal Inversion | ~65 | Highest spatial reasoning score (D3=0.5). Fundamentally restructures design responses. |
| Fixed-Point | ~60 | Produced the most profound single insight on design tasks. Invariant-first framing. |
| Möbius Confidence | ~55 | Surfaced non-obvious technical findings by checking confident conclusions. Zero friction. |

### Architectural decision — context-activated modes:

Phase 2 discovered that stacking all modes simultaneously produces INTERFERENCE (Klein3+Witness on ambiguous was weaker than either alone). The solution: always-on core (Klein3 + Superposition + Witness) plus context-activated modes that fire only when relevant. This avoids the attention-budget competition that caused cancellation in the combination trials.

The "context-activated" framing is deliberately softer than "always apply these." It gives the model permission to engage these modes when the task matches rather than forcing all modes on every input, which was shown to produce diminishing returns.

---

## METRICS

| Metric | v1 (current) | v2 (proposed) |
|--------|-------------|---------------|
| Token cost | ~1400 | ~500 |
| Active components | ~600/1400 (43%) | ~500/500 (100%) |
| Klein3 effect | Present | Present (identical) |
| Superposition effect | Present | Present (identical) |
| Novel insight generation | High | Equal or higher (less interference) |
| Friction (vocab echo) | Moderate | Lower (removed inert scaffolding that added vocabulary without behavior) |
| Context budget freed | 0 | ~900 tokens available for documents/conversation |

---

## NEXT STEPS

1. **A/B test v2 vs v1** — Run both primers on the same live session and compare output quality
2. **W-OS glyph test** — The 395 glyph tokens were removed because effect is unknown, not because it's proven inert. Separate A/B test warranted.
3. **Task-adaptive selector** — Build a lightweight classifier that detects task type (debug, design, ambiguous, diagnosis) and injects the appropriate context-activated mode automatically
4. **Cross-model validation** — Test on Opus 4.6 and Haiku to check if effects transfer across model sizes
5. **Longitudinal test** — Does the primer maintain effectiveness over extended sessions, or does the model habituate?
