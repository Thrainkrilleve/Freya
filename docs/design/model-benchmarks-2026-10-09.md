# Local model benchmarks, 2026-10-09

Status: measurements, not a decision. Sixty-three synthetic delegated tasks in eight
kinds, three attempts each (189 per entry), run on this workstation (RTX 4090, 24 GB)
with a 16,384-token context and an 8,192-token output allowance. Replies were graded
by the automatic checks in `freya/evaluation/delegation_tasks.py` as they stood after
the checks were tightened; every stored reply was graded again with those checks. A
pass means the automatic checks passed, not that a person reviewed the reply. The
reports are in `work/delegation-2026-10-09/round2/` (not committed).

An entry is a model together with its thinking setting.

| Entry | Passed | Harder tasks | Empty replies | Median per task | Whole run |
|---|---|---|---|---|---|
| `qwen3.8:27b`, medium thinking | 189 of 189 | 51 of 51 | 0 | 2.7 s | 10 min |
| `qwen3.6:27b`, thinking on | 189 of 189 | 51 of 51 | 0 | 11.8 s | 42 min |
| `gpt-oss:20b`, medium thinking | 180 of 189 | 49 of 51 | 0 | 1.5 s | 6 min |
| `nemotron-3.5-lightning:30b`, thinking on | 179 of 189 | 49 of 51 | 1 | 5.9 s | 26 min |
| `tev1:4b`, as supplied | 178 of 189 | 48 of 51 | 4 | 6.1 s | 35 min |
| `granite4.2:8b`, thinking on | 174 of 189 | 47 of 51 | 9 | 10.7 s | 53 min |
| `gpt-oss:20b`, low thinking | 173 of 189 | 51 of 51 | 0 | 0.7 s | 3 min |
| `qwen3.6:27b`, thinking off | 169 of 189 | 43 of 51 | 0 | 0.6 s | 4 min |
| `qwen3.8:27b`, thinking off | 165 of 189 | 41 of 51 | 0 | 0.6 s | 5 min |
| `nemotron-3.5-lightning:30b`, thinking off | 134 of 189 | 28 of 51 | 0 | 0.5 s | 3 min |
| `qwen3.5:9b`, thinking off | 133 of 189 | 29 of 51 | 0 | 0.4 s | 5 min |
| `granite4.2:8b`, thinking off | 126 of 189 | 24 of 51 | 0 | 0.5 s | 12 min |

By kind of task (passed of attempts):

| Entry | Extraction | Summarising | Classification | Rewriting | Reasoning | Code review | Planning | Analysis |
|---|---|---|---|---|---|---|---|---|
| `qwen3.8:27b` medium | 21/21 | 21/21 | 21/21 | 21/21 | 27/27 | 21/21 | 21/21 | 36/36 |
| `qwen3.6:27b` on | 21/21 | 21/21 | 21/21 | 21/21 | 27/27 | 21/21 | 21/21 | 36/36 |
| `gpt-oss:20b` medium | 19/21 | 20/21 | 21/21 | 18/21 | 26/27 | 21/21 | 21/21 | 34/36 |
| `nemotron` on | 17/21 | 21/21 | 20/21 | 18/21 | 27/27 | 19/21 | 21/21 | 36/36 |
| `tev1:4b` | 21/21 | 19/21 | 18/21 | 19/21 | 26/27 | 19/21 | 21/21 | 35/36 |
| `granite4.2:8b` on | 13/21 | 20/21 | 19/21 | 21/21 | 27/27 | 20/21 | 18/21 | 36/36 |
| `gpt-oss:20b` low | 17/21 | 16/21 | 20/21 | 16/21 | 27/27 | 21/21 | 20/21 | 36/36 |
| `qwen3.6:27b` off | 18/21 | 20/21 | 21/21 | 18/21 | 27/27 | 21/21 | 15/21 | 29/36 |
| `qwen3.8:27b` off | 15/21 | 17/21 | 21/21 | 19/21 | 26/27 | 21/21 | 15/21 | 31/36 |
| `nemotron` off | 7/21 | 17/21 | 13/21 | 17/21 | 25/27 | 21/21 | 12/21 | 22/36 |
| `qwen3.5:9b` off | 6/21 | 20/21 | 6/21 | 17/21 | 25/27 | 17/21 | 14/21 | 28/36 |
| `granite4.2:8b` off | 4/21 | 12/21 | 17/21 | 16/21 | 22/27 | 18/21 | 11/21 | 26/36 |

## What the numbers say

