#!/usr/bin/env python3
"""Token-authenticated region comparison: HK vs Korea — API + Git clone."""

import asyncio, subprocess, time, shutil, os, json, socket, sys
from collections import Counter

REPO_OWNER = "sgl-project"
REPO_NAME = "sglang"
REPO_URL = f"https://github.com/{REPO_OWNER}/{REPO_NAME}.git"
API_BASE = "https://api.github.com"

SEPARATOR = "=" * 70

TOKEN = sys.argv[1] if len(sys.argv) > 1 else None
if not TOKEN:
    print("Usage: python3 token_region_test.py <github_token>")
    sys.exit(1)

TOKEN_REPO_URL = REPO_URL.replace("https://github.com", f"https://{TOKEN}@github.com")

resolved_ip = socket.gethostbyname("github.com")
print(f"Current github.com resolves to: {resolved_ip}")
print(f"Token: ghp_...{TOKEN[-4:]} (authenticated, rate limit: 5,000/hr)")

ENDPOINTS = [
    f"/repos/{REPO_OWNER}/{REPO_NAME}",
    f"/repos/{REPO_OWNER}/{REPO_NAME}/contents",
    f"/repos/{REPO_OWNER}/{REPO_NAME}/commits?per_page=100",
    f"/repos/{REPO_OWNER}/{REPO_NAME}/branches",
    f"/repos/{REPO_OWNER}/{REPO_NAME}/issues?state=all&per_page=100",
    f"/repos/{REPO_OWNER}/{REPO_NAME}/releases",
    f"/repos/{REPO_OWNER}/{REPO_NAME}/tags",
    f"/repos/{REPO_OWNER}/{REPO_NAME}/contributors",
    f"/repos/{REPO_OWNER}/{REPO_NAME}/stargazers",
    f"/repos/{REPO_OWNER}/{REPO_NAME}/forks",
]


async def api_fetch(session, endpoint, headers, timeout_s):
    url = f"{API_BASE}{endpoint}"
    start = time.monotonic()
    try:
        t = aiohttp.ClientTimeout(total=timeout_s)
        async with session.get(url, headers=headers, timeout=t) as resp:
            elapsed = time.monotonic() - start
            await resp.read()
            return {"type": "api", "status": resp.status, "elapsed": round(elapsed, 2),
                    "endpoint": endpoint, "timed_out": False}
    except asyncio.TimeoutError:
        elapsed = time.monotonic() - start
        return {"type": "api", "status": 504, "elapsed": round(elapsed, 2),
                "endpoint": endpoint, "timed_out": True, "error": f"timeout({timeout_s}s)"}
    except Exception as e:
        elapsed = time.monotonic() - start
        return {"type": "api", "status": 0, "elapsed": round(elapsed, 2),
                "endpoint": endpoint, "timed_out": False, "error": str(e)[:100]}


async def git_clone(idx, timeout_s, base_dir, use_token):
    url = TOKEN_REPO_URL if use_token else REPO_URL
    clone_dir = os.path.join(base_dir, f"clone_{idx}")
    start = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        "git", "clone", "--depth", "1", url, clone_dir,
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
        sd = b""
        try:
            sd = await proc.stderr.read()
        except Exception:
            pass
        st = sd.decode(errors="replace")[-200:]
        shutil.rmtree(clone_dir, ignore_errors=True)
        return {"type": "git", "success": False, "timed_out": True,
                "elapsed": elapsed, "exit_code": -1, "stderr_tail": st, "idx": idx}
    elapsed = round(time.monotonic() - start, 2)
    sd = b""
    try:
        sd = await proc.stderr.read()
    except Exception:
        pass
    st = sd.decode(errors="replace")[-200:]
    rc = proc.returncode or 0
    ok = rc == 0
    if not ok:
        shutil.rmtree(clone_dir, ignore_errors=True)
    return {"type": "git", "success": ok, "timed_out": False,
            "elapsed": elapsed, "exit_code": rc, "stderr_tail": st, "idx": idx}


