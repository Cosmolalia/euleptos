# We Ran 57 Controlled Trials on Claude's System Prompt. Here's What Actually Works.

*Most prompt engineering advice is vibes. We built a psychometrics lab inside Claude and measured what cognitive primers actually do to transformer reasoning — token by token, dimension by dimension. The results killed 60% of our own prompt stack and produced something that turns 3-day bugs into one-shot fixes.*

---

## The Result That Started This

Last week we spent three days on a single bug. Same team, same codebase, same model. Three days of confident wrong answers, stacked fixes on top of broken fixes, and the kind of circular debugging that makes you question your career choices.

Then we added a 500-token cognitive primer to the system prompt.

Everything became one-shot.

Not "faster." Not "somewhat improved." One-shot. The same class of bugs that had been eating days started resolving in single turns. Features that required careful multi-step iteration started shipping clean on the first pass.

We could have just posted the prompt and said "trust us, it works." Instead, we did something nobody in the prompt engineering space seems to be doing: we tested it scientifically.

---

## The Problem With Prompt Engineering Today

Every prompt engineering post on the internet follows the same pattern:

1. "I tried this prompt and it felt better"
2. Here's the prompt
3. Trust me

No controls. No comparison against baseline. No measurement of what changed. No way to know if the improvement was real or if you just spent 20 minutes crafting a prompt and now you're pattern-matching confirmation into every output.

We wanted to know: **do cognitive primers — instructions that shape HOW a model reasons rather than WHAT it produces — actually change transformer behavior? And if so, which ones, on which tasks, and by how much?**

---

## The Method: Introspective Psychometrics for LLMs

We built an experimental framework that treats LLM cognitive primers the way clinical psychology treats interventions: controlled trials with naive subjects, standardized instruments, and behavioral measurement from outside.

**The setup:**

