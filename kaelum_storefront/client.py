# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Kaelum Technologies Ltd. Built on the Apache-2.0 reference
# blueprint at github.com/anthropics/commerce-agents (see NOTICE).

"""A thin async client over the two live KAELUM endpoints this storefront needs.

Both endpoints already exist on the live platform and back the KAELUM WebMCP layer:

  kaelumDiscover                 read-only network discovery. Returns live KAELUM
                                 merchants and in-stock catalogue products, each
                                 priced in KLM at the discount checkout applies.
                                 Moves no money, reads no account.

  createUniversalPaymentSession  headless session creator. Takes a merchant Bearer
                                 token and a gross GBP amount, returns a hosted
                                 gateway URL. The customer completes the KLM spend
                                 on kaelum.app, where a human confirms it.

The client only consumes what the platform returns. It never computes or hardcodes a
KLM price: the price and the discount come back from the endpoints, which read them
dynamically server-side.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx


@dataclass(frozen=True)
class KaelumSettings:
    """Deployment settings, read from the environment by config.py.

    functions_base is the KAELUM functions origin. On the live platform the WebMCP
    script calls functions at ``https://kaelum.app/functions/<name>``, so that is the
    default. merchant_token is a KAELUM MerchantToken issued for the one store this
    deployment sells for; it authorises createUniversalPaymentSession. site_key is the
    merchant's discovery key (the Wix site_key or the Integration site_token) used to
    scope discovery to that store where wanted.
    """

    functions_base: str = "https://kaelum.app/functions"
    merchant_token: str | None = None
    site_key: str | None = None
    success_url: str = "https://kaelum.app/checkout/success"
    cancel_url: str = "https://kaelum.app/checkout/cancel"
    request_timeout_s: float = 20.0


class KaelumClientError(RuntimeError):
    """A KAELUM endpoint returned a non-ok response."""


class KaelumClient:
    def __init__(self, settings: KaelumSettings, http: httpx.AsyncClient | None = None) -> None:
        self._s = settings
        self._http = http or httpx.AsyncClient(timeout=settings.request_timeout_s)
        self._owns_http = http is None

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    # -- Discovery -----------------------------------------------------------------

    async def discover(
        self, query: str = "", category: str | None = None, limit: int = 8, offset: int = 0
    ) -> dict[str, Any]:
        """Call kaelumDiscover and return its JSON body.

        The body carries ``klm_price_gbp`` (read dynamically server-side), ``merchants``
        and ``products``. Each product carries title, description, ``rrp_gbp``,
        ``discount_pct``, ``net_gbp``, ``klm_units``, ``canonical_url`` and
        ``kaelum_pay_target``. Discovery has no per-product id or single-product lookup,
        so backend.py derives a stable id from ``canonical_url`` and caches the record.
        """
        params: dict[str, Any] = {"q": query or "", "limit": limit, "offset": offset}
        if category:
            params["category"] = category
        if self._s.site_key:
            # Scope hint for stores that pass a site key; harmless where discovery is
            # network-wide.
            params["siteKey"] = self._s.site_key
        resp = await self._http.get(f"{self._s.functions_base}/kaelumDiscover", params=params)
        body = _json(resp)
        if not body.get("ok", False):
            raise KaelumClientError(f"kaelumDiscover failed: {body.get('message') or resp.status_code}")
        return body

    # -- Checkout hand-off ---------------------------------------------------------

    async def create_payment_session(
        self,
        *,
        order_id: str,
        amount_gbp: float,
        discount_pct: float | None = None,
        customer_email: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a KAELUM payment session and return its JSON body.

        ``amount_gbp`` is the GROSS cart total in GBP. KAELUM applies the merchant
        discount itself (floored at 6 percent), so gross is passed here, never a
        pre-discounted figure, to avoid discounting twice. When ``discount_pct`` is
        supplied (as a fraction, e.g. 0.08) the session applies exactly that rate, which
        keeps the quoted discount and the charged discount equal. The body returns
        ``gateway_url``: the hosted URL the checkout card hands the customer to.
        """
        if not self._s.merchant_token:
            raise KaelumClientError(
                "No KAELUM merchant token configured. Set KAELUM_MERCHANT_TOKEN."
            )
        payload: dict[str, Any] = {
            "order_id": order_id,
            "amount": round(float(amount_gbp), 2),
            "currency": "GBP",
            "success_url": self._s.success_url,
            "cancel_url": self._s.cancel_url,
            "metadata": metadata or {},
        }
        if discount_pct is not None:
            payload["discount_pct"] = discount_pct
        if customer_email:
            payload["customer_email"] = customer_email

        resp = await self._http.post(
            f"{self._s.functions_base}/createUniversalPaymentSession",
            headers={"Authorization": f"Bearer {self._s.merchant_token}"},
            json=payload,
        )
        body = _json(resp)
        if not body.get("ok", False) or not body.get("gateway_url"):
            raise KaelumClientError(
                f"createUniversalPaymentSession failed: {body.get('message') or resp.status_code}"
            )
        return body


def _json(resp: httpx.Response) -> dict[str, Any]:
    try:
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        raise KaelumClientError(f"Non-JSON response ({resp.status_code}) from KAELUM") from exc
    if not isinstance(data, dict):
        raise KaelumClientError("Unexpected KAELUM response shape")
    return data
