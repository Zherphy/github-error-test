#!/usr/bin/env python3
"""Region comparison stress test: HK vs Korea GitHub CDN node."""

import asyncio, subprocess, time, shutil, os, json, socket
from collections import Counter

REPO_URL = "https://github.com/sgl-project/sglang.git"
SEPARATOR = "=" * 70

resolved_ip = socket.gethostbyname("github.com")
print(f"Current github.com resolves to: {resolved_ip}")
print(f"Test target: {REPO_URL}")


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


async def run_round(concurrency, timeout_s, round_num, region_label):
    base_dir = f"/tmp/region_test_r{round_num}"
    os.makedirs(base_dir, exist_ok=True)
    print(f"\n{SEPARATOR}")
    print(f"  Round {round_num} [{region_label}] | concurrency={concurrency} | timeout={timeout_s}s")
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

    print(f"  Wall: {wall_elapsed}s | OK: {ok} | Failed: {fail} | 504: {timed_out} | Avg: {avg_t}s | Max: {max_t}s")

    for r in results:
        if not r["success"]:
            status = "TIMEOUT" if r["timed_out"] else "ERROR"
            print(f"    [{status}] idx={r['idx']} elapsed={r['elapsed']}s")
            for line in r["stderr_tail"].split("\n")[-3:]:
                if line.strip():
                    print(f"      > {line.strip()}")

    shutil.rmtree(base_dir, ignore_errors=True)
    return {
        "round": round_num, "region": region_label,
        "concurrency": concurrency, "timeout": timeout_s,
        "wall_time": wall_elapsed, "ok": ok, "failed": fail,
        "timed_out": timed_out, "avg_elapsed": avg_t, "max_elapsed": max_t,
        "results": results,
    }


async def run_full_test(region_label):
    configs = [
        (1,   5,   30),
        (2,  10,   20),
        (3,  20,   15),
        (4,  30,   10),
        (5,  50,    8),
    ]
    rounds = []
    for round_num, concurrency, timeout_s in configs:
        rd = await run_round(concurrency, timeout_s, round_num, region_label)
        rounds.append(rd)
        await asyncio.sleep(3)
    return rounds


