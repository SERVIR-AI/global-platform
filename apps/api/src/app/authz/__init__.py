"""Who may do what, as relationships in an OpenFGA store (GRP_FGA_URL).

Roles (operator, reviewer, contributor), hub membership, a dataset's owner, its
visibility level (private, hub, platform, public), its grants to named users or whole
hubs, and whether it is approved are all tuples in ONE store described by
`model.fga` (the readable twin of `model_json()`, which is what the server loads).
Nothing is a list typed into code or an environment variable: a reviewer is
`user:<id> reviewer platform:servir`, a hub member is `user:<id> member hub:<hub>`.

Every decision is a Check or a ListObjects against the store; when the store is
unreachable the answer is a refusal, never a pass (fail closed). With GRP_FGA_URL
unset the platform keeps today's behaviour (two-state visibility, GRP_REVIEWERS).

The store and model are created on first use and their ids kept in
<cache_dir>/mcp/authz.json unless GRP_FGA_STORE_ID / GRP_FGA_MODEL_ID pin them.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from ..config import get_settings

PLATFORM = "platform:servir"
VISIBILITIES = ("private", "hub", "platform", "public")
_LOCK = threading.RLock()
_CLIENT: dict = {}


class AuthzUnavailable(RuntimeError):
    """The authorization store could not answer: the caller is refused, not waved through."""


def enabled() -> bool:
    return bool(str(get_settings().grp_fga_url or "").strip())


# --- the model ------------------------------------------------------------------------

def _this():
    return {"this": {}}


def _cu(rel):
    return {"computedUserset": {"object": "", "relation": rel}}


def _union(*rels):
    return {"union": {"child": [r if isinstance(r, dict) else _cu(r) for r in rels]}}


def _ttu(tupleset, rel):
    return {"tupleToUserset": {"tupleset": {"object": "", "relation": tupleset},
                               "computedUserset": {"object": "", "relation": rel}}}


def _types(*specs):
    out = []
    for s in specs:
        if s.endswith(":*"):
            out.append({"type": s[:-2], "wildcard": {}})
        elif "#" in s:
            t, r = s.split("#", 1)
            out.append({"type": t, "relation": r})
        else:
            out.append({"type": s})
    return {"directly_related_user_types": out}


def model_json() -> dict:
    """The authorization model, as OpenFGA's JSON. Keep `model.fga` beside it in step."""
    none = {"directly_related_user_types": []}
    return {"schema_version": "1.1", "type_definitions": [
        {"type": "user"},
        {"type": "anon"},
        {"type": "platform",
         "relations": {"operator": _this(), "reviewer": _union(_this(), "operator"),
                       "contributor": _union(_this(), "operator")},
         "metadata": {"relations": {"operator": _types("user"), "reviewer": _types("user"),
                                    "contributor": _types("user", "hub#member")}}},
        {"type": "hub",
         "relations": {"admin": _this(), "member": _union(_this(), "admin")},
         "metadata": {"relations": {"admin": _types("user"), "member": _types("user")}}},
        {"type": "dataset",
         "relations": {
             "platform": _this(), "hub": _this(), "owner": _this(), "approved": _this(),
             "shared_user": _this(), "shared_hub": _this(), "visibility_hub": _this(),
             "visibility_platform": _this(), "visibility_public": _this(),
             "reviewer": _ttu("platform", "reviewer"), "operator": _ttu("platform", "operator"),
             "grantee": _union("shared_user", "shared_hub", "visibility_hub",
                               "visibility_platform", "visibility_public"),
             "approved_grantee": {"intersection": {"child": [_cu("grantee"), _cu("approved")]}},
             "can_preview": _union("owner", "reviewer", "operator"),
             "can_view": _union("can_preview", "approved_grantee"),
             "can_share": _union("owner", "operator"),
             "can_review": {"difference": {"base": _cu("reviewer"), "subtract": _cu("owner")}},
             "can_edit": _union("owner", "operator")},
         "metadata": {"relations": {
             "platform": _types("platform"), "hub": _types("hub"), "owner": _types("user"),
             "approved": _types("user:*", "anon:*"), "shared_user": _types("user"),
             "shared_hub": _types("hub#member"), "visibility_hub": _types("hub#member"),
             "visibility_platform": _types("user:*"),
             "visibility_public": _types("user:*", "anon:*"),
             **{r: none for r in ("reviewer", "operator", "grantee", "approved_grantee",
                                  "can_preview", "can_view", "can_share", "can_review",
                                  "can_edit")}}}}]}


# --- the client -----------------------------------------------------------------------

def _state_path() -> Path:
    return Path(get_settings().cache_dir) / "mcp" / "authz.json"


