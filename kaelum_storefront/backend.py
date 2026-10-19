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
  get_disclosure                          -> the KLM terms facts box, from discovery
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

import contextlib
import hashlib
import uuid
from typing import Any

from shopping_agent import (
    Cart,
    CartItem,
    CheckoutHandoff,
    Disclosure,
    DisclosureRow,
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
# Cold lookups page through discovery in this size, up to this many records.
_PAGE = 24
_WARM_CAP = 480


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
        # The KLM unit price discovery last reported (read server-side from
        # TokenPricing). Used only to label the facts box; never hardcoded here.
        self._klm_price_gbp: float | None = None

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
        self._note_price(body)
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
        # One merchant per session. Every line must carry the merchant_ref discovery gave
        # it, and it must be the merchant this deployment sells for. A line with no ref,
        # or another merchant's ref, is refused here rather than settled to the token's
        # account.
        records = [self._catalog.get(i.product_id) or {} for i in cart.items]
        refs = {rec.get("merchant_ref") for rec in records}
        if None in refs:
            raise Unavailable(
                "An item in this cart has no KAELUM merchant reference, so it cannot be "
                "checked out here."
            )
        if len(refs) > 1:
            raise Unavailable(
                "This cart holds items from more than one KAELUM merchant. "
                "Check out each merchant's items separately."
            )
        merchant_ref = next(iter(refs))
        own_ref = await self._client.merchant_ref()
        if own_ref and merchant_ref != own_ref:
            raise Unavailable("These items belong to a different KAELUM merchant.")
        # The discount this merchant's records were quoted at, passed as a fraction so
        # the charged discount equals the quoted one. Absent, the server applies its own.
        discount = _num(records[0].get("discount_pct"))
        discount_fraction = round(discount / 100.0, 4) if discount and discount > 0 else None
        # Unique per hand-off, so a second checkout in one session never reuses an id.
        order_id = f"cca-{session.session_id[:12]}-{uuid.uuid4().hex[:10]}"
        summary = ", ".join(f"{i.quantity} x {i.title}" for i in cart.items)[:280]
        body = await self._client.create_payment_session(
            order_id=order_id,
            amount_gbp=cart.subtotal,
            discount_pct=discount_fraction,
            merchant_ref=merchant_ref,
            metadata={
                "platform": "claude-commerce-agents",
                "initiator": "agent",
                "item_description": summary,
            },
        )
        return [CheckoutHandoff(url=body["gateway_url"], label="Pay with KAELUM")]

    # -- Customer context ----------------------------------------------------------

    async def get_account_context(self, session: ShoppingSessionContext) -> dict[str, Any] | None:
        """KAELUM's terms for KLM, sent with every request so the agent answers questions
        about KLM itself from these facts rather than guessing. The price comes from
        discovery (read server-side from TokenPricing), never from this file."""
        if self._klm_price_gbp is None:
            # The facts below still hold if discovery is unreachable; only the figure is lost.
            with contextlib.suppress(Exception):
                self._note_price(await self._client.discover(query="", limit=1))
        price = f"£{self._klm_price_gbp:.2f}" if self._klm_price_gbp else "a fixed price"
        return {
            "klm_terms": (
                f"KLM is the KAELUM commerce currency, for spending with KAELUM merchants "
                f"and creators. One KLM costs {price}, a fixed price that does not go up "
                "or down. KLM is not an investment, earns no return and is not worth "
                "holding or buying in bulk for gain; never suggest otherwise. Customers "
                "buy KLM at full price; the saving is the merchant's discount on goods "
                "(at least 6%) when they spend KLM. A 4.5% platform fee is added at "
                "checkout. The shopper always confirms payment themselves on kaelum.app."
            )
        }


    async def get_preferences(self, session: ShoppingSessionContext) -> UserPreferences:
        # No account is read for a discovery-and-hand-off pilot; a guest profile keeps
        # the agent's per-turn read cheap.
        return UserPreferences(user_id=session.user_id, default_location="United Kingdom")

    # -- Disclosure: the KLM terms for one product -----------------------------------

    async def get_disclosure(
        self, session: ShoppingSessionContext, product_id: str
    ) -> Disclosure | None:
        """A facts box with the shopper's KLM terms for one product, built only from the
        figures discovery returned for it. Nothing is computed from a hardcoded price or
        fee rate: the fee percentage is derived from the platform's own fee and net
        figures, and the KLM unit price comes from discovery."""
        record = self._catalog.get(product_id)
        if record is None:
            return None
        rows: list[DisclosureRow] = []
        rrp = _num(record.get("rrp_gbp"))
        disc = _num(record.get("discount_pct"))
        net = _num(record.get("net_gbp"))
        fee = _num(record.get("fee_gbp"))
        total = _num(record.get("total_gbp"))
        klm = _num(record.get("klm_units"))
        if rrp is not None:
            rows.append(DisclosureRow(label="Price", value=f"£{rrp:.2f}"))
        if disc is not None:
            rows.append(
                DisclosureRow(
                    label="KLM discount",
                    value=f"{disc:g}%",
                    note="A merchant discount on these goods when you pay in KLM.",
                )
            )
        if net is not None:
            rows.append(DisclosureRow(label="Price after discount", value=f"£{net:.2f}"))
        if fee is not None:
            fee_note = "Paid by the buyer on top of the discounted price."
            if net:
                rows.append(
                    DisclosureRow(
                        label="KAELUM platform fee",
                        value=f"£{fee:.2f} ({fee / net * 100:.1f}%)",
                        note=fee_note,
                    )
                )
            else:
                rows.append(DisclosureRow(label="KAELUM platform fee", value=f"£{fee:.2f}", note=fee_note))
        if total is not None:
            rows.append(DisclosureRow(label="Total to pay", value=f"£{total:.2f}"))
        if klm is not None:
            rows.append(DisclosureRow(label="KLM to spend", value=f"{klm:g} KLM"))
        if self._klm_price_gbp:
            rows.append(
                DisclosureRow(
                    label="KLM price", value=f"£{self._klm_price_gbp:.2f} per KLM", note="Fixed."
                )
            )
        if not rows:
            return None
        return Disclosure(
            title="Paying with KLM",
            product_id=product_id,
            rows=rows,
            sources=[str(record["canonical_url"])] if record.get("canonical_url") else [],
            footnotes=[
                "The discount applies to the goods, never to the price of KLM.",
                (
                    "KLM is the KAELUM commerce currency for spending with KAELUM merchants. "
                    "It is not an investment and its price does not change."
                ),
                (
                    "You confirm the payment yourself on kaelum.app; the assistant cannot "
                    "pay for you."
                ),
            ],
        )

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
        """Page through the whole of discovery (to a cap) so a cold id resolves even when
        its product sits past the first page."""
        offset = 0
        while offset < _WARM_CAP:
            body = await self._client.discover(query="", limit=_PAGE, offset=offset)
            self._note_price(body)
            page = body.get("products", []) or []
            for rec in page:
                self._to_product(rec)
            total = body.get("product_count")
            offset += len(page)
            if len(page) < _PAGE or (isinstance(total, int) and offset >= total):
                break

    def _note_price(self, body: dict[str, Any]) -> None:
        price = _num(body.get("klm_price_gbp"))
        if price and price > 0:
            self._klm_price_gbp = price

    def _to_product(self, record: dict[str, Any]) -> Product | None:
        if not isinstance(record, dict):
            return None
        pid = _product_id_for(record)
        self._catalog[pid] = record

        disc_pct = record.get("discount_pct")

        attributes: dict[str, str] = {"pay_in": "KLM"}
        for key in ("rrp_gbp", "net_gbp", "fee_gbp", "total_gbp", "klm_units", "discount_pct"):
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


def _num(val: Any) -> float | None:
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        return None
    return float(val)


def _gross_gbp(record: dict[str, Any]) -> float:
    """The gross GBP price. KAELUM applies the merchant discount at the session, so the
    cart must hold gross. Use rrp when present; otherwise gross the net price back up by
    the record's own discount, so a net-only record is never discounted twice."""
    rrp = _num(record.get("rrp_gbp"))
    if rrp and rrp > 0:
        return round(rrp, 2)
    net = _num(record.get("net_gbp"))
    if net and net > 0:
        disc = _num(record.get("discount_pct")) or 0.0
        if 0 < disc < 100:
            return round(net / (1 - disc / 100.0), 2)
        return round(net, 2)
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
