# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Kaelum Technologies Ltd. Built on the Apache-2.0 reference
# blueprint at github.com/anthropics/commerce-agents (see NOTICE).

"""Environment-driven settings and the KAELUM shopping-agent config.

``settings_from_env`` reads the deployment knobs. ``build_kaelum_shopping_config``
returns a ``ShoppingAgentConfig`` for a KAELUM-network pilot: cart, checkout and the
KLM terms facts box (disclosures) on,
orders and policies and fulfillment off (KAELUM discovery does not serve those yet),
and the id pattern extended so the catalog grounding gate fires on ``klm-XXXXXXXX`` ids.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv
from shopping_agent import ShoppingAgentConfig

from .client import KaelumSettings

# Claude Sonnet 5.5: the standard tier for KAELUM agents. Override with KAELUM_AGENT_MODEL.
DEFAULT_MODEL = "claude-sonnet-5-5"


def _clean_token(raw: str | None) -> str | None:
    """Strip stray spaces and quotes, and catch the usual paste mix-ups early with a plain
    message instead of a 401 from KAELUM."""
    token = (raw or "").strip().strip("'\"").strip()
    if not token:
        return None
    if not token.startswith("mk_"):
        hint = {
            "kmr_": "that is the merchant reference (KAELUM_MERCHANT_REF), not the token",
            "wix_": "that is the Wix site key (KAELUM_SITE_KEY), not the token",
            "shop_": "that is a Shopify site token (KAELUM_SITE_KEY), not the token",
        }
        why = next((v for k, v in hint.items() if token.startswith(k)), None)
        raise ValueError(
            "KAELUM_MERCHANT_TOKEN does not look like a merchant token "
            "(they start with mk_live_). "
            + (f"It starts {token[:5]!r}: {why}. " if why else "")
            + "Copy the token from Generate Merchant Token in the KAELUM merchant dashboard."
        )
    return token


def settings_from_env() -> KaelumSettings:
    # Load a local .env if present. Searches the working directory and its parents,
    # so it works whether the script is run from the repo root or a subfolder. Values
    # already set in the environment win, so a shell export still overrides .env.
    load_dotenv()
    return KaelumSettings(
        functions_base=os.environ.get("KAELUM_FUNCTIONS_BASE", "https://kaelum.app/functions"),
        merchant_token=_clean_token(os.environ.get("KAELUM_MERCHANT_TOKEN")),
        site_key=os.environ.get("KAELUM_SITE_KEY") or None,
        merchant_ref=os.environ.get("KAELUM_MERCHANT_REF") or None,
        success_url=os.environ.get("KAELUM_SUCCESS_URL", "https://kaelum.app/checkout/success"),
        cancel_url=os.environ.get("KAELUM_CANCEL_URL", "https://kaelum.app/checkout/cancel"),
    )


def build_kaelum_shopping_config(
    brand_name: str = "KAELUM", model: str | None = None
) -> ShoppingAgentConfig:
    model = model or os.environ.get("KAELUM_AGENT_MODEL") or DEFAULT_MODEL
    extra = {"model": model} if model else {}
    return ShoppingAgentConfig(
        **extra,
        brand_name=brand_name,
        # No shopper memory in this pilot: nothing about a guest is kept between sessions.
        enable_memory=False,
        assistant_name="the KAELUM shopping assistant",
        brand_voice="confident, plain about the KLM discount, and honest about trade-offs",
        # The KLM discount is a merchant-side discount on goods when a shopper spends
        # KLM; it is never a discount on the KLM price itself. The agent should say so.
        domain_search_notes=(
            "Every result is a KAELUM merchant that accepts KLM, the KAELUM commerce "
            "currency. The listed price is the gross price in pounds; the attributes "
            "carry the KLM units and the merchant discount the shopper receives when "
            "they pay in KLM. Present the KLM price and the saving, never a price change "
            "on KLM itself. When a shopper is close to buying, show the KLM terms for "
            "the product with present_disclosure so they see the fee and the total "
            "before they pay."
        ),
        enable_disclosures=True,
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
