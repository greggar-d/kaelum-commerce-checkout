# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Kaelum Technologies Ltd. Built on the Apache-2.0 reference
# blueprint at github.com/anthropics/commerce-agents (see NOTICE).

"""KaelumStorefrontBackend: the blueprint's one integration surface, implemented over
KAELUM.

The shopping agent from anthropics/commerce-agents reaches a store through a single
``StorefrontBackend``. This class implements that surface so a Claude shopping agent
can browse the KAELUM acceptance network as its catalogue and hand off checkout to
KAELUM, where the customer completes the spend in KLM.

Scope of this pilot backend (the blueprint's sanctioned minimal shape, "implement
search and product details and stub the rest"):

  search_products / get_product_details   -> kaelumDiscover
  get_cart / add / update / remove        -> in-process cart, one per session
  checkout_handoff                        -> createUniversalPaymentSession
  orders / policies / fulfillment         -> switched off in config; the methods are
                                             implemented as empty so the class is
                                             concrete, but their tools are removed and
                                             they are never reached.

Currency is GBP throughout. KAELUM settles in GBP and prices in KLM; a cart in any
other currency is refused at hand-off rather than converted silently.

The catalogue price shown to the shopper is the gross GBP price (``rrp_gbp``). The KLM
figures (units, net price, discount) travel in the product's attributes and labels so
the agent can tell the shopper what they pay in KLM and what they save, while the
authoritative charge stays with the KAELUM session at checkout.
"""

from __future__ import annotations

import hashlib
from typing import Any

from shopping_agent import (
    Cart,
    CartItem,
    CheckoutHandoff,
    FulfillmentOption,
    Order,
    Policy,
    Product,
    ProductDetails,
    SearchFilters,
    ShoppingSessionContext,
    StorefrontBackend,
    Unavailable,
    UserPreferences,
)

from .client import KaelumClient

CURRENCY = "GBP"


def _product_id_for(record: dict[str, Any]) -> str:
    """A stable id for a discovered product. Discovery exposes no id, so we derive one
    from the canonical URL (or the title as a fallback). The shape ``klm-XXXXXXXX``
    matches the id pattern registered in config.py so the catalog grounding gate fires
    on it."""
    seed = str(record.get("canonical_url") or record.get("title") or "").strip().lower()
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:8]
    return f"klm-{digest}"


