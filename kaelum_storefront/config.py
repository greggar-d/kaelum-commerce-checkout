# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Kaelum Technologies Ltd. Built on the Apache-2.0 reference
# blueprint at github.com/anthropics/commerce-agents (see NOTICE).

"""Environment-driven settings and the KAELUM shopping-agent config.

``settings_from_env`` reads the deployment knobs. ``build_kaelum_shopping_config``
returns a ``ShoppingAgentConfig`` for a KAELUM-network pilot: cart and checkout on,
orders and policies and fulfillment off (KAELUM discovery does not serve those yet),
and the id pattern extended so the catalog grounding gate fires on ``klm-XXXXXXXX`` ids.
"""

from __future__ import annotations

import os

from shopping_agent import ShoppingAgentConfig

from .client import KaelumSettings


def settings_from_env() -> KaelumSettings:
    return KaelumSettings(
        functions_base=os.environ.get("KAELUM_FUNCTIONS_BASE", "https://kaelum.app/functions"),
        merchant_token=os.environ.get("KAELUM_MERCHANT_TOKEN") or None,
        site_key=os.environ.get("KAELUM_SITE_KEY") or None,
        success_url=os.environ.get("KAELUM_SUCCESS_URL", "https://kaelum.app/checkout/success"),
        cancel_url=os.environ.get("KAELUM_CANCEL_URL", "https://kaelum.app/checkout/cancel"),
    )


def build_kaelum_shopping_config(brand_name: str = "KAELUM") -> ShoppingAgentConfig:
    return ShoppingAgentConfig(
        brand_name=brand_name,
        assistant_name="the KAELUM shopping assistant",
        brand_voice="confident, plain about the KLM discount, and honest about trade-offs",
        # The KLM discount is a merchant-side discount on goods when a shopper spends
        # KLM; it is never a discount on the KLM price itself. The agent should say so.
        domain_search_notes=(
            "Every result is a KAELUM merchant that accepts KLM, the KAELUM commerce "
            "currency. The listed price is the gross price in pounds; the attributes "
            "carry the KLM units and the merchant discount the shopper receives when "
            "they pay in KLM. Present the KLM price and the saving, never a price change "
            "on KLM itself."
        ),
        enable_cart=True,
        enable_orders=False,
        enable_policies=False,
        enable_fulfillment=False,
        # Register the derived KAELUM id shape so the catalog grounding gate recognises
        # it alongside the blueprint's default patterns.
        product_id_patterns=(
            r"\bklm-[0-9a-f]{8}\b",
            r"\b[A-Z]{2,4}-\d{3,4}\b",
            r"\b[A-Z]{2,4}-[A-Z]{2,6}-\d{2,4}(?:-[A-Z0-9]{2,6})?\b",
        ),
    )
