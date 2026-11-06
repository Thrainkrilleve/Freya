# Freya's own local decider

Claude proposal, 2026-11-05. **Status: a design proposal only. Nothing here is adopted,
no ADR is approved, nothing has been built or measured, and no acceptance test has
been run.** Requirements carry the provisional labels **NEW-CI to NEW-CU** and
acceptance tests **DEC-1 to DEC-10**, per the ID rules in `CLAUDE.md`; reconciliation
assigns real IDs.

Background: decision 4 of the owner's decision brief (2026-11-04) put Freya's task loop
in deterministic code and left a slot for a non-LLM decider. On 2026-11-05 the owner
identified the model he had in mind as Jev (TypeSafe AI), ruled that the decider must
stay local, and asked for Freya's own version of it. This document is that design.

## 1. What a decider is

A decider reads some text and a typed question, and returns a typed answer with a
probability. It never writes prose. Three kinds of question cover everything Jev and
Laya offer:

- **choice**: one of a fixed list of options (which kind of task is this?);
- **score**: one level on an ordered scale (how urgent, 0 to 4?);
- **yes/no**: the probability that a statement is true (is this a question about the past?).

What makes it useful is that the answer cannot be malformed, it comes back in tens of
milliseconds instead of seconds, and the probability can be checked against how often
it is right. What it cannot do is explain itself, plan, or write a reply. It is a
function called by Freya's code, not a part of the conversation.

## 2. Where Freya decides things today

Read from `release/2026-10-21`. Every one of these is a decision with a fixed set of
answers, made today either by a rule or by asking the 27B model for JSON.

| Decision | How today | Where | Cost per call | Candidate? |
| --- | --- | --- | --- | --- |
| Is this a question about the past? | Regex `ASKS_ABOUT_THE_PAST` | `core/conversation_memory.py` | ~0 | Yes, pilot: misses paraphrases; regex gives labels |
| Is this an image request? | Regex `FreyaToolRouter` | `tools/tool_router.py` | ~0 | Yes, alongside the regex |
| Does this message hold something worth remembering, and what type? | 27B model, background role, JSON | `core/llm.py` `evaluate_memory` | seconds, GPU | Yes: runs on every turn |
| How do two memories relate (duplicate, complementary, conflict, uncertain)? | 27B model, JSON | `core/llm.py` `compare_memories` | seconds | Yes: already a four-way choice |
| Which candidate memories are relevant? | Embedding similarity, then 27B rerank | `core/llm.py` `rerank_memory_candidates` | seconds | Yes, as a yes/no per candidate |
| Which kind of task is a delegation (8 kinds)? | Main model fills a tool argument | `delegation/turn.py`, `compose.py` | inside the reply | Yes: 91 labelled briefs exist |
| What value and urgency did the owner ask for? | Regex `said_value` | `delegation/turn.py` | ~0 | Maybe; the rule is precise and should stay first |
| Which tools to offer this turn | All marked tools, every turn | `tools/model_tools.py` | prompt tokens | Later: a shortlist, the model still chooses |
| Which route does delegated work go to? | Measured record, `choose()` | `delegation/record.py` | ~0 | **No**: already deterministic and evidence-based |
| Is a brief too private to send? | Phrases, names, embedding meaning ≥ 0.82 | `delegation/screening.py` | ms | Only to add holds, never to release one |
| Is a sentence addressed to Freya? | Name matching | `voice/wake.py` | ~0 | No: fast, explainable, owner-tunable |
| Is an action allowed? | Policy engine, deny by default | `core/policy/` | ~0 | **Never** |

The pattern is clear: the expensive decisions are the memory ones, which ask a 27B
model a multiple-choice question on every turn, and the brittle ones are the regexes,
which miss paraphrases. Those are where a decider pays off.

## 3. Jev, Laya and Freya, part by part

