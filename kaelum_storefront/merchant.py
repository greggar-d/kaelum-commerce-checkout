# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Kaelum Technologies Ltd. Built on the Apache-2.0 reference
# blueprint at github.com/anthropics/commerce-agents (see NOTICE).

"""KaelumMerchantBackend: the blueprint's merchant-agent integration surface, implemented
over KAELUM for one merchant (the one the merchant token pays).

What a KAELUM merchant agent can read:

  get_business_snapshot / query_metrics  -> kaelumMerchantInsights (KLM checkout sales,
                                            orders, checkouts started, conversion, AOV,
                                            discount given; this period vs the prior one)
  search_listings / get_listing          -> kaelumDiscover, scoped to the merchant_ref
  get_pricing_context                    -> the same record: price, KLM discount, net, fee,
                                            KLM units

What it cannot, and says so (``get_merchant_context`` limitations):

  stock levels and campaigns             -> live in the merchant's own store and ad tools;
                                            switched off in build_kaelum_merchant_config
  order exceptions                       -> KAELUM has no order-exception object; none
                                            are invented

Writes. Listing and price changes can be staged, so the merchant sees a reviewed
proposal with its guardrails checked. KAELUM does not write to the merchant's store, so
``apply_change`` never mutates anything: it refuses with the instruction to make the
approved change in the merchant's own store. The agent proposes; the merchant applies.

Currency is GBP throughout. Listing ids are the same ``klm-XXXXXXXX`` ids the shopping
backend uses, so one product has one id on both sides.
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, date, datetime, timedelta
from typing import Any

from merchant_agent import (
    ActorKind,
    AlertCounts,
    BusinessSnapshot,
    Campaign,
    CampaignDraft,
    ChangeItem,
    ChangeKind,
    ChangeLedger,
    ChangeNotApplicable,
    InventoryActionItem,
    InventoryAlert,
    Listing,
    ListingDetails,
    ListingFilters,
    MerchantAgentConfig,
    MerchantBackend,
    MerchantSessionContext,
    MetricPoint,
    MetricSeries,
    OrderIssue,
    PriceUpdateItem,
    PricingContext,
    PromotionDraft,
    StagedChange,
)

from .backend import _gross_gbp, _product_id_for
from .client import KaelumClient

CURRENCY = "GBP"

# Claude Sonnet 5: the standard tier for KAELUM agents and the blueprint's own default.
# The blueprint forces a tool on grounding turns, which needs a model that accepts a
# forced tool_choice. Override with KAELUM_AGENT_MODEL.
DEFAULT_MODEL = "claude-sonnet-5"
DEFAULT_PERIOD = "last_7_days"
PAGE_PAUSE_S = 1.0

# Metric name -> (field in the insights payload, unit). Names follow the snapshot.
METRICS: dict[str, tuple[str, str]] = {
    "sales": ("sales_gbp", "GBP"),
    "orders": ("orders", "count"),
    "checkouts_started": ("checkouts_started", "count"),
    "conversion_rate": ("conversion_rate", "fraction"),
    "average_order_value": ("average_order_value_gbp", "GBP"),
    "discount_given": ("discount_given_gbp", "GBP"),
}
_ALIASES = {
    "revenue": "sales",
    "aov": "average_order_value",
    "conversion": "conversion_rate",
    "checkouts": "checkouts_started",
    "discount": "discount_given",
}

APPLY_REFUSAL = (
    "KAELUM does not write to the merchant's store. The change is reviewed and approved "
    "here; make it in the store's own admin (for 8T Clothing, the Wix dashboard), and "
    "KAELUM discovery picks it up from there."
)


def resolve_period(period: str | None, today: date) -> tuple[date, date, str]:
    """(from, to, label) for a reporting period. Accepts last_N_days, today, yesterday,
    this_month, last_month, and ISO ranges written A/B, A..B or 'A to B'. Anything else
    falls back to the default period, and the label says which was used."""
    p = (period or DEFAULT_PERIOD).strip().lower()
    if m := re.fullmatch(r"last_(\d{1,3})_days?", p):
        n = max(1, min(int(m.group(1)), 366))
        return today - timedelta(days=n - 1), today, f"last_{n}_days"
    if p == "today":
        return today, today, "today"
    if p == "yesterday":
        y = today - timedelta(days=1)
        return y, y, "yesterday"
    if p == "this_month":
        return today.replace(day=1), today, "this_month"
    if p == "last_month":
        end = today.replace(day=1) - timedelta(days=1)
        return end.replace(day=1), end, "last_month"
    m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})\s*(?:/|\.\.|to)\s*(\d{4}-\d{2}-\d{2})", p)
    if m:
        a, b = date.fromisoformat(m.group(1)), date.fromisoformat(m.group(2))
        if a <= b and (b - a).days < 366:
            return a, b, f"{a.isoformat()}/{b.isoformat()}"
    start, end, label = resolve_period(DEFAULT_PERIOD, today)
    return start, end, label


def _change_pct(now: float | None, before: float | None) -> float | None:
    if now is None or before in (None, 0):
        return None
    return round((now - before) / before * 100, 1)


class KaelumMerchantBackend(MerchantBackend):
    def __init__(self, client: KaelumClient, config: MerchantAgentConfig | None = None) -> None:
        self._client = client
        self._config = config or build_kaelum_merchant_config()
        self.ledger = ChangeLedger(self._config)
        self._catalog: dict[str, dict[str, Any]] = {}
        # One insights read per period per briefing: the agent often asks for the
        # snapshot and several series over the same range.
        self._insights_cache: dict[tuple[str, str], dict[str, Any]] = {}

    # -- Performance -----------------------------------------------------------------

    async def _insights(self, session: MerchantSessionContext, period: str | None):
        today = (session.now or datetime.now(UTC)).date()
        start, end, label = resolve_period(period, today)
        key = (start.isoformat(), end.isoformat())
        if key not in self._insights_cache:
            self._insights_cache[key] = await self._client.merchant_insights(*key)
        return self._insights_cache[key], label

    async def get_business_snapshot(
        self, session: MerchantSessionContext, period: str | None = None
    ) -> BusinessSnapshot:
        body, label = await self._insights(session, period)
        cur, prev = body.get("current") or {}, body.get("previous") or {}
        listings = await self._listings()
        out_of_stock = sum(1 for r in listings if r.get("availability") not in (None, "IN_STOCK"))
        return BusinessSnapshot(
            period=label,
            compare_to=f"{body['previous_period']['from']}/{body['previous_period']['to']}",
            sales=float(cur.get("sales_gbp") or 0.0),
            orders=int(cur.get("orders") or 0),
            traffic=None,
            conversion_rate=cur.get("conversion_rate"),
            average_order_value=cur.get("average_order_value_gbp"),
            sales_change_pct=_change_pct(cur.get("sales_gbp"), prev.get("sales_gbp")),
            orders_change_pct=_change_pct(cur.get("orders"), prev.get("orders")),
            conversion_change_pct=_change_pct(cur.get("conversion_rate"), prev.get("conversion_rate")),
            currency=CURRENCY,
            alerts=AlertCounts(
                low_stock=out_of_stock,
                pending_changes=len(self.ledger.pending()),
            ),
            note="KLM checkout sales only; conversion is paid / started checkouts; no traffic data.",
        )

    async def query_metrics(
        self,
        session: MerchantSessionContext,
        metric: str,
        period: str | None = None,
        granularity: str = "day",
        segment: str | None = None,
    ) -> MetricSeries:
        name = _ALIASES.get(metric.strip().lower(), metric.strip().lower())
        body, label = await self._insights(session, period)
        if name not in METRICS:
            return MetricSeries(
                metric=metric,
                period=label,
                note=f"Not available. Metrics: {', '.join(METRICS)}.",
            )
        field, unit = METRICS[name]
        if segment:
            return MetricSeries(
                metric=name, unit=unit, period=label, segment=segment,
                note="KAELUM does not segment checkout data by category or product yet.",
            )
        daily = body.get("daily") or []
        points = _bucket(daily, name, field, granularity)
        return MetricSeries(
            metric=name,
            unit=unit,
            granularity=granularity if granularity in ("day", "week", "month") else "day",
            period=label,
            points=points,
        )

    async def get_campaign_performance(
        self, session: MerchantSessionContext, campaign_id: str | None = None
    ) -> list[Campaign]:
        return []

    # -- Catalog -----------------------------------------------------------------------

    async def _listings(self) -> list[dict[str, Any]]:
        if not self._catalog:
            offset = 0
            while offset < 480:
                body = await self._client.discover(query="", limit=24, offset=offset)
                page = body.get("products", []) or []
                for rec in page:
                    self._catalog[_product_id_for(rec)] = rec
                offset += len(page)
                total = body.get("product_count")
                if len(page) < 24 or (isinstance(total, int) and offset >= total):
                    break
                # Each discovery call does many reads on the platform; space the pages
                # out so a briefing never bursts past KAELUM's rate limit.
                await asyncio.sleep(PAGE_PAUSE_S)
        return list(self._catalog.values())

    def _to_listing(self, rec: dict[str, Any]) -> Listing:
        # stock is 0 because KAELUM does not track counts; the attribute says so.
        attributes = {"pay_in": "KLM", "stock_count": "not tracked by KAELUM"}
        for key in ("discount_pct", "net_gbp", "fee_gbp", "total_gbp", "klm_units"):
            if rec.get(key) is not None:
                attributes[key] = str(rec[key])
        if rec.get("canonical_url"):
            attributes["store_url"] = str(rec["canonical_url"])
        in_stock = rec.get("availability") in (None, "IN_STOCK")
        return Listing(
            listing_id=_product_id_for(rec),
            title=str(rec.get("title") or "Untitled"),
            status="active" if in_stock else "out_of_stock",
            price=_gross_gbp(rec),
            currency=CURRENCY,
            stock=0,
            category=rec.get("category_id"),
            attributes=attributes,
            image_url=rec.get("image_url"),
            short_description=(str(rec.get("description") or "")[:280] or None),
        )

    async def search_listings(
        self,
        session: MerchantSessionContext,
        query: str,
        filters: ListingFilters | None = None,
        limit: int = 8,
    ) -> list[Listing]:
        if query.strip():
            body = await self._client.discover(query=query, limit=max(limit, 8))
            records = body.get("products", []) or []
            for rec in records:
                self._catalog[_product_id_for(rec)] = rec
        else:
            records = await self._listings()
        listings = [self._to_listing(r) for r in records]
        if filters and (filters.content_quality is not None or filters.max_stock is not None):
            # KAELUM rates no listing content and holds no stock counts, so these filters
            # match nothing; returning everything would read as every listing qualifying.
            return []
        if filters:
            if filters.status:
                listings = [x for x in listings if x.status == filters.status]
            if filters.category:
                listings = [x for x in listings if x.category == filters.category]
            if filters.sort == "price_asc":
                listings.sort(key=lambda x: x.price)
            elif filters.sort == "price_desc":
                listings.sort(key=lambda x: x.price, reverse=True)
        return listings[:limit]

    async def get_listing(
        self, session: MerchantSessionContext, listing_id: str
    ) -> ListingDetails | None:
        rec = self._catalog.get(listing_id)
        if rec is None:
            await self._listings()
            rec = self._catalog.get(listing_id)
        if rec is None:
            return None
        base = self._to_listing(rec)
        return ListingDetails(
            **base.model_dump(), long_description=str(rec.get("description") or "") or None
        )

    # -- Inventory and order health -------------------------------------------------------

    async def get_inventory_alerts(self, session: MerchantSessionContext) -> list[InventoryAlert]:
        # Stock counts live in the merchant's store; KAELUM only sees availability. An
        # unavailable listing is reported with stock 0; nothing else is inferred.
        alerts = []
        for rec in await self._listings():
            if rec.get("availability") not in (None, "IN_STOCK"):
                alerts.append(
                    InventoryAlert(
                        listing_id=_product_id_for(rec),
                        title=str(rec.get("title") or "Untitled"),
                        kind="low_stock",
                        stock=0,
                        storefront_visible=False,
                    )
                )
        return alerts

    async def get_order_issues(self, session: MerchantSessionContext) -> list[OrderIssue]:
        return []

    # -- Pricing -------------------------------------------------------------------------

    async def get_pricing_context(
        self, session: MerchantSessionContext, listing_id: str
    ) -> PricingContext | None:
        listing = await self.get_listing(session, listing_id)
        if listing is None:
            return None
        return PricingContext(
            listing_id=listing.listing_id,
            current_price=listing.price,
            currency=CURRENCY,
            max_price_delta_pct=self._config.max_price_delta_pct,
            max_promotion_discount_pct=self._config.max_promotion_discount_pct,
        )

    # -- Staged writes ----------------------------------------------------------------------

    async def stage_listing_update(
        self,
        session: MerchantSessionContext,
        listing_id: str,
        fields: dict[str, Any],
        note: str | None = None,
    ) -> StagedChange:
        listing = await self.get_listing(session, listing_id)
        if listing is None:
            raise ChangeNotApplicable(f"No listing {listing_id}.")
        current = listing.model_dump()
        items = [
            ChangeItem(target=listing_id, field=name, before=current.get(name), after=value)
            for name, value in fields.items()
        ]
        return self.ledger.stage(
            kind=ChangeKind.LISTING_UPDATE,
            summary=note or f"Update {', '.join(fields)} on {listing.title}",
            items=items,
            actor=session.operator,
            actor_kind=ActorKind.AGENT,
            guardrail_notes=[APPLY_REFUSAL],
        )

    async def stage_price_update(
        self,
        session: MerchantSessionContext,
        items: list[PriceUpdateItem],
        note: str | None = None,
    ) -> StagedChange:
        change_items = []
        for item in items:
            listing = await self.get_listing(session, item.listing_id)
            if listing is None:
                raise ChangeNotApplicable(f"No listing {item.listing_id}.")
            change_items.append(
                ChangeItem(target=item.listing_id, field="price", before=listing.price, after=item.new_price)
            )
        return self.ledger.stage(
            kind=ChangeKind.PRICE_UPDATE,
            summary=note or f"Change the price of {len(change_items)} listing(s)",
            items=change_items,
            actor=session.operator,
            actor_kind=ActorKind.AGENT,
            currency=CURRENCY,
            guardrail_notes=[APPLY_REFUSAL],
        )

    async def stage_inventory_action(
        self,
        session: MerchantSessionContext,
        items: list[InventoryActionItem],
        note: str | None = None,
    ) -> StagedChange:
        raise ChangeNotApplicable("Stock is managed in the merchant's own store, not in KAELUM.")

    async def stage_promotion(
        self, session: MerchantSessionContext, promotion: PromotionDraft
    ) -> StagedChange:
        raise ChangeNotApplicable(
            "KAELUM promotions are the merchant's KLM discount (minimum 6%), set in KAELUM "
            "Integrations. Store promotions are run in the merchant's own store."
        )

    async def stage_campaign(
        self, session: MerchantSessionContext, campaign: CampaignDraft
    ) -> StagedChange:
        raise ChangeNotApplicable("Campaigns are not managed in KAELUM.")

    async def get_pending_changes(self, session: MerchantSessionContext) -> list[StagedChange]:
        return self.ledger.pending()

    async def apply_change(self, session: MerchantSessionContext, change_id: str) -> StagedChange:
        if self.ledger.get(change_id) is None:
            raise ChangeNotApplicable(f"No change {change_id}.")
        raise ChangeNotApplicable(APPLY_REFUSAL)

    async def discard_change(
        self,
        session: MerchantSessionContext,
        change_id: str,
        actor_kind: ActorKind = ActorKind.OPERATOR,
    ) -> StagedChange:
        return self.ledger.discard(change_id, actor=session.operator, actor_kind=actor_kind)

    # -- Context ----------------------------------------------------------------------------

    async def get_merchant_context(self, session: MerchantSessionContext) -> dict[str, Any] | None:
        # Read on every turn, so one small discovery call: the count and the discount
        # rate need no full catalogue.
        if self._catalog:
            records, count = list(self._catalog.values()), len(self._catalog)
        else:
            body = await self._client.discover(query="", limit=24)
            records = body.get("products", []) or []
            count = body.get("product_count", len(records))
        rates = {r.get("discount_pct") for r in records if r.get("discount_pct") is not None}
        return {
            "platform": "KAELUM",
            "currency": CURRENCY,
            "default_period": DEFAULT_PERIOD,
            "listings": count,
            "klm_discount_pct": next(iter(rates)) if len(rates) == 1 else sorted(rates),
            "limitations": [
                {"source": "sales", "note": "KLM checkout sessions only; paylinks, QR and transfers are not counted."},
                {"source": "traffic", "note": "KAELUM has no storefront traffic data; conversion is paid / started checkouts."},
                {"source": "stock", "note": "Stock counts live in the merchant's store; KAELUM sees in or out of stock only."},
                {"source": "campaigns", "note": "Ad campaigns are run outside KAELUM and are not visible here."},
                {"source": "changes", "note": "Approved changes are made by the merchant in their own store; KAELUM never writes to it."},
            ],
        }


def _bucket(daily: list[dict[str, Any]], name: str, field: str, granularity: str) -> list[MetricPoint]:
    """Daily rows into day, week (ISO Monday) or month buckets. Sums add up; conversion
    and average order value are recomputed from the summed parts so buckets reconcile."""
    if granularity not in ("week", "month"):
        if name in ("conversion_rate", "average_order_value"):
            return [MetricPoint(date=r["date"], value=_ratio(name, [r])) for r in daily]
        return [MetricPoint(date=r["date"], value=float(r.get(field) or 0)) for r in daily]
    groups: dict[str, list[dict[str, Any]]] = {}
    for r in daily:
        d = date.fromisoformat(r["date"])
        key = (d - timedelta(days=d.weekday())).isoformat() if granularity == "week" else d.replace(day=1).isoformat()
        groups.setdefault(key, []).append(r)
    out = []
    for key, rows in sorted(groups.items()):
        if name in ("conversion_rate", "average_order_value"):
            value = _ratio(name, rows)
        else:
            value = round(sum(float(x.get(field) or 0) for x in rows), 2)
        out.append(MetricPoint(date=key, value=value))
    return out


def _ratio(name: str, rows: list[dict[str, Any]]) -> float:
    sales = sum(float(r.get("sales_gbp") or 0) for r in rows)
    orders = sum(int(r.get("orders") or 0) for r in rows)
    started = sum(int(r.get("checkouts_started") or 0) for r in rows)
    if name == "conversion_rate":
        return round(orders / started, 4) if started else 0.0
    return round(sales / orders, 2) if orders else 0.0


def build_kaelum_merchant_config(brand_name: str = "8T Clothing", model: str | None = None) -> MerchantAgentConfig:
    """Merchant-agent config for a KAELUM merchant. Stock and campaigns are switched off
    because KAELUM does not hold them; listing and price changes stay on as proposals the
    merchant applies in their own store."""
    import os

    model = model or os.environ.get("KAELUM_AGENT_MODEL") or DEFAULT_MODEL
    kwargs: dict[str, Any] = {}
    if model:
        kwargs["model"] = model
    return MerchantAgentConfig(
        brand_name=brand_name,
        assistant_name="the KAELUM merchant assistant",
        brand_voice="plain and specific, numbers first, honest about what KAELUM cannot see",
        enable_inventory=False,
        enable_campaigns=False,
        enable_listing_edits=True,
        enable_pricing=True,
        **kwargs,
    )


__all__ = ["KaelumMerchantBackend", "build_kaelum_merchant_config", "resolve_period"]
