# Freya

Freya is a local-first, modular, multi-user agentic AI platform. It runs on
Windows, Linux, and macOS. The owner's machine is a Windows 11 workstation
with a Ryzen 7 9800X3D and an AMD Radeon RX 9070 XT (16 GB). Chat and
embeddings go to Ollama on `127.0.0.1:11434`. If `FREYA_MODEL` is unset, the
chat model is `qwen3.8:27b`. The embedding model is `nomic-embed-text`.

## Quick start

Python 3.12 (`>=3.12,<3.13`). From a checkout, `uv sync` installs the locked
dependencies into `.venv`. Ollama must already be running on this machine
with the chat model installed.

```
uv sync
FREYA_MODEL=qwen3.5-4b-mtp freya
```

On Windows PowerShell the same choice is `$env:FREYA_MODEL = "qwen3.5-4b-mtp"`
before `freya`. `qwen3.5-4b-mtp` is only an example of `FREYA_MODEL`. It is
not the default, and Freya does not download it.

- `freya` starts the console and the window.
- `freya-window` starts the window only, with no console.
- `python -m freya` is the same entry as `freya`.

Image generation uses ComfyUI on `127.0.0.1:8188` when a picture is asked for.

## Minecraft companion (Player 2, M1)

Thrain runs a Minecraft Java world on this machine. Freya can join it
through Mineflayer. The adapter is not registered with the control plane,
and Freya does not download Node, the plugins, or the game. The tests
mock the bridge, so CI does not need Node or a server.

Install Node.js 20 or newer. From `adapters/minecraft`:

```
npm install
```

That installs Mineflayer, mineflayer-pathfinder, mineflayer-pvp,
mineflayer-collectblock, mineflayer-tool, and mineflayer-armor-manager.
They are not vendored.

Start a local server bound to loopback, with online mode off so the
offline name can join. In `server.properties`:

```
server-ip=127.0.0.1
online-mode=false
```

Write an allow-list, for example `allow.json`:

```
[{"adapter":"minecraft","target":"127.0.0.1:25565"}]
```

Run one goal. Reflexes (eat when hungry, flee when health is low, dodge,
and jump when stuck) stay in the adapter. The play model name is
`qwen3.5-4b-mtp` unless `FREYA_PLAY_MODEL` names another installed model.
This command prints that name and does not load the model. A session that
does load a local planner refuses when free graphics memory is unknown.

```
python -m freya.player.minecraft --allow allow.json --target 127.0.0.1:25565 --goal get_wood
```

Goals are `build_shelter`, `get_iron`, `get_wood`, and `get_food`.

Type `stop`, or press Ctrl+C. Movement halts and the child process ends.
While she can act, the status line is `Freya in control`. When she
cannot, it is `Freya paused`. Those words are fixed.

Microsoft sign-in is optional. Set `FREYA_MINECRAFT_AUTH=microsoft` and
complete the device code Mineflayer prints on the error stream. The cache
stays in Mineflayer's own folder, outside this repository. Freya does not
read or store those tokens. Leave the variable unset for offline play on
the loopback server, which is the default. A server on another machine is
refused.

`FREYA_MINECRAFT_USERNAME` is the offline name (default `Freya`).
`FREYA_MINECRAFT_VERSION` is optional, for example `1.20.4`.
`FREYA_MINECRAFT_NODE` names the Node program when it is not `node` on
`PATH`.

The adapter opens no listening port. It talks to the child on standard
input and output, and the child connects only to a loopback address. A
public server, another machine, and EVE Online are refused.

## Graphics cards

Live GPU status (`system.gpu`), model fit (`python -m freya.gpu fit`), and
the free-memory check before a large speech engine all use `freya.gpu.status`.
Cards are read AMD first, then Intel, then NVIDIA. Within one vendor, a card
that reported dedicated memory is listed before that vendor's integrated
adapter. Model fit and the speech check use the first card that reported a
dedicated total, so an integrated adapter does not hide a discrete card. If
that card's free memory was not reported, speech does not borrow another
card's number. A missing tool does not crash the probe: the fields it would
have filled stay null.

