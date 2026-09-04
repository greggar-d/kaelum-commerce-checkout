# KAELUM Commerce Checkout

A KAELUM storefront backend for Anthropic's [commerce-agents](https://github.com/anthropics/commerce-agents) shopping agent.

The blueprint builds a Claude shopping agent and deliberately stops at the money: its checkout renders the cart for the host to complete, and it never charges a card. This repository fills that gap. It implements the blueprint's one integration surface, `StorefrontBackend`, over KAELUM, so a Claude shopping agent can browse the KAELUM acceptance network as its catalogue and hand checkout off to KAELUM, where the customer completes the spend in KLM, the KAELUM commerce currency.

It follows the same pattern as Shopify's [claude-for-commerce-examples](https://github.com/Shopify/claude-for-commerce-examples): the blueprint packages install unchanged from a pinned commit, and this repository adds a storefront on top. It does not modify the blueprint.

## The seam

Two live KAELUM endpoints do the work. Both already back the KAELUM WebMCP layer.

| Blueprint surface | KAELUM endpoint | What it does |
| --- | --- | --- |
| `search_products`, `get_product_details` | `kaelumDiscover` | Read-only network discovery. Returns live KAELUM merchants and in-stock products, each priced in KLM at the discount checkout applies. Moves no money, reads no account. |
| `checkout_handoff` | `createUniversalPaymentSession` | Takes a merchant Bearer token and a gross GBP amount, returns a hosted gateway URL. The customer completes the KLM spend on kaelum.app, where a human confirms it. |

The blueprint calls `checkout_handoff` after the model's `checkout` tool call and pins the returned URL onto the checkout card. The model never sees or supplies the URL, which matches KAELUM's own posture: an agent may stage the order, but a human confirms the spend on kaelum.app.

## Money maths, so it reconciles

The catalogue shows the gross GBP price. KAELUM applies the merchant discount itself, floored at 6 percent, when the session is created, so this backend passes the gross amount to the session and never a pre-discounted figure. Passing a net figure would discount twice. The KLM units, net price and discount for each product travel in the product's attributes, so the agent can tell the shopper what they pay in KLM and what they save while the authoritative charge stays with the KAELUM session. The backend also passes the merchant's discount to the session explicitly, so the discount quoted in discovery equals the discount charged at checkout.

## Install

Python 3.11+ and a local clone of the blueprint for its shipped skills.

```
git clone https://github.com/anthropics/commerce-agents.git   # sibling clone, for skills
git clone <this-repo> kaelum-commerce-checkout && cd kaelum-commerce-checkout
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt      # blueprint packages install from the pinned commit
pip install -e .
cp .env.example .env                  # fill in the values below
```

## Configure

Set these in `.env`:

- `KAELUM_MERCHANT_TOKEN`: a KAELUM MerchantToken issued for the store this deployment sells for. Required for the checkout hand-off. Discovery works without it.
- `KAELUM_SITE_KEY`: optional discovery scope hint (the store's Wix site_key or Integration site_token).
- `KAELUM_FUNCTIONS_BASE`: defaults to `https://kaelum.app/functions`.
- `ANTHROPIC_API_KEY`: only needed to run a live model turn, not for discovery.

## Run

Check the KAELUM seam with no model and no API key. This runs discovery, maps the results, drops one product into a cart, and, if a merchant token is set, creates a real payment session and prints the gateway URL:

```
python scripts/smoke_kaelum.py "shirt"
```

Run a live agent turn (needs `ANTHROPIC_API_KEY` and the blueprint skills):

```
python app/main.py "show me KAELUM merchants with tees under 40 pounds"
```

Run the tests (no network, no key; a fake client stands in for KAELUM):

```
pytest -q
```

## Scope of this pilot

This is the blueprint's sanctioned minimal shape: implement search and product details, and switch off what the store does not serve. Cart and checkout are on. Orders, policies and fulfillment are off in `build_kaelum_shopping_config`, because KAELUM discovery does not serve those today; their tools are removed on every path.

## Known seams to close before production

These are deliberate scaffold simplifications, not blockers for a demo. They are the honest list of what a production integration would tidy.

1. **Two merchant identity systems.** The session creator authenticates with a `MerchantToken`; discovery resolves a store by `site_key` or `site_token`. This backend uses a `MerchantToken` for settlement and an optional `site_key` for discovery. Unifying them, so discovery and payment resolve the same record from one identifier, is a small platform task.
2. **Single-merchant scope.** One deployment sells for one store (one `MerchantToken`). A multi-seller cart would create one session per seller and return one `CheckoutHandoff` per seller, which the blueprint already supports through the `seller` field.
3. **No product id or single-product lookup in discovery.** This backend derives a stable id from each product's canonical URL and caches the record. A production catalogue would expose a real id and a single-product read.
4. **GBP only.** KAELUM settles in GBP. A cart in any other currency is refused at hand-off rather than converted silently.

## Licence

Apache-2.0. See `LICENSE` and `NOTICE`. Built on the Apache-2.0 reference blueprint at github.com/anthropics/commerce-agents, installed unchanged from a pinned commit.
