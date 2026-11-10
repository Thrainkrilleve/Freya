# Running a larger model on this workstation

Status: a proposal and a measuring tool, written 2026-11-10. Nothing here changes
which model Freya uses. Figures from other people's machines are marked as such; none
has been measured on this PC yet.

## Where things stand

- The card is an RTX 4090 with 24 GB; about 2 GB is taken by the desktop. System
  memory is 64 GB (checked 2026-10-11, `code-sandbox.md`).
- Freya answers with `qwen3.8:27b` (17 GB file, about 19 GB on the card at 16,384
  tokens). It passed every delegated task in the 2026-10-09 benchmark.
- `nemotron-3.5-lightning:30b` (25 GB) was the only model tried that did not fit the
  card; Ollama put 17% of it in system memory.

So about 22 GB on the card and roughly 50 GB of system memory (after Windows, Freya,
speech recognition, Kokoro and the window) are available to a model and its cache:
about 72 GB in all.

## What actually lets a larger model run

**1. A mixture-of-experts model with its experts in system memory. This is the one
that works.** A dense model reads every weight for every token, so whatever sits in
system memory, which is read 10 to 20 times slower than the card, sets the pace. A
mixture-of-experts model reads only a few of its experts per token. If the experts
are what leaves the card and everything else stays on it, a model three times the
size of the card can still answer at conversation speed.

The estimates from `python -m freya.gpu fit` for this machine at 16,384 tokens:

| Model | File | Where it goes | Speed ceiling |
|---|---|---|---|
| `qwen3.8:27b` (today) | 17 GB | all on the card | ~48 tokens/s |
| a 70B dense model, 4-bit | ~40 GB | 22 GB card, 25 GB memory | ~2 tokens/s |
| `gpt-oss:120b` (117B total, 5.1B active) | ~61 GB | 22 GB card, 41 GB memory | ~65 tokens/s |
| Qwen3.8 Flash Next, 2-bit (125B, 6B active) | ~68 GB | 22 GB card, 49 GB memory | ~57 tokens/s |

The ceiling is worked out from memory speeds and is always above what is achieved.
Reports from other machines put `gpt-oss:120b` with experts in memory at roughly 25 to
40 tokens a second on a 4090 with DDR5, depending on memory speed. The 70B dense
model is the cautionary row: it fits, but at two tokens a second it is not usable for
conversation.

`gpt-oss:120b` is the strongest candidate: Apache 2.0, same family as the
`gpt-oss:20b` Freya used until October, about 61 GB, and with 41 GB in system memory it
leaves around 9 GB spare. Qwen3.8 Flash Next fits only at 2-bit (its 4-bit file is
about 94 to 120 GB), and leaves almost nothing spare; 2-bit costs quality, so it would
have to earn its place in the benchmark.

**2. Making the cache smaller.** A cache stored at 8 bits (`q8_0`) takes about half
the memory of the default and costs little accuracy; it buys either a longer context
or a little more of the model on the card. In Ollama this is a setting for the whole
program (`OLLAMA_FLASH_ATTENTION=1`, `OLLAMA_KV_CACHE_TYPE=q8_0`), so changing it is an
owner action and affects every model. `fit --kv-cache q8_0` shows the difference.

**3. Squeezing a bigger dense model onto the card.** A 70B model at about 2 bits fits
in 22 GB, but loses a lot of quality at that size. Not recommended: a well-quantized
mixture-of-experts model is better in the same memory.

**4. The runtime.** Reports say that since Ollama 0.30 (May 2026) its models run in
llama.cpp's server, and that when no layer count is forced, llama.cpp's automatic
fitting moves experts to system memory first. Freya never forces a layer count, so
this may already happen; `measure` shows where the model actually went. These reports
come from articles, not from Ollama's own documentation, so measure before relying on
them.

If Ollama's placement turns out poor, the next step is running llama.cpp's own server
directly, which has explicit controls: `--n-cpu-moe N` (experts of the first N layers
in memory) and, merged in October 2026, `--moe-cache-mib N`, which keeps the most used
experts on the card and was reported at about 1.6 times faster on a 4090 for one large
model. That would be a second local model provider beside `ollama-local`, which fits
the agreed decision that Ollama is a replaceable runtime. It is not built: measure
first.

**5. Spreading one model over several machines.** llama.cpp can split a model across
machines on the network (its RPC back end). Every token then waits on the network,
and the spare laptops would add 8 to 16 GB each while slowing every token, so for a
machine with 88 GB of its own this is not worth it. The laptops are better used for
small models of their own or for Home Assistant. The federation work owns enrolling
other machines; benchmarking a split runtime stays here if it is ever wanted.

## What a large model costs Freya

- **Reloading.** Freya unloads her model for every picture and every large voice
  engine. A 61 GB file is larger than Windows can keep cached in 64 GB beside
  everything else, so each reload reads it from disk: about 30 seconds from an NVMe
  drive, against about 7 for `qwen3.8:27b`. ComfyUI also wants system memory while it
  runs.
- **System memory.** With 41 GB of the model in memory, less is left for the rest. If
  Windows starts paging, everything slows.
- **Disk.** About 61 GB per large model.

## Proposal NEW-DN: a large model as an on-demand role

Keep `qwen3.8:27b` as the model that answers every turn. Add a role for a larger model
that is loaded only when work calls for it (the hardest planning or analysis, or the
"strongest model plans" tier of the model-tiers proposal), never while an image or a
voice engine needs the card, and unloaded afterwards. Which work goes to it would be
decided by the model record (measured pass rates), not by the model. This is a
provisional label and not adopted.

## Steps for the owner

1. Check free space on the drive holding Ollama's models (about 65 GB is needed).
2. `python -m freya.gpu fit` estimates every installed model;
   `python -m freya.gpu fit --name gpt-oss-120b --size-gb 61 --total-b 117 --active-b 5.1`
   checks one before downloading.
3. `ollama pull gpt-oss:120b`.
4. `python -m freya.gpu measure gpt-oss:120b` reports how much went on the card and
   how fast it writes. It unloads Freya's model until her next turn.
5. If it is fast enough: `python -m freya.evaluation.delegation --model gpt-oss:120b
   --think medium` gives the same benchmark the other models had.

## The tool

`freya/gpu/model_fit.py` reads the card and memory totals live, reads each model's
description from Ollama (layers, cache shape, experts), and estimates where it would
go. It loads nothing. `measure` loads one model at Freya's context size with no layer
count forced, asks one made-up question, and reports Ollama's own figures: how much is
on the card, load time, and writing speed. Neither sends anything personal or changes
any setting.
