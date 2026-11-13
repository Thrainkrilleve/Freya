"""
The Laya helper: answers one decision at a time with Laya's English
checkpoint (convaiinnovations/laya, Apache 2.0).

It runs in its own environment (data/decider_envs/laya), started by
Freya with that environment's interpreter, with the model hub offline.
It does not import Freya. It reads one JSON request per line on
standard input and writes one JSON answer per line back. It never
writes the text it was asked about anywhere.
"""
import json
import os
import sys
import traceback


# Answers go on the real standard output; anything the libraries print
# goes to standard error, so a stray print can never corrupt an answer.
_ANSWERS = os.fdopen(os.dup(1), "w", encoding="utf-8", newline="\n")
os.dup2(2, 1)
sys.stdout = sys.stderr


def answer(message):
    _ANSWERS.write(json.dumps(message) + "\n")
    _ANSWERS.flush()


def main():
    import torch
    import laya

    torch.manual_seed(0)
    revision = os.environ["LAYA_REVISION"]
    device = "cuda" if torch.cuda.is_available() else "cpu"
    agent = laya.load("convaiinnovations/laya", device=device, revision=revision)

    sys.stdin.reconfigure(encoding="utf-8")
    answer({"ready": True, "device": device})

    for line in sys.stdin:
        line = line.strip()

        if not line:
            continue

        try:
            request = json.loads(line)

            if request.get("op") != "decide":
                raise ValueError("Unknown operation.")

            question = request["question"]
            # One state per forward pass, so batch shape cannot change
            # the answer (Freya asks for the same answer every time).
            with torch.inference_mode():
                result = agent.system_one(request["state"], {"q": question})
            found = result["answers"]["q"]

            if question["type"] == "noul":
                answer({"ok": True, "p_true": float(found["noul"])})
            else:
                answer({
                    "ok": True,
                    "probabilities": {
                        str(key): float(value) for key, value in found["probabilities"].items()
                    },
                })
        except Exception as exc:
            traceback.print_exc()
            # The error type only: the message could quote the text.
            answer({"ok": False, "error": type(exc).__name__})


if __name__ == "__main__":
    main()
