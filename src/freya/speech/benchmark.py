"""
Measure the speech engines against each other on this computer.

    python -m freya.speech.benchmark
    python -m freya.speech.benchmark --reference me.wav --transcript "..." --i-have-the-right

Every engine speaks the same sentences. The cloning engines copy the
same reference recordings: by default two synthetic ones made with
Kokoro's supplied voices, so no real person's voice is involved. To
use a real recording, pass it with --reference and state that you have
the right to use it.

What is measured:
  - time to first audio and total time, per sentence, once loaded
  - how long the engine takes to load
  - peak graphics memory and peak main memory
  - pronunciation: Freya's speech recognition transcribes the result
    and the share of words it gets wrong is reported
  - speaker similarity to the reference, by Chatterbox's speaker
    encoder (a number, not a listening test)

Naturalness is not measured. The clips are saved so it can be judged
by ear.
"""
import argparse
import json
import re
import statistics
import time
from pathlib import Path

import numpy as np
import psutil
import soundfile as sf

from freya.speech.chatterbox_engine import ChatterboxEngine
from freya.speech.engines import ROLE_CLONE, ROLE_DESIGN, ROLE_PRESET
from freya.speech.helper import DATA_DIR
from freya.storage.locations import store_path
from freya.speech.kokoro_engine import KokoroEngine
from freya.speech.qwen_engine import QwenEngine
from freya.speech.references import MAX_SECONDS, ReferenceStore
from freya.speech.setup import make_room_on_gpu


SENTENCES = {
    "short": "Good morning. The kettle is on, and your first meeting is at nine.",
    "long": (
        "I checked the forecast for you: light rain until early afternoon, "
        "then clearing, with a high of about eighteen degrees. The parcel "
        "from Edinburgh should arrive on Thursday, and I have moved your "
        "dentist appointment to the following Wednesday."
    ),
}

REFERENCE_TEXT = (
    "The quick brown fox jumps over the lazy dog near the river bank. "
    "She sells sea shells by the sea shore, and the shells she sells "
    "are surely sea shells."
)

SYNTHETIC_REFERENCES = {
    "synthetic-female": {"language": "a", "voice": "af_heart", "speed": 1.0},
    "synthetic-male": {"language": "b", "voice": "bm_george", "speed": 1.0},
}

DESIGN_DESCRIPTION = (
    "A calm, warm woman in her thirties with a low, gentle voice."
)


# Speech recognition writes some numbers as digits. They are read back
# as words so that a correctly spoken number is not counted as wrong.
NUMBERS = {"9": "nine", "18": "eighteen"}


def words(text: str):
    text = re.sub(r"(\d):00\b", r"\1", text.lower().replace("-", " "))
    text = re.sub(r"[^a-z0-9' ]+", " ", text)

    return [NUMBERS.get(word, word) for word in text.split()]


def word_error_rate(expected: str, heard: str) -> float:
    """
    The share of the expected words that were heard wrongly, missed,
    or added: the usual edit distance over words.
    """
    a, b = words(expected), words(heard)

    if not a:
        return 0.0

    row = list(range(len(b) + 1))

    for i, word in enumerate(a, 1):
        previous, row[0] = row[0], i

        for j, other in enumerate(b, 1):
            previous, row[j] = row[j], min(
                row[j] + 1,
                row[j - 1] + 1,
                previous + (word != other),
            )

    return round(row[-1] / len(a), 3)


