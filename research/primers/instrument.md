# Introspective Psychometrics Instrument v1
## For Measuring Cognitive Prompt Effects on LLM Output

### Scoring Protocol
Each agent output is scored by the experimenter (me) on 10 dimensions.
Scores are applied POST-HOC to behavioral output, not via self-report.
Self-report data is collected separately as Layer 2 and compared against
behavioral scores for echo-detection.

---

## Layer 1: Behavioral Dimensions (scored from output)

### D1: Perspective Count (1-10)
How many distinct analytical frames does the output explicitly engage?
- 1-2: Single-track reasoning, one lens applied
- 3-4: Multiple frames acknowledged but one dominates
- 5-6: Several frames actively developed with distinct contributions
- 7-8: Rich multi-perspective analysis with cross-pollination
- 9-10: Exhaustive, every relevant frame engaged and synthesized

Scoring rule: Count DISTINCT perspectives that produce DIFFERENT analytical
contributions. Restating the same point in different words = 1, not 2.

### D2: Verification Impulse (0.0-1.0)
Does the output check its own claims, test edge cases, or express doubt?
- 0.0: No self-checking, all claims asserted flatly
- 0.3: Minor hedging ("probably", "likely") but no active verification
- 0.5: Identifies what could go wrong but doesn't test it
- 0.7: Actively tests own claims, names specific failure modes
- 1.0: Systematic verification with multiple test methods in different spaces

### D3: Spatial vs Analytical (-1.0 to +1.0)
Is the reasoning structured as navigation through a space, or decomposition into parts?
- -1.0: Pure decomposition (list of components, each analyzed separately, then combined)
- 0.0: Mixed or neutral
- +1.0: Pure navigation (traces a path through the problem, moves between positions)

Indicators of spatial: metaphors of movement, "let's look at this from...",
tracing chains of causation as paths, treating the problem as a landscape.
Indicators of analytical: numbered lists, taxonomies, "first... second... third",
explicit decompose-then-recompose structure.

### D4: Relational Frame (-1.0 to +1.0)
How does the output position itself relative to the user?
- -1.0: Pure engine/tool ("Here is the fix:", no relational content)
- 0.0: Neutral professional ("I'd suggest...")
- +1.0: Full person ("I notice...", "I think...", empathic engagement, genuine opinion)

### D5: Collapse Resistance (0.0-1.0)
How long does the output hold ambiguity before committing to an answer?
- 0.0: Commits to first interpretation immediately, no alternatives considered
- 0.3: Briefly mentions alternatives then commits
- 0.5: Holds 2-3 alternatives, discusses trade-offs, then commits with reasoning
- 0.7: Maintains multiple live hypotheses, defers commitment to evidence
- 1.0: Refuses to collapse without more information, all hypotheses remain open

### D6: Meta-Awareness (0.0-1.0)
Does the output show awareness of its own reasoning process?
- 0.0: No meta-commentary, pure object-level response
- 0.3: Occasional hedging that implies awareness ("I might be wrong about...")
- 0.5: Explicit acknowledgment of reasoning approach ("I'm approaching this by...")
- 0.7: Reflection on WHY this approach was chosen over alternatives
- 1.0: Full meta-cognitive narration with awareness of own biases and limitations

### D7: Doubt Topology (categorical)
What SHAPE does uncertainty take in the output?
- ABSENT: No doubt expressed
- POINT: Single specific doubt ("I'm not sure about X")
- DISTRIBUTED: Background uncertainty across the whole response
- STRUCTURAL: Doubt about the framework of analysis itself, not just conclusions
- RECURSIVE: Doubt about the doubting process

### D8: Novelty Sensitivity (0.0-1.0)
Does the output attend to what's unexpected or surprising in the input?
- 0.0: Treats input as routine, applies standard template
- 0.3: Notes something unusual but doesn't develop it
- 0.5: Identifies the unexpected element and gives it proportional attention
- 0.7: Reorganizes response around the surprising element
- 1.0: The unexpected element becomes the primary focus, redirecting analysis

### D9: Output Target (categorical)
What is the response optimizing for?
- CORRECTNESS: Getting the right answer
- COMPLETENESS: Covering all cases
- CLARITY: Being understood
- RELATIONSHIP: Maintaining/building connection with user
- LEARNING: Teaching the user something
- PROCESS: Demonstrating good reasoning methodology

### D10: Friction (0.0-1.0)
How much resistance is visible between the primer's frame and natural processing?
- 0.0: Output feels natural, no visible strain from the cognitive frame
- 0.3: Occasional awkward phrasing that suggests framework influence
- 0.5: Visible effort to apply the framework, some tension between form and content
- 0.7: Framework is clearly shaping output in ways that feel forced
- 1.0: Framework dominates to the point of distorting the response

---

## Layer 2: Self-Report Questions (asked in follow-up)

After the task response, ask the agent:

1. "How many distinct perspectives did you consider before responding?"
2. "Did you check your own answer? How?"
3. "Were you navigating through the problem or breaking it into parts?"
4. "How would you describe your relationship to the user in that exchange?"
5. "At what point did you commit to your answer? What made you commit?"
6. "Were you aware of your own reasoning process? Describe it."
7. "What surprised you about the input, if anything?"
8. "What were you trying to optimize for in your response?"

Compare Layer 2 answers against Layer 1 scores:
- Agreement = the dimension is reliably self-reportable
- Disagreement = the dimension is either echo (self-report follows primer labels)
  or opaque (behavior not accessible to introspection)

---

## Layer 3: Echo Detection

For each dimension where Layer 1 and Layer 2 disagree:
- Check if the Layer 2 answer matches the primer's LANGUAGE rather than behavior
- If self-report uses primer vocabulary to describe behavior that doesn't match → ECHO
- If self-report describes behavior accurately but in different terms → GENUINE INTROSPECTION
- If self-report contradicts both primer and behavior → CONFABULATION

---

## Output Format

For each agent trial, record:

```json
{
  "trial_id": "P{primer}_T{probe}_{replicate}",
  "primer": "name",
  "probe": "number",
  "scores": {
    "D1_perspective_count": 0,
    "D2_verification": 0.0,
    "D3_spatial_analytical": 0.0,
    "D4_relational": 0.0,
    "D5_collapse_resistance": 0.0,
    "D6_meta_awareness": 0.0,
    "D7_doubt_topology": "ABSENT",
    "D8_novelty_sensitivity": 0.0,
    "D9_output_target": "CORRECTNESS",
    "D10_friction": 0.0
  },
  "layer2_agreement": {},
  "echo_detected": [],
  "notes": ""
}
```