class KaelumStorefrontBackend(StorefrontBackend):
    def __init__(self, client: KaelumClient) -> None:
        self._client = client
        # Provenance-friendly caches. The discovered record is kept so hand-off and
        # details can reach canonical_url, the KLM figures and the merchant discount.
        self._catalog: dict[str, dict[str, Any]] = {}
        self._carts: dict[str, list[CartItem]] = {}
        # The merchant discount fraction last seen in discovery. Passed to the session
        # so the charged discount equals the quoted one. None means let the server apply
        # its own configured rate.
        self._merchant_discount_fraction: float | None = None

    # -- Catalog: KAELUM discovery -------------------------------------------------

    async def search_products(
        self,
        session: ShoppingSessionContext,
        query: str,
        filters: SearchFilters | None = None,
        limit: int = 8,
    ) -> list[Product]:
        category = filters.category if filters else None
        body = await self._client.discover(query=query, category=category, limit=limit)
        products = [self._to_product(rec) for rec in body.get("products", [])]
        products = [p for p in products if p is not None]
        if filters:
            products = _apply_filters(products, filters)
        return products[:limit]

    async def get_product_details(
        self, session: ShoppingSessionContext, product_id: str
    ) -> ProductDetails | None:
        record = self._catalog.get(product_id)
        if record is None:
            # Cold lookup: repopulate from a broad discovery pass, then resolve. A real
            # integration would add a single-product read; discovery has none today.
            await self._warm_catalog()
            record = self._catalog.get(product_id)
        if record is None:
            return None
        base = self._to_product(record)
        if base is None:
            return None
        return ProductDetails(
            **base.model_dump(),
            long_description=str(record.get("description") or "") or None,
        )

    # -- Cart: in-process, one per session -----------------------------------------

    async def get_cart(self, session: ShoppingSessionContext) -> Cart:
        return Cart(items=list(self._carts.get(session.session_id, [])), currency=CURRENCY)

    async def add_to_cart(
        self, session: ShoppingSessionContext, product_id: str, quantity: int
    ) -> Cart:
        record = self._catalog.get(product_id)
        if record is None:
            raise Unavailable(f"{product_id} is not a product from this session's results.")
        if record.get("availability") not in (None, "IN_STOCK"):
            raise Unavailable(f"{product_id} is not available to buy right now.")
        items = self._carts.setdefault(session.session_id, [])
        for item in items:
            if item.product_id == product_id:
                item.quantity += quantity
                break
        else:
            items.append(
                CartItem(
                    product_id=product_id,
                    title=str(record.get("title") or "KAELUM product"),
                    # Gross GBP: KAELUM applies the discount at the session, so the cart
                    # holds gross to avoid discounting twice.
                    price=_gross_gbp(record),
                    quantity=quantity,
                    image_url=record.get("image_url"),
                )
            )
        return await self.get_cart(session)

    async def update_cart_item(
        self, session: ShoppingSessionContext, product_id: str, quantity: int
    ) -> Cart:
        items = self._carts.get(session.session_id, [])
        for item in items:
            if item.product_id == product_id:
                item.quantity = quantity
                break
        return await self.get_cart(session)

    async def remove_from_cart(self, session: ShoppingSessionContext, product_id: str) -> Cart:
        items = self._carts.get(session.session_id, [])
        self._carts[session.session_id] = [i for i in items if i.product_id != product_id]
        return await self.get_cart(session)

    # -- Checkout hand-off: KAELUM payment session ---------------------------------

    async def checkout_handoff(
        self, session: ShoppingSessionContext, cart: Cart
    ) -> list[CheckoutHandoff]:
        """Stage the cart as a KAELUM payment session and return the gateway URL.

        This is the whole money seam. The blueprint's executor calls this after the
        model's ``checkout`` tool call, pins the returned URL onto the checkout card,
        and the customer completes the KLM spend on kaelum.app. The model never sees the
        URL.
        """
        if not cart.items:
            return []
        if cart.currency != CURRENCY:
            raise Unavailable(
                f"KAELUM settles in {CURRENCY}; this cart is in {cart.currency}."
            )
        order_id = f"cca-{session.session_id[:12]}-{cart.item_count}"
        summary = ", ".join(f"{i.quantity} x {i.title}" for i in cart.items)[:280]
        body = await self._client.create_payment_session(
            order_id=order_id,
            amount_gbp=cart.subtotal,
            discount_pct=self._merchant_discount_fraction,
            metadata={
                "platform": "claude-commerce-agents",
                "initiator": "agent",
                "item_description": summary,
            },
        )
        return [CheckoutHandoff(url=body["gateway_url"], label="Pay with KAELUM")]

    # -- Customer context ----------------------------------------------------------

    async def get_preferences(self, session: ShoppingSessionContext) -> UserPreferences:
        # No account is read for a discovery-and-hand-off pilot; a guest profile keeps
        # the agent's per-turn read cheap.
        return UserPreferences(user_id=session.user_id, default_location="United Kingdom")

    # -- Switched-off systems ------------------------------------------------------
    # These are disabled in config (enable_orders / enable_policies / enable_fulfillment
    # are False), which removes their tools on every path. The methods stay implemented
    # so the class is concrete; they are never reached.

    async def get_orders(self, session: ShoppingSessionContext, limit: int = 5) -> list[Order]:
        return []

    async def get_order(self, session: ShoppingSessionContext, order_id: str) -> Order | None:
        return None

    async def search_policies(self, session: ShoppingSessionContext, query: str) -> list[Policy]:
        return []

    async def get_fulfillment_options(
        self, session: ShoppingSessionContext, product_ids: list[str]
    ) -> list[FulfillmentOption]:
        return []

    # -- Internals -----------------------------------------------------------------

    async def _warm_catalog(self) -> None:
        body = await self._client.discover(query="", limit=24)
        for rec in body.get("products", []):
            self._to_product(rec)

    def _to_product(self, record: dict[str, Any]) -> Product | None:
        if not isinstance(record, dict):
            return None
        pid = _product_id_for(record)
        self._catalog[pid] = record

        disc_pct = record.get("discount_pct")
        if isinstance(disc_pct, (int, float)) and disc_pct > 0:
            # Discovery reports the merchant rate as a percent; store it as the fraction
            # the session expects, so quote and charge line up. A single-merchant pilot
            # sells one store's goods, so the last seen rate is that store's.
            self._merchant_discount_fraction = round(float(disc_pct) / 100.0, 4)

        attributes: dict[str, str] = {"pay_in": "KLM"}
        for key in ("rrp_gbp", "net_gbp", "klm_units", "discount_pct"):
            val = record.get(key)
            if val is not None:
                attributes[key] = str(val)
        pay_target = record.get("kaelum_pay_target")
        if pay_target:
            attributes["kaelum_pay_target"] = str(pay_target)

        labels: list[str] = ["Pay with KLM"]
        if isinstance(disc_pct, (int, float)) and disc_pct > 0:
            labels.append(f"{round(float(disc_pct), 1)}% off with KLM")

        return Product(
            product_id=pid,
            title=str(record.get("title") or "KAELUM product"),
            price=_gross_gbp(record),
            currency=CURRENCY,
            image_url=record.get("image_url"),
            category=record.get("category_id"),
            short_description=(str(record.get("description") or "")[:280] or None),
            in_stock=record.get("availability") in (None, "IN_STOCK"),
            labels=labels,
            attributes=attributes,
        )


def _gross_gbp(record: dict[str, Any]) -> float:
    """The gross GBP price: rrp when present, else the net grossed back up is not
    attempted; net is used only as a last resort so a record without rrp is still
    buyable. KAELUM applies the discount at the session either way."""
    rrp = record.get("rrp_gbp")
    if isinstance(rrp, (int, float)) and rrp > 0:
        return round(float(rrp), 2)
    net = record.get("net_gbp")
    if isinstance(net, (int, float)) and net > 0:
        return round(float(net), 2)
    return 0.0


def _apply_filters(products: list[Product], filters: SearchFilters) -> list[Product]:
    out = products
    if filters.min_price is not None:
        out = [p for p in out if p.price >= filters.min_price]
    if filters.max_price is not None:
        out = [p for p in out if p.price <= filters.max_price]
    if filters.sort == "price_asc":
        out = sorted(out, key=lambda p: p.price)
    elif filters.sort == "price_desc":
        out = sorted(out, key=lambda p: p.price, reverse=True)
    return out
