# Local models on this workstation

Status: a record written 2026-10-09. It is not the component manifest the baseline
asks for (LIC-006), which also covers datasets, exact digests of every file, intended
use and review status. It records what is installed, under what terms as read from
each model's own licence text, and what was noticed.

| Model (Ollama name) | Id | Size | Licence | Used for |
|---|---|---|---|---|
| `gpt-oss:20b` | `9ba9cc2b4e02` | 13 GB | Apache 2.0 | Freya's main model until 2026-10-09; kept for comparison |
| `nomic-embed-text` | `0a109f422b47` | 274 MB | Apache 2.0 | Embeddings for memory search |
| `qwen3.6:27b` | `bcbdbd4bcf86` | 17 GB | Apache 2.0 | Benchmarked; not used by Freya |
| `qwen3.8:27b` | `aaee06c39dcf` | 17 GB | Apache 2.0 | Freya's replies and background work since 2026-10-09 (`docs/design/model-benchmarks-2026-10-09.md`) |
| `qwen3.5:9b` | `56671c2ab938` | 6.6 GB | Apache 2.0 | Benchmarked; not used by Freya |
| `granite4.2:8b` | `f586c02fdecd` | 5.3 GB | Apache 2.0 | Benchmarked; not used by Freya |
| `tev1:4b` | `9b5bb969e46c` | 4.5 GB | Apache 2.0 | Benchmarked; not used by Freya |
| `nemotron-3.5-lightning:30b` | `e7a64ff15fb1` | 25 GB | NVIDIA Open Model License | Benchmarked; not used by Freya |
| `freya-qwen36-preserve:27b-v2` | `a237527920fc` | 17 GB | Not checked | A local variant of `qwen3.6:27b`; origin not recorded here |

The four models added on 2026-10-09 (`granite4.2:8b`, `tev1:4b`, `qwen3.8:27b`,
`nemotron-3.5-lightning:30b`) were downloaded at the owner's request for benchmarking.

## The NVIDIA Open Model License (read in full, version of 24 October 2025)

This is a reading of the licence text that came with the model, not legal advice.

- The model may be used commercially, derivative models may be made and distributed,
  and NVIDIA claims no ownership of what the model outputs.
- **Guardrails.** Rights end automatically if a safety guardrail or other technical
  limitation contained in the model is bypassed, disabled or weakened without a
  substantially similar one being put in its place. Using the model as supplied,
  including switching its thinking on or off, does not touch this. Modifying or
  fine-tuning it to remove a safeguard would.
- **Trustworthy AI terms.** Use must be consistent with NVIDIA's Trustworthy AI terms,
  which are on NVIDIA's website and were not read for this note.
- **Passing it on.** Anyone given a copy must also be given the agreement and a
  "Notice" file reading "Licensed by NVIDIA Corporation under the NVIDIA Open Model
  License". This matters if Freya is ever installed on other nodes with the model.
- **It can change.** NVIDIA may update the agreement to meet legal requirements, and
  the choice is then to comply or stop using the model. The licence is revocable for
  breach, and ends for anyone who sues claiming the model infringes.
- The user indemnifies NVIDIA against third-party claims arising from their use.

Nothing in it prevents the present use: one person benchmarking and running the model
unmodified on their own PC.

## Notes

- `nemotron-3.5-lightning:30b` is 25 GB, larger than the 24 GB graphics card, so part
  of it runs on the processor.
- `tev1:4b` is listed by Ollama as a "decision" model, not a general chat model.
- Ollama also offers models that run on its own servers. Freya's local provider
  refuses those: see `providers/model/ollama.py`.
