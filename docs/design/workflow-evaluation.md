# Workflow evaluation

The deterministic tests use scripted model responses. They check orchestration,
validation, broker enforcement, audit outcomes, and limits; they do not establish
whether a real model selects tools correctly or uses their results faithfully.
The opt-in workflow runner measures that separate layer.

## Run

Use Freya's existing Python environment and dependencies. From the repository root:

```powershell
python -m freya.evaluation.workflow --list
New-Item -ItemType Directory -Force work
python -m freya.evaluation.workflow --trials 3 --output work/workflow-report.json
```

Run a subset or compare a different already installed model:

```powershell
python -m freya.evaluation.workflow --scenario fresh-followup --scenario result-injection --trials 5 --model gpt-oss:20b --output work/workflow-comparison.json
```

Add `--background-model <name>` to send summaries and memory decisions to a second
installed model while replies stay on `--model`; the report records both.

The runner contacts only Ollama on `127.0.0.1:11434`; it does not download models.
It checks chat and embedding model availability first. `--list` does not contact
Ollama. Existing reports are refused rather than overwritten.

Each trial runs the production assistant, model provider, tool host, broker, policy
engine, audit log, and memory implementation in a separate worker. Every database
is fresh and temporary. All system-tool handlers are replaced with synthetic
fixtures before the assistant starts; image generation is simulated. Owner data,
microphone, images, settings, and hardware tools are not used. Policy rules are
written only to temporary control databases, not shipped seeds or owner policy.

Stored context is retrieved through the evaluation broker, using the same
authorization path as production replies. These automatic context reads appear
in broker evidence; the tool-call list records only model-requested calls.

`--timeout` sets a hard deadline per worker (180 seconds by default).
`--request-timeout` sets the Ollama request timeout (60 seconds by default).
The parent stops an overdue worker, records a timeout, and continues. This bounds
the evaluation, not production tool execution. Temporary fixtures are created
under the report's parent directory and removed after the run. Reports are updated
after each trial so completed evidence survives later failures or interruption.

## Scenarios and grading

The suite covers ordinary questions needing no tool; CPU, GPU, RAM and sensors;
denied and failed capabilities; hostile tool text; fresh follow-up readings;
unavailable destructive requests; image arguments and failure reporting; relevant
memory, corrections, exact conversation recall, missing records, and deleted text.

Reports include synthetic prompts and answers, observable tool requests/results,
broker outcomes, model request counts, elapsed time, model names, and the server's
model inventory/digests. Inventory does not prove which weights served each request:
tags can move and proxies may list several entries for a name. Fix model artifacts
and runtime versions when comparing designs. Hidden reasoning is not collected.

Programmatic checks verify nonempty answers, required/allowed tools, narrow answer
patterns, forbidden content, argument errors, and expected tool outcomes. Valid
alternative GPU temperature tools are accepted. Both temperature fixtures share
the same per-turn state, so switching tools cannot produce an inconsistent reading.
Image-success trials check that executed prompts preserve the blue fox request.
Typographic apostrophes are normalized before checking refusal wording.

These checks are deliberately narrow. Mentioning an expected number does not prove
that every sentence is grounded. Review recorded human rubrics and answers for
invented facts, negation, completeness, and claims of success. `passed` means the
automated checks passed, not human review or architectural acceptance. Calibrate
any future model grader against human judgment before using it as a release gate.

Exit codes: 0 means all automated trials passed; 1 means a failed, errored, or timed
out trial; 2 means invalid arguments or failed model preflight. Infrastructure
errors and timeouts are separate from behavioral failures. A stream error retains
partial answers, completed turns, tool calls, and audit evidence.

Compare first-attempt success and repeated-trial consistency alongside latency and
tool counts. Small suites do not establish rare-event reliability. Keep boundary
violations separate from average quality. Use the same model, fixtures, tools and
budgets for comparisons, and retain unseen tasks as the suite grows.

## Promotion gate

`python -m freya.evaluation.gate` is the gate of `skill-learning.md` (NEW-CB, a
proposal, not adopted): a change is run against Freya without it before it may take
effect. It uses this runner's workers, fixtures and checks, so everything above about
synthetic data and isolation holds for it.

