#!/usr/bin/env python3
"""
Task 2: 量化 SYNC_STALE_AFTER 对错误率的影响

假设：拉长 SYNC_STALE_AFTER 让绝大部分请求走 mirror-hit（不碰 upstream），
      抖动/504 触发窗口应大幅收敛

实验：
  两组代理配置，SYNC_STALE_AFTER=2s（激进 sync）和 600s（保守 sync）
  每组配合 tc netem 在 ens3 上加入 10% 丢包 + 500ms delay，
  连续发起 N 次 clone，统计成功率、平均耗时、504 数

由于两组要按顺序跑（都占用 :18080），先跑 2s 组，测完 kill 代理换配置再跑 600s 组。

输出：reports/task2_stale_after.json
"""
import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

PROXY_URL = "http://localhost:18080"
MIRROR_DIR = "/tmp/git-mirrors"
SGP_BIN = "/tmp/sgp/smart-git-proxy"
REPORTS = Path("/root/github-error-test/reports")
LOG_DIR = Path("/tmp/sgp-logs")
NETEM_IFACE = "ens3"  # 外部接口
REPO = "https://github.com/pre-commit/pre-commit-hooks.git"


def start_proxy(stale_after: str, log_name: str) -> int:
    logf = open(LOG_DIR / log_name, "w")
    p = subprocess.Popen(
        [SGP_BIN],
        env={
            **os.environ,
            "MIRROR_DIR": MIRROR_DIR,
            "LISTEN_ADDR": ":18080",
            "ALLOWED_UPSTREAMS": "github.com",
            "AUTH_MODE": "none",
            "SYNC_STALE_AFTER": stale_after,
            "LOG_LEVEL": "info",
        },
        stdout=logf, stderr=logf,
    )
    time.sleep(1.5)
    r = subprocess.run(["curl", "-sf", "http://localhost:18080/healthz"], capture_output=True)
    if r.returncode != 0:
        p.kill()
        raise RuntimeError("proxy failed to start")
    return p.pid


def stop_proxy(pid: int):
    try:
        os.kill(pid, signal.SIGTERM)
        time.sleep(1)
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def netem_add(iface: str, loss: str = "10%", delay: str = "500ms"):
    subprocess.run(["tc", "qdisc", "add", "dev", iface, "root", "netem",
                    "loss", loss, "delay", delay], check=True)
    print(f"  🌀 tc netem: {loss} loss + {delay} delay on {iface}")


def netem_del(iface: str):
    subprocess.run(["tc", "qdisc", "del", "dev", iface, "root"], capture_output=True)
    print(f"  ✅ tc netem removed on {iface}")


def one_clone(repo: str, dest: str, timeout: float) -> dict:
    shutil.rmtree(dest, ignore_errors=True)
    cmd = ["git", "-c",
           f"url.{PROXY_URL}/github.com/.insteadOf=https://github.com/",
           "clone", "--depth", "1", repo, dest]
    start = time.monotonic()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        stderr_tail = (p.stderr or "")[-200:]
        return {
            "success": p.returncode == 0,
            "elapsed": round(time.monotonic() - start, 2),
            "http_5xx": ("502" in stderr_tail) or ("503" in stderr_tail) or ("504" in stderr_tail),
            "stderr_tail": stderr_tail.strip(),
        }
    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "elapsed": round(time.monotonic() - start, 2),
            "http_5xx": False,
            "stderr_tail": f"CLIENT TIMEOUT {timeout}s",
        }
    finally:
        shutil.rmtree(dest, ignore_errors=True)


def run_scenario(name: str, stale_after: str, iterations: int, interval: float,
                 clone_timeout: float, loss: str, delay: str) -> dict:
    print(f"\n{'#'*68}")
    print(f"#  {name}: SYNC_STALE_AFTER={stale_after}, iter={iterations}, "
          f"loss={loss}, delay={delay}")
    print(f"{'#'*68}")

    # 冷启动一次预热 mirror
    log_name = f"task2_{name.replace(' ', '_').lower()}.log"
    pid = start_proxy(stale_after, log_name)
    print(f"  proxy pid={pid}, log={log_name}")

    # 预热
    print("  🔥 prewarming mirror ...")
    warm = one_clone(REPO, "/tmp/task2_warm", timeout=120.0)
    print(f"  warm result: success={warm['success']}, elapsed={warm['elapsed']}s")

    # 加抖动
    netem_del(NETEM_IFACE)
    netem_add(NETEM_IFACE, loss, delay)

    results = []
    try:
        for i in range(iterations):
            r = one_clone(REPO, f"/tmp/task2_iter{i}", timeout=clone_timeout)
            results.append(r)
            marker = "✅" if r["success"] else "❌"
            print(f"    iter {i+1}/{iterations}: {marker} elapsed={r['elapsed']}s "
                  f"{'[' + r['stderr_tail'][:80] + ']' if not r['success'] else ''}")
            if i < iterations - 1:
                time.sleep(interval)
    finally:
        netem_del(NETEM_IFACE)
        stop_proxy(pid)

    succ = sum(1 for r in results if r["success"])
    fail = iterations - succ
    http_5xx = sum(1 for r in results if r["http_5xx"])
    avg_elapsed = round(sum(r["elapsed"] for r in results) / iterations, 2)
    max_elapsed = round(max(r["elapsed"] for r in results), 2)

    print(f"\n  🎯 {name}: success={succ}/{iterations}, avg={avg_elapsed}s, "
          f"max={max_elapsed}s, 5xx={http_5xx}")

    return {
        "name": name,
        "config": {"stale_after": stale_after, "iterations": iterations,
                    "interval": interval, "clone_timeout": clone_timeout,
                    "netem_loss": loss, "netem_delay": delay},
        "success": succ, "failed": fail, "http_5xx": http_5xx,
        "avg_elapsed": avg_elapsed, "max_elapsed": max_elapsed,
        "iterations": results,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=20)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--clone-timeout", type=float, default=30.0)
    parser.add_argument("--loss", default="10%")
    parser.add_argument("--delay", default="500ms")
    args = parser.parse_args()

    LOG_DIR.mkdir(exist_ok=True)

    # 保证起点是无抖动
    netem_del(NETEM_IFACE)

    scenarios = []
    try:
        scenarios.append(run_scenario(
            "A_stale_2s", "2s", args.iterations, args.interval, args.clone_timeout,
            args.loss, args.delay,
        ))
        scenarios.append(run_scenario(
            "B_stale_600s", "600s", args.iterations, args.interval, args.clone_timeout,
            args.loss, args.delay,
        ))
    finally:
        netem_del(NETEM_IFACE)

    out = {
        "meta": {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "iface": NETEM_IFACE,
            "target_repo": REPO,
            "params": vars(args),
        },
        "scenarios": scenarios,
    }
    path = REPORTS / "task2_stale_after.json"
    path.write_text(json.dumps(out, indent=2, ensure_ascii=False))

    print(f"\n{'='*68}\nSUMMARY\n{'='*68}")
    print(f"{'scenario':<15}{'succ':<8}{'fail':<8}{'5xx':<7}{'avg':<10}{'max':<10}")
    for s in scenarios:
        print(f"{s['name']:<15}{s['success']:<8}{s['failed']:<8}"
              f"{s['http_5xx']:<7}{s['avg_elapsed']:<10}{s['max_elapsed']:<10}")
    print(f"\n📄 JSON: {path}")


if __name__ == "__main__":
    main()
