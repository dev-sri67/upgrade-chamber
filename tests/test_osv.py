import json

import httpx
import pytest

from upgrade_chamber.osv import MAX_RESPONSE_BYTES, OSV_API_ROOT, query_osv


PACKAGE = "requests"
VERSION = "2.31.0"
FETCHED = "2026-09-26T00:00:00+00:00"


def offline_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)


def assert_snapshot_common(snapshot: dict, status: str) -> None:
    assert snapshot["schema_version"] == 1
    assert snapshot["package"] == PACKAGE
    assert snapshot["version"] == VERSION
    assert snapshot["fetched_utc"] == FETCHED
    assert snapshot["status"] == status


def test_successful_query_records_parsed_snapshot():
    seen = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"vulns": [
            {"id": "PYSEC-2024-0001", "aliases": ["CVE-2024-0001", "GHSA-aaaa-bbbb-cccc"],
             "summary": "credential leak", "details": "must not be copied"},
            {"id": "GHSA-dddd-eeee-ffff", "summary": "proxy bypass"},
            {"id": "PYSEC-2025-0002", "aliases": ["CVE-2025-0002"], "summary": 7},
        ]})

    with offline_client(respond) as client:
        snapshot = query_osv(PACKAGE, VERSION, client=client, now_utc=FETCHED)

    assert_snapshot_common(snapshot, "available")
    assert snapshot["vulnerabilities"] == [
        {"id": "PYSEC-2024-0001", "aliases": ["CVE-2024-0001", "GHSA-aaaa-bbbb-cccc"],
         "summary": "credential leak"},
        {"id": "GHSA-dddd-eeee-ffff", "aliases": [], "summary": "proxy bypass"},
        {"id": "PYSEC-2025-0002", "aliases": ["CVE-2025-0002"], "summary": ""},
    ]
    request = seen[0]
    assert request.url == f"{OSV_API_ROOT}/query"
    assert json.loads(request.content) == {
        "package": {"name": PACKAGE, "ecosystem": "PyPI"}, "version": VERSION}
    assert "Authorization" not in request.headers


def test_zero_vulnerabilities_is_available_with_empty_list():
    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"vulns": []})

    with offline_client(respond) as client:
        snapshot = query_osv(PACKAGE, VERSION, client=client, now_utc=FETCHED)

    assert_snapshot_common(snapshot, "available")
    assert snapshot["vulnerabilities"] == []


def test_http_error_status_records_unavailable_without_provider_body():
    def reject(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="diagnostic provider body")

    with offline_client(reject) as client:
        snapshot = query_osv(PACKAGE, VERSION, client=client, now_utc=FETCHED)

    assert_snapshot_common(snapshot, "unavailable")
    assert snapshot["error"] == "OSVError: OSV returned HTTP 500"
    assert "diagnostic provider body" not in json.dumps(snapshot)


def test_transport_error_records_unavailable():
    def fail(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with offline_client(fail) as client:
        snapshot = query_osv(PACKAGE, VERSION, client=client, now_utc=FETCHED)

    assert_snapshot_common(snapshot, "unavailable")
    assert "transport failed" in snapshot["error"]


def test_invalid_json_records_unavailable():
    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"{not json")

    with offline_client(respond) as client:
        snapshot = query_osv(PACKAGE, VERSION, client=client, now_utc=FETCHED)

    assert_snapshot_common(snapshot, "unavailable")
    assert "invalid JSON" in snapshot["error"]


def test_oversized_body_records_unavailable():
    def oversized(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * (MAX_RESPONSE_BYTES + 1))

    with offline_client(oversized) as client:
        snapshot = query_osv(PACKAGE, VERSION, client=client, now_utc=FETCHED)

    assert_snapshot_common(snapshot, "unavailable")
    assert "exceeds byte limit" in snapshot["error"]


def test_unexpected_json_shape_records_unavailable():
    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"vulns": {}})

    with offline_client(respond) as client:
        snapshot = query_osv(PACKAGE, VERSION, client=client, now_utc=FETCHED)

    assert_snapshot_common(snapshot, "unavailable")
    assert "unexpected shape" in snapshot["error"]


def test_redirect_is_not_followed_even_when_injected_client_follows_redirects():
    seen = []

    def redirect(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(302, headers={"Location": "https://example.com/mirror"})

    with httpx.Client(transport=httpx.MockTransport(redirect), trust_env=False,
                      follow_redirects=True) as client:
        snapshot = query_osv(PACKAGE, VERSION, client=client, now_utc=FETCHED)

    assert len(seen) == 1
    assert_snapshot_common(snapshot, "unavailable")
    assert "HTTP 302" in snapshot["error"]


def test_input_validation_rejects_bad_package_and_version():
    def forbidden(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("request must not be sent")

    with offline_client(forbidden) as client:
        with pytest.raises(ValueError, match="package"):
            query_osv("", VERSION, client=client)
        with pytest.raises(ValueError, match="package"):
            query_osv("   ", VERSION, client=client)
        with pytest.raises(ValueError, match="package"):
            query_osv("p" * 201, VERSION, client=client)
        with pytest.raises(ValueError, match="version"):
            query_osv(PACKAGE, "")
        with pytest.raises(ValueError, match="version"):
            query_osv(PACKAGE, "2." + "1" * 200)


def test_owned_client_is_created_and_closed():
    real_client = httpx.Client
    created = []

    def factory(**kwargs):
        owner = real_client(**kwargs)
        closed = []
        original_close = owner.close

        def close():
            closed.append(True)
            original_close()

        owner.close = close
        created.append((kwargs, owner, closed))
        return owner

    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"vulns": []})

    httpx.Client = factory
    try:
        snapshot = query_osv(PACKAGE, VERSION, now_utc=FETCHED)
    finally:
        httpx.Client = real_client

    assert_snapshot_common(snapshot, "available")
    kwargs, owner, closed = created[0]
    assert kwargs == {"follow_redirects": False, "trust_env": False, "timeout": 10.0}
    assert closed == [True]