async def run_api_round(concurrency, timeout_s, round_num, region, headers):
    connector = aiohttp.TCPConnector(
        limit=concurrency * len(ENDPOINTS),
        limit_per_host=concurrency * len(ENDPOINTS),
    )
    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = [api_fetch(session, ep, headers, timeout_s) for ep in ENDPOINTS] * concurrency
        total = len(tasks)
        print(f"\n{SEPARATOR}")
        print(f"  Round {round_num} [API/{region}] | concurrency={concurrency} | total={total} | timeout={timeout_s}s")
        print(SEPARATOR)
        wall = time.monotonic()
        results = await asyncio.gather(*tasks)
        wall_e = round(time.monotonic() - wall, 2)

        ok = sum(1 for r in results if r["status"] == 200)
        fail = sum(1 for r in results if r["status"] != 200)
        t504 = sum(1 for r in results if r["timed_out"])
        rate = sum(1 for r in results if r["status"] in (403, 429))
        avg = round(sum(r["elapsed"] for r in results) / len(results), 2)
        mx = round(max(r["elapsed"] for r in results), 2)

        print(f"  Wall: {wall_e}s | 200: {ok} | 403/429: {rate} | 504: {t504} | other: {fail-rate-t504} | Avg: {avg}s | Max: {mx}s")
        errs = [r for r in results if r["status"] != 200]
        if errs:
            print(f"  Errors ({len(errs)}, show first 10):")
            for r in errs[:10]:
                print(f"    {r['status']} | {r['elapsed']}s | {r['endpoint']} | {r.get('error','-')}")

        return {"round": round_num, "mode": "api", "region": region,
                "concurrency": concurrency, "timeout": timeout_s,
                "wall_time": wall_e, "ok": ok, "rate_limited": rate,
                "timed_out": t504, "other_errors": fail - rate - t504,
                "avg_elapsed": avg, "max_elapsed": mx, "results": results}


async def run_git_round(concurrency, timeout_s, round_num, region, use_token):
    base_dir = f"/tmp/token_test_r{round_num}_{region}"
    os.makedirs(base_dir, exist_ok=True)
    print(f"\n{SEPARATOR}")
    print(f"  Round {round_num} [GIT/{region}] | concurrency={concurrency} | timeout={timeout_s}s | token={use_token}")
    print(SEPARATOR)
    tasks = [git_clone(i, timeout_s, base_dir, use_token) for i in range(concurrency)]
    wall = time.monotonic()
    results = await asyncio.gather(*tasks)
    wall_e = round(time.monotonic() - wall, 2)
    shutil.rmtree(base_dir, ignore_errors=True)

    ok = sum(1 for r in results if r["success"])
    fail = sum(1 for r in results if not r["success"])
    t504 = sum(1 for r in results if r["timed_out"])
    avg = round(sum(r["elapsed"] for r in results) / len(results), 2)
    mx = round(max(r["elapsed"] for r in results), 2)

    print(f"  Wall: {wall_e}s | OK: {ok} | Failed: {fail} | 504: {t504} | Avg: {avg}s | Max: {mx}s")
    errs = [r for r in results if not r["success"]]
    if errs:
        print(f"  Failures ({len(errs)}, show first 10):")
        for r in errs[:10]:
            label = "TIMEOUT" if r["timed_out"] else "ERROR"
            print(f"    [{label}] idx={r['idx']} elapsed={r['elapsed']}s exit={r['exit_code']}")

    return {"round": round_num, "mode": "git", "region": region,
            "concurrency": concurrency, "timeout": timeout_s,
            "wall_time": wall_e, "ok": ok, "rate_limited": 0,
            "timed_out": t504, "other_errors": fail - t504,
            "avg_elapsed": avg, "max_elapsed": mx, "results": results}


