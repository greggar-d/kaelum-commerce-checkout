# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Kaelum Technologies Ltd.

"""Merchant backend behaviour, with a fake KAELUM client (no network, no key)."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from typing import Any

import pytest
from merchant_agent import ChangeNotApplicable, MerchantSessionContext, PriceUpdateItem

import kaelum_storefront.merchant as merchant_mod
from kaelum_storefront.backend import _product_id_for
from kaelum_storefront.merchant import KaelumMerchantBackend, resolve_period

merchant_mod.PAGE_PAUSE_S = 0.0

PRODUCTS = [
    {
        "title": "8T Signature Tee",
        "rrp_gbp": 35.0,
        "discount_pct": 18.0,
        "net_gbp": 28.7,
        "fee_gbp": 1.29,
        "total_gbp": 29.99,
        "klm_units": 149.95,
        "canonical_url": "https://8tclothing.com/p/signature-tee",
        "availability": "IN_STOCK",
        "merchant_ref": "kmr_8t",
    },
    {
        "title": "8T Mosaic Hoodie",
        "rrp_gbp": 60.0,
        "discount_pct": 18.0,
        "canonical_url": "https://8tclothing.com/p/mosaic-hoodie",
        "availability": "OUT_OF_STOCK",
        "merchant_ref": "kmr_8t",
    },
]

INSIGHTS = {
    "ok": True,
    "currency": "GBP",
    "period": {"from": "2026-10-13", "to": "2026-10-19", "days": 7},
    "previous_period": {"from": "2026-10-06", "to": "2026-10-12", "days": 7},
    "current": {
        "sales_gbp": 57.4, "orders": 2, "checkouts_started": 5, "conversion_rate": 0.4,
        "average_order_value_gbp": 28.7, "discount_given_gbp": 12.6, "abandoned": 1,
    },
    "previous": {
        "sales_gbp": 28.7, "orders": 1, "checkouts_started": 4, "conversion_rate": 0.25,
        "average_order_value_gbp": 28.7, "discount_given_gbp": 6.3, "abandoned": 0,
    },
    "daily": [
        {"date": f"2026-10-{d}", "sales_gbp": s, "orders": o, "checkouts_started": c}
        for d, s, o, c in [
            ("13", 0, 0, 1), ("14", 28.7, 1, 1), ("15", 0, 0, 0), ("16", 0, 0, 1),
            ("17", 0, 0, 1), ("18", 28.7, 1, 1), ("19", 0, 0, 0),
        ]
    ],
}


class FakeClient:
    def __init__(self) -> None:
        self.insight_calls: list[tuple[str, str]] = []

    async def discover(self, query: str = "", category=None, limit: int = 8, offset: int = 0):
        return {"ok": True, "product_count": len(PRODUCTS), "products": PRODUCTS[offset:offset + limit]}

    async def merchant_insights(self, from_day: str, to_day: str):
        self.insight_calls.append((from_day, to_day))
        return INSIGHTS


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def session():
    return MerchantSessionContext(
        session_id="m1", merchant_id="kmr_8t", operator="greggar",
        now=datetime(2026, 10, 19, 9, 0, tzinfo=UTC),
    )


def test_resolve_period():
    today = date(2026, 10, 19)
    assert resolve_period(None, today) == (date(2026, 10, 13), today, "last_7_days")
    assert resolve_period("last_30_days", today)[0] == date(2026, 9, 20)
    assert resolve_period("2026-10-01/2026-10-05", today)[:2] == (date(2026, 10, 1), date(2026, 10, 5))
    assert resolve_period("last_month", today)[:2] == (date(2026, 9, 1), date(2026, 9, 30))
    assert resolve_period("nonsense", today)[2] == "last_7_days"


def test_snapshot_reports_real_figures_and_changes(session):
    fake = FakeClient()
    snap = _run(KaelumMerchantBackend(fake).get_business_snapshot(session))
    assert fake.insight_calls == [("2026-10-13", "2026-10-19")]
    assert snap.currency == "GBP"
    assert snap.sales == 57.4 and snap.orders == 2
    assert snap.sales_change_pct == 100.0
    assert snap.traffic is None  # KAELUM has no traffic data; never invented
    assert snap.alerts.low_stock == 1  # the out-of-stock hoodie


def test_weekly_series_reconciles_with_snapshot(session):
    backend = KaelumMerchantBackend(FakeClient())
    series = _run(backend.query_metrics(session, "revenue", granularity="week"))
    assert series.metric == "sales"
    assert round(sum(p.value for p in series.points), 2) == 57.4
    conv = _run(backend.query_metrics(session, "conversion_rate", granularity="week"))
    # Recomputed from summed parts, not averaged: 2 paid / 5 started.
    started = sum(r["checkouts_started"] for r in INSIGHTS["daily"])
    assert started == 5
    assert {p.date for p in conv.points} == {"2026-10-12", "2026-10-19"}


def test_unknown_metric_and_segment_say_why(session):
    backend = KaelumMerchantBackend(FakeClient())
    assert "Not available" in _run(backend.query_metrics(session, "traffic")).note
    seg = _run(backend.query_metrics(session, "sales", segment="hoodies"))
    assert not seg.points and "segment" in seg.note


def test_listings_share_ids_with_the_shopping_side(session):
    backend = KaelumMerchantBackend(FakeClient())
    listings = _run(backend.search_listings(session, ""))
    assert [x.listing_id for x in listings] == [_product_id_for(p) for p in PRODUCTS]
    assert listings[1].status == "out_of_stock"
    assert listings[0].attributes["stock_count"] == "not tracked by KAELUM"


def test_price_change_stages_but_never_applies(session):
    backend = KaelumMerchantBackend(FakeClient())
    lid = _product_id_for(PRODUCTS[0])
    change = _run(backend.stage_price_update(session, [PriceUpdateItem(listing_id=lid, new_price=32.0)]))
    assert change.items[0].before == 35.0 and change.items[0].after == 32.0
    with pytest.raises(ChangeNotApplicable, match="never|does not write"):
        _run(backend.apply_change(session, change.change_id))
    assert [c.change_id for c in _run(backend.get_pending_changes(session))] == [change.change_id]


def test_guardrail_holds_an_oversized_price_move(session):
    from merchant_agent import GuardrailViolation

    backend = KaelumMerchantBackend(FakeClient())
    lid = _product_id_for(PRODUCTS[0])
    with pytest.raises(GuardrailViolation):
        _run(backend.stage_price_update(session, [PriceUpdateItem(listing_id=lid, new_price=10.0)]))


def test_unmanaged_systems_refuse_plainly(session):
    from merchant_agent import CampaignDraft, PromotionDraft

    backend = KaelumMerchantBackend(FakeClient())
    with pytest.raises(ChangeNotApplicable):
        _run(backend.stage_inventory_action(session, []))
    with pytest.raises(ChangeNotApplicable, match="KLM discount"):
        _run(backend.stage_promotion(session, PromotionDraft(
            name="x", listing_ids=["klm-1"], discount_pct=10, starts="2026-10-20", ends="2026-10-27")))
    with pytest.raises(ChangeNotApplicable):
        _run(backend.stage_campaign(session, CampaignDraft(name="x")))


def test_context_states_limitations(session):
    ctx = _run(KaelumMerchantBackend(FakeClient()).get_merchant_context(session))
    assert ctx["klm_discount_pct"] == 18.0
    assert {x["source"] for x in ctx["limitations"]} >= {"sales", "traffic", "stock", "changes"}


def test_merchant_agent_runs_a_turn_over_kaelum(session):
    """The blueprint's MerchantAgent drives this backend end to end, with the blueprint's
    scripted model client standing in for the API."""
    from commerce_common.testing import FakeClient as ScriptedModel
    from commerce_common.testing import text_message, tool_use_message
    from merchant_agent import MerchantSessionState
    from merchant_agent_runtime import MerchantAgent

    from kaelum_storefront.merchant import build_kaelum_merchant_config

    config = build_kaelum_merchant_config()
    config = config.model_copy(update={"enable_memory": False})
    agent = MerchantAgent(
        backend=KaelumMerchantBackend(FakeClient(), config),
        config=config,
        client=ScriptedModel([
            tool_use_message("get_business_snapshot", {"period": "last_7_days"}),
            text_message("Sales were £57.40 from 2 orders, double the week before."),
        ]),
    )

    async def go() -> list[Any]:
        messages = [{"role": "user", "content": "How did we do this week?"}]
        return [e async for e in agent.stream_turn(messages, session, MerchantSessionState())]

    events = _run(go())
    results = [e.data for e in events if e.type == "tool_result"]
    assert results and results[0]["tool"] == "get_business_snapshot"
    assert results[0]["status"] == "ok"
    assert "57.4" in "".join(e.data.get("text", "") for e in events if e.type == "text_delta")


def test_insights_are_read_once_per_period(session):
    fake = FakeClient()
    backend = KaelumMerchantBackend(fake)
    _run(backend.get_business_snapshot(session))
    _run(backend.query_metrics(session, "sales"))
    _run(backend.query_metrics(session, "orders", granularity="week"))
    assert len(fake.insight_calls) == 1


def test_unsupported_filters_match_nothing(session):
    from merchant_agent import ListingFilters

    backend = KaelumMerchantBackend(FakeClient())
    assert _run(backend.search_listings(session, "", ListingFilters(content_quality="poor"))) == []
    assert _run(backend.search_listings(session, "", ListingFilters(max_stock=5))) == []
    assert len(_run(backend.search_listings(session, "", ListingFilters(status="out_of_stock")))) == 1
