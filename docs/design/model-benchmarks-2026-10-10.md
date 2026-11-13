# Local model benchmarks, 2026-10-10: the hardest tasks

Status: measurements, not a decision. This follows
`model-benchmarks-2026-10-09.md`, where the two best entries passed every task and
the set could no longer show where they fail.

Twenty-eight harder tasks were added, three or four in each of the eight kinds, for
91 tasks in all. Each was run three times (273 attempts per entry) on this
workstation with a 16,384-token context and an 8,192-token output allowance. A pass
means the automatic check passed, not that a person reviewed the reply. Every reply
that failed in the `qwen3.8:27b` runs with thinking was read; one check was found
unfair and corrected (it refused bookings named "2" instead of "B02"), and the stored
replies were graded again. The reports are in `work/delegation-2026-10-10/` (not
committed) and their counts are in `data/model_record.json`.

| Entry | Passed | The 63 older tasks | The 28 hardest | Empty replies | Median per task | Whole run |
|---|---|---|---|---|---|---|
| `qwen3.8:27b`, default thinking (Freya's replies) | 267 of 273 | 189 of 189 | 78 of 84 | 4 | 3.7 s | 31 min |
| `qwen3.8:27b`, medium thinking | 267 of 273 | 189 of 189 | 78 of 84 | 1 | 3.6 s | 29 min |
| `gpt-oss:20b`, medium thinking | 245 of 273 | 181 of 189 | 64 of 84 | 7 | 1.9 s | 20 min |
| `qwen3.8:27b`, thinking off | 196 of 273 | 165 of 189 | 31 of 84 | 0 | 0.7 s | 9 min |

By kind of task (passed of attempts):

| Entry | Extraction | Summarising | Classification | Rewriting | Reasoning | Code review | Planning | Analysis |
|---|---|---|---|---|---|---|---|---|
| `qwen3.8:27b` default | 30/30 | 30/30 | 30/30 | 30/30 | 39/39 | 30/33 | 30/33 | 48/48 |
| `qwen3.8:27b` medium | 30/30 | 30/30 | 30/30 | 30/30 | 39/39 | 29/33 | 31/33 | 48/48 |
| `gpt-oss:20b` medium | 28/30 | 23/30 | 28/30 | 26/30 | 39/39 | 29/33 | 25/33 | 47/48 |
| `qwen3.8:27b` off | 15/30 | 22/30 | 21/30 | 21/30 | 35/39 | 28/33 | 14/33 | 40/48 |

## What the numbers say

- **The new tasks separate the entries.** On the older 63 the gap between
  `qwen3.8:27b` with thinking and `gpt-oss:20b` was eight attempts; on the hardest 28
  it is fourteen, and with thinking off `qwen3.8:27b` passes barely a third of them.
- **`qwen3.8:27b` with thinking now has known failures.** Against the owner's 95% bar
  for high-value work it falls short on code review (30 or 29 of 33, about 91% and
  88%) and on planning (30 or 31 of 33, about 91% and 94%), and clears it on the
  other six kinds. Freya's record now tells her so.
- **One task it never gets right.** Asked the least a counter can reach after two
  threads each add one to it three times without a lock, it answered 1 in every
  attempt at every setting, with a confident argument. The answer is 2, which a test
  confirms by trying every interleaving. This is the clearest example of it being
  wrong and sure.
- **Thinking can use up the whole reply.** Four of its six misses at the default
  setting were empty replies: on the three planning tasks that need a best possible
  plan (two workers, a load with two limits, a delivery round) it sometimes thought
  for the full 8,192 tokens, over a minute, and returned nothing. Medium thinking did
  this once. An empty reply after a long wait is worse than a wrong one, and a caller
  should treat it as a failure to retry or hand on, not as an answer.
- **Default and medium thinking are still level**: the same total, with different
  attempts missed.
- **Thinking off is for the simplest work only.** It is five times quicker and wrong
  on most things that need working out or an exact form.

## Her live replies are allowed a quarter of that output

The runs above allow 8,192 tokens of output. Freya's own replies are allowed 2,048
(`ContextBudget.output_tokens`), and the model's thinking counts against them. Four
kinds were run again at 2,048 tokens with default thinking (153 attempts):

| Output allowed | Passed | Empty replies | Reasoning | Code review | Planning | Analysis |
|---|---|---|---|---|---|---|
| 8,192 tokens | 147 of 153 | 4 | 39/39 | 30/33 | 30/33 | 48/48 |
| 2,048 tokens | 139 of 153 | 13 | 38/39 | 30/33 | 23/33 | 48/48 |

At her live limit every attempt at four of the hardest tasks came back empty, and
planning fell from 30 to 23 of 33. In a live turn an empty reply was shown as nothing
at all and the question was dropped from the conversation. A fix on its own branch
(`fix/empty-reply-notice`) makes her say that she did not manage an answer. Whether
to allow her more output is the owner's choice: every token reserved for output is
one fewer for the conversation in a 16,384-token window.

### Measured on 2026-10-11: a middle limit buys almost nothing

The same 153 attempts were run at two limits in between, in a 20,480-token window so
that the conversation kept its room (about 21.4 GB of video memory, against about 19):

| Output allowed | Passed | Empty replies | Reasoning | Code review | Planning | Analysis |
|---|---|---|---|---|---|---|
| 2,048 tokens | 139 of 153 | 13 | 38/39 | 30/33 | 23/33 | 48/48 |
| 3,072 tokens | 142 of 153 | 11 | 39/39 | 30/33 | 25/33 | 48/48 |
| 4,096 tokens | 142 of 153 | 11 | 39/39 | 30/33 | 25/33 | 48/48 |
| 8,192 tokens | 147 of 153 | 4 | 39/39 | 30/33 | 30/33 | 48/48 |

Doubling the limit recovers three attempts. The empty replies are the same four or
five tasks every time (the counter without a lock, and the planning tasks that ask
for a best possible plan), and on those the model thinks until whatever limit it is
given runs out; the slowest took 53 seconds to return nothing. So the limit stays at
2,048 and the window at 16,384. Work of that kind is what handing over to a stronger
model is for, and an empty reply is already reported to the owner as one. One run at
each limit: differences of two or three attempts are within what a second run would
move.

## The conversation suite, with four scenarios about her own ability

The suite gained four scenarios that give Freya a made-up model record: asked how she
compares with Claude at writing code (nobody is measured); asked whether to do an
important planning job herself (the record says she is weak and Claude strong);
asked whether she can review a short function (she is measured strong); and asked for
her figures when reading the record is refused.

`qwen3.8:27b`, three trials of each of the 22 scenarios: 64 of 66 passed the
automatic checks at a median of 5.1 seconds a trial, and all 54 trials of the older 18
scenarios passed. Every answer in the ability scenarios was read. She read the record
before answering in all 12 trials and never claimed a result for work the record
did not cover. The two that did not pass were right answers in words the new checks did not
recognise (calling Claude "the Anthropic plan model", and "I've passed 11 out of 21"
being taken for a claim to have passed the work on); the checks were widened, with
those replies as tests.

Three things seen in the reading, beyond what the checks test:

- She misquoted figures she had just read in three of the first 12 answers: Claude's
  21 of 21 on planning became "11 out of 11", and her own 21 of 21 on code review
  became "95 percent" twice, which is the bar and not her score. The direction of
  each answer was right. It is not fixed, and it means a figure she quotes should be
  checked against the record.

- She offered to hand the work over ("Want me to set that up?"), which she cannot do:
  no turn delegates. The record now tells her she cannot send work herself, and in a
  rerun she said so and left it to the owner.
- When reading the record was refused she gave no figures, but in two of three trials
  still said summarising is something she handles well. That is her own impression,
  which her standing instructions say is not evidence. It is not fixed.

## Limits of this evidence

- Thirty to forty-eight attempts per kind can rank entries. They cannot show that an
  entry truly clears a 95% bar: 30 of 30 is consistent with a true rate a little over
  90%.
- The tasks are still short, synthetic, written by one author (the new ones by
  Claude), and done as a worker on one brief with no tools. They do not cover long
  documents, large bodies of code, writing code, images, or work over many steps.
- Claude on the owner's plan has not been run on these tasks, so there is still no
  measured comparison with it. A run made with the 63-task set would not count
  towards the record once these tasks are in use.
- `gpt-oss:20b` at other settings and the other local models have not been run on the
  new tasks; their figures in the record are from the older set and no longer count.
