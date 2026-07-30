from __future__ import annotations

from sports_analytics.ui.product_pages import trusted_sport_options


def test_sport_selector_uses_only_trusted_persisted_capability_rows() -> None:
    payload = {
        "market_capabilities": [
            {"sport_code": "tennis"},
            {"sport_code": "football"},
            {"sport_code": "basketball"},
            {"sport_code": "football"},
            {"sport_code": None},
            "untrusted",
        ]
    }
    assert trusted_sport_options(payload) == ("basketball", "football", "tennis")


def test_sport_selector_has_no_hardcoded_fallback() -> None:
    assert trusted_sport_options({}) == ()
    assert trusted_sport_options({"market_capabilities": []}) == ()
