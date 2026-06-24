"""Plain-text, step-by-step narration of a run — for demos.

Off by default; `run.py -v` turns it on, so normal output stays clean. Everything
prints to stdout so it shows inline as the agent works.
"""

_ON = False


def enable():
    global _ON
    _ON = True


def on():
    return _ON


def banner():
    if _ON:
        print("\n" + "─" * 64)
        print("  how it's working  (verbose)")
        print("─" * 64)


def step(title):
    if _ON:
        print(f"\n▸ {title}")


def detail(msg):
    if _ON:
        print(f"    {msg}")


def decision(msg):
    if _ON:
        print(f"    → DECISION: {msg}")


def end():
    if _ON:
        print("─" * 64)


def fmt_args(args):
    return ", ".join(f"{k}={v!r}" for k, v in args.items())
