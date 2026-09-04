# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Kaelum Technologies Ltd. Built on the Apache-2.0 reference
# blueprint at github.com/anthropics/commerce-agents (see NOTICE).

"""Construct the blueprint shopping agent over the KAELUM backend.

This mirrors the blueprint's own host wiring (examples/retail/api/main.py): build the
backend, then hand it to ShoppingAgent with the blueprint's shipped skills. Running a
live turn needs ANTHROPIC_API_KEY and the blueprint packages installed from
requirements.txt; see the README. For a no-key check of the KAELUM calls themselves,
use scripts/smoke_kaelum.py instead.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from shopping_agent_runtime import ShoppingAgent

from kaelum_storefront import (
    KaelumClient,
    KaelumStorefrontBackend,
    build_kaelum_shopping_config,
    settings_from_env,
)

# The blueprint's shipped skills live in the cloned commerce-agents checkout. Point
# SKILLS_DIR at it (see README); it defaults to a sibling clone.
SKILLS_DIR = Path(
    os.environ.get(
        "COMMERCE_AGENTS_SKILLS_DIR",
        str(Path(__file__).resolve().parents[2] / "commerce-agents" / "shopping-agent" / "skills"),
    )
)


def build_agent() -> ShoppingAgent:
    client = KaelumClient(settings_from_env())
    backend = KaelumStorefrontBackend(client)
    return ShoppingAgent(
        backend=backend,
        skills_dir=SKILLS_DIR,
        config=build_kaelum_shopping_config(),
    )


async def _one_turn(prompt: str) -> None:
    from commerce_common.types import new_session  # provided by the blueprint

    agent = build_agent()
    messages: list[dict] = [{"role": "user", "content": prompt}]
    session, state = new_session(user_id="guest", session_id="demo-session")
    async for event in agent.stream_turn(messages, session, state):
        kind = getattr(event, "kind", getattr(event, "type", ""))
        if kind == "text_delta":
            print(getattr(event, "text", ""), end="", flush=True)
    print()


if __name__ == "__main__":
    import sys

    ask = " ".join(sys.argv[1:]) or "show me what KAELUM merchants have under 40 pounds"
    asyncio.run(_one_turn(ask))
