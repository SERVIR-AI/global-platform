"""Conversation loop: the model may ask the user a clarifying question (and wait),
then route to a deterministic tool; we record cost + a groundedness check.
"""
from . import llm, narrate, tools, trace

MAX_TURNS = 6


def ask(question, ask_user):
    """`ask_user(prompt) -> str` is called when the model needs clarification."""
    narrate.banner()
    narrate.step("the question")
    narrate.detail(f'"{question}"')
    messages = [{"role": "system", "content": llm.SYSTEM},
                {"role": "user", "content": question}]
    usages, result, args = [], None, None
    for _ in range(MAX_TURNS):
        out = llm.complete(messages)
        usages.append(out["usage"])
        messages.append(out["assistant"])
        if "text" in out:
            return trace.record(question, out["text"], usages, result=result, args=args)
        if out["tool"] == "ask_user":
            narrate.step("the model needs clarification")
            narrate.detail("it won't guess the severity — handing back to ask you")
            reply = ask_user(out["args"].get("question", "Could you clarify?"))
            narrate.detail(f"your answer: {reply!r}")
            messages.append({"role": "tool", "tool_call_id": out["call_id"], "content": reply})
            continue
        narrate.step("routing — the model picks a tool and fills the arguments")
        narrate.detail(f"{out['tool']}({narrate.fmt_args(out['args'])})  ← the model only extracts these; it computes no numbers")
        try:
            result, args = tools.dispatch(out["tool"], out["args"]), out["args"]
            messages.append({"role": "tool", "tool_call_id": out["call_id"], "content": str(result)})
        except Exception as e:
            args = out["args"]
            messages.append({"role": "tool", "tool_call_id": out["call_id"], "content": f"error: {e}"})
    return trace.record(question, "Stopped after too many turns.", usages, result=result, args=args)
