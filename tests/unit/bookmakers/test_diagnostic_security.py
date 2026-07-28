"""Fail-closed diagnostic persistence tests with fake values only."""

from __future__ import annotations

import json
from collections import UserDict
from pathlib import Path

import pytest

from sports_analytics.bookmakers.diagnostics.persistence import (
    UnsafeDiagnosticError,
    publish_diagnostic_json,
    scan_diagnostic_payload,
)


def test_recursive_mapping_and_list_secret_detection_hides_value() -> None:
    fake = "fake-super-secret-value"
    with pytest.raises(UnsafeDiagnosticError) as caught:
        scan_diagnostic_payload({"outer": [{"authorization": fake}]})
    assert "secret-like-key" in str(caught.value)
    assert fake not in str(caught.value)
    assert "authorization" not in str(caught.value).casefold()


def test_tuple_and_mapping_sequences_are_scanned_recursively() -> None:
    with pytest.raises(UnsafeDiagnosticError, match="secret-like-string"):
        scan_diagnostic_payload(UserDict({"outer": [("safe", "fake token material")]}))


@pytest.mark.parametrize(
    "value",
    [
        "Bearer fake-value",
        "https://example.test/path",
        "https://example.test/path?api_key=fake",
        "https://example.test/path#fake-fragment",
        "https://fake-user:fake-pass@example.test/path",
        "//example.test/path",
        "www.example.test/path?x=1",
    ],
)
def test_unsafe_string_values_are_rejected(value: str) -> None:
    with pytest.raises(UnsafeDiagnosticError):
        scan_diagnostic_payload({"nested": [value]})


@pytest.mark.parametrize(
    "payload",
    [
        {"https://example.test/path": "safe"},
        {"x": "prefix https://example.test/path suffix"},
        {"x": "<redacted-credential>https://example.test/path"},
        {"route_id": "my-secret-token"},
        {"response_url": "safe"},
    ],
)
def test_reviewed_scanner_bypasses_are_rejected(payload: dict[str, str]) -> None:
    with pytest.raises(UnsafeDiagnosticError):
        scan_diagnostic_payload(payload)


@pytest.mark.parametrize(
    ("payload", "raw_content"),
    [
        (
            {"x": "prefix x://example.test/path suffix"},
            "prefix x://example.test/path suffix",
        ),
        ({"x://example.test/path": "safe"}, "x://example.test/path"),
        ({"x": "prefix X://example.test/path suffix"}, "X://example.test/path"),
        (
            {"x": f"prefix {'a' * 40}://example.test/path suffix"},
            f"{'a' * 40}://example.test/path",
        ),
        (
            {"x": "<redacted-path>x://example.test/path"},
            "x://example.test/path",
        ),
        (
            {"x": "prefix x://fake-user:fake-pass@example.test/path?x=1#part suffix"},
            "x://fake-user:fake-pass@example.test/path?x=1#part",
        ),
    ],
)
def test_every_valid_embedded_scheme_length_is_rejected_without_raw_content(
    payload: dict[str, str],
    raw_content: str,
) -> None:
    with pytest.raises(UnsafeDiagnosticError, match="embedded-url-with-scheme") as caught:
        scan_diagnostic_payload(payload)
    message = str(caught.value)
    assert raw_content not in message
    assert next(iter(payload)) not in message


def test_ordinary_prose_containing_a_colon_remains_accepted() -> None:
    scan_diagnostic_payload({"summary": "Synthetic status: available"})


@pytest.mark.parametrize(
    "value",
    [
        "market#1",
        "fixture?status",
        "MRES#primary",
        "price?decimal",
        "#structural-marker",
        "question?answer#label",
    ],
)
def test_query_and_fragment_characters_in_non_url_text_are_accepted(value: str) -> None:
    scan_diagnostic_payload({"summary": value})


@pytest.mark.parametrize(
    "value",
    [
        "https://example.test/path#fragment",
        "https://example.test/path?x=1",
        "prefix https://example.test/path?x=1#fragment suffix",
        "//example.test/path#fragment",
        "www.example.test/path?x=1",
    ],
)
def test_url_queries_and_fragments_are_rejected_without_raw_value(value: str) -> None:
    with pytest.raises(UnsafeDiagnosticError) as caught:
        scan_diagnostic_payload({"nested": value})
    assert value not in str(caught.value)


def test_url_like_mapping_key_with_fragment_is_rejected_opaquely() -> None:
    raw_key = "https://example.test/path#fragment"
    with pytest.raises(UnsafeDiagnosticError) as caught:
        scan_diagnostic_payload({raw_key: "safe"})
    assert raw_key not in str(caught.value)