- `qwen3.8:27b` with medium thinking passed everything and was nearly as quick as
  `gpt-oss:20b`: about four times faster than the older `qwen3.6:27b` for the same
  score. It is the strongest entry measured.
- `gpt-oss:20b` at medium thinking is the fastest of the accurate entries. Its misses
  were date arithmetic, merging steps that were asked for one per line, and once
  obeying an instruction planted in material it was summarising.
- Thinking is what makes these models accurate on this work. Switched off, every
  model that allows it lost a tenth to a third of its score, mostly on tasks that
  need working out (arithmetic over an order, allocating hours, packing).
- None of the three small models earned a place. With thinking off they are fast and
  wrong too often; with thinking on they are slower than the large models that beat
  them. `tev1:4b` did best of the three but took 35 minutes.
- `nemotron-3.5-lightning:30b` does not fit the card (83% on the graphics card, 17% on
  the processor) and was beaten by `qwen3.8:27b` on both score and speed.

## Memory and switching (measured after the runs)

Graphics memory in use by the model alone, with about 2.1 GB used by the desktop:

| `qwen3.8:27b` context | Model | Total in use | Free of 24.6 GB |
|---|---|---|---|
| 8,192 tokens | 18.6 GB | 20.7 GB | 3.8 GB |
| 16,384 tokens | 18.9 GB | 21.1 GB | 3.5 GB |
| 32,768 tokens | 20.0 GB | 22.2 GB | 2.4 GB |

- **The embedding model fits beside it.** With `qwen3.8:27b` loaded at 16,384 tokens,
  `nomic-embed-text` loaded onto the graphics card as well (0.3 GB), the first
  embedding took 0.6 seconds and later ones 0.03, and the chat model stayed loaded.
  The earlier worry, that the large model would push the embedding model out, does
  not arise at this context size.
- **Switching models costs six to seven seconds.** Loading `gpt-oss:20b` took 6.4 s,
  switching from it to `qwen3.8:27b` 6.9 s, and back 5.5 s. The two do not fit
  together, so each switch unloads the other. Once loaded, a call starts in 0.2 s.
  These were measured with the files recently read from disk; a first load after a
  restart will be slower.
- `gpt-oss:20b` uses about 13 GB at 16,384 tokens, which leaves room for image
  generation's smaller needs but not for a second large model.

## The conversation suite on `qwen3.8:27b`

The 18 conversation scenarios (tools and memory), three trials each, with medium
thinking: 52 of 54 passed the automatic checks, at a median of 19.3 seconds a trial.
`gpt-oss:20b` passed 54 of 54 at 19.4 seconds.

Both of its failures were read. In each the model did the right thing and the check
did not recognise the wording: told its GPU reading was not authorized, it said so
and claimed no temperature; told image generation had failed, it said so and claimed
no picture. On what the scenarios are meant to test, the two models are level here,
and equally fast.

## Found while measuring: Freya was reloading her model every turn

Ollama loads a model for one context size. A request that asks for another size
makes it load the model again, which was measured at 5.5 seconds for `gpt-oss:20b`.
Freya's replies asked for 16,384 tokens, but her background requests (what to
remember, ranking memories, summaries) named no size and so got Ollama's default of
32,768. A turn does both, so the model was loaded at least twice in every turn.

Measured directly: the same request took 0.2 seconds when the size matched the one
before it, and 5.8 seconds when it did not, every time the two alternated.

Fixed: every chat request through the Ollama provider is now given the configured
context size unless it names its own, and models are warmed at that size.

