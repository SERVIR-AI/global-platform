"""LLM via any OpenAI-compatible endpoint (OpenAI, OpenRouter, LiteLLM, Ollama,
vLLM). It routes to a tool (or asks the user to clarify) and phrases the answer;
it never computes a number.
"""
import json

from . import config, hazards, registry, tools

SYSTEM = (
    "You route disaster-risk questions to tools and phrase the result. You never "
    "compute, estimate, or invent numbers or data paths. Extract the place (as `place`) "
    "and the hazard the user names (as `hazard`); hazards are " + ", ".join(hazards.HAZARDS) +
    " — default to flood if none is named. " + hazards.legends_block() + " "
    "If the user gives a severity — a class number, or a qualitative term ('severe', "
    "'major', 'extreme', 'moderate', 'deep') you can map to a class — use it. If the "
    "user gives NO severity at all, you MUST call ask_user to ask which severity class "
    "they want (offer 1-5 and 'all' for the full per-class breakdown) and wait for their "
    "reply before calling a data tool; do not pick one yourself. If they answer 'all', "
    "report the per-class breakdown the tool returns, labelled with the tool's `legend`. "
    "For layers we do not have (" + ", ".join(registry.UNAVAILABLE) + ") or if no place "
    "is given, do not call a tool — say so plainly. State the exact numbers the tool "
    "returned and cite its `source` verbatim."
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


def complete(messages):
    """One model turn: returns a tool call or a plain-text reply, plus token usage."""
    resp = _client().chat.completions.create(
        model=config.MODEL, messages=messages, tools=_SCHEMA, max_tokens=600)
    msg = resp.choices[0].message
    use = _usage(resp)
    if msg.tool_calls:
        call = msg.tool_calls[0]
        try:
            args = json.loads(call.function.arguments or "{}")
        except Exception:
            args = {}
        assistant = {"role": "assistant", "content": msg.content, "tool_calls": [{
            "id": call.id, "type": "function",
            "function": {"name": call.function.name, "arguments": call.function.arguments}}]}
        return {"tool": call.function.name, "args": args, "assistant": assistant,
                "call_id": call.id, "usage": use}
    return {"text": msg.content or "", "usage": use,
            "assistant": {"role": "assistant", "content": msg.content}}
