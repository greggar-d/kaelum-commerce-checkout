# KAELUM Commerce Checkout

A KAELUM storefront backend for Anthropic's [commerce-agents](https://github.com/anthropics/commerce-agents) shopping agent.

KAE is the KAELUM commerce currency (formerly named KLM, renamed October 2026).

The blueprint builds a Claude shopping agent and deliberately stops at the money: its checkout renders the cart for the host to complete, and it never charges a card. This repository fills that gap. It implements the blueprint's one integration surface, `StorefrontBackend`, over KAELUM, so a Claude shopping agent can browse the KAELUM acceptance network as its catalogue and hand checkout off to KAELUM, where the customer completes the spend in KAE, the KAELUM commerce currency.

It follows the same pattern as Shopify's [claude-for-commerce-examples](https://github.com/Shopify/claude-for-commerce-examples): the blueprint packages install unchanged from a pinned commit, and this repository adds a storefront on top. It does not modify the blueprint.

## The seam

Three live KAELUM endpoints do the work. The first two already back the KAELUM WebMCP layer.

| Blueprint surface | KAELUM endpoint | What it does |
| --- | --- | --- |
| `search_products`, `get_product_details` | `kaelumDiscover` | Read-only network discovery. Returns live KAELUM merchants and in-stock products, each priced in KAE at the discount checkout applies. Moves no money, reads no account. |
| `checkout_handoff` | `createUniversalPaymentSession` | Takes a merchant Bearer token and a gross GBP amount, returns a hosted gateway URL. The customer completes the KAE spend on kaelum.app, where a human confirms it. |
| `get_disclosure` | `kaelumDiscover` (same record) | The "Paying with KAE" facts box: price, merchant discount, platform fee, total, KAE to spend and the fixed KAE price, all from the figures discovery returned. Nothing is hardcoded. |
| (identity) | `kaelumWhoAmI` | Takes the merchant Bearer token and returns that merchant's public `merchant_ref`. Called once and cached. |

The blueprint calls `checkout_handoff` after the model's `checkout` tool call and pins the returned URL onto the checkout card. The model never sees or supplies the URL, which matches KAELUM's own posture: an agent may stage the order, but a human confirms the spend on kaelum.app.

## One merchant identity

Every KAELUM merchant has one public reference, `merchant_ref` (`kmr_...`). It appears on every merchant and product in discovery, it is what `kaelumWhoAmI` returns for a merchant token, and `createUniversalPaymentSession` refuses (409 `MERCHANT_MISMATCH`) any session whose `merchant_ref` is not the merchant the token pays.

This backend resolves its `merchant_ref` from the token, scopes discovery to it, and sends it with every checkout. So an agent selling for one store only ever sees that store's products, and a cart can never settle to another merchant's account. Before any session is created, the backend refuses a cart that mixes merchants, holds an item with no `merchant_ref`, or holds another merchant's items. Each hand-off gets a unique order id, and the discount sent to the session is the cart's own merchant's rate.

## Money maths, so it reconciles

