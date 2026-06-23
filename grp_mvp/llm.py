"""LLM via any OpenAI-compatible endpoint (OpenAI, OpenRouter, LiteLLM, Ollama,
vLLM). It routes to a tool and phrases the answer; it never computes a number.
"""
import json

from . import config, registry, tools

SYSTEM = (
    "You route disaster-risk questions to tools and phrase the result. You never "
    "compute, estimate, or invent numbers or data paths. Extract the place (city or "
    "district) the user names and pass it as `place`. Flood severity is a class 1 "
    "(low) to 5 (extreme). For layers we do not have (" + ", ".join(registry.UNAVAILABLE) +
    ") or if no place is given, do not call a tool — say so plainly. In your final "
    "answer state the exact number the tool returned and cite its `source` verbatim."
)

_SCHEMA = [{"type": "function", "function": {
    "name": t["name"], "description": t["description"], "parameters": t["parameters"]}}
    for t in tools.TOOLS]


def available():
    return bool(config.BASE_URL) or (config.API_KEY and config.API_KEY != "none")


def _client():
    from openai import OpenAI
    return OpenAI(base_url=config.BASE_URL, api_key=config.API_KEY)


def _usage(resp):
    u = getattr(resp, "usage", None)
    if not u:
        return {"in": 0, "out": 0}
    return {"in": getattr(u, "prompt_tokens", 0) or 0, "out": getattr(u, "completion_tokens", 0) or 0}


def _create(messages, max_tokens):
    return _client().chat.completions.create(
        model=config.MODEL, messages=messages, tools=_SCHEMA, max_tokens=max_tokens)


def route(question):
    """Return either a tool call or a plain-text reply (model declined to call)."""
    resp = _create([{"role": "system", "content": SYSTEM},
                    {"role": "user", "content": question}], 600)
    msg = resp.choices[0].message
    use = _usage(resp)
    if msg.tool_calls:
        call = msg.tool_calls[0]
        assistant = {"role": "assistant", "content": msg.content, "tool_calls": [{
            "id": call.id, "type": "function",
            "function": {"name": call.function.name, "arguments": call.function.arguments}}]}
        return {"tool": call.function.name, "args": json.loads(call.function.arguments),
                "assistant": assistant, "call_id": call.id, "usage": use}
    return {"text": msg.content or "", "usage": use}


def write(question, result, routed):
    """Phrase the final answer from the tool result, constrained to cite its source."""
    resp = _create([{"role": "system", "content": SYSTEM},
                    {"role": "user", "content": question},
                    routed["assistant"],
                    {"role": "tool", "tool_call_id": routed["call_id"], "content": str(result)}], 400)
    return resp.choices[0].message.content or "", _usage(resp)