async def run_region_test(region_label, headers):
    all_rounds = []

    # ── API rounds: token allows high concurrency ──
    api_configs = [
        (1,  20,  30),
        (2,  50,  20),
        (3, 100,  15),
        (4, 200,  10),
        (5, 300,   8),
    ]
    for rnd, conc, tout in api_configs:
        rd = await run_api_round(conc, tout, rnd, region_label, headers)
        all_rounds.append(rd)
        await asyncio.sleep(3)

    # ── Git clone rounds ──
    git_configs = [
        (6,   5,  30),
        (7,  10,  20),
        (8,  20,  15),
        (9,  30,  10),
        (10, 50,   8),
    ]
    for rnd, conc, tout in git_configs:
        rd = await run_git_round(conc, tout, rnd, region_label, True)
        all_rounds.append(rd)
        await asyncio.sleep(3)

    return all_rounds


async def main():
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "GitHub-504-TokenTest",
        "Authorization": f"Bearer {TOKEN}",
    }

    # ── Phase 1: HK ──
    print(f"\n{'#'*70}")
    print(f"#  PHASE 1: Hong Kong CDN ({resolved_ip}) — Authenticated")
    print(f"{'#'*70}")
    hk_rounds = await run_region_test("HK", headers)

    # ── Phase 2: Korea ──
    korea_ip = "20.200.245.247"
    hosts_path = "/etc/hosts"
    hosts_backup = hosts_path + ".bak_token_test"
    shutil.copy2(hosts_path, hosts_backup)
    with open(hosts_path, "a") as f:
        f.write(f"\n# TOKEN TEST — Korea node\n{korea_ip} github.com\n")

    # Also override api.github.com to Korea IP for API tests
    with open(hosts_path, "a") as f:
        f.write(f"\n{korea_ip} api.github.com\n")

    time.sleep(1)
    subprocess.run(["systemd-resolve", "--flush-caches"], capture_output=True, timeout=5)
    time.sleep(1)
    new_ip = socket.gethostbyname("github.com")
    new_api_ip = socket.gethostbyname("api.github.com")
    print(f"\n{'#'*70}")
    print(f"#  PHASE 2: Korea CDN ({korea_ip}) — Authenticated")
    print(f"#  github.com -> {new_ip}, api.github.com -> {new_api_ip}")
    print(f"{'#'*70}")

    kr_rounds = await run_region_test("KR", headers)

    # Restore hosts
    shutil.copy2(hosts_backup, hosts_path)
    os.remove(hosts_backup)
    print(f"\n  /etc/hosts restored")

    # ── Report ──
    print(f"\n{SEPARATOR}")
    print("  TOKEN-AUTHENTICATED COMPARISON REPORT — HK vs Korea")
    print(SEPARATOR)
    print(f"  Token: authenticated (5,000/hr API rate limit)")
    print(f"  HK:  {resolved_ip}")
    print(f"  KR:  {korea_ip}")
    print()

    hdr = f"  {'Rnd':>3} | {'Mode':>4} | {'Reg':>3} | {'Conc':>4} | {'Tout':>4} | {'Wall':>5} | {'200':>4} | {'429':>4} | {'504':>4} | {'Avg':>4} | {'Max':>4} | Flag"
    print(hdr)
    print(f"  {'-'*85}")

    for rd in hk_rounds + kr_rounds:
        flag = "504" if rd["timed_out"] > 0 else ("RL" if rd["rate_limited"] > 0 else "OK")
        m = rd["mode"].upper()
        r = rd["region"]
        print(f"  {rd['round']:>3} | {m:>4} | {r:>3} | {rd['concurrency']:>4} | {rd['timeout']:>4}s | {rd['wall_time']:>5}s | {rd['ok']:>4} | {rd['rate_limited']:>4} | {rd['timed_out']:>4} | {rd['avg_elapsed']:>4}s | {rd['max_elapsed']:>4}s | {flag}")

    # Save
    report = {"hk_ip": resolved_ip, "korea_ip": korea_ip,
              "token_authenticated": True, "hk_rounds": hk_rounds, "korea_rounds": kr_rounds}
    path = "/root/github-error-test/reports/token_auth_hk_vs_korea.json"
    with open(path, "w") as f:
        json.dump(report, f, indent=2, default=str)
    print(f"\n  JSON saved: {path}")


try:
    import aiohttp
except ImportError:
    print("Need aiohttp: pip install aiohttp")
    sys.exit(1)

asyncio.run(main())
