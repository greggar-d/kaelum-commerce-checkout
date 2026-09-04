# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Kaelum Technologies Ltd.

"""Check the KAELUM seam without a model or an API key.

Runs discovery, maps the results into blueprint Product objects, drops one into a cart,
and (if a merchant token is set) creates a real payment session and prints the gateway
URL. Read-only unless KAELUM_MERCHANT_TOKEN is present; discovery moves no money.

    python scripts/smoke_kaelum.py "shirt"
"""

from __future__ import annotations

import asyncio
import sys

from shopping_agent import ShoppingSessionContext

from kaelum_storefront import KaelumClient, KaelumStorefrontBackend, settings_from_env


async def main(query: str) -> None:
    settings = settings_from_env()
    client = KaelumClient(settings)
    backend = KaelumStorefrontBackend(client)
    session = ShoppingSessionContext(session_id="smoke", user_id="guest")

    print(f"Functions base: {settings.functions_base}")
    print(f"Merchant token set: {'yes' if settings.merchant_token else 'no'}\n")

    products = await backend.search_products(session, query, limit=8)
    print(f"Discovered {len(products)} product(s) for {query!r}:")
    for p in products:
        klm = p.attributes.get("klm_units", "?")
        disc = p.attributes.get("discount_pct", "?")
        print(f"  {p.product_id}  {p.title[:48]:<48}  gross £{p.price:<8}  {klm} KLM  {disc}% off")

    if not products:
        print("\nNo live products returned. The network may be in controlled onboarding.")
        await client.aclose()
        return

    await backend.add_to_cart(session, products[0].product_id, 1)
    cart = await backend.get_cart(session)
    print(f"\nCart: {cart.item_count} item(s), gross subtotal £{cart.subtotal} {cart.currency}")

    if settings.merchant_token:
        handoffs = await backend.checkout_handoff(session, cart)
        for h in handoffs:
            print(f"\nCheckout hand-off -> {h.label}: {h.url}")
    else:
        print("\nSet KAELUM_MERCHANT_TOKEN to exercise the checkout hand-off (session create).")

    await client.aclose()


if __name__ == "__main__":
    asyncio.run(main(" ".join(sys.argv[1:]) or "shirt"))
