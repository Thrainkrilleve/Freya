"""Attributed display of worker output, separate from Freya's own answer."""
import uuid


MAX_DISPLAY_TEXT = 64_000


def reply_publisher(publish):
    """The trusted route supplies the name; reply prose cannot impersonate it."""
    def show(route, reply):
        text = getattr(reply, "text", None)
        if not isinstance(text, str) or not text.strip():
            return
        name = route.source_name or route.provider
        model = getattr(reply, "model", "")
        publish(
            "source_response",
            response_id=uuid.uuid4().hex,
            source={
                "name": name,
                "provider": route.provider,
                "route": route.route_id,
                "model": model[:128] if isinstance(model, str) else "",
            },
            text=text[:MAX_DISPLAY_TEXT],
            truncated=bool(getattr(reply, "truncated", False)) or len(text) > MAX_DISPLAY_TEXT,
        )
    return show
