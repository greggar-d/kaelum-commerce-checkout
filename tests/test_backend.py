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
        }
    ],
}


class FakeClient:
    """Duck-typed stand-in for KaelumClient."""

    def __init__(self) -> None:
        self.session_calls: list[dict[str, Any]] = []

    async def discover(self, query: str = "", category=None, limit: int = 8, offset: int = 0):
        return DISCOVER_BODY

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
    # KLM figures travel in attributes for the agent to present.
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
