import re
from typing import Callable, Optional

from freya.delegation.material import KIND_NAMES, KINDS, NEVER
from freya.delegation.window_requests import PROVIDER_LABELS


# A request, spoken or typed, to make the cloud settings stricter.
# Owner decision, 2026-10-11: a spoken request may ask for a setting
# change, and the change is made only by a click on a confirmation
# shown on the device that recorded it. Asking by voice is limited to
# changes that tighten: switching a provider off, and setting a kind
# of material so it is never sent. Everything that loosens stays in
# the Cloud tab. A typed request is asked in the same way, because
# the model that read it may have been steered by something it read.
CLOUD_SETTINGS_TIGHTEN = "cloud.settings.tighten"
TIGHTEN_SEED = "alpha-cloud-tighten"

PROVIDER_OFF = "provider-off"
MATERIAL_NEVER = "material-never"
TIGHTENINGS = (PROVIDER_OFF, MATERIAL_NEVER)

CONFIRM = "change"
CANCEL = "cancel"

TIGHTEN_DESCRIPTION = (
    "Asks the user to confirm making the cloud settings stricter: "
    "switching a cloud provider off, or setting a kind of material so "
    "that it is never sent to a cloud model. Use it only when the user "
    "asks for exactly that. It cannot switch anything on or allow "
    "anything; those are done by the user in the Cloud tab. Nothing "
    "changes unless the user clicks to confirm, so say only what the "
    "tool returned."
)
TIGHTEN_PARAMETERS = {
    "change": {
        "description": "One of: " + ", ".join(TIGHTENINGS) + ".",
        "max_length": 20,
    },
    # Not "name": that is the broker's own word for the capability.
    "target": {
        "description": (
            "For provider-off, the provider: "
            + ", ".join(sorted(PROVIDER_LABELS)) + ". "
            "For material-never, the kind of material: "
            + ", ".join(KINDS) + "."
        ),
        "max_length": 40,
    },
}


class TightenRefused(Exception):
    """
    Nothing was changed. The message is fixed wording that is safe to
    give the model and the audit log.
    """


def _plain(text) -> str:
    return re.sub(r"[^a-z]+", "-", str(text).lower()).strip("-")


def find_provider(name) -> Optional[str]:
    """The provider a name means: its id, or a word of its label."""
    wanted = _plain(name)

    if not wanted:
        return None

    for provider, label in PROVIDER_LABELS.items():
        if wanted in {provider, _plain(label), *_plain(label).split("-")}:
            return provider

    return None


def find_kind(name) -> Optional[str]:
    """The kind of material a name means: its id or its label."""
    wanted = _plain(name)

    for kind in KINDS:
        if wanted and wanted in (_plain(kind), _plain(KIND_NAMES[kind])):
            return kind

    return None


class SettingTightener:
    """
    Puts a tightening the model asked for to the owner and, on their
    click, makes it through the audited settings capability.
    """

    def __init__(
        self,
        cloud,
        ask_owner: Callable[[dict], Optional[str]],
        window_connected: Callable[[], bool],
        change: Callable[[dict], bool],
    ):
        self._cloud = cloud
        self._ask_owner = ask_owner
        self._window_connected = window_connected
        self._change = change

    def available(self) -> bool:
        """Offered only while there is a window to confirm in."""
        try:
            return self._window_connected() is True
        except Exception:
            return False

    def tighten(self, change, target) -> dict:
        if change == PROVIDER_OFF:
            provider = find_provider(target)

            if provider is None:
                raise TightenRefused(
                    "That is not a provider Freya knows. Providers: "
                    + ", ".join(sorted(PROVIDER_LABELS)) + "."
                )

            label = PROVIDER_LABELS[provider]

            if not self._cloud.settings.provider_enabled(provider):
                return {"changed": False, "now": f"{label} is already off."}

            wording = f"Switch {label} off"
            effect = (
                f"No work will be sent to {label} until you switch it "
                "on again in the Cloud tab."
            )
            request = {"action": "provider", "name": provider, "enabled": False}
            done = f"{label} is now off."
        elif change == MATERIAL_NEVER:
            kind = find_kind(target)

            if kind is None:
                raise TightenRefused(
                    "That is not a kind of material. Kinds: "
                    + ", ".join(KINDS) + "."
                )

            label = KIND_NAMES[kind]

            if self._cloud.settings.setting_for(kind) == NEVER:
                return {"changed": False, "now": f"Never sending {label} is already set."}

            wording = f"Never send {label}"
            effect = (
                f"{label[0].upper()}{label[1:]} will never be sent to a cloud "
                "model, whatever you answer in a prompt, until you change "
                "it in the Cloud tab."
            )
            request = {"action": "material", "kind": kind, "setting": NEVER}
            done = f"{label[0].upper()}{label[1:]} will now never be sent."
        else:
            raise TightenRefused(
                "Only these changes can be asked for here: "
                + ", ".join(TIGHTENINGS) + "."
            )

        if not self.available():
            raise TightenRefused(
                "There is no window open to confirm in, so nothing was changed."
            )

        prompt = {
            "kind": "setting",
            "provider": "",
            "lines": [effect],
            "text": effect,
            "text_label": "What would change",
            "reason": (
                "Freya asks before changing a setting. This can only make "
                "your cloud settings stricter."
            ),
            "choices": [
                {"id": CONFIRM, "label": wording},
                {"id": CANCEL, "label": "Cancel"},
            ],
        }

        try:
            answer = self._ask_owner(prompt)
        except Exception:
            answer = None

        if answer != CONFIRM:
            raise TightenRefused(
                "The user did not confirm, so nothing was changed."
            )

        try:
            made = self._change(request) is True
        except Exception:
            made = False

        if not made:
            raise TightenRefused("That change could not be made.")

        return {"changed": True, "now": done}