| Vendor | Linux and macOS | Windows |
| --- | --- | --- |
| AMD | `rocm-smi` and `amd-smi` when they are on `PATH`. Also `/sys/class/drm/card*/device/mem_info_vram_total`, `mem_info_vram_used`, and `gpu_busy_percent`. | `amd-smi` and `rocm-smi` when present. Otherwise WMI (`Win32_VideoController`), DXGI dedicated video memory, and the display-class registry value `HardwareInformation.qwMemorySize`. |
| Intel | `xpu-smi discovery` and `xpu-smi stats` when present. i915 local memory is `lmem_total_bytes`. xe local memory is `physical_vram_size_bytes` and, when the driver exports it, `physical_vram_used_bytes`. | The same `xpu-smi` commands when present, then the same WMI, DXGI, and registry sources as AMD. |
| NVIDIA | `nvidia-smi` (name, temperature, utilization, total, used, free). | The same `nvidia-smi` query. The Windows adapter list can still name the card when `nvidia-smi` is absent. |

macOS uses the Linux tool list. Those tools are usually absent, and the
probe then reports no card.

`AdapterRAM` values at the 32-bit 4 GB ceiling are not treated as the card's
memory. An integrated adapter, such as Intel UHD on an Alder Lake N95, is
reported as Intel with `memory_kind` `shared` and no dedicated total. Shared
system memory from DXGI is not reported as VRAM.

Intel Arc parsing is covered by fixtures (xpu-smi text, i915 and xe sysfs,
and Windows WMI/DXGI adapter rows). It has not been run on a real Arc card.
This workstation does not have one.

## Folders and documents

The model can list, read, and search files only in folders the owner has
opened, and only for reading. Nothing is open until the owner runs
`/folder open <full path>`. `/folder close <full path>` removes one.
`/folders` lists them. There is no tool that writes files.

Chat document uploads are not supported. A chat turn is text. Binary uploads
on the window socket are for a voice-studio recording or a media transcript
import, not for attaching a document to the conversation.

## Recent changes

- Player 2 M1 is a Minecraft adapter for a loopback world. Setup is
  in the section above. The play model is still `qwen3.5-4b-mtp` unless
  `FREYA_PLAY_MODEL` names another installed model. It does not read
  `FREYA_MODEL`. Tests use a fake bridge and a fake planner. Nothing is
  registered with the control plane.
- Background model calls (memory decisions, summaries, and the other unseen
  work around a turn) send `think` false. Set `FREYA_BACKGROUND_THINK` to
  `on`, `low`, `medium`, `high`, or `xhigh` to turn that back on for those
  calls. `off` and any other value stay off. Freya's own replies are
  unchanged. This landed in PR #28.
- Federation profiling measures graphics AMD first, then Intel Arc, then
  NVIDIA, and writes profile version 2 (`runtimes` on each card). A version
  1 offer, which only listed NVIDIA cards and has no `runtimes`, still loads.
  This landed in PR #29.

## Off until the owner turns them on

- Media capabilities stay denied until `python -m freya.media.activate`.
- Cloud send stays off. No provider is switched on, no policy rule allows a
  cloud route, and no route has a usage limit until the owner sets them.
- World-view tools stay off unless `FREYA_WORLD_VIEW=1`.
- The code sandbox (`code.run`) and the local decider (`decide`) are not
  registered, and neither is in a policy seed.
- Player 2 is not registered. M0 is the schema and the mock. M1 is the
  Minecraft adapter, started only by the command above. The control
  plane does not load either of them.

## Memory protection

Startup calls `ensure_memory_protection()` before the assistant is built.
The first time the memory database is unprotected, Freya creates her tables
if needed, keeps a copy from before protection beside the ledger, and gives
every row an identifier. A database that is already protected is left as it
is. The assistant then opens it with that ledger: the two files are checked
against each other, and every recorded deletion is applied, before anything
else is read. If that setup fails, or a protected database has no ledger,
startup prints the reason and does not open memory.

The ledger is `freya_deletion_ledger/deletions.db` beside the data folder,
never inside it. `FREYA_DELETION_LEDGER` names another file and is refused
when that path is inside the memory database's folder. A fact forgotten
after a backup stays forgotten when that backup is restored, because the
ledger is replayed and is not rolled backwards. Other copies can still
bring a fact back. That is not full MEM-008 compliance.

## Database key

On Windows the first run wraps the database key with DPAPI and creates a
recovery key. It does not ask for a passphrase. From a console:

    python -m freya.storage.encrypt --show-recovery-key
    python -m freya.storage.keys add-passphrase

`add-passphrase` types the passphrase twice. It is at least 12 characters,
and it is not read from an argument or the environment. A wrap that is
already there is left in place; replace it with `rotate-passphrase`.
`python -m freya.storage.encrypt --with-passphrase` sets one only when
the key is being created. `FREYA_REQUIRE_PASSPHRASE=1` asks for the
passphrase on unlock even when DPAPI is present. Leave it unset and DPAPI
still opens the databases.