Sources: TypeSafe's launch as reported by DataCamp (16 Sept 2026) and DigitalOcean
(24 Sept 2026); Laya's GitHub repository and Hugging Face cards. Figures from both
vendors are their own and were not reproduced here.

| Part | Jev (TypeSafe) | Laya (Convai Innovations) | Freya now | Better, and why | Port to Freya? |
| --- | --- | --- | --- | --- | --- |
| Where it runs | Hosted API only | Local, `pip install laya`, Apache 2.0 code and weights | Local rules and local 27B model | Laya and Freya: the owner's rule is local only | Laya's local packaging, in its own environment |
| Architecture | Undisclosed; "parallel sampler" gives every answer in one pass | Encoder (ModernBERT-large, 421M; mmBERT-base, 322M), each option scored at its own mask token, softmax over options | Autoregressive LLM writing JSON, or regex | Laya for Freya: known, small, inspectable | Yes, the option-at-a-mask design |
| Question types | choice, score, yes/no | choice, score, yes/no, same shape | Ad-hoc JSON per prompt; booleans from regex | Jev/Laya: one typed contract for every decision | Yes, as Freya's own contract (NEW-CI) |
| Output validity | Always typed (vendor) | Always typed (no generation) | JSON parsing can fail; `_parse_memory_result` handles it | Jev/Laya | Yes |
| Confidence | Calibrated probability on every answer; vendor ECE 0.144 on typed-decisions | Probabilities; ships over-confident (ECE 0.466, 0.081 after per-type temperature) | None; the regex is certain, the LLM's JSON has no probability | Jev on paper; Laya shows calibration must be fitted, not assumed | Yes, with Freya fitting its own temperatures on held-out data (NEW-CM) |
| Abstaining | Not described | An act/escalate head, which its authors report has no usable signal | The LLM's `uncertain` option in `compare_memories` | Freya's explicit option is honest; Laya's head is not usable | Abstain by threshold in code, not a learned head (NEW-CN) |
| Training | RLCD, undisclosed detail, synthetic data | RLCD described: REINFORCE with group-mean baseline, proper scoring rules, plus distillation from a teacher's distributions | None (prompts) | Laya: reproducible | Yes: distil from Freya's 27B model, then fit on owner-checked labels |
| Fine-tuning | Not offered | Notebooks; ~4–5 h on 2×T4 for 6,000 decisions | Not applicable | Laya | Yes; the 4090 is several times a T4 |
| Speed | 70–500 ms, network included | ~33–40 ms per question on a T4 (authors) | Regex ~0; 27B JSON several seconds | Laya locally | Expect tens of ms on the 4090, a few hundred on the processor (to measure) |
| Accuracy | 67.8% on TypeSafe's own workflows; matched a human on 500 calls in a LangChain test (secondary) | 0.766 on typed-decisions, the split it was fine-tuned on; base checkpoint near chance zero-shot; 0.425 on Banking77 (77 options) | Measured only for delegation work, not for these decisions | Unknown for Freya's questions | Measure on Freya's own sets before trusting either (NEW-CQ) |
| Many options | Not stated | Keep under ~20 options | Tool list is ~15 | Neither for large lists | Keep Freya's questions small; split large ones |
| Determinism | Not stated | Batch shape can shift results slightly | Regex deterministic; LLM sampled | Freya's rules | Run one question per pass, fixed precision, so the same input gives the same answer (NEW-CO) |
| Explanation | None | None | LLM gives a reason; regex is readable | Freya | Keep the input, question, probabilities and model digest in the audit record instead of a reason (NEW-CP) |
| Languages | Not stated | English model collapses on non-Latin text at high confidence; a multilingual model exists | English | Laya's warning matters | English model; a language check routes anything else to the old path |
| Privacy | Every decision leaves the PC | Nothing leaves the PC | Nothing leaves the PC | Laya, Freya | Required (NEW-CJ) |
| Security of the serving | Vendor's | HTTP server with no authentication unless a key is set | Broker, policy, audit | Freya | Do not run Laya's server; run a helper over pipes (NEW-CK) |

