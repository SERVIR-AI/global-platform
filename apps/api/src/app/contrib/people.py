"""Who an account id is, for the people who review contributions. A sign-in token always carries
the id, and a name or an e-mail only when the identity provider is set to add them (a WorkOS JWT
template; AuthKit's default access token has neither). They are kept apart from the label every
record carries, which stays the id because records reach public answers. A token's values are held
in this process's memory and written only for people who contribute: when they submit, or read
their own contributions. The operator may record, or forget, someone no token has described; a
later token's values replace what the operator recorded, except a name the token does not carry."""

from __future__ import annotations

import logging
import re
import unicodedata

from ..mcp import store
from . import identity

log = logging.getLogger(__name__)
_HELD: dict[str, tuple] = {}         # the latest token's (e-mail, name) per id, never written as such
_EMAIL = re.compile(r"[^@\s]{1,64}@[^@\s]{1,189}")
_NOT_PEOPLE = frozenset({identity.ANONYMOUS, identity.LOCAL_DEV, identity.CLI_OPERATOR})
_VARIATION = re.compile("[\ufe00-\ufe0f\U000e0100-\U000e01ef]")


def _text(value, limit: int, drop: str = "<>") -> str | None:
    """A claim as reviewers will read it: a string only, in its compatibility form (so look-alike
    brackets become brackets), without control, format or variation characters or `drop`,
    whitespace collapsed, at most `limit` characters."""
    if not isinstance(value, str):
        return None
    value = unicodedata.normalize("NFKC", value)
    kept = (" " if unicodedata.category(c)[0] == "Z" else c for c in value
            if unicodedata.category(c)[0] != "C" and c not in drop and not _VARIATION.match(c))
    return " ".join("".join(kept).split())[:limit].strip() or None


def _name(value) -> str | None:
    return _text(value, 120, drop="<>@")      # a name never looks like an address


def _email(value) -> str | None:
    value = _text(value, 254)
    return value if value and _EMAIL.fullmatch(value) else None


def seen(user_id: str, email=None, name=None) -> None:
    """Hold what a verified token said about its subject, in memory only."""
    _HELD[user_id] = (_email(email), _name(name))


def held_email(user_id: str) -> str | None:
    """The e-mail this caller's verified token carried, for a reviewer list kept by e-mail."""
    return (_HELD.get(user_id) or (None, None))[0]


def keep(user_id: str) -> None:
    """Write what the latest token said, for someone who contributes, when it differs from what
    is stored. Never fails the call."""
    held = _HELD.get(user_id)
    if not held or not any(held):
        return
    try:
        stored = store.load_people([user_id]).get(user_id) or {}
        if (stored.get("email"), stored.get("name")) != held:
            store.save_person(user_id, *held, "token")
    except Exception as exc:
        log.warning("could not keep who %s is: %s", user_id, exc)


def describe(user_id: str, known: dict | None = None) -> str:
    """'Name <e-mail>', whichever of the two is known, or else the id itself."""
    person = (store.load_people([user_id]) if known is None else known).get(user_id) or {}
    email, name = person.get("email"), person.get("name")
    if email and name:
        return f"{name} <{email}>"
    return email or name or user_id


def main(argv=None) -> int:
    import argparse
    import json
    p = argparse.ArgumentParser(prog="python -m app.contrib.people",
                                description="Who an account id is, as reviewers see it.")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("set", help="record a person's e-mail and name by account id")
    s.add_argument("user_id")
    s.add_argument("--email")
    s.add_argument("--name")
    sub.add_parser("forget", help="remove what is known about an account id").add_argument("user_id")
    sub.add_parser("show", help="what is known about these account ids").add_argument("user_ids", nargs="+")
    sub.add_parser("missing", help="contributors no name or e-mail is known for, with their counts")
    sub.add_parser("import", help="record many people in one call").add_argument(
        "people", help='JSON: {"<account id>": {"email": "<address>", "name": "<name>"}, ...}')
    args = p.parse_args(argv)
    if args.cmd == "missing":
        counts = store.contributor_counts()
        known = store.load_people(counts)
        print(json.dumps({u: n for u, n in counts.items()
                          if u not in known and u not in _NOT_PEOPLE}, indent=1))
        return 0
    if args.cmd == "import":
        return _import(p, args.people)
    if args.cmd == "set":
        email, name = _email(args.email), _name(args.name)
        if args.email and not email:
            p.error(f"not an e-mail address: {args.email!r}")
        if not (email or name):
            p.error("give --email, --name or both")
        store.save_person(args.user_id, email, name, "operator")
    elif args.cmd == "forget":
        store.forget_person(args.user_id)
    ids = args.user_ids if args.cmd == "show" else [args.user_id]
    print(json.dumps({u: describe(u) for u in ids}, indent=1))
    return 0


def _import(parser, raw: str) -> int:
    """Record each entry as the operator; say per id what was done, and why one was not."""
    import json
    try:
        entries = json.loads(raw)
    except ValueError as exc:
        parser.error(f"not JSON: {exc}")
    if not isinstance(entries, dict):
        parser.error('give an object: {"<account id>": {"email": ..., "name": ...}, ...}')
    out = {}
    for user_id, entry in entries.items():
        entry = entry if isinstance(entry, dict) else {}
        email, name = _email(entry.get("email")), _name(entry.get("name"))
        if entry.get("email") and not email:
            out[user_id] = f"skipped: not an e-mail address: {entry.get('email')!r}"
        elif not (email or name):
            out[user_id] = "skipped: neither an e-mail nor a name"
        else:
            store.save_person(user_id, email, name, "operator")
            out[user_id] = describe(user_id)
    print(json.dumps(out, indent=1))
    return 0 if all(not v.startswith("skipped") for v in out.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