@pytest.mark.parametrize(
    "payload",
    [
        {"prefix //example.test/path suffix": "safe"},
        {"prefix www.example.test/path suffix": "safe"},
        {"url": "safe"},
        {"source_url": "safe"},
        {"final_url": "safe"},
        {"request_url": "safe"},
        {"responseUrl": "safe"},
    ],
)
def test_embedded_url_keys_and_prohibited_url_fields_are_rejected(
    payload: dict[str, str],
) -> None:
    raw_key = next(iter(payload))
    with pytest.raises(UnsafeDiagnosticError) as caught:
        scan_diagnostic_payload(payload)
    assert raw_key not in str(caught.value)


def test_rejected_publication_leaves_no_target_or_partial_file(tmp_path: Path) -> None:
    target = tmp_path / "diagnostic.json"
    with pytest.raises(UnsafeDiagnosticError):
        publish_diagnostic_json(target, {"nested": {"token": "fake"}})
    assert not target.exists()
    assert list(tmp_path.iterdir()) == []


def test_embedded_one_character_scheme_publication_leaves_no_files(
    tmp_path: Path,
) -> None:
    target = tmp_path / "diagnostic.json"
    with pytest.raises(UnsafeDiagnosticError) as caught:
        publish_diagnostic_json(
            target,
            {"x": "prefix x://fake-user:fake-pass@example.test/path suffix"},
        )
    assert "x://fake-user:fake-pass@example.test/path" not in str(caught.value)
    assert not target.exists()
    assert list(tmp_path.iterdir()) == []


def test_url_fragment_rejection_leaves_no_target_or_temporary_file(tmp_path: Path) -> None:
    target = tmp_path / "diagnostic.json"
    raw_value = "prefix https://example.test/path#fragment suffix"
    with pytest.raises(UnsafeDiagnosticError) as caught:
        publish_diagnostic_json(target, {"nested": raw_value})
    assert raw_value not in str(caught.value)
    assert not target.exists()
    assert list(tmp_path.iterdir()) == []


def test_only_exact_redaction_markers_are_accepted() -> None:
    scan_diagnostic_payload(
        [
            "<redacted-path>",
            "<redacted-email>",
            "<redacted-credential>",
            "<truncated>",
        ]
    )
    with pytest.raises(UnsafeDiagnosticError, match="unapproved-redaction-marker"):
        scan_diagnostic_payload("<redacted-credentials>")
    with pytest.raises(UnsafeDiagnosticError, match="embedded-url-with-scheme"):
        scan_diagnostic_payload("<redacted-path> prefix https://example.test/path")


def test_malicious_key_content_is_never_in_error() -> None:
    malicious_key = "access_token=VERYSECRET"
    with pytest.raises(UnsafeDiagnosticError) as caught:
        scan_diagnostic_payload({malicious_key: "fake"})
    message = str(caught.value)
    assert malicious_key not in message
    assert "VERYSECRET" not in message
    assert "access_token" not in message


def test_valid_hostname_may_contain_secret_like_structural_word() -> None:
    scan_diagnostic_payload({"hostname": "session-cdn.example.test"})
    scan_diagnostic_payload({"hostname": "token-routing.example.test"})


def test_non_hostname_secret_like_string_remains_rejected() -> None:
    with pytest.raises(UnsafeDiagnosticError, match="secret-like-string"):
        scan_diagnostic_payload({"summary": "session token material"})


def test_hostname_field_does_not_allow_url_or_credentials() -> None:
    unsafe_values = (
        "https://session-cdn.example.test/path",
        "fake-user:fake-pass@session-cdn.example.test",
        "session-cdn.example.test/path",
    )
    for value in unsafe_values:
        with pytest.raises(UnsafeDiagnosticError):
            scan_diagnostic_payload({"hostname": value})


def test_safe_metadata_publication_has_no_complete_url(tmp_path: Path) -> None:
    target = tmp_path / "diagnostic.json"
    payload = {
        "network_metadata": [
            {
                "hostname": "offering.begmedia.com",
                "sanitized_path_hash": "a" * 64,
                "approved_route_id": "betclic-match-service-get-popular-v2",
                "approved_path_template": (
                    "/web/offering.access.api/offering.access.api.MatchService/GetPopularV2"
                ),
            }
        ]
    }
    publish_diagnostic_json(target, payload)
    persisted = json.loads(target.read_text(encoding="utf-8"))
    encoded = json.dumps(persisted)
    assert "response_url" not in encoded
    assert "?" not in encoded
    assert "#" not in encoded
