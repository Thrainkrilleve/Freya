import threading

from freya.core.capabilities import CapabilityDefinition
from freya.core.providers import ProviderDefinition
from freya.sandbox.contract import (
    DEFAULT_TIMEOUT_SECONDS,
    RunResult,
    Sandbox,
    SandboxError,
    build_request,
    checked_result,
    profile_problems,
)


SANDBOX_PROVIDER = "code-sandbox-local"
CODE_RUN = "code.run"


def _usable(sandbox) -> bool:
    try:
        return not profile_problems(sandbox.profile()) and bool(sandbox.ready())
    except Exception:
        return False


def create_sandbox_provider(sandbox: Sandbox) -> ProviderDefinition:
    """
    Describe a sandbox as a provider. It is healthy only while the
    sandbox states everything Freya requires and says it is ready.
    """
    return ProviderDefinition(
        provider_id=SANDBOX_PROVIDER,
        name="Sandbox for code a model wrote",
        capabilities=[CODE_RUN],
        start_handler=lambda: None,
        health_handler=lambda: _usable(sandbox),
        metadata={"runs_untrusted_code": True},
    )


def register_code_run(registry, sandbox: Sandbox) -> None:
    """
    Register the one capability a sandbox offers. It is not offered to
    the model as a tool and is in no default policy seed: only a rule
    the owner adds allows it.

    A sandbox that does not state the required properties is refused
    here, so it can never be reached through the broker.
    """
    problems = profile_problems(sandbox.profile())

    if problems:
        raise ValueError(" ".join(problems))

    # One run at a time: a sandbox is one small machine, and two runs
    # in it could see each other's files.
    one_at_a_time = threading.Lock()

    def run(files, command, target="", timeout=DEFAULT_TIMEOUT_SECONDS) -> RunResult:
        request = build_request(files, command, target, timeout)

        with one_at_a_time:
            if not _usable(sandbox):
                raise SandboxError("The sandbox is not ready.")

            try:
                result = sandbox.run(request)
            except SandboxError:
                raise
            except Exception as exc:
                # What went wrong may quote the code, so it stays out
                # of the message, which the broker records.
                raise SandboxError(
                    "The sandbox could not run the code.", detail=str(exc)[:2000],
                ) from exc

        return checked_result(result)

    registry.register(
        CapabilityDefinition(
            name=CODE_RUN,
            description=(
                "Runs the given text files with Python or pytest inside "
                "the sandbox and returns what they printed and how they "
                "ended. The files are all the code can see: it has no "
                "network and none of this machine's files."
            ),
            provider=SANDBOX_PROVIDER,
            handler=run,
            category="execution",
        )
    )
