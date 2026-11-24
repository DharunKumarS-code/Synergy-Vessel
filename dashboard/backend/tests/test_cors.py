"""Post-audit Phase 3 -- CORS allows every method the frontend uses (incl. PUT/DELETE for location synonyms) when it
calls the API cross-origin via VITE_API_BASE_URL. Origins, headers, credentials and auth are unchanged.
In-process ASGI client, synthetic data and temporary folders only."""

from __future__ import annotations

import pytest

from app.config import Settings
from conftest import sign_in
from test_imports import make  # noqa: F401  (isolated temporary app factory)
from test_location_intel import client, no_network  # noqa: F401  (trusted seed fixture + network guard)

ALLOWED = "http://localhost:5173"          # Vite dev server (default Settings.cors_origins)
ALLOWED_ALT = "http://localhost:3000"
EVIL = "http://evil.example"
URL = "/api/locations/synonyms"
FCL = "FORECASTLE DECK AREA"
METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")


def preflight(c, path, method, origin=ALLOWED, headers="authorization,content-type"):
    return c.options(path, headers={"Origin": origin, "Access-Control-Request-Method": method,
                                    "Access-Control-Request-Headers": headers})


def test_default_origins_are_unchanged():
    assert Settings().cors_origins == (ALLOWED, ALLOWED_ALT)


@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize("origin", (ALLOWED, ALLOWED_ALT))
def test_preflight_allows_every_method_the_frontend_uses(client, method, origin):
    r = preflight(client, f"{URL}/SYN-000000000000", method, origin)
    assert r.status_code == 200, r.text
    assert r.headers["access-control-allow-origin"] == origin                  # echoed, never "*"
    assert method in [m.strip() for m in r.headers["access-control-allow-methods"].split(",")]
    allowed_headers = r.headers["access-control-allow-headers"].lower()
    assert "authorization" in allowed_headers and "content-type" in allowed_headers
    assert "access-control-allow-credentials" not in r.headers                  # bearer tokens, not cookies: unchanged


def test_preflight_needs_no_sign_in_and_does_not_reach_the_route(client):
    client.headers.pop("Authorization", None)                                   # browsers never send credentials here
    before = client.get("/api/admin/audit?limit=200")                           # 401 without a token
    r = preflight(client, f"{URL}/SYN-000000000000", "DELETE")
    assert r.status_code == 200 and r.content == b"OK"                          # answered by the middleware itself
    assert before.status_code == 401


@pytest.mark.parametrize("method", METHODS)
def test_unapproved_origins_are_still_rejected(client, method):
    r = preflight(client, URL, method, EVIL)
    assert r.status_code == 400
    assert "access-control-allow-origin" not in r.headers
    actual = client.get("/api/health", headers={"Origin": EVIL})
    assert "access-control-allow-origin" not in actual.headers                  # browser blocks the response


@pytest.mark.parametrize("method", ("TRACE", "CONNECT"))
def test_methods_outside_the_list_are_still_rejected(client, method):
    r = preflight(client, URL, method)
    assert r.status_code == 400 and "Disallowed CORS method" in r.text


def test_cross_origin_synonym_create_update_delete(client):
    o = {"Origin": ALLOWED}
    r = client.post(URL, json={"synonym": "Bow station", "canonical": FCL}, headers=o)
    assert r.status_code == 201 and r.headers["access-control-allow-origin"] == ALLOWED
    sid = r.json()["synonym"]["id"]
    r = client.put(f"{URL}/{sid}", json={"synonym": "Bow mooring station", "canonical": FCL}, headers=o)
    assert r.status_code == 200 and r.headers["access-control-allow-origin"] == ALLOWED
    assert r.json()["synonym"]["synonym"] == "Bow mooring station"
    r = client.get(URL, headers=o)
    assert r.status_code == 200 and r.headers["access-control-allow-origin"] == ALLOWED
    assert [s["synonym"] for s in r.json()["synonyms"]] == ["Bow mooring station"]
    r = client.delete(f"{URL}/{sid}", headers=o)
    assert r.status_code == 200 and r.headers["access-control-allow-origin"] == ALLOWED
    assert r.json()["deleted"]["synonym"] == "Bow mooring station"
    assert client.get(URL, headers=o).json()["synonyms"] == []


def test_cross_origin_validation_errors_are_unchanged(client):
    o = {"Origin": ALLOWED}
    sid = client.post(URL, json={"synonym": "Bow station", "canonical": FCL}, headers=o).json()["synonym"]["id"]
    dup = client.post(URL, json={"synonym": "Bow station", "canonical": FCL}, headers=o)
    assert dup.status_code == 409 and dup.headers["access-control-allow-origin"] == ALLOWED   # readable by the page
    bad = client.put(f"{URL}/{sid}", json={"synonym": "Bow station", "canonical": "NOT A REAL PLACE"}, headers=o)
    assert bad.status_code == 422 and bad.headers["access-control-allow-origin"] == ALLOWED
    assert client.delete(f"{URL}/SYN-000000000000", headers=o).status_code == 404
    assert [s["synonym"] for s in client.get(URL).json()["synonyms"]] == ["Bow station"]       # nothing changed


def test_cross_origin_auth_and_roles_are_unchanged(client):
    o = {"Origin": ALLOWED}
    sid = client.post(URL, json={"synonym": "Bow station", "canonical": FCL}, headers=o).json()["synonym"]["id"]
    sign_in(client, "reviewer")
    for r in (client.put(f"{URL}/{sid}", json={"synonym": "X station", "canonical": FCL}, headers=o),
              client.delete(f"{URL}/{sid}", headers=o)):
        assert (r.status_code, r.json()["code"]) == (403, "forbidden")
    client.headers.pop("Authorization")
    for r in (client.put(f"{URL}/{sid}", json={"synonym": "X station", "canonical": FCL}, headers=o),
              client.delete(f"{URL}/{sid}", headers=o)):
        assert r.status_code == 401
    sign_in(client, "admin")
    assert [s["synonym"] for s in client.get(URL).json()["synonyms"]] == ["Bow station"]      # still there


def test_same_origin_requests_through_the_vite_proxy_are_unaffected(client):
    r = client.get(URL)                                                          # no Origin header (proxy / same origin)
    assert r.status_code == 200 and "access-control-allow-origin" not in r.headers