def to_pcm(audio) -> bytes:
    return (np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes()


def middle(values):
    values = [value for value in values if value is not None]

    return round(statistics.median(values), 3) if values else None


class Benchmark:
    def __init__(self, out_dir: Path, runs: int):
        self.out_dir = out_dir
        self.runs = runs
        self.references = ReferenceStore(out_dir / "references")
        self.kokoro = KokoroEngine()
        self.chatterbox = ChatterboxEngine(
            self.references, make_room=make_room_on_gpu, idle_seconds=0
        )
        self.qwen = QwenEngine(
            self.references, make_room=make_room_on_gpu, idle_seconds=0
        )
        self.rows = []
        self._listener = None

    # -- measuring -------------------------------------------------------

    def heard(self, audio, sample_rate) -> str:
        if self._listener is None:
            from freya.voice.stt import FreyaSTT

            self._listener = FreyaSTT()

        return self._listener.transcribe_audio(audio, sample_rate)

    def kokoro_once(self, text, settings):
        """
        Kokoro yields speech a sentence at a time, so its first audio
        arrives before the whole clip is done.
        """
        settings = self.kokoro.validate(ROLE_PRESET, settings)
        pipeline = self.kokoro._pipeline(settings["language"])
        started = time.perf_counter()
        first = None
        parts = []

        for _, _, audio in pipeline(
            text, voice=settings["voice"], speed=settings["speed"]
        ):
            if audio is None:
                continue

            if first is None:
                first = time.perf_counter() - started

            parts.append(np.asarray(audio, dtype=np.float32))

        return np.concatenate(parts), 24000, {
            "first_audio_seconds": round(first, 3),
            "generate_seconds": round(time.perf_counter() - started, 3),
            "gpu_peak_mb": 0,
        }

    def measure(self, label, reference_name, speak, load_seconds, memory):
        """
        Speak each sentence `runs` times with an engine that is already
        loaded, and record the middle value of each measurement.
        """
        for sentence_name, text in SENTENCES.items():
            first, total, gpu, errors, lengths = [], [], [], [], []
            transcript = ""
            clip = None

            for run in range(self.runs):
                audio, sample_rate, metrics = speak(text)
                first.append(metrics["first_audio_seconds"])
                total.append(metrics["generate_seconds"])
                gpu.append(metrics.get("gpu_peak_mb"))
                lengths.append(len(audio) / sample_rate)
                heard = self.heard(audio, sample_rate)
                errors.append(word_error_rate(text, heard))

                if run == 0:
                    transcript = heard
                    clip = self.out_dir / (
                        f"{label}__{reference_name}__{sentence_name}.wav"
                    ).replace(" ", "_")
                    sf.write(clip, audio, sample_rate)

            row = {
                "engine": label,
                "reference": reference_name,
                "sentence": sentence_name,
                "load_seconds": load_seconds,
                "first_audio_seconds": middle(first),
                "total_seconds": middle(total),
                "audio_seconds": middle(lengths),
                "speed_ratio": round(middle(lengths) / middle(total), 1),
                "gpu_peak_mb": max(
                    [value for value in gpu if value is not None], default=None
                ),
                "ram_peak_mb": memory(),
                "word_error_rate": round(statistics.mean(errors), 3),
                "clip": clip.name,
                "heard": transcript,
                "similarity": None,
            }
            self.rows.append(row)
            print(
                f"  {label:26} {reference_name:17} {sentence_name:5} "
                f"first {row['first_audio_seconds']:6.2f}s  "
                f"total {row['total_seconds']:6.2f}s  "
                f"words wrong {row['word_error_rate']:.1%}",
                flush=True,
            )

    def helper_speak(self, engine, kind, settings):
        def speak(text):
            audio, sample_rate = engine.synthesize(text, kind, settings)

            return audio, sample_rate, engine.last_metrics

        return speak

    def helper_variant(self, label, engine, kind, settings, reference_name):
        # Start from nothing loaded, so the load time is a true one.
        self.chatterbox.unload()
        self.qwen.unload()

        try:
            engine.synthesize("Ready.", kind, settings)
        except Exception as exc:
            print(f"  {label:26} {reference_name:17} FAILED: {exc}", flush=True)
            self.rows.append(
                {
                    "engine": label,
                    "reference": reference_name,
                    "failed": str(exc)[:300],
                }
            )
            return

        self.measure(
            label,
            reference_name,
            self.helper_speak(engine, kind, settings),
            engine.last_metrics["load_seconds"],
            engine._helper.memory_megabytes,
        )

    # -- the run -----------------------------------------------------------

    def run(self, references: dict):
        own_memory = lambda: round(  # noqa: E731
            psutil.Process().memory_info().rss / 1024 / 1024
        )

        print("Kokoro (supplied voices, no cloning)", flush=True)
        started = time.perf_counter()
        self.kokoro_once("Ready.", SYNTHETIC_REFERENCES["synthetic-female"])
        kokoro_load = round(time.perf_counter() - started, 3)

        for name, settings in SYNTHETIC_REFERENCES.items():
            self.measure(
                "Kokoro",
                name.replace("synthetic", "preset"),
                lambda text, settings=settings: self.kokoro_once(text, settings),
                kokoro_load,
                own_memory,
            )

        for name, reference in references.items():
            print(f"Cloning from {name} ({reference['seconds']}s)", flush=True)
            base = {"reference": reference["id"]}

            for model, label in (
                ("turbo", "Chatterbox Turbo"),
                ("nano", "Chatterbox Nano"),
                ("multilingual", "Chatterbox Multilingual V3"),
            ):
                self.helper_variant(
                    label, self.chatterbox, ROLE_CLONE,
                    {**base, "model": model}, name,
                )

            for model, label in (
                ("0.6b", "Qwen3-TTS 0.6B"),
                ("1.7b", "Qwen3-TTS 1.7B"),
            ):
                self.helper_variant(
                    label, self.qwen, ROLE_CLONE,
                    {**base, "model": model, "language": "English"}, name,
                )

        print("Designing a voice from a description", flush=True)
        self.chatterbox.unload()
        self.qwen.unload()
        design = {"description": DESIGN_DESCRIPTION, "language": "English"}
        design_text = "Hello. This is how I sound with this voice. I hope you like it."

        try:
            audio, sample_rate = self.qwen.design(design_text, design)
            designed = self.references.add_designed(
                audio, sample_rate, self.qwen.id, design_text
            )
            references = {**references, "designed": designed}
            self.design_metrics = dict(self.qwen.last_metrics)
            sf.write(self.out_dir / "designed_sample.wav", audio, sample_rate)
            self.helper_variant(
                "Qwen3-TTS designed voice", self.qwen, ROLE_DESIGN,
                {**design, "reference": designed["id"], "model": "1.7b"},
                "designed",
            )
        except Exception as exc:
            self.design_metrics = {"failed": str(exc)[:300]}
            print(f"  design FAILED: {exc}", flush=True)

        self.qwen.unload()
        self.score(references)
        self.chatterbox.unload()

    def score(self, references: dict):
        """
        Speaker similarity of every cloned clip to its reference, and
        a check that Chatterbox's watermark is still in its clips.
        """
        print("Scoring speaker similarity", flush=True)
        self.watermarks = {}

        for row in self.rows:
            reference = references.get(row.get("reference"))

            if reference is None or "clip" not in row:
                continue

            clip = self.out_dir / row["clip"]
            row["similarity"] = round(
                self.chatterbox.speaker_similarity(
                    self.references.path(reference["id"]), clip
                ),
                3,
            )

            if row["engine"].startswith("Chatterbox"):
                self.watermarks[row["clip"]] = self.chatterbox.watermark_strength(
                    clip
                )

    # -- the report --------------------------------------------------------

    def report(self) -> str:
        lines = [
            "| Engine | Reference | Sentence | Load (s) | First audio (s) | "
            "Total (s) | Audio (s) | x real time | GPU peak (MB) | "
            "RAM peak (MB) | Words wrong | Similarity |",
            "|---|---|---|---|---|---|---|---|---|---|---|---|",
        ]

        for row in self.rows:
            if "failed" in row:
                lines.append(
                    f"| {row['engine']} | {row['reference']} | FAILED: "
                    f"{row['failed']} | | | | | | | | | |"
                )
                continue

            similarity = (
                "n/a" if row["similarity"] is None else f"{row['similarity']:.2f}"
            )
            lines.append(
                f"| {row['engine']} | {row['reference']} | {row['sentence']} | "
                f"{row['load_seconds']:.1f} | {row['first_audio_seconds']:.2f} | "
                f"{row['total_seconds']:.2f} | {row['audio_seconds']:.1f} | "
                f"{row['speed_ratio']} | {row['gpu_peak_mb']} | "
                f"{row['ram_peak_mb']} | {row['word_error_rate']:.1%} | "
                f"{similarity} |"
            )

        return "\n".join(lines)


def load_reference(store: ReferenceStore, path: Path, transcript: str) -> dict:
    audio, sample_rate = sf.read(path, dtype="float32", always_2d=True)
    audio = audio.mean(axis=1)[: int(sample_rate * MAX_SECONDS)]

    return store.add_upload(to_pcm(audio), int(sample_rate), True, transcript)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--reference", type=Path, action="append", default=[])
    parser.add_argument("--transcript", action="append", default=[])
    parser.add_argument(
        "--i-have-the-right",
        action="store_true",
        help="state that you have the right to use the --reference voices",
    )
    parser.add_argument("--out", type=Path)
    arguments = parser.parse_args()

    if arguments.reference and not arguments.i_have_the_right:
        parser.error(
            "pass --i-have-the-right to confirm you may use these recordings"
        )

    out_dir = arguments.out or (
        store_path("voice_benchmarks") / time.strftime("%Y%m%d-%H%M%S")
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    benchmark = Benchmark(out_dir, arguments.runs)
    references = {}

    for name, settings in SYNTHETIC_REFERENCES.items():
        audio, sample_rate = benchmark.kokoro.synthesize(
            REFERENCE_TEXT, ROLE_PRESET, settings
        )
        references[name] = benchmark.references.add_upload(
            to_pcm(audio), sample_rate, True, REFERENCE_TEXT
        )

    for index, path in enumerate(arguments.reference):
        transcript = (
            arguments.transcript[index]
            if index < len(arguments.transcript)
            else ""
        )
        references[path.stem] = load_reference(
            benchmark.references, path, transcript
        )

    try:
        benchmark.run(references)
    finally:
        benchmark.chatterbox.unload()
        benchmark.qwen.unload()

    report = benchmark.report()
    (out_dir / "results.md").write_text(report + "\n", encoding="utf-8")
    (out_dir / "results.json").write_text(
        json.dumps(
            {
                "rows": benchmark.rows,
                "design": getattr(benchmark, "design_metrics", None),
                "watermarks": getattr(benchmark, "watermarks", None),
                "runs": arguments.runs,
                "sentences": SENTENCES,
            },
            indent=1,
        ),
        encoding="utf-8",
    )

    print()
    print(report)
    print()
    print("Design sample:", getattr(benchmark, "design_metrics", None))
    print("Chatterbox watermark strength:", getattr(benchmark, "watermarks", None))
    print(f"Clips and results are in {out_dir}")


if __name__ == "__main__":
    main()