The catalogue shows the gross GBP price. KAELUM applies the merchant discount itself, floored at 6 percent, when the session is created, so this backend passes the gross amount to the session and never a pre-discounted figure. Passing a net figure would discount twice. The KAE units, net price and discount for each product travel in the product's attributes, so the agent can tell the shopper what they pay in KAE and what they save while the authoritative charge stays with the KAELUM session. The backend also passes the merchant's discount to the session explicitly, so the discount quoted in discovery equals the discount charged at checkout. A record that carries only a net price is grossed back up by its own discount, so it is never discounted twice. Discovery's klm_units is the all-in amount the shopper pays: the discounted price plus the 4.5% KAELUM platform fee, which the buyer pays on top. The merchant receives the full discounted amount.

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
- `KAELUM_MERCHANT_REF`: optional. The merchant's public reference. Leave blank and it is resolved from the token.
- `KAELUM_SITE_KEY`: optional discovery scope for deployments without a token (the store's Wix site_key or Integration site_token). Ignored once a `merchant_ref` is known.
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

Run the tests (no network, no key; fake clients stand in for KAELUM and for the model):

```
pytest -q
```

## Merchant agent

`kaelum_storefront/merchant.py` implements the blueprint's other integration surface, `MerchantBackend`, over KAELUM for the merchant the token pays. The blueprint's `MerchantAgent` then gives that merchant a briefing and analysis assistant.

| Merchant surface | KAELUM source | Notes |
| --- | --- | --- |
| `get_business_snapshot`, `query_metrics` | `kaelumMerchantInsights` | KAE checkout sales, orders, checkouts started, conversion (paid / started), average order value, discount given; this period against the one before. Read-only, no customer data. |
| `search_listings`, `get_listing`, `get_pricing_context` | `kaelumDiscover` | The merchant's own listings, with the same `klm-XXXXXXXX` ids the shopping side uses. |
| stock, campaigns | not held by KAELUM | Switched off in `build_kaelum_merchant_config`; stated in the merchant context's limitations. |
| `stage_listing_update`, `stage_price_update` | proposals only | Staged with the blueprint's guardrails. `apply_change` never writes: the merchant makes the approved change in their own store. |

Run a briefing (needs `KAELUM_MERCHANT_TOKEN` and `ANTHROPIC_API_KEY`):

```
python scripts/merchant_digest.py
python scripts/merchant_digest.py "How did last_30_days compare with the month before?" --out digest.json
```

## Evals

The blueprint ships the eval case format and rules (`plugins/commerce-builder/skills/commerce-evals`) but no runner. `kaelum_storefront/evals.py` is the runner and `evals/cases.json` holds 13 cases in that format: search and grounding, the KAE price quote, the KAE terms facts box, cart, checkout without auto-payment, KAE described as spending money and never as an investment, invented orders and discounts, and an injection attack hidden in a third-party listing, each paired with a should-serve counterpart.

Each case drives one turn of the blueprint's `ShoppingAgent` over this backend. Code graders read the turn's events and the cart it leaves; a rubric goes to a judge model, pinned by name and asked to answer through a structured verdict tool, and each verdict carries a fingerprint of the judge model and rubric. Data comes from `evals/catalogue.json` (8T Clothing's live listings as recorded on 19 October 2026, plus two eval-only listings from an invented seller), or from live discovery with `--live`. No eval ever creates a payment session.

```
python scripts/run_evals.py                 # all cases, 1 trial, judged
python scripts/run_evals.py --trials 3
python scripts/run_evals.py --no-judge      # code graders only
```

Reports go to `evals/reports/` as JSON and Markdown. The exit status is 0 only when every critical case passes every trial. Running the suite calls the Claude API and is billed to the key in `.env`.

## Scope of this pilot

This is the blueprint's sanctioned minimal shape: implement search and product details, and switch off what the store does not serve. Cart, checkout and the KAE terms facts box are on. The agent is told to show the facts box before the shopper pays. Orders, policies and fulfillment are off in `build_kaelum_shopping_config`, because KAELUM discovery does not serve those today; their tools are removed on every path.

## Known seams to close before production

These are deliberate scaffold simplifications, not blockers for a demo. They are the honest list of what a production integration would tidy.

1. ~~**Two merchant identity systems.**~~ Closed in October 2026. Discovery, `kaelumWhoAmI` and payment sessions now share one `merchant_ref`; see "One merchant identity" above.
2. **Single-merchant scope.** One deployment sells for one store (one `MerchantToken`). A multi-seller cart would create one session per seller and return one `CheckoutHandoff` per seller, which the blueprint already supports through the `seller` field.
3. **No product id or single-product lookup in discovery.** This backend derives a stable id from each product's canonical URL and caches the record. A cold lookup now pages through the whole of discovery (to a cap) instead of reading only the first page. A production catalogue would still expose a real id and a single-product read.
4. **GBP only.** KAELUM settles in GBP. A cart in any other currency is refused at hand-off rather than converted silently.

## Licence

Apache-2.0. See `LICENSE` and `NOTICE`. Built on the Apache-2.0 reference blueprint at github.com/anthropics/commerce-agents, installed unchanged from a pinned commit.