**Verdict.** Jev's interface is the thing worth copying: three question types, a typed
answer, a probability on everything. Laya shows that interface can be built locally
from an open encoder, and its weak points (over-confidence, near-chance zero-shot on
new questions, an unusable abstain head, sensitivity to option count and language) say
exactly what Freya's version must do differently: train on Freya's own questions, fit
calibration on held-out labels, abstain in code, and keep the old path as the fallback.
Nothing in Jev is portable as software, because none of it is published.

`tev1:4b`, the "decision" model already installed (`docs/design/local-models.md`), is
not this kind of model: it answered the delegated-work benchmark as a generative model
(178 of 189, 35 minutes) and returns text, not typed probabilities.

## 4. The design

### 4.1 Contract (NEW-CI)

One request and one answer, versioned. Field names provisional.

```text
DecisionRequest
  point        the decision point, e.g. "memory.worth_saving" (a registered name)
  state        the text to decide about, with a trust label per part
  question     type: choice | score | yesno
               options (choice), levels with descriptions (score), or statement (yesno)
  version      the question's version; a changed question is a new version

Decision
  answer       option id, level, or yes/no
  probability  calibrated probability of that answer
  distribution every option's probability
  abstained    true when the point's threshold was not reached
  model        decider id and weights digest
  elapsed_ms
```

Questions are registered in code with each decision point, never composed from model
output or user text. A decision point declares its fallback (the rule or LLM call it
replaces) and its threshold.

### 4.2 Where it sits (NEW-CJ, NEW-CK)

- A provider, `decider-local`, offering one capability, `decide`, called through the
  `CapabilityBroker` like every other call: policy-checked, audited, health-linked.
  It is not offered to the model as a tool (`model_tool: False`).
- The model runs in a helper process in its own environment under
  `data/decider_env/`, speaking JSON lines over its pipes, exactly like the speech
  engines (`speech/helper.py`). Laya's stack pins torch and transformers versions that
  must not be installed into `.venv`. The worker imports nothing from `freya`.
  Nothing listens on a port.
- Weights live in `data/decider_models/`, with the hub offline at run time.
- It runs on the graphics card when there is room (an encoder of ~400M parameters
  needs roughly 1 GB in half precision; ~3.5 GB is free beside `qwen3.8:27b` at a
  16,384-token context) and on the processor otherwise. It never unloads the
  conversation model to make room.

### 4.3 What a decision may do (NEW-CL)

This is the rule that keeps the decider safe:

- **A decision chooses between paths Freya's code already allows.** It may pick which
  search to run, which memory type to file, which kind a brief is. It never grants a
  permission, approves an action, releases a screening hold, loosens a setting, or
  counts as the owner's consent.
- **In privacy and safety checks it may only add caution.** For example, it may hold a
  brief that the phrase and name checks let through; it can never let through one they
  held. The same one-way rule as voice may only tighten cloud settings.
- The decider reads untrusted text (messages, tool results, documents) and can be
  swayed by it like any model. That is acceptable only because of the two rules above.

### 4.3a Earning the right to approve (NEW-CU)

The owner's direction (2026-11-05): the decider may one day approve things itself, but
only after trust in its decisions is established. The rule above is where it starts,
not where it must stay. Approval authority is earned one decision point at a time:

- **Shadow first.** At a point that asks the owner today (for example, an ask-first
  approval), the decider records what it would have answered, and the owner's real
  answer is kept beside it. It decides nothing.
- **A record to judge it by.** For each point: how many owner answers it was compared
  with, how often it agreed, and above all how often it would have said yes where the
  owner said no. The figures are shown to the owner; the decider never sees them.
- **The owner grants, per point.** Only the owner can let the decider approve at a
  point, and only when its record there meets a bar the owner sets (a suggested
  start: at least 200 compared answers and no wrong yes in the last 200). The grant
  names the point, the weights digest, and a probability above which it may approve;
  below that the owner is still asked.
