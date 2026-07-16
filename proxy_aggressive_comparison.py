#!/usr/bin/env python3
"""
严格超时对照：用前次报告的攻击参数
  (concurrency, git_timeout): (20, 15), (30, 10), (50, 8)

同时跑直连 vs smart-git-proxy，输出 aggressive_comparison 报告。
"""
import argparse
import asyncio
import json
import time
from datetime import datetime
from pathlib import Path

from reproduce_504 import run_git_round, GitCloneResult, REPO_URL

REPORTS_DIR = Path(__file__).parent / "reports"
SCENARIOS = [(20, 15.0), (30, 10.0), (50, 8.0)]


def to_dict(r):
    stats = r.git_stats
    timeouts = sum(
        1 for res in r.results
        if isinstance(res, GitCloneResult)
        and ("TIMEOUT" in res.stderr_tail or "timed out" in res.stderr_tail.lower())
    )
    early_eof = sum(
        1 for res in r.results
        if isinstance(res, GitCloneResult) and "early EOF" in res.stderr_tail
    )
    return {
        "concurrency": r.concurrency,
        "success": stats.get("success", 0),
        "failed": stats.get("failed", 0),
        "timed_out": timeouts,
        "early_eof": early_eof,
        "wall_time": round(r.duration, 2),
        "avg_elapsed": round(stats.get("avg_elapsed", 0.0), 2),
        "max_elapsed": round(stats.get("max_elapsed", 0.0), 2),
    }


async def run_scenarios(name, proxy_url, delay=5.0):
    print(f"\n{'#'*70}\n#  {name}\n{'#'*70}")
    out = []
    for i, (c, t) in enumerate(SCENARIOS, 1):
        report = await run_git_round(c, i, t, proxy_url=proxy_url)
        d = to_dict(report)
        d["timeout"] = t
        out.append(d)
        if i < len(SCENARIOS):
            print(f"\n  ⏳ Waiting {delay}s ...")
            await asyncio.sleep(delay)
    return out


async def amain():
    parser = argparse.ArgumentParser()
    parser.add_argument("--proxy-url", required=True)
    parser.add_argument("--delay", type=float, default=5.0)
    args = parser.parse_args()

    REPORTS_DIR.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    direct = await run_scenarios("A. Direct GitHub (tight timeouts)", None, args.delay)
    proxied = await run_scenarios("B. Via smart-git-proxy (tight timeouts)", args.proxy_url, args.delay)

    out = {
        "meta": {
            "timestamp": ts,
            "repo": REPO_URL,
            "proxy_url": args.proxy_url,
            "scenarios": [{"concurrency": c, "timeout": t} for c, t in SCENARIOS],
            "notes": "Reuses aggressive params from prior 504_test_report.json: c=20/t=15, c=30/t=10, c=50/t=8",
        },
        "direct": direct,
        "via_smart_git_proxy": proxied,
    }
    json_path = REPORTS_DIR / "smart_git_proxy_aggressive.json"
    json_path.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    print(f"\n✅ JSON: {json_path}")

    # Console summary
    print(f"\n{'='*70}\nSUMMARY\n{'='*70}")
    print(f"{'scenario':<20} {'direct fail/total':<25} {'proxy fail/total':<25}")
    for d, p in zip(direct, proxied):
        s = f"c={d['concurrency']}, t={d['timeout']}s"
        print(f"{s:<20} "
              f"{d['failed']}/{d['concurrency']} (avg={d['avg_elapsed']}s)".ljust(25),
              f"{p['failed']}/{p['concurrency']} (avg={p['avg_elapsed']}s)".ljust(25))


if __name__ == "__main__":
    asyncio.run(amain())