def _ids() -> dict:
    s = get_settings()
    saved = {}
    try:
        saved = json.loads(_state_path().read_text())
    except (OSError, ValueError):
        pass
    return {"store_id": s.grp_fga_store_id or saved.get("store_id") or "",
            "model_id": s.grp_fga_model_id or saved.get("model_id") or ""}


def _save_ids(ids: dict) -> None:
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({**ids, "url": get_settings().grp_fga_url}, indent=2))


def bootstrap(force_model: bool = False) -> dict:
    """Make sure a store and the model exist; returns {store_id, model_id}. Idempotent."""
    from openfga_sdk import ClientConfiguration
    from openfga_sdk.models import CreateStoreRequest, WriteAuthorizationModelRequest
    from openfga_sdk.sync import OpenFgaClient
    url = str(get_settings().grp_fga_url).rstrip("/")
    ids = _ids()
    try:
        with OpenFgaClient(ClientConfiguration(api_url=url)) as fga:
            if not ids["store_id"]:
                resp = fga.create_store(CreateStoreRequest(name="servir-global-platform"))
                ids["store_id"] = resp.id
            fga.set_store_id(ids["store_id"])
            if force_model or not ids["model_id"]:
                body = WriteAuthorizationModelRequest(**model_json())
                resp = fga.write_authorization_model(body)
                ids["model_id"] = resp.authorization_model_id
    except Exception as exc:
        raise AuthzUnavailable(f"authorization store at {url}: {type(exc).__name__}: {exc}") from exc
    _save_ids(ids)
    _CLIENT.clear()
    return ids


def _fga():
    """One sync client per process, bound to the store and model.

    Rebuilt whenever the saved ids change: another process (the demo runner, the operator
    CLI) may restart the store and bootstrap it again while this server keeps running.
    Holding on to the old store would fail every check closed from then on.
    """
    from openfga_sdk import ClientConfiguration
    from openfga_sdk.sync import OpenFgaClient
    url = str(get_settings().grp_fga_url).rstrip("/")
    with _LOCK:
        c = _CLIENT.get("c")
        ids = _ids()
        if c is not None and _CLIENT.get("url") == url and _CLIENT.get("ids") == ids:
            return c
        if not ids["store_id"] or not ids["model_id"]:
            ids = bootstrap()
        c = OpenFgaClient(ClientConfiguration(api_url=url, store_id=ids["store_id"],
                                              authorization_model_id=ids["model_id"]))
        _CLIENT.update({"c": c, "url": url, "ids": ids})
        return c


def ids() -> dict:
    """The store and model a decision is made against (recorded in receipts)."""
    _fga()
    return dict(_CLIENT.get("ids") or {})


