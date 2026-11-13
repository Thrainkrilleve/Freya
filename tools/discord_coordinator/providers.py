"""API adapters. No SDK required; HTTP requests are bounded and never log response bodies."""
import asyncio
import json
import urllib.request
import urllib.error

class ProviderError(RuntimeError):
    pass

def _post(url, key, payload, headers):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", **headers}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=45) as response:
            data = response.read(1_000_001)
            if len(data) > 1_000_000:
                raise ProviderError("Provider response too large")
            return json.loads(data)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise ProviderError("Provider request failed; inspect account, billing, or network settings locally") from None

async def generate(role, key, model, system, prompt):
    if role == "openai":
        payload = {"model": model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}], "max_completion_tokens": 900}
        result = await asyncio.to_thread(_post, "https://api.openai.com/v1/chat/completions", key, payload, {"Authorization": "Bearer " + key})
        try:
            return result["choices"][0]["message"]["content"] or "(No text returned)"
        except (KeyError, IndexError, TypeError):
            raise ProviderError("Unexpected OpenAI response") from None
    if role == "claude":
        payload = {"model": model, "system": system, "messages": [{"role": "user", "content": prompt}], "max_tokens": 900}
        result = await asyncio.to_thread(_post, "https://api.anthropic.com/v1/messages", key, payload, {"x-api-key": key, "anthropic-version": "2023-06-01"})
        try:
            return "\n".join(block["text"] for block in result["content"] if block["type"] == "text") or "(No text returned)"
        except (KeyError, TypeError):
            raise ProviderError("Unexpected Anthropic response") from None
    raise ValueError("Unknown provider")
