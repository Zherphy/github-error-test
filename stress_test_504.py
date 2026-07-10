#!/usr/bin/env python3
"""Progressive git clone stress test to reproduce 504 behavior."""

import asyncio, subprocess, time, shutil, os, json
from collections import Counter

REPO_URL = "https://github.com/sgl-project/sglang.git"
SEPARATOR = "=" * 65
all_rounds = []


async def clone_one(idx, timeout_s, base_dir):
    clone_dir = os.path.join(base_dir, f"clone_{idx}")
    start = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        "git", "clone", "--depth", "1", REPO_URL, clone_dir,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout_s)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
        elapsed = round(time.monotonic() - start, 2)
        stderr_data = b""
        try:
            stderr_data = await proc.stderr.read()
        except Exception:
            pass
        stderr_tail = stderr_data.decode(errors="replace")[-300:]
        shutil.rmtree(clone_dir, ignore_errors=True)
        return {
            "idx": idx, "success": False, "timed_out": True,
            "elapsed": elapsed, "exit_code": -1,
            "stderr_tail": stderr_tail,
        }

    elapsed = round(time.monotonic() - start, 2)
    stderr_data = b""
    try:
        stderr_data = await proc.stderr.read()
    except Exception:
        pass
    stderr_tail = stderr_data.decode(errors="replace")[-300:]
    rc = proc.returncode or 0
    success = rc == 0
    if not success:
        shutil.rmtree(clone_dir, ignore_errors=True)
    return {
        "idx": idx, "success": success, "timed_out": False,
        "elapsed": elapsed, "exit_code": rc,
        "stderr_tail": stderr_tail,
    }


async def run_round(concurrency, timeout_s, round_num):
    base_dir = f"/tmp/sglang_504_r{round_num}"
    os.makedirs(base_dir, exist_ok=True)
    print(f"\n{SEPARATOR}")
    print(f"  Round {round_num} | concurrency={concurrency} | clone_timeout={timeout_s}s")
    print(SEPARATOR)

    tasks = [clone_one(i, timeout_s, base_dir) for i in range(concurrency)]
    wall_start = time.monotonic()
    results = await asyncio.gather(*tasks)
    wall_elapsed = round(time.monotonic() - wall_start, 2)

    ok = sum(1 for r in results if r["success"])
    fail = sum(1 for r in results if not r["success"])
    timed_out = sum(1 for r in results if r["timed_out"])
    avg_t = round(sum(r["elapsed"] for r in results) / len(results), 2)
    max_t = round(max(r["elapsed"] for r in results), 2)

    print(f"  Wall time: {wall_elapsed}s")
    print(f"  OK: {ok} | Failed: {fail} | TimedOut(504-sim): {timed_out}")
    print(f"  Avg elapsed: {avg_t}s | Max elapsed: {max_t}s")

    for r in results:
        status = "OK" if r["success"] else ("TIMEOUT" if r["timed_out"] else "ERROR")
        print(f"    [{status}] idx={r['idx']} elapsed={r['elapsed']}s exit={r['exit_code']}")
        if not r["success"] and r["stderr_tail"]:
            for line in r["stderr_tail"].split("\n")[-5:]:
                if line.strip():
                    print(f"      > {line.strip()}")

    shutil.rmtree(base_dir, ignore_errors=True)

    round_data = {
        "round": round_num, "concurrency": concurrency, "timeout": timeout_s,
        "wall_time": wall_elapsed, "ok": ok, "failed": fail, "timed_out": timed_out,
        "avg_elapsed": avg_t, "max_elapsed": max_t, "results": results,
    }
    all_rounds.append(round_data)
    return round_data


async def main():
    # Progressive stress: increase concurrency, decrease timeout
    configs = [
        # (round_num, concurrency, timeout_seconds)
        (1,   5,   30),   # Baseline: should mostly succeed
        (2,  10,   20),   # Moderate pressure
        (3,  20,   15),   # High pressure, tight timeout
        (4,  30,   10),   # Very high, aggressive timeout
        (5,  50,    8),   # Extreme — expect many timeouts (504)
    ]

    for round_num, concurrency, timeout_s in configs:
        await run_round(concurrency, timeout_s, round_num)
        await asyncio.sleep(3)

    # ── Final Report ──
    print(f"\n{SEPARATOR}")
    print("  TEST REPORT — GitHub 504 Gateway Timeout Reproduction")
    print(SEPARATOR)
    print(f"  Target: {REPO_URL}")
    print(f"  Strategy: Progressive concurrency + shrinking timeout")
    print(f"  Timeout = simulated gateway proxy_read_timeout")
    print()

    hdr = f"  {'Round':>5} | {'Conc':>5} | {'Timeout':>7} | {'Wall':>6} | {'OK':>4} | {'Fail':>4} | {'504':>4} | {'Avg':>5} | {'Max':>5} | Result"
    print(hdr)
    print(f"  {'-'*80}")
    for rd in all_rounds:
        flag = "504!" if rd["timed_out"] > 0 else "OK" if rd["ok"] > 0 else "ERR"
        line = (
            f"  {rd['round']:>5} | {rd['concurrency']:>5} | {rd['timeout']:>5}s   "
            f"| {rd['wall_time']:>5}s | {rd['ok']:>4} | {rd['failed']:>4} "
            f"| {rd['timed_out']:>4} | {rd['avg_elapsed']:>4}s | {rd['max_elapsed']:>4}s | {flag}"
        )
        print(line)

    first_504 = next((r for r in all_rounds if r["timed_out"] > 0), None)
    if first_504:
        print(f"\n  First 504-simulated timeout at:")
        print(f"    concurrency={first_504['concurrency']}, proxy_timeout={first_504['timeout']}s")
        print(f"    {first_504['timed_out']}/{first_504['concurrency']} clones timed out")
    else:
        print(f"\n  No timeouts detected")

    # Save JSON report
    report_path = "/root/504_test_report.json"
    with open(report_path, "w") as f:
        json.dump(all_rounds, f, indent=2, default=str)
    print(f"\n  JSON report saved: {report_path}")

asyncio.run(main())
