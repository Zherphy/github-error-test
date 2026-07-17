#!/usr/bin/env python3
"""
Task 1（重设计）: 验证 smart-git-proxy 的 serve-stale-on-error，并量化 upstream 失败模式对触发延迟的影响

关键洞察（首轮测试发现）：源码里 serve-stale 是有的，但 git-fetch 在 iptables DROP 场景下要 ~129s
才会失败，慢于绝大多数客户端超时——所以生产上"serve-stale"往往来不及触发。

现在的实验设计：
  Phase A: 无阻断，warm mirror，baseline
  Phase B1: iptables REJECT（快速 fail，模拟 TCP RST / ICMP unreachable）
           warm mirror + client timeout 60s → 期望 serve-stale 快速触发，客户端应成功
  Phase B2: iptables DROP（慢 fail，模拟静默丢包）
           warm mirror + client timeout 240s → 期望等 git-fetch 超时（~130s）后 serve-stale 才生效
           客户端 240s 内应最终成功；但 128s 内的普通客户端就会 fail
  Phase C: iptables REJECT + fresh repo（无 mirror）→ 期望失败（无 stale 可 serve）

自动清理 iptables（try/finally）
"""
import json
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

PROXY_URL = "http://localhost:18080"
LOG_PATH = "/tmp/sgp-logs/task1.log"
REPORTS = Path("/root/github-error-test/reports")
WARM_REPO = "https://github.com/pre-commit/pre-commit-hooks.git"
COLD_REPO_A = "https://github.com/pre-commit/action.git"  # 未预热
COLD_REPO_B = "https://github.com/pre-commit/pre-commit.git"  # 未预热（备用）


def resolve_github_ips() -> list[str]:
    result = subprocess.run(["getent", "ahostsv4", "github.com"], capture_output=True, text=True)
    return sorted({line.split()[0] for line in result.stdout.strip().split("\n") if line})


def iptables_action(action: str, ips: list[str], mode: str):
    """action: -I OUTPUT 1 或 -D OUTPUT ；mode: DROP 或 REJECT"""
    for ip in ips:
        cmd = ["iptables", action.split()[0]] + action.split()[1:] + [
            "-d", ip, "-p", "tcp", "--dport", "443", "-j", mode,
        ]
        if action.startswith("-D"):
            for _ in range(5):
                r = subprocess.run(cmd, capture_output=True)
                if r.returncode != 0:
                    break
        else:
            subprocess.run(cmd, check=True)


def do_clone(repo: str, dest: str, timeout: float) -> dict:
    shutil.rmtree(dest, ignore_errors=True)
    cmd = ["git",
           "-c", f"url.{PROXY_URL}/github.com/.insteadOf=https://github.com/",
           "clone", "--depth", "1", repo, dest]
    start = time.monotonic()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return {
            "success": p.returncode == 0,
            "elapsed": round(time.monotonic() - start, 2),
            "returncode": p.returncode,
            "stderr_tail": (p.stderr or "")[-300:].strip(),
        }
    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "elapsed": round(time.monotonic() - start, 2),
            "returncode": -1,
            "stderr_tail": f"TIMEOUT after {timeout}s (client-side)",
        }


def read_log_since(offset: int) -> tuple[str, int]:
    try:
        with open(LOG_PATH) as f:
            f.seek(offset)
            content = f.read()
            return content, offset + len(content)
    except FileNotFoundError:
        return "", offset


def find_serve_stale_delay_ms(log_slice: str) -> int | None:
    """从 log 里找 sync failed serving stale 事件里的 duration_ms"""
    import re
    for line in log_slice.split("\n"):
        if "sync failed" in line and "serving stale" in line:
            m = re.search(r'"duration_ms":(\d+)', line)
            if m:
                return int(m.group(1))
    return None


def wait_for_stale(seconds: float, msg: str):
    print(f"  ⏳ {msg}: waiting {seconds}s ...")
    time.sleep(seconds)