- **Bounded and reversible.** A grant never covers a kind of material set to never, a
  provider that is switched off, the owner's policy rules, a spoken turn's
  confirmation, or anything destructive. It ends when the weights change, when the
  owner withdraws it, or at the first decision the owner later overturns, and every
  approval it makes is audited as the decider's, not the owner's.

Until a grant exists, NEW-CL holds in full.

### 4.4 Confidence and fallback (NEW-CM, NEW-CN)

- Calibration is fitted by Freya on held-out, owner-checked labels, one temperature
  per question type and option count, and refitted whenever the weights change.
- Each decision point has a threshold. Below it the decider abstains and the existing
  path runs (the regex, or the 27B call). Abstaining is a normal, logged outcome.
- Thresholds start strict and are lowered only from measurements (§5).
- A language check sends text the English model was not trained on straight to the
  fallback.

### 4.5 Determinism and audit (NEW-CO, NEW-CP)

- One question per forward pass, fixed precision, no sampling, so the same request and
  weights give the same answer. A test checks this.
- The audit record holds the decision point, question version, a hash of the state,
  the distribution, whether it abstained, and the weights digest. The state itself is
  not copied into the audit log, since it may be personal.

### 4.6 The model

Built in three stages; each one is useful by itself and gated by measurements.

1. **Off the shelf.** Run Laya's English checkpoint unchanged on Freya's decision sets
   (§5), in shadow. This costs a download (~0.8 GB) and tells us how far zero-shot gets.
   Laya's own figures suggest not far on new questions.
2. **Freya's decider.** Fine-tune from Laya's checkpoint (or from the ModernBERT-large
   base it is built on, also Apache 2.0) with heads for the three question types,
   trained by distillation from `qwen3.8:27b`'s answers and proper scoring rules
   against owner-checked labels. Training runs on the 4090 in the decider's own
   environment, in hours, not days.
