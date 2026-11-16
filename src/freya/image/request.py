"""
Ask for an image only through the broker.

The handler registered as ``image.generate`` is what talks to ComfyUI.
This function does not. If the broker is missing, or the call is
refused, nothing is generated.
"""

from freya.providers.image.comfyui import IMAGE_GENERATE


def request_image(broker, prompt) -> bool:
    """True when the broker allowed a picture and the handler finished it."""
    text = str(prompt or "").strip()

    if not text or broker is None:
        if broker is None and text:
            print("Freya: I can't generate images right now.\n")

        return False

    result = broker.invoke(IMAGE_GENERATE, prompt=text)

    if result.success:
        return True

    if result.outcome in ("denied", "unavailable"):
        print("Freya: I can't generate images right now.\n")

    return False
