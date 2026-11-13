"""
Check whether a larger model fits this workstation, and measure it.

    python -m freya.gpu fit                       every installed chat model
    python -m freya.gpu fit gpt-oss:120b          one installed model
    python -m freya.gpu fit --name big-moe --size-gb 65 --total-b 117 --active-b 5.1
                                                  a model not yet downloaded
    python -m freya.gpu measure gpt-oss:120b      load it and time a reply

`fit` reads only: the card, system memory, and each model's description
from Ollama. `measure` loads the model (unloading Freya's own until her
next turn) and asks it one made-up question; nothing personal is sent.
"""

import argparse
import json
import sys

from freya.config import CONTEXT_WINDOW_TOKENS
from freya.gpu import model_fit


def _client():
    # The same pinned, local-only client Freya's provider uses.
    from freya.providers.model.ollama import ollama
    return ollama


def _installed_chat_models(client):
    names = []
    for entry in model_fit._get(client.list(), "models") or []:
        name = model_fit._get(entry, "model", "name")
        if name and "embed" not in name:
            names.append(name)
    return names


def _print_plan(plan, machine):
    print(f"\n{plan.model}  ({plan.context_tokens:,} tokens, {plan.kv_cache} cache)")
    print(f"  {plan.verdict}")
    print(
        f"  weights {plan.weights_gb} GB + cache {plan.cache_gb} GB: "
        f"{plan.on_card_gb} GB on the card ({plan.card_share_percent}%), "
        f"{plan.in_ram_gb} GB in system memory"
    )
    if plan.tokens_per_second_ceiling:
        print(
            f"  at most about {plan.tokens_per_second_ceiling} tokens a second "
            "(a ceiling from memory speeds, not a measurement)"
        )
    print(f"  first load from disk: about {plan.first_load_seconds} s")
    for note in plan.notes:
        print(f"  - {note}")


def _fit(args):
    machine = model_fit.this_machine(
        vram_reserve_gb=args.vram_reserve_gb,
        ram_reserve_gb=args.ram_reserve_gb,
        ram_bandwidth_gbs=args.ram_bandwidth_gbs,
    )
    print(
        f"{machine.gpu_name or 'Graphics card'}: {machine.vram_total_gb:.1f} GB, "
        f"{machine.vram_budget_gb:.1f} GB for models. "
        f"System memory: {machine.ram_total_gb:.1f} GB, "
        f"{machine.ram_budget_gb:.1f} GB for models."
    )

    facts = []

    if args.size_gb:
        facts.append(
            model_fit.facts_from_figures(
                args.name or "model from figures",
                args.size_gb,
                total_b=args.total_b or 0.0,
                active_b=args.active_b or 0.0,
                layers=args.layers or 0,
                kv_heads=args.kv_heads or 0,
                head_dim=args.head_dim or 0,
            )
        )
    else:
        client = _client()
        names = args.models or _installed_chat_models(client)
        for name in names:
            try:
                facts.append(model_fit.facts_from_ollama(client, name))
            except LookupError as error:
                print(f"\n{error}")

    plans = [
        model_fit.plan(item, machine, args.context, args.kv_cache) for item in facts
    ]

    if args.json:
        print(json.dumps([plan.as_dict() for plan in plans], indent=2))
    else:
        for plan in plans:
            _print_plan(plan, machine)

    return 0


def _measure(args):
    result = model_fit.measure(
        _client(), args.model, args.context, keep_loaded=args.keep_loaded
    )
    if args.json:
        print(json.dumps(result, indent=2))
        return 0

    print(f"{result['model']} at {result['context_tokens']:,} tokens")
    if "loaded_gb" in result:
        print(
            f"  loaded {result['loaded_gb']} GB, {result['on_card_gb']} GB on the "
            f"card ({result['card_share_percent']}%)"
        )
    print(f"  load {result['load_seconds']} s, whole request {result['wall_seconds']} s")
    print(
        f"  wrote {result['reply_tokens']} tokens at "
        f"{result['tokens_per_second']} a second; read the question at "
        f"{result['prompt_tokens_per_second']} a second"
    )
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m freya.gpu", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    commands = parser.add_subparsers(dest="command", required=True)

    fit = commands.add_parser("fit", help="estimate where models would go")
    fit.add_argument("models", nargs="*", help="installed Ollama models")
    fit.add_argument("--context", type=int, default=CONTEXT_WINDOW_TOKENS)
    fit.add_argument(
        "--kv-cache", choices=sorted(model_fit.KV_CACHE_BYTES), default="f16"
    )
    fit.add_argument("--vram-reserve-gb", type=float)
    fit.add_argument("--ram-reserve-gb", type=float)
    fit.add_argument("--ram-bandwidth-gbs", type=float)
    fit.add_argument("--name", help="name for a model given by figures")
    fit.add_argument("--size-gb", type=float, help="download size of the file")
    fit.add_argument("--total-b", type=float, help="total parameters, billions")
    fit.add_argument("--active-b", type=float, help="active parameters, billions")
    fit.add_argument("--layers", type=int)
    fit.add_argument("--kv-heads", type=int)
    fit.add_argument("--head-dim", type=int)
    fit.add_argument("--json", action="store_true")
    fit.set_defaults(handler=_fit)

    measure = commands.add_parser("measure", help="load a model and time a reply")
    measure.add_argument("model")
    measure.add_argument("--context", type=int, default=CONTEXT_WINDOW_TOKENS)
    measure.add_argument(
        "--keep-loaded", action="store_true",
        help="leave it loaded for five minutes instead of thirty seconds",
    )
    measure.add_argument("--json", action="store_true")
    measure.set_defaults(handler=_measure)

    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
