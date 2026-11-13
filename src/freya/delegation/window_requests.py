from typing import Callable, Dict, List

from freya.delegation.compose import TASK_KINDS
from freya.delegation.material import KIND_NAMES, KINDS, SETTINGS
from freya.delegation.screening import Corrections


# What the Cloud tab may ask for. Reading shows the owner's own
# choices; every other action changes one of them.
READS = ("state", "sent")
# How much the next message typed in this window matters. It is the
# owner's choice for one message, not a setting, so it is not kept.
NEXT = "next"
CHANGES = ("provider", "limit", "material", "bars", "mark", "unmark", "unrule")
# A rule is made only by the owner's answer to a prompt that showed
# exactly what it covers, never by a request from the tab: the tab
# may list rules and remove them.
RULE = "rule"

SENT_SHOWN = 50
MAX_PHRASE = 200

PROVIDER_LABELS = {
    "anthropic": "Claude (Anthropic)",
    "openai": "ChatGPT (OpenAI)",
}

_MARK_LISTS = {
    "not-private": "mark_not_private",
    "always-private": "mark_always_private",
}


class CloudRequestRefused(ValueError):
    """A request from the Cloud tab was not one Freya will act on."""


def _provider_state(cloud, name) -> dict:
    available = name in cloud.available_providers
    entry = {
        "name": name,
        "label": PROVIDER_LABELS.get(name, name),
        "enabled": cloud.settings.provider_enabled(name),
        "available": available,
        "weekly_limit": None,
        "used": None,
        "resets_at": None,
    }
    limit = cloud.weekly_limit(name) if available else None

    if limit is not None:
        entry["weekly_limit"] = limit.capacity
        entry["used"] = limit.used
        entry["resets_at"] = (
            limit.resets_at.isoformat() if limit.resets_at else None
        )

    return entry


def cloud_state(cloud) -> dict:
    """
    Everything the Cloud tab shows. A provider is listed whether or
    not Freya can reach it yet, so the owner sees what exists.
    """
    settings = cloud.settings
    material = settings.material

    return {
        "providers": [
            _provider_state(cloud, name)
            for name in sorted(
                set(PROVIDER_LABELS) | set(cloud.available_providers)
            )
        ],
        "material": [
            {
                "kind": kind,
                "label": KIND_NAMES[kind],
                "setting": material[kind],
            }
            for kind in KINDS
        ],
        "settings": list(SETTINGS),
        "bars": settings.bars,
        "corrections": cloud.corrections.listing(),
        "rules": [
            {
                "id": rule["id"],
                "provider": PROVIDER_LABELS.get(rule["provider"], rule["provider"]),
                "task": rule["task"],
                "material": [KIND_NAMES.get(kind, kind) for kind in rule["material"]],
                "made": rule["made"],
            }
            for rule in settings.rules
        ],
        "sent_count": len(cloud.sent.entries()),
    }


def apply_cloud_change(cloud, message) -> None:
    """
    Make one change the owner asked for in the Cloud tab. Every field
    is checked here; the settings and lists check their own values
    again.
    """
    action = message.get("action")

    if action == "provider":
        name = message.get("name")
        if not isinstance(name, str) or name not in PROVIDER_LABELS:
            raise CloudRequestRefused("That is not a provider Freya knows.")
        if not isinstance(message.get("enabled"), bool):
            raise CloudRequestRefused("A provider is either on or off.")
        cloud.settings.set_provider(name, message["enabled"])
        # Switching a provider on is the owner unlocking it: its
        # policy rule and a starting allowance come with the switch.
        if message["enabled"]:
            cloud.unlock(name)
        else:
            cloud.lock(name)

    elif action == "limit":
        name = message.get("name")
        if not isinstance(name, str) or name not in PROVIDER_LABELS:
            raise CloudRequestRefused("That is not a provider Freya knows.")
        cloud.set_weekly_limit(name, message.get("weekly"))

    elif action == "material":
        cloud.settings.set_material(message.get("kind"), message.get("setting"))

    elif action == "bars":
        cloud.settings.set_bars(message.get("bars"))

    elif action == "mark":
        method = _MARK_LISTS.get(message.get("list"))
        phrase, memory = message.get("phrase"), message.get("memory")
        if method is None:
            raise CloudRequestRefused("That is not one of the two lists.")
        if isinstance(phrase, str) and phrase.strip() and memory is None:
            if len(phrase) > MAX_PHRASE:
                raise CloudRequestRefused("That phrase is too long.")
            getattr(cloud.corrections, method)(phrase=phrase)
        elif isinstance(memory, (int, str)) and not isinstance(memory, bool) and phrase is None:
            getattr(cloud.corrections, method)(memory_id=memory)
        else:
            raise CloudRequestRefused("Mark either a phrase or a memory.")

    elif action == "unmark":
        name = message.get("list")
        entry = message.get("entry")
        if name not in Corrections._LISTS or not isinstance(entry, str):
            raise CloudRequestRefused("That entry could not be found.")
        cloud.corrections.remove(name, entry)

    elif action == RULE:
        provider, task = message.get("provider"), message.get("task")
        material = message.get("material")
        if provider not in cloud.available_providers:
            raise CloudRequestRefused("That is not a provider Freya can reach.")
        if task not in TASK_KINDS:
            raise CloudRequestRefused("That is not a kind of task.")
        if not isinstance(material, list):
            raise CloudRequestRefused("A rule needs the kinds of material it covers.")
        cloud.settings.add_rule(provider, task, material)

    elif action == "unrule":
        if not isinstance(message.get("id"), str):
            raise CloudRequestRefused("That rule could not be found.")
        cloud.settings.remove_rule(message["id"])

    else:
        raise CloudRequestRefused("Freya did not expect that request.")


def cloud_window_request(
    cloud,
    message: dict,
    change: Callable[[dict], None],
) -> Dict[str, object]:
    """
    Answer one request from the Cloud tab.

    `change` carries out a change; the caller passes one that goes
    through the broker, so each change is allowed by policy and
    written to the audit log. The answer always holds the present
    state, so the tab shows what is true after the request, whatever
    happened to it.
    """
    answer: Dict[str, object] = {}
    action = message.get("action") if isinstance(message, dict) else None

    try:
        if action == "sent":
            entries: List[dict] = cloud.sent.entries()[-SENT_SHOWN:]
            answer["sent"] = list(reversed(entries))
        elif action == NEXT:
            delegation = getattr(cloud, "turn_delegation", None)
            if delegation is None:
                raise CloudRequestRefused("Work cannot be handed over just now.")
            delegation.set_next(
                message.get("value"), message.get("urgent"), message.get("text"),
            )
        elif action in CHANGES:
            change(message)
        elif action != "state":
            raise CloudRequestRefused("Freya did not expect that request.")
    except ValueError as refused:
        answer["error"] = str(refused)
    except Exception:
        answer["error"] = "That change could not be made."

    try:
        answer["state"] = cloud_state(cloud)
    except Exception:
        # The tab must still get its answer, so it can say what it
        # could not show rather than waiting for ever.
        answer["state"] = None
        answer.setdefault("error", "The cloud settings could not be read.")

    return answer