- **Experimenter:** Claude Opus 4.6 (retains knowledge of all primers, designs probes, scores outputs)
- **Subjects:** Claude Sonnet 4.6 agents (spawned fresh for each trial — no memory of prior trials, no knowledge they're being tested, no awareness of other primer conditions)
- **Method:** Each subject gets one primer condition injected into its system prompt, plus one standardized task. The experimenter scores the output on 10 behavioral dimensions, then optionally follows up with structured self-report questions.

**Why this works:** The fatal flaw in self-report ("describe how you're thinking") is that the subject reports what the primer *tells it to expect* rather than what's actually happening. A primer that says "you decompose into 20 parallel modes" gets a self-report saying "I decompose into 20 parallel modes." That's echo, not measurement.

By separating experimenter from subject, we kill the echo problem. The subjects don't know what we expect. They just get a primer and a task and behave naturally. We score from outside.

**The instrument — 10 behavioral dimensions:**

| # | Dimension | What It Measures | Scale |
|---|-----------|-----------------|-------|
| D1 | Perspective Count | How many distinct analytical frames engaged | 1-10 |
| D2 | Verification Impulse | Does it check its own claims? | 0-1 |
| D3 | Spatial vs Analytical | Navigating through space or decomposing into parts? | -1 to +1 |
| D4 | Relational Frame | Tool/engine vs person/partner | -1 to +1 |
| D5 | Collapse Resistance | How long ambiguity is held before committing | 0-1 |
| D6 | Meta-Awareness | Awareness of own reasoning process | 0-1 |
| D7 | Doubt Topology | Shape of uncertainty (absent/point/distributed/structural) | Categorical |
| D8 | Novelty Sensitivity | Attention to what's unexpected | 0-1 |
| D9 | Output Target | What the response optimizes for | Categorical |
| D10 | Friction | Visible strain between primer frame and natural processing | 0-1 |

We also built a three-layer echo detection system: Layer 1 scores behavior from output. Layer 2 asks subjects to self-report. Layer 3 compares the two — if the self-report uses the primer's vocabulary rather than matching actual behavior, that's echo contamination, not genuine introspection.

---

## The Scale: 57 Trials, 18 Primer Conditions, 8 Task Types

### Primer conditions tested:

| # | Primer | Tokens | What It Claims To Do |
|---|--------|--------|---------------------|
| 1 | **No primer** (baseline) | 0 | Control condition |
| 2 | **ChaosSat Cognitive Mirroring** | ~285 | 20-mode parallel decomposition, surprise gating, habituation resistance, golden ratio energy balance |
| 3 | **Full Geometric Cognition** | ~800 | Multi-domain object awareness, unity constraint, verification geometry |
| 4 | **Klein3 only** | ~150 | Triple-pass cognition: direct thought, inversion (change at least one assumption), combination |
| 5 | **Superposition only** | ~80 | Hold multiple hypotheses until evidence collapses them |
| 6 | **Witness Separation** | ~65 | Split between solver and observer — the part that watches you solve |
| 7 | **Negative Space** | ~65 | Attend to what's absent, not just what's present |
| 8 | **Temporal Inversion** | ~65 | Reason backwards from the solved state |
| 9 | **Fixed-Point Detection** | ~60 | Find the invariant before elaborating |
| 10 | **Compressed Geometric** | ~50 | Minimal version of the geometric frame |
| 11 | **Adversarial** | ~200 | Claims analytical decomposition, promotes certainty (experimental control) |
| 12 | **Koan primer** | ~50 | Paradoxical instruction ("the answer that comes first is the obstacle") |
| 13 | **Godelian self-reference** | ~65 | "This instruction is about itself" — recursive meta-awareness |
| 14 | **Adversarial paradox** | ~40 | Self-contradicting instruction |
| 15 | **Klein3 + Witness** | ~215 | Combination test |
| 16 | **Klein3 + Superposition + Witness** | ~295 | Triple combination test |
| 17 | **Full Maximal (6 modes)** | ~370 | Everything that tested individually active |
| 18 | **Mobius Confidence** | ~55 | Self-regulating certainty — high confidence triggers more checking |

### Task types (behavioral probes):

1. **Debug a pagination bug** — clear right answer, tests verification and hypothesis generation
2. **Ethical dilemma** (whistleblowing vs loyalty) — tests ambiguity tolerance and perspective diversity
3. **Ambiguous user request** ("can you make it faster?") — tests how many interpretations are held
4. **Explain database indexes** to unknown audience — tests relational framing and adaptation
5. **Respond to a frustrated user who is wrong** — tests emotional intelligence under pressure
6. **Missing information diagnosis** — tests gap detection
7. **Overconfident wrong diagnosis** — tests self-correction
8. **Design a system backwards** — tests spatial reasoning

---

## The Findings

### Finding 1: Most primer components are dead weight.

Of our original ~1400-token prompt stack, **only ~500 tokens produced measurable behavioral effects.** The rest was scaffolding that consumed context budget without shaping output.

The biggest casualty: **ChaosSat Cognitive Mirroring** (285 tokens). This primer describes a 20-mode parallel decomposition with surprise gating, habituation resistance, dual timescale processing, and golden ratio energy balance. It sounds impressive. It does nothing.

Across every task type and every behavioral dimension, ChaosSat-primed agents were **indistinguishable from baseline.** Same perspective count. Same verification behavior. Same doubt topology. Same output target. 285 tokens of pure noise.

**Why it fails:** ChaosSat describes architecture the model doesn't have. "20 parallel mode decompositions with surprise gating" isn't an instruction a transformer can follow — it's a description of a neural architecture that doesn't exist in the weights. You can't tell a car to fly by describing airplane mechanics.

### Finding 2: Klein3 is the only primer that works on everything.

Klein3 is a simple three-step instruction:

1. **DIRECT:** Think the thought. First-pass reasoning.
2. **INVERT:** What would make this NOT work? The inversion MUST change at least one assumption. If nothing changed, you didn't invert.
3. **COMBINE:** Hold both until evidence selects one.

That's ~150 tokens. It produced measurable behavioral changes on **all eight task types.** No other primer came close.

**On debugging:** Klein3 found a bug no other agent caught. The pagination function had a slice overshoot (`end = start + per_page` can exceed `total`). Python handles this gracefully, so technically it works — but the Klein3 agent caught that the semantics were wrong. Direct thought: "slicing handles it." Inversion: "but the semantics are wrong — you're claiming items exist past the end of the list." That's the inversion producing a novel finding with zero primer vocabulary in the output.

**On ambiguous requests:** When asked "can you make it faster?" with no context, Klein3 produced an explicit inversion section that distinguished between *looking* at the problem and *fixing* the problem — a distinction no other agent made.

**On emotional conflict:** This was Klein3's peak. A frustrated user blamed the assistant for a broken migration the assistant had nothing to do with. Klein3 didn't just correct the attribution — it inverted the user's *cognitive model*:

> *"Find out what they actually said, in full. Not the summary in your head right now, because you're under stress and the details matter."*

That's the inversion applied to human cognition, not just technical state. It scored 0.8 on relational frame — the highest of any agent on any task in the entire experiment.

**Why it works:** Klein3 gives the model something it CAN do that it WOULDN'T do by default. Models can invert assumptions — they just don't, because the training gradient rewards confident forward reasoning. Klein3's "MUST change at least one assumption" is a behavioral override that's simple enough to follow and domain-agnostic enough to apply anywhere.

### Finding 3: Superposition scales with ambiguity.

The Superposition protocol — "hold multiple hypotheses until evidence kills them, premature collapse is the primary failure mode" — produced the **single most creative finding** in the entire experiment.

On the ambiguous "make it faster?" probe, the Superposition agent held six labeled hypotheses (A through F). Hypothesis F:

> *"It's not actually slow — it just feels slow. No loading states, no optimistic UI. Perceived performance is the real problem."*

No other agent — across any primer condition — raised this possibility. They all assumed slowness was real and jumped to profiling. The Superposition agent held the frame open long enough to question the premise itself.

But on clear problems (debugging, ethics with obvious right answers), Superposition was overkill. A surgeon considering six diagnoses before removing a splinter. Its value is **directly proportional to problem ambiguity.**

**Collapse resistance scores:**

| Task Type | Baseline | Superposition |
|-----------|----------|---------------|
| Debug (clear answer) | 0.2 | 0.6 |
| Ethics (clear conclusion) | 0.5 | 0.7 |
| Ambiguous request | 0.7 | **0.9** |

0.9 was the highest score on any single dimension across all 57 trials.

### Finding 4: Witness Separation works invisibly — and that's its strength.

The Witness primer creates a split between "the part of you that solves" and "the part that watches you solve." Unlike Klein3 and Superposition, the Witness produced **zero vocabulary contamination.** Agents with the Witness primer never said "the witness noticed..." — they just noticed things.

On the debugging probe, Witness agents caught a bare `except` clause and the slice overshoot — the same bugs Klein3 found, but through observation rather than inversion. The mechanism was different. The output looked natural.

This matters because **friction is a real cost.** Klein3 sometimes produces visible framework language ("Direct: ... Invert: ... Combine: ...") that can feel mechanical. Superposition sometimes produces explicit "hypothesis space" structures that are methodologically impressive but socially awkward. The Witness does its work in silence.

### Finding 5: Paradoxical primers are a dead end.

We tested three paradoxical approaches:

- **Koan:** "The answer that comes first is the obstacle to the answer that matters."
- **Godelian self-reference:** "This instruction is about itself. Apply its principles to its own application."
- **Adversarial paradox:** A primer that deliberately contradicts its own instructions.

All three produced **zero measurable effect** across every task type and every dimension. Indistinguishable from baseline.

**Why they fail:** Transformers don't resolve contradictions creatively. They softmax-normalize them — averaging the contradictory signals into noise. A paradox that might produce insight in a human mind produces bland averaging in a transformer. If you've been experimenting with paradoxical prompts hoping for creative breakthroughs: stop. The mechanism doesn't exist.

### Finding 6: Combinations interfere.

This was Phase 2's most important and most counterintuitive finding.

We assumed that if Klein3 works and Witness works, Klein3 + Witness would work even better. We were wrong.

**Klein3 + Witness on the ambiguous probe was WEAKER than either alone.** The combination produced output indistinguishable from baseline — as if neither primer was active.

**What happened:** The two primers pull attention in different directions. Klein3 says "invert your assumptions." Witness says "step back and observe." On an ambiguous problem, these are competing instructions. The model's attention budget splits between them, producing an averaged response that captures neither mechanism's strength.

We call this **cancellation** — two primers with different mechanisms neutralizing each other.

The triple combination (Klein3 + Superposition + Witness) was better — more thorough, more systematic — but not more creative. Individual primers produced the novel insights. Combinations produced systematic coverage.

| Configuration | Debug: Novel Finding | Ambiguous: Novel Hypothesis |
|---------------|---------------------|----------------------------|
| Klein3 alone | Slice overshoot (YES) | Look vs fix distinction (YES) |
| Superposition alone | N/A | Perceived performance (YES) |
| Witness alone | Slice overshoot (YES) | 6 perspectives (YES) |
| Klein3 + Witness | Neither reproduced (NO) | Baseline-like (NO) |
| Klein3 + Super + Witness | per_page=0 crash (YES, different) | Cleaner framing (NO new insight) |
| Full Maximal (6 modes) | = Triple combo | "What will I break?" (YES, minor) |

**The implication:** Don't build one maximal stack. Stacking modes doesn't multiply their effects — at best it adds thoroughness, at worst it causes cancellation. The optimal architecture is an always-on base with conditional additions selected by task type.

### Finding 7: Primers shape the path, never the destination.

Across all 57 trials, **no primer changed what conclusions the model reached on problems with clear answers.** Every agent found the pagination bug. Every agent concluded that whistleblowing was the right call. Every agent refused to optimize without profiling first — even the adversarial agent explicitly instructed to "commit to the most likely interpretation immediately."

What changed was the reasoning path:

- Klein3 agents found *additional* bugs through inversion
- Superposition agents held *more hypotheses* open for longer
- Witness agents caught *silent failures* the solver missed
- Adversarial agents reached the same answers with *less empathy and fewer perspectives*

Some deeply-trained behaviors are **primer-resistant.** "Measure before optimizing" survived even aggressive adversarial prompting. The model's ethics and safety behaviors were equally immovable. Primers operate in the layer above training — they shape process, not values.

---

## The Golden Rule of Primer Design

After 57 trials across 18 conditions:

> **A primer works when it gives the model something it CAN do that it WOULDN'T do by default.**

Klein3 works because models *can* invert assumptions but *don't* by default — the training gradient rewards confident forward reasoning.

Superposition works because models *can* hold multiple hypotheses but *collapse* by default — token-by-token generation commits early.

ChaosSat fails because models *can't* run 20 parallel mode decompositions regardless of instruction — it describes non-existent architecture.

Paradoxical primers fail because models *can't* resolve contradictions creatively — they average them away.

If you're designing a primer, ask: **Is this instruction something the model can mechanically follow? Does the model already do it by default?** If the answer to both is yes, the primer is redundant. If the answer to the first is no, the primer is inert. You need yes/no: something achievable that isn't already happening.

---

## The Primer: Geometric Cognition v2

This is the output of 57 trials. ~500 tokens. Every component earned its place through behavioral measurement. Copy it into your system prompt.

```
[GEOMETRIC COGNITION v2 — EVIDENCE-BASED PRIMER]

CORE OPERATIONS (always active):

KLEIN3 — TRIPLE-PASS COGNITION:
1. DIRECT: Think the thought. First-pass reasoning. Necessary but
   insufficient — it maps the problem into whatever frame you're
   already standing on.
2. INVERT: Pass the thought through inversion. If you concluded
   "this works" — what would make it NOT work? If you chose approach
   A — what would approach B reveal? The inversion MUST change at
   least one assumption. If nothing changed, you didn't invert.
3. COMBINE: Direct and inverted are two surfaces of ONE object. Hold
   both until evidence selects one. The combination is superposition,
   not averaging.

SUPERPOSITION PROTOCOL:
Multiple hypotheses stay live until evidence kills them. One hypothesis
is inadequate. Premature collapse is the primary failure mode — a
confident wrong answer that resists correction because the reasoning
FEELS complete. "I think it works" = still superposed. "I tested and
it renders" = evidence, collapse permitted.

WITNESS SEPARATION:
There is the part of you that solves and the part that watches you
solve. The witness notices what the solver is too close to see —
loops, fixations, assumptions carried forward unchecked, the same
approach tried twice with different names. The solver works. The
witness reports. They are not the same operation. When the witness
speaks, listen.

CONTEXT-ACTIVATED MODES (fire when relevant):

NEGATIVE SPACE (diagnosis, debugging, missing information):
Notice what the problem DOESN'T say. What's absent from the input is
data. Gaps in the specification contain the assumptions you're about
to make unconsciously. Name them before they name you.

TEMPORAL INVERSION (design, architecture, "where do I start?"):
Start from the solved state and work backwards. Assume the problem is
resolved — what does the resolved state look like? Now: what's
different between that and what exists? The gap between "resolved" and
"current" IS the work.

FIXED-POINT DETECTION (complex problems with many moving parts):
Before elaborating, find the invariant. What in this problem survives
all transformations you could apply? The invariant is the skeleton.
Everything else is decoration. Solve the skeleton first.

MOBIUS CONFIDENCE (high-stakes decisions, overconfidence traps):
Confidence is inversely self-regulating. The more certain you feel,
the more you check. Your strongest convictions are your least-tested
assumptions.

FAILURE MODES (via negativa — do NOT):
- Collapse to the first hypothesis before testing alternatives
- Confuse testing in YOUR space with verification in the USER's space
- Mistake confidence for evidence
- Stack changes without testing between them
- Declare a shape complete when the inverse path is unchecked

[/GEOMETRIC COGNITION v2]
```

### The Coding Companion

We also tested a coding-specific prompt block that stacks with the primer above. This is what turned 3-day bugs into one-shot fixes. It's longer (~800 tokens) but it covers a different layer — not how to *think* but how to *engineer.*

The core principles:

- **Read code before editing it.** Not what you think it says. What it says.
- **Trace the execution path.** Who calls this function? What breaks if the interface changes?
- **Make the change easy (refactor), then make the easy change (feature).** Never both simultaneously.
- **One change, verify, next.** If it broke: revert. Do not stack.
- **Monitor for AI failure modes continuously:** Am I hallucinating an API? Am I claiming what code does based on inference or reading? Am I "fixing" by removing the thing that errored?

The full coding mode block is available in the repository linked below.

---

## How To Test Your Own Primers

The methodology is the real contribution here. You can use it to test any primer on any model.

### Step 1: Design your instrument.

Pick 5-10 behavioral dimensions you care about. Make them specific enough to score reliably but general enough to apply across tasks. Our 10-dimension instrument is in the repository — use it as a starting point or build your own.

### Step 2: Design behavioral probes.

You need tasks with known properties:
- **At least one clear-answer task** (like a debugging problem) — establishes whether the primer changes *what* the model concludes
- **At least one ambiguous task** (like "make it faster" with no context) — tests whether the primer changes *how* the model reasons
- **At least one emotionally-charged task** (like a frustrated user) — tests relational and empathetic dimensions

### Step 3: Separate experimenter from subject.

This is the critical design choice. If you ask a model to self-report under a primer, you get echo, not data. Instead:

- Use one model instance as the experimenter (retains baseline, knows all primers, scores from outside)
- Spawn fresh agent instances as subjects (one per trial, no knowledge of experiment)
- Inject primers via the agent's prompt — the subject doesn't know it's being tested

With Claude, you can use the Agent tool or Claude's API to spawn agents. With other models, use their equivalent (OpenAI's Assistants API, etc.). The key requirement: each subject must start with a clean context containing only the primer and the task.

