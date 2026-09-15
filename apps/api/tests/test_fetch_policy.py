"""The fetch policy's teeth: every hop checked and pinned, redirects to private
hosts refused, the byte cap enforced on declared and streamed bodies, odd 3xx
answers refused — exercised against a scripted transport, no network."""
import ipaddress

import pytest
from requests.structures import CaseInsensitiveDict

from app.contrib import fetch_policy

PUBLIC = {"example.org": ["93.184.216.34"], "cdn.example.org": ["93.184.216.35"],
          "evil.internal": ["10.1.30.110"], "six.example.org": ["2606:2800:220:1:248:1893:25c8:1946"]}


class FakeResponse:
    def __init__(self, status=200, headers=None, body=b"", chunks=None):
        self.status_code = status
        self.headers = CaseInsensitiveDict(headers or {})
        self._chunks = chunks if chunks is not None else [body]
        self.closed = False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")

    def iter_content(self, chunk_size=65536):
        yield from self._chunks

    def close(self):
        self.closed = True


class FakeSession:
    calls = []
    script = {}

    def __init__(self, hostname, scheme):
        self.hostname, self.scheme = hostname, scheme

    def get(self, url, timeout=None, stream=None, allow_redirects=None, headers=None):
        FakeSession.calls.append((url, headers.get("Host")))
        resp = FakeSession.script[headers["Host"] + "|" + url.split("/", 3)[-1].split("?")[0]]
        return resp

    def close(self):
        pass


@pytest.fixture
def transport(monkeypatch):
    FakeSession.calls, FakeSession.script = [], {}
    monkeypatch.setattr(fetch_policy, "_addresses",
                        lambda host: [ipaddress.ip_address(a) for a in PUBLIC[host]])
    monkeypatch.setattr(fetch_policy, "_session", lambda hostname, scheme: FakeSession(hostname, scheme))
    return FakeSession


def test_redirect_to_a_private_host_is_refused_after_one_request(transport, log):
    transport.script["example.org|doc.pdf"] = FakeResponse(302, {"Location": "http://evil.internal/secret"})
    with pytest.raises(fetch_policy.FetchRefused) as exc:
        fetch_policy.fetch("https://example.org/doc.pdf", max_bytes=1000)
    log("OUTPUT", f"{exc.value} | calls={transport.calls}")
    assert "private" in str(exc.value) and len(transport.calls) == 1


def test_connection_is_pinned_to_the_checked_address_with_host_header(transport, log):
    transport.script["example.org|doc.txt"] = FakeResponse(200, {"Content-Length": "5"}, b"hello")
    data, name = fetch_policy.fetch("https://example.org/doc.txt", max_bytes=1000)
    log("OUTPUT", f"{transport.calls} -> {name}")
    assert data == b"hello" and name == "doc.txt"
    url, host = transport.calls[0]
    assert url.startswith("https://93.184.216.34/") and host == "example.org"


def test_relative_redirect_is_followed_on_the_same_host_and_ipv6_is_bracketed(transport, log):
    transport.script["example.org|a"] = FakeResponse(301, {"Location": "/b"})
    transport.script["example.org|b"] = FakeResponse(302, {"Location": "https://six.example.org/c"})
    transport.script["six.example.org|c"] = FakeResponse(200, {}, b"done")
    data, _ = fetch_policy.fetch("https://example.org/a", max_bytes=1000)
    log("OUTPUT", str(transport.calls))
    assert data == b"done" and transport.calls[2][0].startswith("https://[2606:2800:220:1:248:1893:25c8:1946]/c")


def test_too_many_redirects_and_redirect_without_location_are_refused(transport, log):
    transport.script["example.org|loop"] = FakeResponse(302, {"Location": "/loop"})
    with pytest.raises(fetch_policy.FetchRefused, match="more than 5"):
        fetch_policy.fetch("https://example.org/loop", max_bytes=1000)
    transport.script["example.org|odd"] = FakeResponse(304, {})
    with pytest.raises(fetch_policy.FetchRefused, match="without a location"):
        fetch_policy.fetch("https://example.org/odd", max_bytes=1000)
    log("OUTPUT", "loop and location-less 3xx refused")


def test_byte_cap_holds_for_declared_and_streamed_bodies(transport, log):
    transport.script["example.org|big"] = FakeResponse(200, {"Content-Length": "5000"}, b"x" * 5000)
    with pytest.raises(fetch_policy.FetchRefused, match="declares 5,000 bytes"):
        fetch_policy.fetch("https://example.org/big", max_bytes=1000)
    transport.script["example.org|lie"] = FakeResponse(200, {"Content-Length": "10"}, chunks=[b"x" * 600, b"x" * 600])
    with pytest.raises(fetch_policy.FetchRefused, match="exceeds the 1,000-byte cap"):
        fetch_policy.fetch("https://example.org/lie", max_bytes=1000)
    log("OUTPUT", "declared and streamed caps both refused")


def test_http_errors_surface_and_the_response_is_closed(transport, log):
    resp = FakeResponse(404, {})
    transport.script["example.org|gone"] = resp
    with pytest.raises(RuntimeError, match="http 404"):
        fetch_policy.fetch("https://example.org/gone", max_bytes=1000)
    log("OUTPUT", f"closed={resp.closed}")
    assert resp.closed


def test_check_url_refuses_private_bad_schemes_and_credentials(log):
    assert any(w in fetch_policy.check_url("http://127.0.0.1:8080/x")[0] for w in ("loopback", "private"))
    assert fetch_policy.check_url("ftp://example.org/x")[0].startswith("url scheme")
    assert fetch_policy.check_url("http://user:pw@example.org/x")
    assert fetch_policy.check_url("http://10.1.30.110/x")
    assert fetch_policy.check_url("http://[::1]/x")
    log("OUTPUT", "loopback v4/v6, private, ftp and embedded credentials all refused")
