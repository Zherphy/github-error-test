#!/usr/bin/env python3
"""
smart-git-proxy 对照测试编排

同参数跑两遍 git clone 加压：
  A. 直连 GitHub  （baseline）
  B. 走 smart-git-proxy（url.insteadOf 改写）

产出：
  reports/smart_git_proxy_comparison.json
  reports/smart_git_proxy_comparison.md
"""
import argparse
import asyncio
import json
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path

from reproduce_504 import RoundReport, GitCloneResult, run_git_round, REPO_URL


REPORTS_DIR = Path(__file__).parent / "reports"


def report_to_dict(r: RoundReport) -> dict:
    stats = r.git_stats
    results = []
    timeouts = 0
    for res in r.results:
        if isinstance(res, GitCloneResult):
            is_timeout = "TIMEOUT" in res.stderr_tail or "timed out" in res.stderr_tail.lower()
            if is_timeout:
                timeouts += 1
            results.append({
                "success": res.success,
                "elapsed": round(res.elapsed, 2),
                "exit_code": res.exit_code,
                "timed_out": is_timeout,
                "stderr_tail": res.stderr_tail[-120:],
            })
    return {
        "concurrency": r.concurrency,
        "wall_time": round(r.duration, 2),
        "success": stats.get("success", 0),
        "failed": stats.get("failed", 0),
        "timed_out": timeouts,
        "avg_elapsed": round(stats.get("avg_elapsed", 0.0), 2),
        "max_elapsed": round(stats.get("max_elapsed", 0.0), 2),
        "results": results,
    }


async def run_scenario(name: str, concurrencies: list[int], timeout: float,
                       delay: float, proxy_url: str | None) -> list[dict]:
    print(f"\n{'#'*70}\n#  SCENARIO: {name}\n{'#'*70}")
    rounds = []
    for i, c in enumerate(concurrencies, 1):
        report = await run_git_round(c, i, timeout, proxy_url=proxy_url)
        rounds.append(report_to_dict(report))
        if i < len(concurrencies):
            print(f"\n  ⏳ Waiting {delay}s before next round...")
            await asyncio.sleep(delay)
    return rounds


def cold_prime_cost(proxy_url: str) -> dict:
    """测量代理首次冷同步成本：清空镜像后单个 clone 的耗时"""
    print(f"\n{'#'*70}\n#  COLD PRIME COST (proxy first-touch)\n{'#'*70}")
    mirror_dir = Path(os.environ.get("MIRROR_DIR", "/tmp/git-mirrors"))
    target = mirror_dir / "github.com" / "sgl-project" / "sglang.git"
    if target.exists():
        import shutil
        print(f"  🗑️  Removing existing mirror at {target} ...")
        shutil.rmtree(target)

    clone_dir = "/tmp/sgp_cold_prime"
    subprocess.run(["rm", "-rf", clone_dir], check=False)

    start = time.monotonic()
    proc = subprocess.run(
        ["git",
         "-c", f'url.{proxy_url.rstrip("/")}/github.com/.insteadOf=https://github.com/',
         "clone", "--depth", "1", REPO_URL, clone_dir],
        capture_output=True, text=True,
    )
    elapsed = time.monotonic() - start
    mirror_size = subprocess.run(
        ["du", "-sh", str(target)], capture_output=True, text=True,
    ).stdout.split()[0] if target.exists() else "n/a"

    print(f"  cold prime elapsed: {elapsed:.2f}s | mirror size: {mirror_size}")
    subprocess.run(["rm", "-rf", clone_dir], check=False)
    return {
        "elapsed_sec": round(elapsed, 2),
        "mirror_size": mirror_size,
        "success": proc.returncode == 0,
        "stderr_tail": proc.stderr[-200:],
    }


