"""
The play kill switch as a capability (NEW-EO).

`play.stop` is the owner ending control. It is not a model tool, and it
is not in a policy seed that the control plane applies.
`build_local_control_plane` does not register it, so a running Freya
has no play capability until a later change the owner accepts.

`play.act`, `play.observe`, and `play.project` are named so those names
are not reused. M0 does not register them. M0 does not register
`play.stop` either, except through `register_play_stop`, which nothing
in the control plane calls.
"""

from freya.core.capabilities import CapabilityDefinition, declare
from freya.core.policy import seed_alpha_policy
from freya.core.providers import ProviderDefinition
from freya.player.indicator import PAUSED
from freya.player.schema import PlayError


PLAY_PROVIDER = "play-local"
PLAY_STOP = "play.stop"
PLAY_ACT = "play.act"
PLAY_OBSERVE = "play.observe"
PLAY_PROJECT = "play.project"

# Named now so a later milestone does not reuse them. M0 applies none
# of these, and the control plane calls none of them.
PLAY_STOP_SEED = "alpha-play-stop"
PLAY_ACT_SEED = "alpha-play-act"
PLAY_OBSERVE_SEED = "alpha-play-observe"
PLAY_PROJECT_SEED = "alpha-play-project"

M0_CAPABILITIES = (PLAY_STOP,)
UNREGISTERED = (PLAY_ACT, PLAY_OBSERVE, PLAY_PROJECT)


def create_play_provider(ready) -> ProviderDefinition:
    """
    A provider for the kill switch. `ready` says whether it can be
    called. It does not start an adapter.
    """
    return ProviderDefinition(
        provider_id=PLAY_PROVIDER,
        name="Play, on this machine",
        capabilities=[PLAY_STOP],
        start_handler=lambda: None,
        health_handler=lambda: ready() is True,
        metadata={"model_tool": False},
    )


def register_play_stop(registry, stop) -> None:
    """
    Register the owner kill switch. Not a model tool, and not seeded.

    `stop` is called only after policy has allowed the capability. The
    session's own `stop` method does not consult policy: removing
    control stays available to the owner who already holds the session.
    """

    def play_stop():
        try:
            stop()
        except PlayError:
            raise
        except Exception as exc:
            raise PlayError("Play could not be stopped.") from exc
        return {"control": PAUSED}

    registry.register(
        CapabilityDefinition(
            name=PLAY_STOP,
            description=(
                "Ends play, releases input, and clears the control "
                "indicator. It does not start a session and it does "
                "not approve a target."
            ),
            provider=PLAY_PROVIDER,
            handler=play_stop,
            category="play",
            declaration=declare(
                (
                    "process",
                    "the play adapter on this machine, which is stopped",
                ),
                effect="consequential",
            ),
        )
    )


def seed_play_stop(policy, actor) -> bool:
    """
    Allow the owner to invoke `play.stop` through the broker, once.

    The control plane does not call this. Without it, the broker
    denies the capability. The session method is the kill switch and
    does not require this seed.
    """
    return seed_alpha_policy(policy, PLAY_STOP_SEED, actor, [PLAY_STOP])
