"""Per-query record: cost, a groundedness check, and a JSON trace on disk."""
import json
import os
import time

from .config import PRICE

DIR = os.path.join(os.path.dirname(__file__), "traces")


def record(question, answer, usages, result=None, args=None):
    usages = [u for u in usages if u]
    cost = sum(u["in"] * PRICE[0] + u["out"] * PRICE[1] for u in usages)

    # grounded = the tool's number actually appears in the answer (no fabrication)
    grounded = True
    if result is not None:
        number = result.get("count", result.get("length_km"))
        grounded = str(number) in answer.replace(",", "")

    rec = {"question": question, "answer": answer, "tool_call": args, "tool_result": result,
           "grounded": grounded, "cost_usd": round(cost, 6),
           "tokens": {"in": sum(u["in"] for u in usages), "out": sum(u["out"] for u in usages)}}
    os.makedirs(DIR, exist_ok=True)
    json.dump(rec, open(os.path.join(DIR, f"{int(time.time() * 1000)}.json"), "w"), indent=2)
    return rec