async def main():
    # ── Phase 1: HK (current DNS) ──
    print(f"\n{'#'*70}")
    print(f"#  PHASE 1: Hong Kong CDN Node ({resolved_ip})")
    print(f"{'#'*70}")
    hk_rounds = await run_full_test("HK")

    # ── Phase 2: Korea (forced IP) ──
    korea_ip = "20.200.245.247"
    print(f"\n{'#'*70}")
    print(f"#  PHASE 2: Korea CDN Node ({korea_ip})")
    print(f"#  Switching /etc/hosts: github.com -> {korea_ip}")
    print(f"{'#'*70}")

    # Backup /etc/hosts and add Korea entry
    hosts_path = "/etc/hosts"
    hosts_backup = hosts_path + ".bak_region_test"
    shutil.copy2(hosts_path, hosts_backup)
    with open(hosts_path, "a") as f:
        f.write(f"\n# REGION TEST — Korea GitHub node\n{korea_ip} github.com\n")
    # Flush DNS cache
    subprocess.run(["sh", "-c", "echo flush_dns_cache not available || true"], capture_output=True)

    # Verify resolution
    new_ip = socket.gethostbyname("github.com")
    print(f"  github.com now resolves to: {new_ip}")
    if new_ip != korea_ip:
        print(f"  WARNING: DNS override may not work; trying nscd/unbound flush...")
        subprocess.run(["systemd-resolve", "--flush-caches"], capture_output=True, timeout=5)
        # Re-check
        time.sleep(1)
        new_ip = socket.gethostbyname("github.com")
        print(f"  After flush: github.com resolves to: {new_ip}")

    korea_rounds = await run_full_test("Korea")

    # Restore /etc/hosts
    shutil.copy2(hosts_backup, hosts_path)
    os.remove(hosts_backup)
    print(f"\n  Restored /etc/hosts")

    # ── Comparison Report ──
    print(f"\n{SEPARATOR}")
    print("  COMPARISON REPORT — HK vs Korea GitHub CDN")
    print(SEPARATOR)
    print(f"  HK IP:  {resolved_ip} (current DNS)")
    print(f"  Korea IP: {korea_ip} (forced via /etc/hosts)")
    print()

    hdr = f"  {'Rnd':>3} | {'Region':>5} | {'Conc':>4} | {'TOut':>4} | {'Wall':>5} | {'OK':>3} | {'Fail':>4} | {'504':>3} | {'Avg':>4} | {'Max':>4} | Result"
    print(hdr)
    print(f"  {'-'*75}")

    for rd in hk_rounds:
        flag = "504!" if rd["timed_out"] > 0 else "OK"
        print(f"  {rd['round']:>3} | {'HK':>5} | {rd['concurrency']:>4} | {rd['timeout']:>4}s | {rd['wall_time']:>5}s | {rd['ok']:>3} | {rd['failed']:>4} | {rd['timed_out']:>3} | {rd['avg_elapsed']:>4}s | {rd['max_elapsed']:>4}s | {flag}")
    print(f"  {'-'*75}")
    for rd in korea_rounds:
        flag = "504!" if rd["timed_out"] > 0 else "OK"
        print(f"  {rd['round']:>3} | {'KR':>5} | {rd['concurrency']:>4} | {rd['timeout']:>4}s | {rd['wall_time']:>5}s | {rd['ok']:>3} | {rd['failed']:>4} | {rd['timed_out']:>3} | {rd['avg_elapsed']:>4}s | {rd['max_elapsed']:>4}s | {flag}")

    # Side-by-side comparison
    print(f"\n  Key Metrics Comparison:")
    print(f"  {'Metric':>20} | {'HK':>10} | {'Korea':>10} | {'Delta':>10}")
    print(f"  {'-'*55}")

    hk_504 = next((r for r in hk_rounds if r["timed_out"] > 0), None)
    kr_504 = next((r for r in korea_rounds if r["timed_out"] > 0), None)
    hk_504_conc = hk_504["concurrency"] if hk_504 else "N/A"
    kr_504_conc = kr_504["concurrency"] if kr_504 else "N/A"

    hk_avg_r1 = hk_rounds[0]["avg_elapsed"]
    kr_avg_r1 = korea_rounds[0]["avg_elapsed"]
    hk_avg_r2 = hk_rounds[1]["avg_elapsed"]
    kr_avg_r2 = korea_rounds[1]["avg_elapsed"]

    delta_r1 = round(kr_avg_r1 - hk_avg_r1, 2)
    delta_r2 = round(kr_avg_r2 - hk_avg_r2, 2)

    print(f"  {'First 504 conc':>20} | {str(hk_504_conc):>10} | {str(kr_504_conc):>10} | {'':>10}")
    print(f"  {'Avg clone R1(5c)':>20} | {hk_avg_r1:>9}s | {kr_avg_r1:>9}s | {delta_r1:>9}s")
    print(f"  {'Avg clone R2(10c)':>20} | {hk_avg_r2:>9}s | {kr_avg_r2:>9}s | {delta_r2:>9}s")
    print(f"  {'504 rate at 20c':>20} | {hk_rounds[2]['timed_out']:>10} | {korea_rounds[2]['timed_out']:>10} | {'':>10}")
    print(f"  {'504 rate at 30c':>20} | {hk_rounds[3]['timed_out']:>10} | {korea_rounds[3]['timed_out']:>10} | {'':>10}")

    # Save JSON
    report = {
        "hk_ip": resolved_ip,
        "korea_ip": korea_ip,
        "hk_rounds": hk_rounds,
        "korea_rounds": korea_rounds,
    }
    report_path = "/root/github-error-test/reports/hk_vs_korea_report.json"
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2, default=str)
    print(f"\n  JSON report saved: {report_path}")

asyncio.run(main())
