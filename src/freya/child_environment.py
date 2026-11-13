import os
import re
from collections.abc import Mapping


# TOKENIZERS_* configures speech libraries; it is not a credential.
CREDENTIAL_NAME = re.compile(
    r"TOKEN(?!IZER)|SECRET|PASSWORD|PASSWD|CREDENTIAL"
    r"|API_?KEY|ACCESS_?KEY|PRIVATE_?KEY",
    re.IGNORECASE,
)


def child_environment(
    extra: Mapping[str, str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """
    Preserve runtime settings while removing credential-named variables.
    `extra` explicitly supplies values intended for this child only.
    """
    environ = os.environ if environ is None else environ
    kept = {
        name: value
        for name, value in environ.items()
        if not CREDENTIAL_NAME.search(name)
    }
    kept.update(extra or {})

    return kept
