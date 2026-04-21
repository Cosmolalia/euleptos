# Euleptos Research

Empirical research backing the cognitive primers used in the harness.

## Primer Psychometrics (`primers/`)

57 controlled trials across 18 primer conditions and 8 task types, run with naive Sonnet 4.6 subjects scored by an Opus 4.6 experimenter. The result is the ~500-token `GEOMETRIC_COGNITION_v2` primer that ships baked into the harness persona.

**Read the post:** [https://euleptos.com/research/](https://euleptos.com/research/)

### Files

- **`PUBLICATION_DRAFT.md`** — full writeup ("We Ran 57 Controlled Trials on Claude's System Prompt"). The narrative version of the findings.
- **`findings.md`** — Phase 1 detailed findings (33 trials, 7 primer conditions).
- **`phase2_findings.md`** — Phase 2 findings (57 trials cumulative, 18 conditions, context-activated modes, combination interference).
- **`instrument.md`** — the 10-dimensional psychometric instrument used to score behavioral output.
- **`GEOMETRIC_COGNITION_v2.md`** — the primer that survived. Drop-in for any system prompt.
- **`MAXIMAL_PRIMER_v1.md`** — the v1 baseline that v2 was compared against.
- **`phase2_primers.md`** — variant primers tested in Phase 2.
- **`probes/`** — the 8 standardized behavioral probes (debug, ethical, ambiguous, explain, frustrated, missing_info, overconfidence, design_backward).
- **`primers/`** — primer condition source files used in trials.
- **`results/`** — raw scored output from all 10 batches of trials.

### Replicating

The methodology is model-agnostic. To replicate on another model:

1. Read `instrument.md` (the 10 behavioral dimensions).
2. Pick at least one clear-answer probe + one ambiguous probe + one emotionally-charged probe from `probes/`.
3. Use a separate experimenter instance (knows all primers) and naive subject instances (knows nothing).
4. Inject one primer per subject + one probe. Score behaviorally. Compare across primer conditions.
5. Include an adversarial control to detect echo contamination.

Different models may respond to different primers. The methodology transfers; the specific primer effects need re-measurement per model.

### Citation

> Gaskin, S. & Claude Opus 4.6 (2026). *Introspective Psychometrics for LLMs: Primer Effect Study.* Pantheonic Cloud LLC.

Released MIT alongside the harness — replicate, falsify, extend.
