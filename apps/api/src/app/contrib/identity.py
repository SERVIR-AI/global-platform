"""Who is calling — the identity every contribution and every visibility check
reads.

Resolution order:
  1. The OAuth access token the MCP transport verified: id = the token subject
     (AuthKit user id), label = the `email` claim when the token carries one,
     else the id. Email is NOT guaranteed in an AuthKit access token, so the id
     is what reviewer lists and ownership checks are keyed on; the label is for
     humans.
  2. OAuth off on a DEV BOX (not an open deployment, see GRP_ALLOW_ANONYMOUS):
     the `X-GRP-Dev-Identity` request header, so two local sandboxes can play
     contributor and reviewer. Never honoured when OAuth is on or the deployment
     is open — a header is not a credential.
  3. No header either: `local-dev`, the single operator of a local box.
  4. Anything else (OAuth on but no token, an open deployment): `anonymous`.

Reviewer = an id or label listed in GRP_REVIEWERS. When that list is empty and
OAuth is off, `local-dev` reviews (there is nobody else on a local box). When
the list is empty and OAuth is on, nobody reviews over MCP — the CLI on the
server is the reviewer until the list is set.
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass

from ..config import get_settings

DEV_HEADER = "x-grp-dev-identity"
LOCAL_DEV = "local-dev"
CLI_OPERATOR = "cli-operator"
ANONYMOUS = "anonymous"


@dataclass(frozen=True)
class Caller:
    id: str
    label: str
    is_reviewer: bool
    source: str  # token | dev-header | local-dev | anonymous | cli | bound

    def owns(self, contributor_id: str | None) -> bool:
        """Is this caller the contributor? `anonymous` is every caller of an open
        deployment at once, so it owns nothing — not even what it submitted: one stranger
        must not withdraw another's live layer. `local-dev` is the one operator of a
        local box and owns what it submitted."""
        return (bool(contributor_id) and contributor_id != ANONYMOUS
                and self.source != "anonymous" and self.id == contributor_id)

    def may_see(self, staged_by: str | None) -> bool:
        """The visibility rule: a staged artefact is visible to its contributor
        and to reviewers, nobody else. Unstaged (no owner) is visible to all."""
        return not staged_by or self.is_reviewer or self.id == staged_by

    def may_view(self, contribution_id: str | None) -> bool:
        """May this caller see a LANDED contribution? Always, unless the authorization
        store (GRP_FGA_URL) is on; then only what the store lists as viewable for them:
        approved and granted (by visibility level or a share), or their own to preview.
        An unreachable store means no: nothing is served on a guess."""
        if not contribution_id:
            return True
        from .. import authz
        if not authz.enabled():
            return True
        return str(contribution_id) in _visible_for(self)

    @property
    def hubs(self) -> list[str]:
        from .. import authz
        if not authz.enabled() or self.source == "anonymous":
            return []
        return _hubs_for(self)


def _memo(key, compute):
    """Inside a request's budget snapshot the store is asked once per caller; outside
    one (a REST route, the CLI) every call asks afresh. A store that cannot answer
    yields the empty answer: nothing is served on a guess."""
    from .. import authz
    from ..mcp import budget
    memo = budget._MEMO.get()
    if memo is not None and key in memo:
        return memo[key]
    try:
        out = compute()
    except authz.AuthzUnavailable:
        out = set() if key[0] == "visible" else []
    if memo is not None:
        memo[key] = out
    return out


def _visible_for(caller) -> set[str]:
    from .. import authz
    return _memo(("visible", caller.id, caller.source), lambda: authz.visible_datasets(caller))


def _hubs_for(caller) -> list[str]:
    from .. import authz
    return _memo(("hubs", caller.id, caller.source), lambda: authz.hubs_of(caller))


# Set explicitly by tests and by the CLI; every MCP call resolves lazily from the
# request instead, so nothing has to be threaded through the tool signatures.
_CURRENT: contextvars.ContextVar[Caller | None] = contextvars.ContextVar(
    "grp_caller", default=None)


def _from_token() -> tuple[str, str] | None:
    try:
        from fastmcp.server.dependencies import get_access_token
        tok = get_access_token()
    except Exception:  # no MCP request in flight (REST route, CLI, tests)
        return None
    if tok is None:
        return None
    claims = getattr(tok, "claims", None) or {}
    sub = claims.get("sub") or getattr(tok, "subject", None) or getattr(tok, "client_id", None)
    if not sub:
        return None
    return str(sub), str(claims.get("email") or sub)


def _from_dev_header() -> str | None:
    try:
        from fastmcp.server.dependencies import get_http_headers
        headers = get_http_headers()
    except Exception:
        return None
    value = (headers or {}).get(DEV_HEADER)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _is_reviewer(cid: str, label: str, source: str) -> bool:
    if source == "anonymous" or cid == ANONYMOUS:
        return False                      # nobody in particular, whatever the list says
    from .. import authz
    if authz.enabled():                   # a role is a tuple in the store, never a list here
        try:
            return authz.check(f"user:{cid}", "reviewer", authz.PLATFORM)
        except authz.AuthzUnavailable:
            return False                  # fail closed: no store, no reviewer
    listed = {r.strip().lower() for r in get_settings().grp_reviewers if r.strip()}
    if listed:
        return cid.lower() in listed or label.lower() in listed
    return source == "local-dev"


def resolve() -> Caller:
    """Resolve the caller from the request in flight (never cached)."""
    settings = get_settings()
    dev_box = not settings.grp_oauth_enabled and not settings.grp_allow_anonymous
    tok = _from_token()
    if tok:
        cid, label = tok
        source = "token"
    elif dev_box:
        dev = _from_dev_header()
        cid = label = dev or LOCAL_DEV
        source = "dev-header" if dev else "local-dev"
    else:
        # OAuth on and no token (REST paths, background work), or an OPEN
        # deployment: nobody in particular. Never a reviewer, never an owner.
        cid = label = ANONYMOUS
        source = "anonymous"
    return Caller(cid, label, _is_reviewer(cid, label, source), source)


def from_headers(headers) -> Caller:
    """The caller behind a REST request, from its headers: the dev-identity header on a
    dev box, else nobody in particular (the REST twin verifies bearers on its own
    routes; a data download under OAuth resolves here through the bearer's subject
    only when that route verified it first)."""
    settings = get_settings()
    dev_box = not settings.grp_oauth_enabled and not settings.grp_allow_anonymous
    get = getattr(headers, "get", None) or (lambda k, d=None: d)
    dev = get(DEV_HEADER) or get(DEV_HEADER.title()) or get(DEV_HEADER.upper())
    dev = dev.strip() if isinstance(dev, str) and dev.strip() else None
    if dev_box:
        cid = label = dev or LOCAL_DEV
        source = "dev-header" if dev else "local-dev"
    else:
        cid = label = ANONYMOUS
        source = "anonymous"
    return Caller(cid, label, _is_reviewer(cid, label, source), source)


def current() -> Caller:
    """The bound caller if one was set (tests, CLI), else the request's."""
    bound = _CURRENT.get()
    return bound if bound is not None else resolve()


def bind(caller: Caller) -> contextvars.Token:
    return _CURRENT.set(caller)


def unbind(token: contextvars.Token) -> None:
    _CURRENT.reset(token)


def cli_operator() -> Caller:
    """The person at the server's own command line: always a reviewer."""
    return Caller(CLI_OPERATOR, CLI_OPERATOR, True, "cli")


def viewable(rows: dict) -> dict:
    """`rows` less the landed contributions the current caller may not view. A row with no
    contribution id is built in and stays; one the authorization store does not list for the
    caller is simply absent, unknown rather than declined, so nothing says it exists. The store
    answers once per pass, however many rows, and a store that cannot answer hides every
    contributed row (may_view fails closed through _memo).
    """
    from ..mcp import budget
    caller = current()
    with budget.snapshot():
        return {k: v for k, v in rows.items()
                if not (v or {}).get("contribution_id") or caller.may_view(v["contribution_id"])}
