# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Kaelum Technologies Ltd. Built on the Apache-2.0 reference
# blueprint at github.com/anthropics/commerce-agents (see NOTICE).

"""A KAELUM merchant briefing: one turn of the blueprint's MerchantAgent over this
merchant's KAELUM data, printed to the terminal and optionally saved as JSON.

    python scripts/merchant_digest.py                       # this week
    python scripts/merchant_digest.py "How did last_30_days compare?" --out digest.json

Needs KAELUM_MERCHANT_TOKEN (the merchant it reports on) and ANTHROPIC_API_KEY. The agent
reads KLM checkout sales, the merchant's listings and their KLM pricing; it can propose
listing and price changes but never applies them (the merchant does, in their own store).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from merchant_agent import MerchantSessionContext, MerchantSessionState
from merchant_agent_runtime import MerchantAgent

from kaelum_storefront import KaelumClient, settings_from_env
from kaelum_storefront.merchant import KaelumMerchantBackend, build_kaelum_merchant_config

DEFAULT_PROMPT = (
    "Give me this week's briefing: how KLM sales went against last week, what needs "
    "attention, and one change worth considering."
)
SKILLS_DIR = Path(
    os.environ.get(
        "COMMERCE_AGENTS_MERCHANT_SKILLS_DIR",
        str(Path(__file__).resolve().parents[2] / "commerce-agents" / "merchant-agent" / "skills"),
    )
)


async def main(prompt: str, out: str | None) -> int:
    settings = settings_from_env()
    if not settings.merchant_token:
        print("Set KAELUM_MERCHANT_TOKEN in .env first.", file=sys.stderr)
        return 2
    client = KaelumClient(settings)
    ref = await client.merchant_ref()
    config = build_kaelum_merchant_config(brand_name=os.environ.get("KAELUM_BRAND_NAME", "8T Clothing"))
    agent = MerchantAgent(
        backend=KaelumMerchantBackend(client, config),
        skills_dir=SKILLS_DIR if SKILLS_DIR.is_dir() else None,
        config=config,
    )
    base = {
        "session_id": "kaelum-digest",
        "merchant_id": ref or "kaelum-merchant",
        "operator": os.environ.get("MERCHANT_OPERATOR", "merchant"),
    }
    try:
        session = MerchantSessionContext(**base, timezone=os.environ.get("MERCHANT_TIMEZONE", "Europe/London"))
    except ValueError:
        # No time zone database on this machine (Windows without tzdata): use its clock.
        session = MerchantSessionContext(**base, now=datetime.now())
    messages = [{"role": "user", "content": prompt}]
    text: list[str] = []
    components: list[dict] = []
    async for event in agent.stream_turn(messages, session, MerchantSessionState()):
        if event.type == "text_delta":
            chunk = event.data.get("text", "")
            text.append(chunk)
            print(chunk, end="", flush=True)
        elif event.type == "ui":
            components.append(event.data)
    print()
    await client.aclose()
    if out:
        Path(out).write_text(json.dumps({
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "prompt": prompt,
            "text": "".join(text).strip(),
            "components": components,
        }, indent=2, default=str))
        print(f"Saved to {out}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("prompt", nargs="?", default=DEFAULT_PROMPT)
    parser.add_argument("--out")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.prompt, args.out)))
