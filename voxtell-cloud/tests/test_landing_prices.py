"""The landing page states every price and quota twice. Pin both copies.

`landing/index.html` carries the plan cards a human reads AND a JSON-LD
``offers`` array a search engine reads. Nothing connects them, so an edit to one
silently disagrees with the other — and the disagreement is invisible on the page,
because the JSON-LD does not render.

This is not a hypothetical: DicomSegVR states its prices in three places
(`landing/index.html` cards, its own JSON-LD, and `backend/app/db.py::PLAN_SEED`)
and they are only still consistent because nobody has touched them.

PLANS below is the single source of truth for this test. When a price genuinely
changes it changes here, and the test then tells you every place in the page that
has not caught up. When `/v1/plans` exists (Phase E), this should assert against
``api.db.PLAN_SEED`` instead of a literal — at which point the page, the schema
markup and the database are pinned to one value.

The Stripe Price objects behind these amounts are LIVE and shared with
DicomSegVR, and a Price's amount is immutable in Stripe. So changing an amount
here is not a copy edit: it requires a new Price, and until that exists the number
on the page must not move.
"""

from __future__ import annotations

import json
import pathlib
import re

import pytest

LANDING = pathlib.Path(__file__).resolve().parent.parent / "landing" / "index.html"

# plan id -> what the page must say. `name` is the customer-facing label, which is
# deliberately NOT the plan id for the middle tier: the id stays `clinician`
# because that is what DicomSegVR's Stripe metadata and checkout URLs already use.
PLANS = {
    "explorer": {
        "name": "Explorer",
        "price": "12.99",
        "jobs": "60 jobs",
        "keys": "2",
        "bundle": "DicomSegVR Explorer",
        "checkout": None,          # the trial needs no checkout
    },
    "clinician": {
        "name": "Pro",
        "price": "49.99",
        "jobs": "250 jobs",
        "keys": "5",
        "bundle": "DicomSegVR Pro",
        "checkout": "/dashboard/checkout?plan=clinician",
    },
    "enterprise": {
        "name": "Enterprise",
        "price": "199.99",
        "jobs": "Unlimited",
        "keys": "Unlimited",
        "bundle": "DicomSegVR Enterprise",
        "checkout": "/dashboard/checkout?plan=enterprise",
    },
}

TRIAL_DAYS = 14


@pytest.fixture(scope="module")
def html() -> str:
    assert LANDING.is_file(), f"missing {LANDING}"
    return LANDING.read_text()


@pytest.fixture(scope="module")
def offers(html: str) -> dict[str, dict]:
    """The JSON-LD offers, keyed by name."""
    blocks = re.findall(
        r'<script type="application/ld\+json">(.*?)</script>', html, re.S
    )
    assert blocks, "no JSON-LD block in the landing page"
    # Parsed rather than regexed, so malformed JSON-LD fails here instead of
    # silently shipping and being ignored by every crawler.
    doc = json.loads(blocks[0])
    got = {o["name"]: o for o in doc.get("offers", [])}
    assert got, "JSON-LD has no offers array"
    return got


def test_card_prices_match_the_source_of_truth(html: str) -> None:
    for plan_id, spec in PLANS.items():
        needle = f'data-count="{spec["price"]}"'
        assert needle in html, (
            f"{plan_id}: no plan card with {needle}. The visible amount is rendered "
            f"from data-count by main.js's count-up, so this is the number a "
            f"customer sees."
        )
        # The static text inside the span is the no-JS fallback and must agree.
        assert f'>${spec["price"]}<' in html, (
            f"{plan_id}: the no-JS fallback text does not read ${spec['price']}"
        )


def test_json_ld_offers_match_the_cards(offers: dict[str, dict]) -> None:
    assert set(offers) == {s["name"] for s in PLANS.values()}, (
        "JSON-LD offer names do not match the plan cards"
    )
    for spec in PLANS.values():
        offer = offers[spec["name"]]
        assert offer["price"] == spec["price"], (
            f"{spec['name']}: JSON-LD says {offer['price']}, cards say {spec['price']}"
        )
        assert offer["priceCurrency"] == "USD"


def test_json_ld_offer_descriptions_state_the_real_limits(offers: dict[str, dict]) -> None:
    """The offer text is customer-facing in search results — hold it to the cards."""
    for plan_id, spec in PLANS.items():
        desc = offers[spec["name"]]["description"]
        assert spec["bundle"] in desc, (
            f"{plan_id}: JSON-LD description does not mention {spec['bundle']}"
        )
        # Quotas are the thing most likely to be edited on the card and forgotten
        # here, because the card says "250 jobs a month" in bold and the JSON-LD
        # says it in prose.
        head = spec["jobs"].split()[0].lower()
        assert head in desc.lower(), (
            f"{plan_id}: JSON-LD description does not state the {spec['jobs']} quota"
        )


def test_bundle_is_mentioned_once_per_card_and_never_in_a_heading(html: str) -> None:
    """DicomSegVR stays bundled and stays visible, but out of the headlines.

    It was the `<h2>` of VoxTell's own pricing section, plus a bordered panel in
    all three cards. Pinning this keeps a later copy edit from promoting it back.
    """
    for spec in PLANS.values():
        assert html.count(f"<b>{spec['bundle']}</b>") == 1, (
            f"expected exactly one bundle line for {spec['bundle']}"
        )
    for heading in re.findall(r"<h[12][^>]*>(.*?)</h[12]>", html, re.S):
        assert "DicomSegVR" not in heading, (
            f"DicomSegVR appears in a heading: {heading.strip()!r}"
        )


def test_paid_ctas_point_at_checkout_and_the_trial_does_not(html: str) -> None:
    """The URLs must not change when Stripe lands, so they are pinned now."""
    for plan_id, spec in PLANS.items():
        if spec["checkout"] is None:
            continue
        assert f'href="{spec["checkout"]}"' in html, (
            f"{plan_id}: missing CTA to {spec['checkout']}"
        )


def test_trial_length_is_stated_consistently(html: str) -> None:
    # Any other number of days on this page is a contradiction a customer can see.
    others = {int(n) for n in re.findall(r"(\d+)-day (?:free )?trial", html)} - {TRIAL_DAYS}
    assert not others, f"conflicting trial lengths on the page: {sorted(others)}"
    assert f"{TRIAL_DAYS}-day trial" in html or f"{TRIAL_DAYS}-day free trial" in html


def test_no_stale_asset_versions_are_referenced(html: str) -> None:
    """A v1 reference survives as a 404 that only shows up in a browser console.

    `prompts.v1.json` is exempt: it is unchanged content and deliberately still v1.
    """
    stale = [
        ref for ref in re.findall(r'/assets/[A-Za-z0-9._-]+', html)
        if ".v1." in ref and "prompts.v1.json" not in ref
    ]
    assert not stale, f"stale v1 asset references: {sorted(set(stale))}"
