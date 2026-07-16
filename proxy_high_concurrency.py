#!/usr/bin/env python3
"""
高并发压测：c=50、c=100 分别走 直连 vs smart-git-proxy

安全护栏（防 OOM）：
  - 每秒采样 /proc/meminfo 的 MemAvailable
  - 一旦 available < MIN_AVAIL_MB（默认 1500 MB），
    立即 kill 全部 git 子进程 + 停止代理，本轮终止
  - 也支持采样代理进程的 RSS，超过阈值同样中断

产出：reports/smart_git_proxy_high_concurrency.json
"""
import argparse
import asyncio
import json
import os
import signal
import subprocess
import time
from datetime import datetime
from pathlib import Path

from reproduce_504 import run_git_round, GitCloneResult, REPO_URL

REPORTS_DIR = Path(__file__).parent / "reports"


def read_mem_available_mb() -> float:
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / 1024.0  # kB -> MB
    except Exception:
        pass
    return 0.0


def proc_rss_mb(pid: int) -> float:
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024.0
    except Exception:
        return 0.0
    return 0.0


class SafetyAbort(Exception):
    """当内存低于阈值时抛出，让上层做记录而不是无限重试"""


async def safety_watchdog(min_avail_mb: float, max_proxy_rss_mb: float,
                          proxy_pid: int | None, stop_evt: asyncio.Event,
                          samples: list, aborted_flag: dict):
    """每 1s 采样一次，触发即 kill 所有 git 子进程并置位 aborted_flag"""
    while not stop_evt.is_set():
        avail = read_mem_available_mb()
        p_rss = proc_rss_mb(proxy_pid) if proxy_pid else 0.0
        samples.append({
            "ts": round(time.time(), 2),
            "mem_avail_mb": round(avail, 1),
            "proxy_rss_mb": round(p_rss, 1),
        })
        if avail < min_avail_mb:
            aborted_flag["reason"] = f"MemAvailable={avail:.0f}MB < {min_avail_mb}MB"
            aborted_flag["ts"] = time.time()
            print(f"\n  🚨 SAFETY ABORT: {aborted_flag['reason']} — killing all git/upload-pack")
            subprocess.run(["pkill", "-9", "-f", "^git clone"], check=False)
            subprocess.run(["pkill", "-9", "git-upload-pack"], check=False)
            return
        if proxy_pid and p_rss > max_proxy_rss_mb:
            aborted_flag["reason"] = f"proxy RSS={p_rss:.0f}MB > {max_proxy_rss_mb}MB"
            aborted_flag["ts"] = time.time()
            print(f"\n  🚨 SAFETY ABORT: {aborted_flag['reason']} — killing all git/upload-pack")
            subprocess.run(["pkill", "-9", "-f", "^git clone"], check=False)
            subprocess.run(["pkill", "-9", "git-upload-pack"], check=False)
            return
        await asyncio.sleep(1.0)


def to_dict(r):
    stats = r.git_stats
    timeouts = early_eof = http_5xx = 0
    stderr_samples = []
    for res in r.results:
        if isinstance(res, GitCloneResult):
            se = res.stderr_tail
            if "TIMEOUT" in se or "timed out" in se.lower():
                timeouts += 1
            if "early EOF" in se:
                early_eof += 1
            if "HTTP 5" in se or " 502" in se or " 503" in se or " 504" in se:
                http_5xx += 1
            if not res.success and len(stderr_samples) < 4:
                stderr_samples.append(se[-140:].strip())
    return {
        "concurrency": r.concurrency,
        "success": stats.get("success", 0),
        "failed": stats.get("failed", 0),
        "timed_out": timeouts,
        "early_eof": early_eof,
        "http_5xx": http_5xx,
        "wall_time": round(r.duration, 2),
        "avg_elapsed": round(stats.get("avg_elapsed", 0.0), 2),
        "max_elapsed": round(stats.get("max_elapsed", 0.0), 2),
        "sample_errors": stderr_samples,
    }


