#!/usr/bin/env python3
"""
Task R2 + R3 + R4：性能 / 并发 / netem 抖动测试
—— 与 smart-git-proxy 的 proxy_comparison.py / proxy_high_concurrency.py / task2 场景对齐

R2 (perf):    小仓 pre-commit-hooks + 大仓 sglang 冷启 & warm clone 耗时对比
R3 (concur):  c=20 / c=50 并发 clone（sglang 大仓），带内存看门狗
R4 (netem):   tc netem 10% loss + 500ms delay 下的错误率
"""
import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

GITCDN_URL = "http://localhost:18000"
CONTAINER = "git-cdn-test"
WORKDIR = "/tmp/gitcdn-workdir"
REPORTS = Path("/root/github-error-test/reports")
NETEM_IFACE = "ens3"
GH_PAT = os.environ.get("GH_PAT")
if not GH_PAT:
    print("❌ GH_PAT env var required", file=sys.stderr)
    sys.exit(1)

SMALL = "pre-commit/pre-commit-hooks"
LARGE = "sgl-project/sglang"  # 1.8 GB


def build_url(repo: str) -> str:
    return f"http://x-access-token:{GH_PAT}@localhost:18000/{repo}.git"


def clear_mirror(repo: str):
    owner, name = repo.split("/")
    for d in [f"{WORKDIR}/git/{owner}/{name}.git",
              f"{WORKDIR}/bundles/{name}_clone.bundle",
              f"{WORKDIR}/bundles/{name}.lock"]:
        try:
            if os.path.isdir(d):
                shutil.rmtree(d)
            elif os.path.exists(d):
                os.remove(d)
        except Exception:
            pass


def scrub(s: str) -> str:
    return re.sub(r"ghp_[A-Za-z0-9]+", "ghp_XXX", s or "")


def read_mem_avail_mb() -> float:
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / 1024.0
    except Exception:
        pass
    return 0.0


def netem_add(loss: str, delay: str):
    subprocess.run(["tc", "qdisc", "del", "dev", NETEM_IFACE, "root"], capture_output=True)
    subprocess.run(["tc", "qdisc", "add", "dev", NETEM_IFACE, "root", "netem",
                    "loss", loss, "delay", delay], check=True)


def netem_del():
    subprocess.run(["tc", "qdisc", "del", "dev", NETEM_IFACE, "root"], capture_output=True)


async def clone_one(repo: str, dest: str, timeout: float) -> dict:
    shutil.rmtree(dest, ignore_errors=True)
    url = build_url(repo)
    start = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "clone", "--depth", "1", url, dest,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return {"success": False, "elapsed": round(time.monotonic() - start, 2),
                    "stderr_tail": f"CLIENT_TIMEOUT_{timeout}s"}
        stderr = (await proc.stderr.read()).decode(errors="replace")[-200:]
        return {"success": proc.returncode == 0,
                "elapsed": round(time.monotonic() - start, 2),
                "stderr_tail": scrub(stderr).strip()}
    finally:
        shutil.rmtree(dest, ignore_errors=True)


# ------------------ R2: perf ------------------

async def r2_perf() -> dict:
    print("\n### R2: perf ###")
    out = {"cold": {}, "warm": {}}
    for tag, repo in [("small_precommit_hooks", SMALL), ("large_sglang", LARGE)]:
        # cold
        clear_mirror(repo)
        r = await clone_one(repo, f"/tmp/gcdn_r2_cold_{tag}", timeout=600)
        out["cold"][tag] = r
        mirror_path = f"{WORKDIR}/git/{repo}.git"
        r["mirror_size"] = subprocess.run(
            ["du", "-sh", mirror_path], capture_output=True, text=True,
        ).stdout.split()[0] if os.path.exists(mirror_path) else "n/a"
        print(f"  cold {tag}: success={r['success']}, "
              f"elapsed={r['elapsed']}s, mirror={r['mirror_size']}")
        # warm: 立即再 clone 一次
        w = await clone_one(repo, f"/tmp/gcdn_r2_warm_{tag}", timeout=120)
        out["warm"][tag] = w
        print(f"  warm {tag}: success={w['success']}, elapsed={w['elapsed']}s")
    return out


# ------------------ R3: concurrent ------------------