def phase(name: str, block_mode: str | None, repo: str, client_timeout: float,
          ips: list[str], warm_stale_wait: float = 0) -> dict:
    print(f"\n=== {name} ===")
    if block_mode:
        iptables_action("-I OUTPUT 1", ips, block_mode)
        print(f"  🚫 iptables {block_mode} to github.com:443")
    if warm_stale_wait > 0:
        wait_for_stale(warm_stale_wait, "wait to force stale check")

    log_off = Path(LOG_PATH).stat().st_size
    r = do_clone(repo, f"/tmp/task1_{name.split(':')[0]}", timeout=client_timeout)
    log_slice, _ = read_log_since(log_off)
    stale_ms = find_serve_stale_delay_ms(log_slice)

    print(f"  clone: success={r['success']}, elapsed={r['elapsed']}s")
    print(f"  serve-stale trigger delay (from proxy log): {stale_ms}ms")
    if not r["success"]:
        print(f"  stderr: {r['stderr_tail'][:200]}")

    if block_mode:
        iptables_action("-D OUTPUT", ips, block_mode)
    return {**r, "serve_stale_delay_ms": stale_ms, "log_slice_tail": log_slice[-800:]}


def main():
    ips = resolve_github_ips()
    if not ips:
        print("❌ Cannot resolve github.com; abort")
        sys.exit(1)
    print(f"github.com IPs to block: {ips}")

    results = {
        "meta": {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "proxy": PROXY_URL,
            "github_ips_blocked": ips,
            "note": "只阻断 tcp/443，不阻断其他，避免影响 dns/ssh 等",
        },
        "phases": {},
    }

    try:
        # A: baseline
        results["phases"]["A_baseline"] = phase(
            "A:baseline_no_block", None, WARM_REPO, 30.0, ips
        )

        # B1: REJECT + warm mirror + normal timeout
        # 让 SYNC_STALE_AFTER=30s 过掉
        results["phases"]["B1_reject_warm_60s"] = phase(
            "B1:REJECT_warm_client60s", "REJECT", WARM_REPO, 60.0, ips,
            warm_stale_wait=35,
        )

        # B2: DROP + warm mirror + long timeout（能等到 serve-stale 触发）
        results["phases"]["B2_drop_warm_240s"] = phase(
            "B2:DROP_warm_client240s", "DROP", WARM_REPO, 240.0, ips,
            warm_stale_wait=35,
        )

        # C: REJECT + fresh repo (no mirror)
        results["phases"]["C_reject_fresh"] = phase(
            "C:REJECT_no_mirror", "REJECT", COLD_REPO_A, 60.0, ips,
        )
    finally:
        # 双保险清理
        for mode in ("REJECT", "DROP"):
            iptables_action("-D OUTPUT", ips, mode)
        for d in ["A:baseline_no_block", "B1:REJECT_warm_client60s",
                  "B2:DROP_warm_client240s", "C:REJECT_no_mirror"]:
            shutil.rmtree(f"/tmp/task1_{d.split(':')[0]}", ignore_errors=True)

    # === Summary ===
    print("\n" + "=" * 62)
    print("SUMMARY")
    print("=" * 62)
    for name, r in results["phases"].items():
        icon = "✅" if r["success"] else "❌"
        stale = f"stale@{r['serve_stale_delay_ms']}ms" if r["serve_stale_delay_ms"] else "no-stale-event"
        print(f"  {icon} {name:<28} elapsed={r['elapsed']}s  {stale}")

    b1 = results["phases"]["B1_reject_warm_60s"]
    b2 = results["phases"]["B2_drop_warm_240s"]
    c = results["phases"]["C_reject_fresh"]

    verdict = {
        "serve_stale_fast_when_upstream_rejects": b1["success"] and (b1["serve_stale_delay_ms"] or 0) < 5000,
        "serve_stale_slow_when_upstream_drops": b2["success"] and (b2["serve_stale_delay_ms"] or 0) > 60000,
        "first_clone_fails_without_stale": not c["success"],
    }
    results["verdict"] = verdict
    print("\nVerdict:")
    print(f"  REJECT 模式下 serve-stale 秒级触发: {'✅' if verdict['serve_stale_fast_when_upstream_rejects'] else '❌'}"
          f"  (delay={b1.get('serve_stale_delay_ms')}ms)")
    print(f"  DROP 模式下 serve-stale 要等 ~130s: {'✅' if verdict['serve_stale_slow_when_upstream_drops'] else '❌'}"
          f"  (delay={b2.get('serve_stale_delay_ms')}ms)")
    print(f"  无 mirror 情况客户端必失败: {'✅' if verdict['first_clone_fails_without_stale'] else '❌'}")

    out = REPORTS / "task1_serve_stale.json"
    out.write_text(json.dumps(results, indent=2, ensure_ascii=False))
    print(f"\n📄 JSON: {out}")


if __name__ == "__main__":
    main()