async def run_round_guarded(name: str, c: int, timeout: float, proxy_url: str | None,
                            proxy_pid: int | None, min_avail_mb: float,
                            max_proxy_rss_mb: float) -> dict:
    """跑一轮，加安全看门狗；OOM 风险触发时中断"""
    print(f"\n{'#'*70}\n#  {name}: c={c}, timeout={timeout}s"
          f", floor MemAvail={min_avail_mb:.0f}MB"
          f"{', proxy_rss_cap='+str(max_proxy_rss_mb)+'MB' if proxy_pid else ''}\n{'#'*70}")
    samples = []
    aborted = {}
    stop_evt = asyncio.Event()
    watchdog = asyncio.create_task(
        safety_watchdog(min_avail_mb, max_proxy_rss_mb, proxy_pid, stop_evt, samples, aborted)
    )
    try:
        r = await run_git_round(c, 1, timeout, proxy_url=proxy_url)
        d = to_dict(r)
    finally:
        stop_evt.set()
        await watchdog

    d["timeout"] = timeout
    d["proxy_used"] = proxy_url is not None
    d["aborted"] = bool(aborted)
    d["abort_reason"] = aborted.get("reason")

    if samples:
        avails = [s["mem_avail_mb"] for s in samples]
        rsss = [s["proxy_rss_mb"] for s in samples]
        d["watchdog"] = {
            "samples": len(samples),
            "min_mem_avail_mb": round(min(avails), 1),
            "max_proxy_rss_mb": round(max(rsss), 1),
        }
    return d


async def amain():
    parser = argparse.ArgumentParser()
    parser.add_argument("--proxy-url", required=True)
    parser.add_argument("--proxy-pid", type=int, required=True)
    parser.add_argument("--concurrencies", default="50,100")
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--delay", type=float, default=15.0)
    parser.add_argument("--min-avail-mb", type=float, default=1500.0,
                        help="Abort if MemAvailable drops below this (MB)")
    parser.add_argument("--max-proxy-rss-mb", type=float, default=8000.0,
                        help="Abort if proxy process RSS exceeds this (MB)")
    args = parser.parse_args()

    concurrencies = [int(c) for c in args.concurrencies.split(",")]
    REPORTS_DIR.mkdir(exist_ok=True)

    # 环境自检
    avail0 = read_mem_available_mb()
    print(f"Initial MemAvailable: {avail0:.0f} MB  |  floor: {args.min_avail_mb:.0f} MB")
    if avail0 < args.min_avail_mb * 1.2:
        print(f"  ⚠️  Startup MemAvailable close to floor; consider raising --min-avail-mb or freeing memory")

    all_rounds = []
    for c in concurrencies:
        # 直连
        d_direct = await run_round_guarded(
            f"Direct GitHub c={c}", c, args.timeout, None, None,
            args.min_avail_mb, args.max_proxy_rss_mb,
        )
        all_rounds.append({"scenario": "direct", **d_direct})
        if d_direct.get("aborted"):
            print(f"  ⏹  Round aborted; skipping remaining scenarios for c={c}+")
            break
        await asyncio.sleep(args.delay)

        # 走代理
        d_proxy = await run_round_guarded(
            f"Via smart-git-proxy c={c}", c, args.timeout, args.proxy_url,
            args.proxy_pid, args.min_avail_mb, args.max_proxy_rss_mb,
        )
        all_rounds.append({"scenario": "proxy", **d_proxy})
        if d_proxy.get("aborted"):
            print(f"  ⏹  Round aborted; skipping remaining scenarios for c>{c}")
            break
        await asyncio.sleep(args.delay)

    out = {
        "meta": {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "repo": REPO_URL,
            "proxy_url": args.proxy_url,
            "timeout": args.timeout,
            "concurrencies_planned": concurrencies,
            "min_avail_mb": args.min_avail_mb,
            "max_proxy_rss_mb": args.max_proxy_rss_mb,
            "initial_mem_avail_mb": round(avail0, 1),
        },
        "rounds": all_rounds,
    }
    path = REPORTS_DIR / "smart_git_proxy_high_concurrency.json"
    path.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    print(f"\n{'='*70}\n✅ JSON: {path}\n{'='*70}")

    print(f"\n{'scenario':<10}{'c':<5}{'succ':<6}{'fail':<6}{'timeout':<9}"
          f"{'eof':<5}{'avg':<8}{'max':<8}{'wall':<8}{'minAvail':<11}{'proxyRSS':<10}{'abort':<10}")
    for r in all_rounds:
        w = r.get("watchdog", {})
        abort = "YES" if r.get("aborted") else "-"
        print(f"{r['scenario']:<10}{r['concurrency']:<5}{r['success']:<6}{r['failed']:<6}"
              f"{r['timed_out']:<9}{r['early_eof']:<5}{r['avg_elapsed']:<8}"
              f"{r['max_elapsed']:<8}{r['wall_time']:<8}"
              f"{w.get('min_mem_avail_mb', '-'):<11}{w.get('max_proxy_rss_mb', '-'):<10}{abort:<10}")


if __name__ == "__main__":
    asyncio.run(amain())