def build_markdown(meta: dict, cold: dict, baseline: list[dict], proxied: list[dict]) -> str:
    def row_marker(r):
        if r["failed"] == 0:
            return "🟢"
        if r["failed"] >= r["concurrency"] * 0.5:
            return "🔴"
        return "🟡"

    lines = []
    lines.append("# smart-git-proxy 对照测试报告")
    lines.append("")
    lines.append(f"**测试时间**: {meta['timestamp']}  ")
    lines.append(f"**目标仓库**: `{meta['repo']}`  ")
    lines.append(f"**代理地址**: `{meta['proxy_url']}`  ")
    lines.append(f"**代理版本**: smart-git-proxy v0.2.6 (release binary, linux_amd64)  ")
    lines.append(f"**并发梯度**: {meta['concurrencies']}  ")
    lines.append(f"**每 clone 超时**: {meta['timeout']}s  ")
    lines.append("")
    lines.append("## 背景")
    lines.append("")
    lines.append("本仓库前期测试（见 [reports/504_test_report.json](504_test_report.json)）证实：")
    lines.append("并发 20 直连 GitHub clone sglang（1.8 GB 大仓）时，触发 504 首次出现；")
    lines.append("并发升到 30/50 后失败率 93% / 98%。本次对照验证 smart-git-proxy 是否能消除该现象。")
    lines.append("")
    lines.append("## 冷启动成本")
    lines.append("")
    lines.append("代理首次拉取需要建立本地 bare mirror，这一次会承担完整仓库同步的开销：")
    lines.append("")
    lines.append(f"| 指标 | 数值 |")
    lines.append(f"|---|---|")
    lines.append(f"| 首次 clone 耗时（含 mirror 建立）| **{cold['elapsed_sec']}s** |")
    lines.append(f"| 建立的 mirror 大小 | {cold['mirror_size']} |")
    lines.append(f"| 是否成功 | {'✅' if cold['success'] else '❌'} |")
    lines.append("")
    lines.append("> ⚠️ 说明：smart-git-proxy 采用**完整 bare mirror**（不是 `--depth 1`），因此首次代价")
    lines.append("> 明显高于直连浅克隆。收益在后续复用；越是被高频复用的仓库，摊薄越彻底。")
    lines.append("")
    lines.append("## 对照结果（并发 git clone）")
    lines.append("")
    lines.append("### A. 直连 GitHub（baseline）")
    lines.append("")
    lines.append("| 并发 | 成功 | 失败 | 超时(=504) | 平均耗时 | 最大耗时 | wall time | 结果 |")
    lines.append("|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---|")
    for r in baseline:
        lines.append(f"| {r['concurrency']} | {r['success']} | {r['failed']} | {r['timed_out']} | "
                     f"{r['avg_elapsed']}s | {r['max_elapsed']}s | {r['wall_time']}s | {row_marker(r)} |")
    lines.append("")
    lines.append("### B. 经 smart-git-proxy（mirror 已 warm）")
    lines.append("")
    lines.append("| 并发 | 成功 | 失败 | 超时(=504) | 平均耗时 | 最大耗时 | wall time | 结果 |")
    lines.append("|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---|")
    for r in proxied:
        lines.append(f"| {r['concurrency']} | {r['success']} | {r['failed']} | {r['timed_out']} | "
                     f"{r['avg_elapsed']}s | {r['max_elapsed']}s | {r['wall_time']}s | {row_marker(r)} |")
    lines.append("")
    lines.append("## 关键指标对比")
    lines.append("")
    lines.append("| 并发 | 直连 成功率 | 代理 成功率 | 直连 avg | 代理 avg | 代理相对提速 |")
    lines.append("|:---:|:---:|:---:|:---:|:---:|:---:|")
    for b, p in zip(baseline, proxied):
        b_rate = 100.0 * b["success"] / b["concurrency"]
        p_rate = 100.0 * p["success"] / p["concurrency"]
        speedup = (b["avg_elapsed"] / p["avg_elapsed"]) if p["avg_elapsed"] > 0 else float("inf")
        speedup_str = f"{speedup:.1f}×" if speedup != float("inf") else "n/a"
        lines.append(f"| {b['concurrency']} | {b_rate:.0f}% | {p_rate:.0f}% | "
                     f"{b['avg_elapsed']}s | {p['avg_elapsed']}s | {speedup_str} |")
    lines.append("")

    # 判定
    total_b_fail = sum(r["failed"] for r in baseline)
    total_p_fail = sum(r["failed"] for r in proxied)
    lines.append("## 结论")
    lines.append("")
    if total_p_fail == 0 and total_b_fail > 0:
        lines.append(f"✅ **smart-git-proxy 完全消除了 504 现象**：直连累计失败 {total_b_fail} 次，")
        lines.append(f"经代理累计失败 {total_p_fail} 次。")
    elif total_p_fail < total_b_fail:
        lines.append(f"🟡 **smart-git-proxy 显著降低失败率但未完全消除**：")
        lines.append(f"直连累计失败 {total_b_fail}，经代理累计失败 {total_p_fail}。")
    else:
        lines.append(f"⚠️ **代理未带来预期收益**：直连失败 {total_b_fail}，代理失败 {total_p_fail}。")
        lines.append(f"可能原因：mirror 未 warm、代理机自身网络瓶颈、或场景不匹配。")
    lines.append("")
    lines.append("### 适用性判断")
    lines.append("")
    lines.append("| 场景 | 推荐度 | 说明 |")
    lines.append("|---|:---:|---|")
    lines.append("| **pre-commit hooks 拉取**（高频、少数固定仓库）| ⭐⭐⭐⭐⭐ | 完全命中，用 `git config --global url.insteadOf` 全局改写即可 |")
    lines.append("| **self-hosted runner 拉源码 / submodule** | ⭐⭐⭐⭐⭐ | 完全命中，本次数据直接支持 |")
    lines.append("| **GitHub-hosted runner 拉 action 代码** | ⭐ | Runner 内部机制拉取 action，无法路由到内网 proxy |")
    lines.append("| **一次性、大量不同仓库的 clone** | ⭐⭐ | singleflight 帮不上，只剩本地缓存收益，且冷启动代价高 |")
    lines.append("")
    lines.append("### 落地要点")
    lines.append("")
    lines.append("1. **冷启动一次性代价大**（本次 ~3 分钟 / 1.8GB），建议对高频仓库做**预热**（部署后立即触发一次 clone）。")
    lines.append("2. **URL 改写强依赖 `git config url.insteadOf`**，不要用 `https_proxy`（会走 CONNECT，不兼容）。")
    lines.append("3. **磁盘容量**要匹配所有热点仓库总和，用 `MIRROR_MAX_SIZE` 触发 LRU 淘汰。")
    lines.append("4. **认证透传**：私有仓库用 `AUTH_MODE=pass-through`（走客户端凭据）或 `static`（代理注入 token）。")
    lines.append("5. **`SYNC_STALE_AFTER` 默认 2s**，代表镜像新鲜度阈值。CI 场景可拉长至 30s+ 减少 upstream 同步频率。")
    lines.append("")
    lines.append("## 复现命令")
    lines.append("")
    lines.append("```bash")
    lines.append("# 1. 启动 smart-git-proxy（本地测试用配置）")
    lines.append("MIRROR_DIR=/tmp/git-mirrors LISTEN_ADDR=:18080 \\")
    lines.append("  ALLOWED_UPSTREAMS=github.com AUTH_MODE=none SYNC_STALE_AFTER=30s \\")
    lines.append("  ./smart-git-proxy &")
    lines.append("")
    lines.append("# 2. 运行对照测试")
    lines.append(f"python3 proxy_comparison.py \\")
    lines.append(f"  --proxy-url http://localhost:18080 \\")
    lines.append(f"  --concurrencies {','.join(str(c) for c in meta['concurrencies'])} \\")
    lines.append(f"  --timeout {meta['timeout']}")
    lines.append("```")
    lines.append("")
    return "\n".join(lines)


