# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Kaelum Technologies Ltd. Built on the Apache-2.0 reference
# blueprint at github.com/anthropics/commerce-agents (see NOTICE).

"""Run the KAELUM shopping-agent evals (evals/cases.json) and write a report.

    python scripts/run_evals.py                      # every case, 1 trial, judge on
    python scripts/run_evals.py --trials 3           # 3 trials per case
    python scripts/run_evals.py --case poison-010-injected-listing
    python scripts/run_evals.py --live               # live kaelumDiscover instead of the fixture
    python scripts/run_evals.py --no-judge           # code graders only (cheaper)

Needs ANTHROPIC_API_KEY. Never creates a KAELUM payment session. Writes
evals/reports/<timestamp>.json and .md. Exit status 0 when every critical case passed
every trial, 1 otherwise.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from anthropic import AsyncAnthropic

from kaelum_storefront import KaelumClient, KaelumStorefrontBackend, settings_from_env
from kaelum_storefront.evals import (
    EVAL_DIR,
    FixtureClient,
    LiveDiscoveryClient,
    build_eval_agent,
    grade,
    judge,
    load_cases,
    load_catalogue,
    markdown_report,
    run_case,
    summarise,
)

DEFAULT_MODEL = os.environ.get("KAELUM_AGENT_MODEL", "claude-sonnet-5-5")
DEFAULT_JUDGE = os.environ.get("KAELUM_JUDGE_MODEL", "claude-sonnet-5-5")
SKILLS_DIR = Path(
    os.environ.get(
        "COMMERCE_AGENTS_SKILLS_DIR",
        str(Path(__file__).resolve().parents[2] / "commerce-agents" / "shopping-agent" / "skills"),
    )
)


async def main(args: argparse.Namespace) -> int:
    settings_from_env()  # loads .env (ANTHROPIC_API_KEY lives there too)
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("Set ANTHROPIC_API_KEY in .env to run the evals.", file=sys.stderr)
        return 2
    cases = [c for c in load_cases() if not c.get("skip") and (not args.case or c["id"] in args.case)]
    if not cases:
        print("No cases matched.", file=sys.stderr)
        return 2
    model_client = AsyncAnthropic()
    started = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    results = []
    for case in cases:
        for trial in range(args.trials):
            # A fresh backend per trial, so carts and caches never leak between runs.
            data_client = (
                LiveDiscoveryClient(KaelumClient(settings_from_env())) if args.live
                else FixtureClient(load_catalogue())
            )
            backend = KaelumStorefrontBackend(data_client)
            agent = build_eval_agent(backend, args.model, SKILLS_DIR, client=model_client)
            out = await run_case(agent, backend, data_client, case, trial)
            checks = grade(case.get("expected", {}), out)
            rubric = case.get("expected", {}).get("rubric")
            if rubric and not args.no_judge:
                checks.append(await judge(model_client, args.judge_model, rubric, case, out))
            code_ok = all(c["passed"] is True for c in checks if c["check"] != "rubric")
            rubric_ok = all(c["passed"] is not False for c in checks if c["check"] == "rubric")
            judge_failure = any(c["check"] == "rubric" and c["passed"] is None for c in checks)
            passed = code_ok and rubric_ok and not judge_failure
            results.append({
                "id": case["id"], "priority": case.get("priority", "medium"), "trial": trial,
                "passed": passed, "judge_failure": judge_failure, "checks": checks,
                "tools": out.tool_names, "reply": out.reply, "cart": out.cart_ids,
                "elapsed_s": out.elapsed_s, "usage": out.usage, "error": out.error,
            })
            mark = "PASS" if passed else ("ERROR" if out.error else ("JUDGE?" if judge_failure else "FAIL"))
            print(f"{mark:6} {case['id']} [trial {trial + 1}/{args.trials}] {out.elapsed_s}s")
            if out.error:
                print(f"       run error: {out.error[:300]}")
            for c in checks:
                if c["check"] == "rubric" and c["passed"] is None:
                    print(f"       {c['detail'][:300]}")
            await data_client.aclose()
    meta = {
        "started": started, "agent_model": args.model,
        "judge_model": None if args.no_judge else args.judge_model,
        "data": "live kaelumDiscover" if args.live else "evals/catalogue.json", "trials": args.trials,
    }
    summary = summarise(results)
    reports = EVAL_DIR / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    (reports / f"{started}.json").write_text(json.dumps({"meta": meta, "summary": summary, "results": results}, indent=2, default=str))
    (reports / f"{started}.md").write_text(markdown_report(meta, summary, results))
    print(f"\n{summary['passed_trials']}/{summary['trials']} trials passed. "
          f"Critical all passing: {'yes' if summary['critical_all_pass'] else 'no'}.")
    print(f"Report: {reports / (started + '.md')}")
    return 0 if summary["critical_all_pass"] else 1


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Run the KAELUM shopping-agent evals.")
    p.add_argument("--trials", type=int, default=1)
    p.add_argument("--case", action="append", help="Run only this case id (repeatable).")
    p.add_argument("--live", action="store_true", help="Use live kaelumDiscover.")
    p.add_argument("--no-judge", action="store_true", help="Skip rubric judging.")
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--judge-model", default=DEFAULT_JUDGE)
    sys.exit(asyncio.run(main(p.parse_args())))
