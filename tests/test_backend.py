# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Kaelum Technologies Ltd.

"""Backend behaviour, with a fake KAELUM client so the suite needs no network or key."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from shopping_agent import ShoppingSessionContext, Unavailable
from kaelum_storefront import KaelumStorefrontBackend

DISCOVER_BODY: dict[str, Any] = {
    "ok": True,
    "klm_price_gbp": 0.2,
    "merchants": [{"name": "8T Clothing", "platform": "wix", "discount_pct": 8.0}],
    "products": [
        {
            "title": "8T Signature Tee",
            "description": "Heavyweight expressive tee.",
            "category_id": "apparel",
            "rrp_gbp": 30.0,
            "discount_pct": 8.0,
            "net_gbp": 27.6,
            "klm_units": 138.0,
            "canonical_url": "https://8tclothing.com/p/signature-tee",
            "kaelum_pay_target": "https://8tclothing.com/p/signature-tee",
            "availability": "IN_STOCK",
            "seller_verified": True,
            "merchant_ref": "kmr_8t",
        }
    ],
}


class FakeClient:
    """Duck-typed stand-in for KaelumClient."""

    def __init__(self) -> None:
        self.session_calls: list[dict[str, Any]] = []

    async def discover(self, query: str = "", category=None, limit: int = 8, offset: int = 0):
        return DISCOVER_BODY

    async def merchant_ref(self):
        return "kmr_8t"

    async def create_payment_session(self, **kwargs):
        self.session_calls.append(kwargs)
        return {"ok": True, "gateway_url": "https://kaelum.app/gateway-checkout?session=klm_sess_test"}

    async def aclose(self):
        pass


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def session():
    return ShoppingSessionContext(session_id="t1", user_id="guest")


def test_discovery_maps_to_gross_priced_products(session):
    backend = KaelumStorefrontBackend(FakeClient())
    products = _run(backend.search_products(session, "tee"))
    assert len(products) == 1
    p = products[0]
    # Gross price is used, not the discounted net (the session applies the discount).
    assert p.price == 30.0
    assert p.currency == "GBP"
    # KAE figures travel in attributes for the agent to present.
    assert p.attributes["klm_units"] == "138.0"
    assert p.attributes["discount_pct"] == "8.0"
    assert p.product_id.startswith("klm-")


def test_add_to_cart_holds_gross_and_checkout_passes_discount(session):
    fake = FakeClient()
    backend = KaelumStorefrontBackend(fake)
    products = _run(backend.search_products(session, "tee"))
    _run(backend.add_to_cart(session, products[0].product_id, 2))
    cart = _run(backend.get_cart(session))
    assert cart.currency == "GBP"
    assert cart.subtotal == 60.0  # 2 x gross 30.00

    handoffs = _run(backend.checkout_handoff(session, cart))
    assert len(handoffs) == 1
    assert handoffs[0].url.endswith("session=klm_sess_test")
    assert handoffs[0].label == "Pay with KAELUM"

    # The session was created with the GROSS amount and the merchant discount as a
    # fraction, so the charged discount equals the quoted one.
    call = fake.session_calls[0]
    assert call["amount_gbp"] == 60.0
    assert call["discount_pct"] == 0.08
    assert call["metadata"]["initiator"] == "agent"
    # The cart's merchant is named, so KAELUM can refuse a token for another merchant.
    assert call["merchant_ref"] == "kmr_8t"


def test_non_gbp_cart_is_refused(session):
    from shopping_agent import Cart, CartItem

    backend = KaelumStorefrontBackend(FakeClient())
    usd_cart = Cart(items=[CartItem(product_id="x", title="X", price=10.0, quantity=1)], currency="USD")
    with pytest.raises(Unavailable):
        _run(backend.checkout_handoff(session, usd_cart))


def test_unknown_product_add_refused(session):
    backend = KaelumStorefrontBackend(FakeClient())
    with pytest.raises(Unavailable):
        _run(backend.add_to_cart(session, "klm-deadbeef", 1))


def test_mixed_merchant_cart_is_refused(session):
    from shopping_agent import Cart, CartItem

    backend = KaelumStorefrontBackend(FakeClient())
    backend._catalog["klm-a"] = {"merchant_ref": "kmr_8t"}
    backend._catalog["klm-b"] = {"merchant_ref": "kmr_other"}
    cart = Cart(
        items=[
            CartItem(product_id="klm-a", title="A", price=10.0, quantity=1),
            CartItem(product_id="klm-b", title="B", price=10.0, quantity=1),
        ],
        currency="GBP",
    )
    with pytest.raises(Unavailable):
        _run(backend.checkout_handoff(session, cart))


# -- Real client against a mock transport: identity flows end to end ---------------

import httpx  # noqa: E402

from kaelum_storefront.client import KaelumClient, KaelumClientError, KaelumSettings  # noqa: E402


def _client(handler, **settings):
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return KaelumClient(KaelumSettings(functions_base="https://k/functions", **settings), http=http)


def test_client_resolves_ref_once_and_scopes_discovery():
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if req.url.path.endswith("/kaelumWhoAmI"):
            assert req.headers["authorization"] == "Bearer tok"
            return httpx.Response(200, json={"ok": True, "merchant_ref": "kmr_8t", "stores": []})
        if req.url.path.endswith("/kaelumDiscover"):
            return httpx.Response(200, json={"ok": True, "products": [], "merchants": []})
        if req.url.path.endswith("/createUniversalPaymentSession"):
            import json
            assert json.loads(req.content)["merchant_ref"] == "kmr_8t"
            return httpx.Response(200, json={"ok": True, "gateway_url": "https://kaelum.app/gateway-checkout?session=s"})
        return httpx.Response(404, json={"ok": False})

    async def go():
        c = _client(handler, merchant_token="tok", site_key="wix_ignored")
        await c.discover("tee")
        await c.discover("hat")
        await c.create_payment_session(order_id="o", amount_gbp=10.0)
        await c.aclose()

    _run(go())
    whoami_calls = [r for r in seen if r.url.path.endswith("/kaelumWhoAmI")]
    assert len(whoami_calls) == 1  # resolved once, then cached
    discover = [r for r in seen if r.url.path.endswith("/kaelumDiscover")]
    assert all(r.url.params["merchant_ref"] == "kmr_8t" for r in discover)
    assert all("siteKey" not in r.url.params for r in discover)  # ref wins over site key


def test_client_without_token_uses_site_key_and_never_calls_whoami():
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json={"ok": True, "products": [], "merchants": []})

    async def go():
        c = _client(handler, site_key="wix_8t")
        await c.discover("tee")
        await c.aclose()

    _run(go())
    assert [r.url.path.rsplit("/", 1)[-1] for r in seen] == ["kaelumDiscover"]
    assert seen[0].url.params["siteKey"] == "wix_8t"


def test_merchant_mismatch_surfaces_as_client_error():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"ok": False, "error": "MERCHANT_MISMATCH", "message": "different merchant"})

    async def go():
        c = _client(handler, merchant_token="tok", merchant_ref="kmr_other")
        try:
            await c.create_payment_session(order_id="o", amount_gbp=10.0)
        finally:
            await c.aclose()

    with pytest.raises(KaelumClientError):
        _run(go())


# -- October 2026 hardening ----------------------------------------------------------


def test_order_ids_are_unique_per_handoff(session):
    fake = FakeClient()
    backend = KaelumStorefrontBackend(fake)
    products = _run(backend.search_products(session, "tee"))
    _run(backend.add_to_cart(session, products[0].product_id, 1))
    cart = _run(backend.get_cart(session))
    _run(backend.checkout_handoff(session, cart))
    _run(backend.checkout_handoff(session, cart))
    ids = [c["order_id"] for c in fake.session_calls]
    assert len(set(ids)) == 2


def test_item_without_merchant_ref_is_refused(session):
    from shopping_agent import Cart, CartItem

    backend = KaelumStorefrontBackend(FakeClient())
    backend._catalog["klm-n"] = {"title": "No ref"}
    cart = Cart(items=[CartItem(product_id="klm-n", title="N", price=10.0, quantity=1)], currency="GBP")
    with pytest.raises(Unavailable):
        _run(backend.checkout_handoff(session, cart))


def test_other_merchants_items_are_refused(session):
    from shopping_agent import Cart, CartItem

    backend = KaelumStorefrontBackend(FakeClient())  # sells for kmr_8t
    backend._catalog["klm-o"] = {"merchant_ref": "kmr_other"}
    cart = Cart(items=[CartItem(product_id="klm-o", title="O", price=10.0, quantity=1)], currency="GBP")
    with pytest.raises(Unavailable):
        _run(backend.checkout_handoff(session, cart))


def test_net_only_record_is_grossed_up_not_discounted_twice():
    from kaelum_storefront.backend import _gross_gbp

    assert _gross_gbp({"net_gbp": 28.7, "discount_pct": 18}) == 35.0
    assert _gross_gbp({"rrp_gbp": 35, "net_gbp": 28.7, "discount_pct": 18}) == 35.0


def test_cold_lookup_pages_past_the_first_page(session):
    def rec(i):
        return {
            "title": f"Tee {i}",
            "rrp_gbp": 35,
            "discount_pct": 18,
            "canonical_url": f"https://8tclothing.com/p/{i}",
            "availability": "IN_STOCK",
            "merchant_ref": "kmr_8t",
        }

    catalogue = [rec(i) for i in range(65)]

    class PagingClient(FakeClient):
        async def discover(self, query="", category=None, limit=8, offset=0):
            return {"ok": True, "product_count": 65, "products": catalogue[offset : offset + limit]}

    from kaelum_storefront.backend import _product_id_for

    backend = KaelumStorefrontBackend(PagingClient())
    target = _product_id_for(catalogue[60])
    details = _run(backend.get_product_details(session, target))
    assert details is not None and details.title == "Tee 60"


def test_disclosure_uses_live_figures(session):
    body = dict(DISCOVER_BODY)
    body["products"] = [dict(DISCOVER_BODY["products"][0], fee_gbp=1.24, total_gbp=28.84)]

    class Client(FakeClient):
        async def discover(self, **kw):
            return body

    backend = KaelumStorefrontBackend(Client())
    products = _run(backend.search_products(session, "tee"))
    d = _run(backend.get_disclosure(session, products[0].product_id))
    assert d is not None
    rows = {r.label: r.value for r in d.rows}
    assert rows["Price"] == "£30.00"
    assert rows["KAE discount"] == "8%"
    assert rows["Total to pay"] == "£28.84"
    assert rows["KAE price"] == "£0.20 per KAE"  # from discovery, not hardcoded
    assert "4.5%" in rows["KAELUM platform fee"]
    assert _run(backend.get_disclosure(session, "klm-unknown0")) is None


def test_token_cleaning_and_mixups():
    from kaelum_storefront.config import _clean_token

    assert _clean_token(None) is None
    assert _clean_token("  ") is None
    assert _clean_token(' "mk_live_abc" ') == "mk_live_abc"
    for wrong in ("kmr_123", "wix_abc", "shop_abc", "6a9a7b2e00deaaa06316222a"):
        with pytest.raises(ValueError):
            _clean_token(wrong)


def test_account_context_carries_fixed_klm_terms(session):
    backend = KaelumStorefrontBackend(FakeClient())
    ctx = _run(backend.get_account_context(session))
    terms = ctx["klm_terms"]
    assert "£0.20" in terms  # read from discovery, not hardcoded
    assert "not an investment" in terms and "does not go up" in terms
    assert "discount_pct" in terms  # quote the product's own discount, not the 6% floor


def test_rate_limited_request_is_retried(monkeypatch):
    import kaelum_storefront.client as client_mod

    monkeypatch.setattr(client_mod, "RETRY_DELAYS_S", (0.0, 0.0))
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(500, json={"ok": False, "message": "Insights are temporarily unavailable (Rate limit exceeded)"})
        return httpx.Response(200, json={"ok": True, "products": [], "merchants": []})

    async def go():
        c = _client(handler, site_key="wix_8t")
        body = await c.discover("tee")
        await c.aclose()
        return body

    assert _run(go())["ok"] is True and calls["n"] == 2