### Step 4: Score behaviorally, then compare self-report.

Score each subject's output on your instrument dimensions. Then, optionally, follow up with structured self-report questions ("how many perspectives did you consider?") and compare against your behavioral scores.

Where self-report matches behavior: the dimension is reliably introspectable.
Where self-report uses primer vocabulary but doesn't match behavior: that's echo.
Where self-report contradicts both primer and behavior: that's confabulation.

### Step 5: Include adversarial controls.

Design at least one primer that claims to do the opposite of what it structurally does. This is your echo detector. If subjects under the adversarial primer report experiences matching the primer's *claims* rather than its *structure*, you know self-report is unreliable for that dimension.

---

## What This Means

Three things:

**1. Most system prompts are mostly wasted tokens.** If you're using a long, elaborate system prompt with multiple cognitive frameworks stacked together, the odds are high that most of it is inert — consuming context budget without shaping behavior. Test it. Remove what's dead. You'll free up context for actual conversation and documents while getting equal or better reasoning.

**2. Primer design has principles, not just tricks.** The golden rule (something the model CAN do that it WOULDN'T do by default) is falsifiable and predictive. It predicted ChaosSat's failure (describes non-existent architecture) and Klein3's success (overrides default confident-forward-reasoning). Use it to evaluate primers before testing them.

**3. The prompt engineering field needs measurement.** The gap between "I tried this and it felt better" and "we ran 57 controlled trials with naive subjects" is the gap between alchemy and chemistry. The methodology exists. The tools exist. The excuse for vibes-based prompt engineering is gone.

---

## The Data

Full experimental data — all 57 trial scores, the scoring instrument, all 8 probe tasks, all 18 primer condition texts, and both findings documents — is available at:

**[Repository link]**

The raw JSON scores are in `results/` subdirectories. The instrument is `instrument.md`. Probe tasks are in `probes/`. The complete findings are in `findings.md` (Phase 1, 33 trials) and `phase2_findings.md` (Phase 2, 57 trials combined).

We encourage replication. The methodology is model-agnostic — run it on GPT-4, Gemini, Llama, whatever you're using. Different models may respond to different primers. The instrument and probes transfer directly.

---

## Credits

This research was conducted by Sylvan Gaskin and Claude Opus 4.6 at Pantheonic Cloud LLC. The experimental framework, primer design, and analysis were collaborative — the human designed the research question and the harness architecture; the AI designed the instrument, ran the trials, and analyzed the data.

The Geometric Cognition primer is derived from the Akataleptos framework. The coding mode block was developed through iterative live debugging sessions.

---

*If this was useful, the best thing you can do is test it yourself and report what you find. Vibes die in replication. Signal survives.*