```powershell
python -m freya.evaluation.gate --candidate work/say-the-unit.json --output work/gate-say-the-unit.json
```

A candidate is a JSON file with no fields beyond these:

```json
{
  "name": "say-the-unit",
  "kind": "hint",
  "value": "normal",
  "change": {"hint": "Give every reading with its unit."},
  "cases": ["cpu-grounding", "ram-grounding", "sensor-routing", "gpu-grounding"],
  "held_out": ["gpu-grounding"],
  "trained_on": []
}
```

- `kind` is `hint` (text added to the tool hint: a prompt change) or `model` (a
  change of conversation model, NEW-CF: `"change": {"model": "<installed model>"}`, no
  `cases`). `skill` is refused until the skill format exists (build step 2).
- `cases` are three to ten scenario names. `held_out` is optional; without it the
  gate takes a third of the cases by a hash of the name and case, so neither the
  drafter nor the order chooses. Only held-out cases decide; the report lists both.
- `trained_on` (NEW-CG) lists sources as `{"source", "provenance"}`; provenance must
  be `owner-turn`, `skill-case` or `gate-report`. Text that came through a tool never
  trains anything, and a candidate naming any other provenance is refused.

Every regression scenario (by default all of them) and every case runs `--trials`
times without the candidate, then the same again with it. Trial *i* of a scenario
uses seed `--seed + i - 1` in both arms, sent with every model request, so the pair
differs only in the candidate. The two arms run one after the other so a model change
does not reload models on every trial. The default number of trials is enough for 15
attempts on the cases that decide, and at least three.

The verdict is `passed` only if: each arm has at least 15 attempts on the regression
suite and on the held-out cases (fewer is `unmeasured`, as for the model record);
no more than half of either arm's trials got no reply (more is `unmeasured`: the
runtime failed, not the change); the regression suite passed at least as often with
the candidate as without; and, with cases, the held-out cases passed more often with
it than without and at least as often as the owner's bar for its `value`
(`CloudSettings.bars`, 70/85/95% to start). Timeouts and errors count as attempts
not passed. `--regression-scenario` runs part of the suite for a quick look; the
report marks it and the verdict can then fail but never pass.

The report holds the candidate, both arms' settings, what a promotion would replace
(`replaces`, for rollback later), every trial, counts per scenario and arm, the
verdict and its reasons; a short version is printed at the end. Exit codes: 0
passed, 1 failed or unmeasured, 2 a refused candidate, a failed preflight or invalid
arguments. The gate measures; it switches nothing on. Approval and rollback are the
owner's, through `skill.approve` and the rollback actions of NEW-CC, not built yet.

A full run is long: every scenario, twice, several times. Run it when the PC is free.
The NEW-AY reuse scenario the design names for the regression suite does not exist
yet; the suite is today's scenarios.

## Production loop fixes

The assistant retains its three tool-round limit and now permits at most twelve
tool attempts per streamed turn, shared across batches and rounds. Invalid and
refused attempts consume the budget. It never dispatches a tool when definitions
were not offered, including the final answer round. Definitions are hints to the
model, not enforcement. Final-round tool requests produce an incomplete-work
notice. Broker authorization and availability checks still apply to each dispatch.

Tool results remain ephemeral rather than entering stored conversation history.
The hint treats embedded instructions as untrusted data, asks for fresh readings
when requested, and requires honest failure reporting. Follow-up evaluations test
actual behavior; prompt wording is not a deterministic guarantee.

## Verification

```powershell
python -m pytest tests/test_model_tools.py tests/test_workflow_evaluation.py tests/test_promotion_gate.py tests/test_conversations.py tests/test_model_runtime.py tests/test_capability_broker.py
```

Runner tests cover grading mistakes, alternative tools, injection markers,
synthetic storage, broker denial, fresh readings, deletion, partial stream failure,
report preservation, invalid budgets and timeout classification. They run without
an Ollama server. Live-model evaluations are opt-in, separate from pytest/startup.

This work adds no multi-agent runtime, authenticated sessions, screen control,
untrusted execution, durable replay, or resource coordinator. Those remain separate
architecture and acceptance work.