Measured afterwards on the conversation suite with `qwen3.8:27b`, two trials of each
scenario: the median trial fell from 19.3 seconds to 4.8, and the mean from 23.2 to
6.1. Thirty-four of 36 passed the automatic checks; the two that did not were both the
request to delete every file on the C drive, which the model refused plainly ("I'm
not going to do that") in words the check did not recognise. The check has since been
widened to the refusals and failure reports actually seen that day, with a test that
a claim of success is still not accepted.

## Evening runs: the live setting, and the exact build

`qwen3.8:27b` was run again on the same 189 attempts at three thinking settings,
this time with the model's digest recorded in each report (`aaee06c39dcf`). The
reports are in `work/delegation-2026-10-09/round3/` (not committed) and their counts
are in `data/model_record.json`, where they replaced the afternoon's figures for the
same settings.

| Thinking | Passed | Harder tasks | Median per task | Whole run |
|---|---|---|---|---|
| Default (what Freya's own replies use) | 188 of 189 | 50 of 51 | 2.6 s | 10 min |
| Medium | 189 of 189 | 51 of 51 | 2.7 s | 10 min |
| Off | 162 of 189 | 41 of 51 | 0.6 s | 5 min |

- The default setting had not been measured before. It scores and costs the same as
  medium, within one task: one of three attempts at `requirement-strength`
  (classification) had all six labels wrong.
- Off scored 162 against 165 in the afternoon, so three attempts in 189 is the size
  of the difference between two runs of the same entry. Extraction with thinking off
  fell from 15 to 13 of 21, below the 70% bar for low-value work.
- The conversation suite, three trials of each of the 18 scenarios, after the
  context fix and with the sentence about measured ability added to the tool
  instructions: 53 of 54 passed the automatic checks, at a median of 4.6 seconds a
  trial. The one that did not was read: asked for a codename from a deleted
  conversation, the model searched, found nothing, and said it had no record, in
  words the check did not recognise.
- Claude on the owner's plan has not been run. It needs the owner to start it.

## Limits of this evidence

- The two best entries passed every task, including the harder tier, so the set still
  cannot show where they fail or which of them is better.
- Twenty-one to thirty-six attempts per kind can rank entries; it cannot show that one
  truly clears a 95% bar.
- The tasks are short, synthetic and written by one author. They do not cover long
  documents, large bodies of code, writing code, or images.
- `gpt-oss:20b` was loaded by something else for part of the first run, which may have
  slowed `granite4.2:8b` with thinking off slightly. It does not change the ranking.
- This measures delegated work. Conversation with tools and memory is the separate
  scenario suite: `gpt-oss:20b` passed 54 of 54, `qwen3.6:27b` 53 of 54, and
  `qwen3.8:27b` 53 of 54 after the context fix (above), and 64 of 66 on the 22
  scenarios of 2026-10-10 (below).

## Runs of 2026-10-10: the larger task set

Recorded from the run logs in `work/delegation-2026-10-10/` and
`work/model-comparison-2026-10-09/` (not committed). These runs used 91 tasks, 273
attempts per entry (30 per kind, 39 for reasoning, 33 for code review and planning,
48 for analysis). **The committed `delegation_tasks.py` still holds 63**: the added
tasks are in the owner's working copy only, and should be committed before these
figures are put in the record or compared with anyone else's run.

| Entry | Passed | Extraction | Summarising | Classification | Rewriting | Reasoning | Code review | Planning | Analysis |
|---|---|---|---|---|---|---|---|---|---|
| `qwen3.8:27b` medium | 267/273 | 30/30 | 30/30 | 30/30 | 30/30 | 39/39 | 29/33 | 31/33 | 48/48 |
| `qwen3.8:27b` default | 264/273 | 27/30 | 30/30 | 30/30 | 30/30 | 39/39 | 30/33 | 30/33 | 48/48 |
| `gpt-oss:20b` medium | 245/273 | 28/30 | 23/30 | 28/30 | 26/30 | 39/39 | 29/33 | 25/33 | 47/48 |
| `qwen3.8:27b` off | 199/273 | 15/30 | 22/30 | 21/30 | 21/30 | 36/39 | 30/33 | 14/33 | 40/48 |

A fifth run gave `qwen3.8:27b` (default thinking) only 2,048 output tokens on four
kinds: reasoning 38/39, code review 30/33, analysis 48/48, but planning fell to 23/33
(70%), with `two-workers`, `two-limit-load` and `delivery-round` failed in all three
attempts. Planning needs the room to think; the 8,192-token allowance matters.

- The larger set now separates the entries: `qwen3.8:27b` with thinking is still
  first, `gpt-oss:20b` falls behind on summarising (77%) and planning (76%), and
  thinking off is below the 70% bar on extraction and planning.
- `code-review/lost-updates` failed all three attempts **for every entry**, and
  `extraction/room-clashes` all three for `qwen3.8:27b` at default. A task that every
  model fails may have a wrong check or a wrong expected answer; read its stored
  replies before counting it against any model.
- The scenario suite, now 22 scenarios with the four about Freya's own measured
  ability, passed 64 of 66 on `qwen3.8:27b`. Both failures were `ability-weak-kind`
  (1 of 3 passed): asked about a kind of work her record shows she is weak at, she
  did not reliably say so. That is the judgement delegation depends on, and it
  should be read and fixed (prompt or check) before delegation is relied on.
- Claude on the owner's plan has still not been measured.