def _guard(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except AuthzUnavailable:
        raise
    except Exception as exc:
        # The store may have been replaced under us; the next call rebuilds the client.
        with _LOCK:
            _CLIENT.clear()
        raise AuthzUnavailable(f"authorization store: {type(exc).__name__}: {exc}") from exc


# --- primitives -------------------------------------------------------------------------

def check(user: str, relation: str, obj: str) -> bool:
    from openfga_sdk.client.models import ClientCheckRequest
    return bool(_guard(lambda: _fga().check(ClientCheckRequest(user=user, relation=relation,
                                                               object=obj)).allowed))


def batch_check(user: str, relation: str, objects: list[str]) -> dict[str, bool]:
    return {o: check(user, relation, o) for o in dict.fromkeys(objects)}


def list_objects(user: str, relation: str, type_: str) -> set[str]:
    from openfga_sdk.client.models import ClientListObjectsRequest
    resp = _guard(lambda: _fga().list_objects(ClientListObjectsRequest(user=user, relation=relation,
                                                                       type=type_)))
    return set(resp.objects or [])


def tuples(obj: str) -> list[tuple[str, str, str]]:
    """Every tuple on one object, as (user, relation, object)."""
    from openfga_sdk.models import ReadRequestTupleKey
    out = []
    token = None
    while True:
        resp = _guard(lambda: _fga().read(ReadRequestTupleKey(object=obj),
                                          {"continuation_token": token} if token else None))
        for t in resp.tuples or []:
            k = t.key
            out.append((k.user, k.relation, k.object))
        token = resp.continuation_token
        if not token:
            break
    return out


def write(items: list[tuple[str, str, str]]) -> int:
    """Add tuples that are not there yet (a duplicate write is an OpenFGA error)."""
    from openfga_sdk.client.models import ClientTuple, ClientWriteRequest
    wanted = list(dict.fromkeys(items))
    have = set()
    for obj in {o for _, _, o in wanted}:
        have.update(tuples(obj))
    new = [ClientTuple(user=u, relation=r, object=o) for u, r, o in wanted if (u, r, o) not in have]
    if new:
        _guard(lambda: _fga().write(ClientWriteRequest(writes=new)))
    return len(new)


def delete(items: list[tuple[str, str, str]]) -> int:
    from openfga_sdk.client.models import ClientTuple, ClientWriteRequest
    have = set()
    for obj in {o for _, _, o in items}:
        have.update(tuples(obj))
    gone = [ClientTuple(user=u, relation=r, object=o) for u, r, o in dict.fromkeys(items)
            if (u, r, o) in have]
    if gone:
        _guard(lambda: _fga().write(ClientWriteRequest(deletes=gone)))
    return len(gone)


# --- the platform's vocabulary --------------------------------------------------------

def user_key(caller) -> str:
    """`user:<id>` for a person or service identity, `anon:guest` for nobody in particular."""
    if caller is None or getattr(caller, "source", "") == "anonymous":
        return "anon:guest"
    return f"user:{caller.id}"


def dataset(contribution_id: str) -> str:
    return f"dataset:{contribution_id}"


def is_reviewer(caller) -> bool:
    return check(user_key(caller), "reviewer", PLATFORM)


def is_operator(caller) -> bool:
    return check(user_key(caller), "operator", PLATFORM)


def hubs_of(caller) -> list[str]:
    """The hubs a caller belongs to (`hub:<id>` objects)."""
    return sorted(h.split(":", 1)[1] for h in list_objects(user_key(caller), "member", "hub"))


def visible_datasets(caller) -> set[str]:
    """Contribution ids the caller may view (approved and granted, or theirs to preview)."""
    return {o.split(":", 1)[1] for o in list_objects(user_key(caller), "can_view", "dataset")}


def can(caller, relation: str, contribution_id: str) -> bool:
    return check(user_key(caller), relation, dataset(contribution_id))


def _visibility_tuples(contribution_id: str, level: str, hub: str | None) -> list[tuple[str, str, str]]:
    d = dataset(contribution_id)
    if level == "hub":
        return [(f"hub:{hub}#member", "visibility_hub", d)] if hub else []
    if level == "platform":
        return [("user:*", "visibility_platform", d)]
    if level == "public":
        return [("user:*", "visibility_public", d), ("anon:*", "visibility_public", d)]
    return []


def on_submit(contribution_id: str, owner, visibility: str | None, share_users=(), share_hubs=(),
              hub: str | None = None) -> dict:
    """Write what a new contribution declares: its owner, its hub, its visibility level
    and its grants. It stays invisible to others until `on_approve`."""
    level = (visibility or get_settings().grp_default_visibility or "platform").strip().lower()
    if level not in VISIBILITIES:
        raise ValueError(f"visibility must be one of {', '.join(VISIBILITIES)}, not {level!r}")
    d = dataset(contribution_id)
    hub = hub or (hubs_of(owner)[0] if hubs_of(owner) else None)
    items = [(PLATFORM, "platform", d), (user_key(owner), "owner", d)]
    if hub:
        items.append((f"hub:{hub}", "hub", d))
    items += _visibility_tuples(contribution_id, level, hub)
    items += [(f"user:{u}", "shared_user", d) for u in share_users or [] if str(u).strip()]
    items += [(f"hub:{h}#member", "shared_hub", d) for h in share_hubs or [] if str(h).strip()]
    write(items)
    if level == "hub" and not hub:
        return {"visibility": level, "hub": None,
                "note": "visibility 'hub' but the contributor belongs to no hub: nobody else sees it"}
    return {"visibility": level, "hub": hub, "shared_users": list(share_users or []),
            "shared_hubs": list(share_hubs or [])}


def on_approve(contribution_id: str) -> None:
    d = dataset(contribution_id)
    write([("user:*", "approved", d), ("anon:*", "approved", d)])


def on_revoke(contribution_id: str) -> None:
    """Approval withdrawn: the owner's sharing choices survive a re-review."""
    d = dataset(contribution_id)
    delete([("user:*", "approved", d), ("anon:*", "approved", d)])


def on_forget(contribution_id: str) -> None:
    """Rejected or withdrawn: every tuple of the dataset goes."""
    delete(tuples(dataset(contribution_id)))


def set_visibility(contribution_id: str, level: str, hub: str | None = None) -> dict:
    level = str(level or "").strip().lower()
    if level not in VISIBILITIES:
        raise ValueError(f"visibility must be one of {', '.join(VISIBILITIES)}, not {level!r}")
    d = dataset(contribution_id)
    delete([t for t in tuples(d) if t[1] in ("visibility_hub", "visibility_platform", "visibility_public")])
    hub = hub or next((o.split(":", 1)[1] for u, r, o in tuples(d) if r == "hub" for o in [u]), None)
    write(_visibility_tuples(contribution_id, level, hub))
    return sharing(contribution_id)


def grant(contribution_id: str, user: str | None = None, hub: str | None = None) -> dict:
    d = dataset(contribution_id)
    items = ([(f"user:{user}", "shared_user", d)] if user else []) + \
            ([(f"hub:{hub}#member", "shared_hub", d)] if hub else [])
    write(items)
    return sharing(contribution_id)


def revoke(contribution_id: str, user: str | None = None, hub: str | None = None) -> dict:
    d = dataset(contribution_id)
    items = ([(f"user:{user}", "shared_user", d)] if user else []) + \
            ([(f"hub:{hub}#member", "shared_hub", d)] if hub else [])
    delete(items)
    return sharing(contribution_id)


def sharing(contribution_id: str) -> dict:
    """The dataset's access as data: owner, hub, level, grants, approved."""
    t = tuples(dataset(contribution_id))
    rels = {}
    for u, r, _ in t:
        rels.setdefault(r, []).append(u)
    level = ("public" if "visibility_public" in rels else "platform" if "visibility_platform" in rels
             else "hub" if "visibility_hub" in rels else "private")
    return {"contribution_id": contribution_id, "visibility": level,
            "owner": [u.split(":", 1)[1] for u in rels.get("owner", [])],
            "hub": [u.split(":", 1)[1] for u in rels.get("hub", [])],
            "shared_users": [u.split(":", 1)[1] for u in rels.get("shared_user", [])],
            "shared_hubs": [u.split(":", 1)[1].split("#")[0] for u in rels.get("shared_hub", [])],
            "approved": "approved" in rels}


def decisions(caller, contribution_ids) -> list[dict]:
    """What the store said, per cited dataset, for a receipt: replayable with the ids."""
    meta = ids()
    out = []
    for cid in dict.fromkeys(c for c in contribution_ids if c):
        out.append({"store_id": meta.get("store_id"), "model_id": meta.get("model_id"),
                    "user": user_key(caller), "relation": "can_view", "object": dataset(cid),
                    "allowed": can(caller, "can_view", cid)})
    return out


# --- operator command line --------------------------------------------------------------

def main(argv=None) -> int:
    import argparse
    p = argparse.ArgumentParser(prog="python -m app.authz",
                                description="Operate the platform's authorization store (OpenFGA).")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("bootstrap", help="create the store and write the model; print the ids")
    sub.add_parser("model", help="print the model as JSON")
    r = sub.add_parser("role", help="give or take a platform role")
    r.add_argument("role", choices=["operator", "reviewer", "contributor"])
    r.add_argument("user")
    r.add_argument("--remove", action="store_true")
    h = sub.add_parser("hub", help="hub membership")
    h.add_argument("hub")
    h.add_argument("--member", action="append", default=[])
    h.add_argument("--admin", action="append", default=[])
    h.add_argument("--remove", action="store_true")
    c = sub.add_parser("check", help="ask one question: user relation object")
    c.add_argument("user")
    c.add_argument("relation")
    c.add_argument("object")
    t = sub.add_parser("tuples", help="every tuple on an object")
    t.add_argument("object")
    v = sub.add_parser("visible", help="datasets a user may view")
    v.add_argument("user")
    m = sub.add_parser("migrate", help="give every approved contribution the default visibility "
                                      "and approved tuples (idempotent)")
    m.add_argument("--visibility", default=None)
    a = p.parse_args(argv)
    if not enabled():
        print(json.dumps({"status": "off", "note": "set GRP_FGA_URL"}))
        return 2
    if a.cmd == "bootstrap":
        print(json.dumps(bootstrap()))
    elif a.cmd == "model":
        print(json.dumps(model_json(), indent=2))
    elif a.cmd == "role":
        item = [(f"user:{a.user}", a.role, PLATFORM)]
        n = delete(item) if a.remove else write(item)
        print(json.dumps({"changed": n, "tuples": tuples(PLATFORM)}))
    elif a.cmd == "hub":
        items = [(f"user:{u}", "member", f"hub:{a.hub}") for u in a.member] + \
                [(f"user:{u}", "admin", f"hub:{a.hub}") for u in a.admin]
        n = delete(items) if a.remove else write(items)
        print(json.dumps({"changed": n, "tuples": tuples(f"hub:{a.hub}")}))
    elif a.cmd == "check":
        print(json.dumps({"allowed": check(a.user, a.relation, a.object)}))
    elif a.cmd == "tuples":
        print(json.dumps(tuples(a.object)))
    elif a.cmd == "visible":
        print(json.dumps(sorted(list_objects(a.user, "can_view", "dataset"))))
    elif a.cmd == "migrate":
        from ..mcp import store
        n = 0
        for rec in store.list_contributions():
            if rec.get("status") != "approved":
                continue
            cid = rec["contribution_id"] if "contribution_id" in rec else rec.get("id")
            owner = type("C", (), {"id": rec.get("contributor_id"), "source": "token"})()
            on_submit(cid, owner, a.visibility, hub=None)
            on_approve(cid)
            n += 1
        print(json.dumps({"migrated": n}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