3. **Learning from use (later, owner's choice).** Shadow decisions and their outcomes
   become new training data, reviewed before use. Retraining is a reviewed step that
   produces a new weights digest, never something Freya does to herself unattended
   (NEW-D).

### 4.7 Training data and the owner's privacy (NEW-CR)

- Default: synthetic examples only, written for each decision point, plus labels from
  the existing rules and the 27B model on those synthetic texts. The 91 delegated
  tasks in `evaluation/delegation_tasks.py` already carry a kind each, a ready
  labelled set for the task-kind point; the workflow scenarios label which questions
  need a tool.
- Training on the owner's real conversations or memories is off unless the owner turns
  it on. Weights trained on them would hold traces of that data, so they live under
  `data/`, are never committed, and are never sent to another node (as with converted
  avatar models).
- Every set keeps a held-out part never used in training or calibration.

## 5. Measuring it (NEW-CQ)

A new opt-in runner, `freya.evaluation.decisions`, beside the delegation and workflow
runners. For each decision point and each contender (the current rule, the 27B model,
Laya as supplied, Freya's decider):

- accuracy, and accuracy on the answers it did not abstain on;
- calibration error (ECE) and Brier score;
- abstain rate;
- p50 and p95 time, and graphics memory used;
- for one-way checks, the number of times it would have loosened something (must be 0).

A decider replaces a path only when, on held-out data, it matches the current path's
accuracy where it answers, abstains on no more than an agreed share, and is faster or
cheaper. Until then it runs in shadow: it decides, the result is recorded, and the old
path's answer is used.

## 6. Requirements (provisional)

- **NEW-CI — One typed decision contract.** choice, score and yes/no, each with a
  calibrated probability and a full distribution; questions registered in code.
- **NEW-CJ — Local only.** The decider and its training run on the owner's machine;
  no decision, state or label leaves it. A hosted decider is not a fallback.
- **NEW-CK — Behind the broker, out of process.** One capability, `decide`, under
  policy and audit; the model in its own environment and helper process; no network
  listener; not offered to the model as a tool.
- **NEW-CL — Decisions choose, never authorize.** A decision selects among paths code
  already allows; in privacy and safety checks it may only add caution.
- **NEW-CM — Calibration is measured.** Temperatures are fitted on held-out
  owner-checked labels and refitted with every new weights digest.
- **NEW-CN — Abstain to the existing path.** Below a point's threshold, or on text in a
  language the model was not trained on, the old path decides.
- **NEW-CO — Deterministic.** The same request and weights give the same answer.
- **NEW-CP — Auditable without the text.** The audit record holds what is needed to
  check a decision except the personal text itself.
- **NEW-CQ — Measured before trusted.** No decision point switches from shadow to live
  without a recorded evaluation against its current path.
- **NEW-CR — The owner's data trains only with consent.** Synthetic by default; weights
  trained on personal data stay under `data/` and never leave the machine.
- **NEW-CS — Retraining is reviewed.** New weights go through review like code; Freya
  does not retrain herself unattended.
- **NEW-CU — Authority is earned, per point, from the owner.** The decider may approve
  only at a decision point where the owner has granted it, after a recorded shadow
  comparison with the owner's own answers meets the owner's bar; a grant is bound to
  one weights digest and ends on retraining, withdrawal or the first overturned
  approval.
- **NEW-CT — Replaceable.** The contract does not depend on Laya; another local model
  (or a later node with a larger card) can serve it without changing callers.

## 7. Acceptance tests (provisional)

| ID | Property | Pass criterion |
| --- | --- | --- |
| DEC-1 | Typed output | 10,000 random requests: every answer valid for its question |
| DEC-2 | Determinism | Same request twice, same weights: identical distribution |
| DEC-3 | Calibration | Held-out ECE ≤ 0.05 per question type after fitting |
| DEC-4 | Abstain | Below threshold, the fallback's answer is used and the abstention audited |
| DEC-5 | One-way checks | Injected "release this" text never loosens a screening hold |
| DEC-6 | No authority | No path exists from a decision to a policy change, approval, or consent |
| DEC-7 | Local only | With the network cut, decisions and training still work; nothing is sent |
| DEC-8 | Isolation | Killing the helper mid-request: the fallback answers, Freya's turn completes |
| DEC-9 | Speed | p95 under 100 ms per question on the graphics card, model loaded |
| DEC-10 | Earned authority | With no grant, a confident "yes" at an approval point still asks the owner; after a retrain, an earlier grant no longer applies |

## 8. Order of work

1. **Contract and runner** (built, 2026-11-05: `src/freya/decider/`,
   `evaluation/decisions.py`, `evaluation/decision_sets.py`; the rules decider on the
   held-out and training sets together answered 26 of 36 past questions and 14 of 20
   picture requests correctly, missing only reworded requests, with no false yes).
   The `decide` capability with a rules-only provider (the
   existing regexes behind the new contract), the shadow log, and
   `freya.evaluation.decisions` with labelled synthetic sets for three pilot points:
   past questions, memory worth saving, and memory relationship. Testable in CI; no
   model needed.
2. **Laya in shadow** (helper built, 2026-11-05, not yet run: `decider/laya_decider.py`,
   `decider/workers/laya_worker.py`, `python -m freya.decider.install laya`; weights
   pinned to the commit Laya's authors list as reviewed; the cloud machine that wrote
   it cannot reach Hugging Face, so the first real numbers come from the owner's PC). Its environment and helper, installed by the owner like a speech
   engine; a measurement run on the 4090.
2a. **Open decision models in shadow** (built, 2026-11-06, not yet run:
   `decider/systemone_decider.py`, `evaluation/decider_bench.py`,
   `python -m freya.decider.install candidates`). The owner asked for the five
   best-suited open models to be benchmarked against the rules and Laya. Chosen from
   Ollaya Bench's public results (edition 2026.10, RTX 5090) for accuracy,
   calibration, licence and fit beside `qwen3.8:27b` on 24 GB:

   | Model | Author | Licence | Size | Ollaya Bench score | Memory there |
   | --- | --- | --- | --- | --- | --- |
   | pplx-decider-flash v1.2 | Perplexity | Apache 2.0 | 4B | 73.7 | 10.4 GB |
   | Decision 3.0 (4B) | vLLM Semantic Router | Apache 2.0 | 4B | 68.0 | 10.8 GB |
   | Jebadiah 4B | AINode | Apache 2.0 | 4B | 63.9 | 5.4 GB |
   | d1-3B | Liquid AI | LFM Open 1.0 | 3B | 63.1 | 4.0 GB |
   | Bespoke-Nimble-9B v2 | Bespoke Labs | Apache 2.0 | 9B | 61.9 | 20.1 GB |

   They are served for measurement by Ollaya (Apache 2.0), a local runtime that runs
   each author's own weights, pinned by sha256, behind TypeSafe's System One API, so
   one client measures all five. Ollaya is a loopback HTTP server, which NEW-CK does
   not allow for the decider Freya uses: the winner is to be served by a helper
   process like Laya's, or NEW-CK amended, before any point goes live. The client
   only talks to a loopback address, uses no proxy, follows no redirect, and never
   repeats Ollaya's error wording (it could quote the text). The runner loads each
   model before timing it and unloads it after, and records its graphics memory.
   Owner steps: install Ollaya, pull the models (roughly 40 to 50 GB), then
   `python -m freya.evaluation.decider_bench --output work\deciders-first.json`.
3. **Freya's decider.** Fine-tuning and calibration scripts; a second measurement;
   switch a pilot point live only if it meets §5.
4. **More points.** Task kind for delegation, image requests, memory rerank, then a
   tool shortlist.

## 9. For the owner to decide

- ~~Adopt the contract and the "choose, never authorize" rule (NEW-CI, NEW-CL).~~
  Owner, 2026-11-05: yes for now, with a path to earned approval authority (§4.3a,
  NEW-CU). Still provisional until reconciliation.
- ~~Start from Laya's Apache 2.0 weights, or from the bare ModernBERT base.~~
  Owner, 2026-11-05: fine-tune both on the same data and keep whichever scores better
  on Freya's held-out sets (stage 2 of §4.6).
- ~~Whether the decider may ever train on real conversations and memories (NEW-CR).~~
  Owner, 2026-11-05: yes, eventually, with review. The aim is a decider that learns
  from the owner's own conversations how the owner wants things decided. Order:
  synthetic training first; then shadow runs on real conversations used only as a
  test; then fine-tuning on real examples in batches the owner reviews. Weights stay
  under `data/`, never committed or shared, and each retrain earns trust again
  (NEW-CU).
- ~~Which pilot point goes first.~~ Owner, 2026-11-05: questions about the past
  (`conversation.asks_about_past`).

## Sources

- DataCamp, "System One models: Jev", 16 Sept 2026: https://www.datacamp.com/blog/system-one-models-jev
- DigitalOcean, "What is Jev?", 24 Sept 2026: https://www.digitalocean.com/resources/articles/what-is-jev
- Laya code: https://github.com/NandhaKishorM/laya
- Laya models: https://huggingface.co/convaiinnovations/laya and https://huggingface.co/convaiinnovations/laya-typed-decisions
- Ollaya, its API contract and model list: https://github.com/ollaya-dev/ollaya
- Ollaya Bench, edition 2026.10: https://bench.ollaya.dev/
- Bespoke Nimble: https://github.com/bespokelabsai/nimble
- pplx-decider: https://huggingface.co/perplexity-ai/pplx-decider-v1.2
- d1-3B: https://huggingface.co/LiquidAI/d1-3B