async def r3_concurrent(concurrencies: list[int], min_avail_mb: float,
                        clone_timeout: float) -> list[dict]:
    print("\n### R3: concurrent (sglang 1.8GB, mirror warm) ###")
    # 先 warm sglang
    print("  warmup sglang ...")
    w = await clone_one(LARGE, "/tmp/gcdn_r3_warmup", timeout=600)
    print(f"  warmup: success={w['success']}, elapsed={w['elapsed']}s")

    out = []
    for c in concurrencies:
        # 内存看门狗 (在同个 event loop 里跑)
        print(f"\n  concurrency={c}, timeout={clone_timeout}s, floor={min_avail_mb}MB")
        samples = []
        aborted = {"flag": False, "reason": None}
        stop = asyncio.Event()

        async def watch():
            while not stop.is_set():
                a = read_mem_avail_mb()
                samples.append(a)
                if a < min_avail_mb:
                    aborted["flag"] = True
                    aborted["reason"] = f"MemAvail={a:.0f}MB<{min_avail_mb}MB"
                    subprocess.run(["pkill", "-9", "-f", "^git clone"], check=False)
                    return
                await asyncio.sleep(1.0)
        wt = asyncio.create_task(watch())

        tasks = [clone_one(LARGE, f"/tmp/gcdn_r3_c{c}_{i}", clone_timeout)
                 for i in range(c)]
        t0 = time.monotonic()
        results = await asyncio.gather(*tasks)
        wall = round(time.monotonic() - t0, 2)
        stop.set()
        await wt

        succ = sum(1 for r in results if r["success"])
        fail = c - succ
        avg = round(sum(r["elapsed"] for r in results) / c, 2)
        mx = round(max(r["elapsed"] for r in results), 2)
        out.append({
            "concurrency": c, "clone_timeout": clone_timeout,
            "success": succ, "failed": fail, "wall_time": wall,
            "avg_elapsed": avg, "max_elapsed": mx,
            "min_mem_avail_mb": round(min(samples), 1) if samples else None,
            "aborted": aborted["flag"], "abort_reason": aborted["reason"],
        })
        print(f"  ==> c={c}: {succ}/{c} success, avg={avg}s, max={mx}s, "
              f"wall={wall}s, min_avail={min(samples):.0f}MB, "
              f"abort={aborted['flag']}")
        if aborted["flag"]:
            print(f"  ⏹ aborted, skipping higher concurrencies")
            break
        await asyncio.sleep(5)
    return out


# ------------------ R4: netem ------------------

async def r4_netem(iterations: int, loss: str, delay: str,
                   clone_timeout: float) -> dict:
    print("\n### R4: netem loss/delay ###")
    # 先确保 mirror warm
    clear_mirror(SMALL)
    await clone_one(SMALL, "/tmp/gcdn_r4_warm", timeout=60)

    out = {}
    for tag, apply_netem in [("no_netem", False), ("with_netem", True)]:
        netem_del()
        if apply_netem:
            netem_add(loss, delay)
            print(f"  🌀 netem: loss={loss}, delay={delay}")
        results = []
        try:
            for i in range(iterations):
                r = await clone_one(SMALL, f"/tmp/gcdn_r4_{tag}_{i}", clone_timeout)
                results.append(r)
                marker = "✅" if r["success"] else "❌"
                print(f"    {marker} iter {i+1}/{iterations}: {r['elapsed']}s")
                await asyncio.sleep(0.5)
        finally:
            netem_del()
        succ = sum(1 for r in results if r["success"])
        out[tag] = {
            "iterations": iterations,
            "success": succ, "failed": iterations - succ,
            "avg_elapsed": round(sum(r["elapsed"] for r in results) / iterations, 2),
            "max_elapsed": round(max(r["elapsed"] for r in results), 2),
        }
        print(f"  ==> {tag}: {succ}/{iterations} success, "
              f"avg={out[tag]['avg_elapsed']}s")
    return out


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-r2", action="store_true")
    parser.add_argument("--skip-r3", action="store_true")
    parser.add_argument("--skip-r4", action="store_true")
    parser.add_argument("--concurrencies", default="20,50")
    parser.add_argument("--concurrent-timeout", type=float, default=180.0)
    parser.add_argument("--min-avail-mb", type=float, default=1500.0)
    parser.add_argument("--netem-iters", type=int, default=15)
    parser.add_argument("--netem-loss", default="10%")
    parser.add_argument("--netem-delay", default="500ms")
    args = parser.parse_args()

    REPORTS.mkdir(exist_ok=True)
    print(f"MemAvail at start: {read_mem_avail_mb():.0f}MB")

    out = {
        "meta": {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "gitcdn_url": GITCDN_URL,
            "small_repo": SMALL,
            "large_repo": LARGE,
        }
    }

    try:
        if not args.skip_r2:
            out["r2_perf"] = await r2_perf()
        if not args.skip_r3:
            c_list = [int(c) for c in args.concurrencies.split(",")]
            out["r3_concurrent"] = await r3_concurrent(
                c_list, args.min_avail_mb, args.concurrent_timeout,
            )
        if not args.skip_r4:
            out["r4_netem"] = await r4_netem(
                args.netem_iters, args.netem_loss, args.netem_delay,
                clone_timeout=60.0,
            )
    finally:
        netem_del()

    # Print concise summary
    print(f"\n{'='*68}\nSUMMARY\n{'='*68}")
    if "r2_perf" in out:
        for tag in ("small_precommit_hooks", "large_sglang"):
            c = out["r2_perf"]["cold"].get(tag, {})
            w = out["r2_perf"]["warm"].get(tag, {})
            print(f"  R2 {tag}: cold={c.get('elapsed')}s / warm={w.get('elapsed')}s / "
                  f"mirror={c.get('mirror_size')}")
    if "r3_concurrent" in out:
        for r in out["r3_concurrent"]:
            print(f"  R3 c={r['concurrency']}: {r['success']}/{r['concurrency']} "
                  f"avg={r['avg_elapsed']}s wall={r['wall_time']}s "
                  f"min_avail={r['min_mem_avail_mb']}MB abort={r['aborted']}")
    if "r4_netem" in out:
        for tag in ("no_netem", "with_netem"):
            r = out["r4_netem"][tag]
            print(f"  R4 {tag}: {r['success']}/{r['iterations']} avg={r['avg_elapsed']}s")

    path = REPORTS / "gitcdn_task_r234_perf.json"
    path.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    print(f"\n📄 JSON: {path}")


if __name__ == "__main__":
    asyncio.run(main())
