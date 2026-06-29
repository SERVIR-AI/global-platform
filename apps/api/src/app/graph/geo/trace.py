"""Per-query record: cost, a groundedness check, and a JSON trace on disk."""
import json
import time

from ...config import get_settings


def record(question, answer, usages, result=None, args=None):
    """Write a per-query trace JSON to disk and return it.

    Computes USD cost from token counts and checks groundedness (whether the
    tool's number appears verbatim in the answer).

    Args:
        question (str): the user's original question
        answer (str): the assistant's final response
        usages (list[dict]): per-LLM-call {'in': int, 'out': int} token dicts
        result (dict | None): the tool result dict (used to check groundedness)
        args (dict | None): the tool arguments passed to the operation

    Returns:
        dict: the trace record that was written to disk, with keys: question, answer,
              tool_call, tool_result, grounded, cost_usd, tokens
    """
    usages = [u for u in usages if u]
    settings = get_settings()
    price_in, price_out = settings.price_in / 1_000_000, settings.price_out / 1_000_000
    cost = sum(u["in"] * price_in + u["out"] * price_out for u in usages)

    # grounded = the tool's number actually appears in the answer (no fabrication)
    grounded = True
    if result is not None:
        number = result.get("count", result.get("length_km"))
        grounded = str(number) in answer.replace(",", "")

    rec = {"question": question, "answer": answer, "tool_call": args, "tool_result": result,
           "grounded": grounded, "cost_usd": round(cost, 6),
           "tokens": {"in": sum(u["in"] for u in usages), "out": sum(u["out"] for u in usages)}}
    settings.traces_dir.mkdir(parents=True, exist_ok=True)
    path = settings.traces_dir / f"{int(time.time() * 1000)}.json"
    path.write_text(json.dumps(rec, indent=2))
    return rec