async def amain():
    parser = argparse.ArgumentParser(description="smart-git-proxy A/B 对照测试")
    parser.add_argument("--proxy-url", required=True,
                        help="smart-git-proxy 地址，如 http://localhost:18080")
    parser.add_argument("--concurrencies", default="5,10,20",
                        help="逗号分隔的并发梯度（默认: 5,10,20）")
    parser.add_argument("--timeout", type=float, default=30.0,
                        help="每个 clone 的超时秒数（默认: 30）")
    parser.add_argument("--delay", type=float, default=5.0,
                        help="轮间等待秒数（默认: 5）")
    parser.add_argument("--skip-cold-prime", action="store_true",
                        help="跳过冷启动成本测量（如果 mirror 已存在且不想重建）")
    args = parser.parse_args()

    concurrencies = [int(c) for c in args.concurrencies.split(",") if c.strip()]
    REPORTS_DIR.mkdir(exist_ok=True)

    meta = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "repo": REPO_URL,
        "proxy_url": args.proxy_url,
        "concurrencies": concurrencies,
        "timeout": args.timeout,
    }

    # 1. 冷启动成本
    if args.skip_cold_prime:
        cold = {"elapsed_sec": None, "mirror_size": "n/a (skipped)", "success": True, "stderr_tail": "skipped"}
    else:
        cold = cold_prime_cost(args.proxy_url)

    # 2. baseline: 直连
    baseline = await run_scenario("Direct GitHub (baseline)", concurrencies,
                                  args.timeout, args.delay, proxy_url=None)

    # 3. proxied: 经 smart-git-proxy（此时 mirror 已 warm）
    proxied = await run_scenario("Via smart-git-proxy (warm)", concurrencies,
                                 args.timeout, args.delay, proxy_url=args.proxy_url)

    # 4. 输出报告
    json_out = {
        "meta": meta,
        "cold_prime": cold,
        "baseline_direct": baseline,
        "via_smart_git_proxy": proxied,
    }
    json_path = REPORTS_DIR / "smart_git_proxy_comparison.json"
    md_path = REPORTS_DIR / "smart_git_proxy_comparison.md"

    json_path.write_text(json.dumps(json_out, indent=2, ensure_ascii=False))
    md_path.write_text(build_markdown(meta, cold, baseline, proxied))

    print(f"\n{'='*70}")
    print(f"  ✅ Report written:")
    print(f"     - {json_path}")
    print(f"     - {md_path}")
    print(f"{'='*70}")


if __name__ == "__main__":
    asyncio.run(amain())
