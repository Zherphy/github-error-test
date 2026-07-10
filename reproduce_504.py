#!/usr/bin/env python3
"""
GitHub 504 Gateway Timeout 复现脚本

三种模式复现 504：
  1. api    — 并发请求 GitHub REST API，逐步加压触发限流/超时
  2. git    — 并发 git clone，模拟多人同时拉取大仓库触发网络层超时
  3. proxy  — 用极短客户端超时模拟中间代理层超时（最容易复现 504）

用法:
  python3 reproduce_504.py --mode api                           # API 并发加压
  python3 reproduce_504.py --mode git --git-count 10            # 并发 git clone
  python3 reproduce_504.py --mode proxy --proxy-timeout 3       # 模拟代理超时(最易出 504)
  python3 reproduce_504.py --mode api --token ghp_xxxx          # 带 Token
  python3 reproduce_504.py --mode all                            # 三种模式依次运行
"""

import argparse
import asyncio
import os
import shutil
import subprocess
import tempfile
import time
from collections import Counter
from dataclasses import dataclass, field

try:
    import aiohttp
except ImportError:
    print("需要 aiohttp: pip install aiohttp")
    raise

# ── 配置 ──────────────────────────────────────────────

REPO_OWNER = "sgl-project"
REPO_NAME = "sglang"
REPO_URL = f"https://github.com/{REPO_OWNER}/{REPO_NAME}.git"
API_BASE = "https://api.github.com"

ENDPOINTS = [
    f"/repos/{REPO_OWNER}/{REPO_NAME}",
    f"/repos/{REPO_OWNER}/{REPO_NAME}/contents",
    f"/repos/{REPO_OWNER}/{REPO_NAME}/commits",
    f"/repos/{REPO_OWNER}/{REPO_NAME}/branches",
    f"/repos/{REPO_OWNER}/{REPO_NAME}/issues?state=all",
    f"/repos/{REPO_OWNER}/{REPO_NAME}/releases",
    f"/repos/{REPO_OWNER}/{REPO_NAME}/tags",
    f"/repos/{REPO_OWNER}/{REPO_NAME}/contributors",
    f"/repos/{REPO_OWNER}/{REPO_NAME}/stargazers",
    f"/repos/{REPO_OWNER}/{REPO_NAME}/forks",
]

STATUS_LABELS = {
    200: "OK",
    301: "Moved",
    401: "Unauthorized",
    403: "Forbidden/RateLimit",
    404: "Not Found",
    429: "Rate Limit Exceeded",
    502: "Bad Gateway",
    503: "Service Unavailable",
    504: "Gateway Timeout",
    0: "Connection Error",
}


# ── 数据结构 ──────────────────────────────────────────

@dataclass
class RequestResult:
    status: int
    elapsed: float
    endpoint: str
    error: str | None = None


@dataclass
class GitCloneResult:
    success: bool
    elapsed: float
    exit_code: int
    stderr_tail: str  # 最后 200 字符的 stderr
    clone_dir: str | None = None


@dataclass
class RoundReport:
    mode: str
    concurrency: int
    total_requests: int
    results: list[RequestResult | GitCloneResult] = field(default_factory=list)
    start_time: float = 0.0
    end_time: float = 0.0

    @property
    def duration(self) -> float:
        return self.end_time - self.start_time

    @property
    def status_counter(self) -> Counter:
        return Counter(r.status for r in self.results if isinstance(r, RequestResult))

    @property
    def git_stats(self) -> dict:
        git_results = [r for r in self.results if isinstance(r, GitCloneResult)]
        if not git_results:
            return {}
        return {
            "success": sum(1 for r in git_results if r.success),
            "failed": sum(1 for r in git_results if not r.success),
            "avg_elapsed": sum(r.elapsed for r in git_results) / len(git_results),
            "max_elapsed": max(r.elapsed for r in git_results),
        }


# ── 模式 1: API 并发加压 ──────────────────────────────

async def fetch_one(
    session: aiohttp.ClientSession,
    endpoint: str,
    headers: dict,
    client_timeout: float,
) -> RequestResult:
    url = f"{API_BASE}{endpoint}"
    start = time.monotonic()
    try:
        timeout = aiohttp.ClientTimeout(total=client_timeout)
        async with session.get(url, headers=headers, timeout=timeout) as resp:
            elapsed = time.monotonic() - start
            await resp.read()
            return RequestResult(
                status=resp.status,
                elapsed=elapsed,
                endpoint=endpoint,
            )
    except asyncio.TimeoutError:
        elapsed = time.monotonic() - start
        return RequestResult(
            status=504,
            elapsed=elapsed,
            endpoint=endpoint,
            error=f"client_timeout ({client_timeout}s)",
        )
    except aiohttp.ClientError as e:
        elapsed = time.monotonic() - start
        return RequestResult(
            status=0,
            elapsed=elapsed,
            endpoint=endpoint,
            error=str(e),
        )


async def run_api_round(
    concurrency: int,
    headers: dict,
    round_num: int,
    client_timeout: float = 30.0,
) -> RoundReport:
    """一轮 API 测试：并发请求所有 endpoint"""
    connector = aiohttp.TCPConnector(limit=concurrency * len(ENDPOINTS), limit_per_host=concurrency * len(ENDPOINTS))
    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = [
            fetch_one(session, ep, headers, client_timeout)
            for ep in ENDPOINTS
        ] * concurrency
        total = len(tasks)

        print(f"\n{'='*60}")
        print(f"  Round {round_num} [API] | concurrency={concurrency} | total={total} requests | timeout={client_timeout}s")
        print(f"{'='*60}")

        report = RoundReport(mode="api", concurrency=concurrency, total_requests=total)
        report.start_time = time.monotonic()
        results = await asyncio.gather(*tasks)
        report.end_time = time.monotonic()
        report.results = list(results)

    _print_api_summary(report)
    return report


def _print_api_summary(report: RoundReport):
    counter = report.status_counter
    print(f"\n  ⏱  Duration: {report.duration:.2f}s")
    elapsed_vals = [r.elapsed for r in report.results if isinstance(r, RequestResult)]
    if elapsed_vals:
        print(f"  📊 Avg elapsed: {sum(elapsed_vals)/len(elapsed_vals):.2f}s | Max: {max(elapsed_vals):.2f}s")
    print(f"  📋 Status distribution:")
    for status, count in sorted(counter.items()):
        label = STATUS_LABELS.get(status, "Unknown")
        marker = " ⚠️" if status in (429, 502, 503, 504, 0) else ""
        print(f"     {status} ({label}): {count}{marker}")

    errors = [r for r in report.results if isinstance(r, RequestResult) and r.status != 200]
    if errors:
        print(f"\n  🔴 Non-200 responses ({len(errors)}):")
        for r in errors[:20]:
            print(f"     {r.status} | {r.elapsed:.2f}s | {r.endpoint} | {r.error or '-'}")
        if len(errors) > 20:
            print(f"     ... and {len(errors) - 20} more")


# ── 模式 2: 并发 git clone ────────────────────────────

async def git_clone_one(
    repo_url: str,
    clone_dir: str,
    clone_timeout: float = 60.0,
) -> GitCloneResult:
    """执行一次 git clone，返回结果"""
    start = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "clone", "--depth", "1", repo_url, clone_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            await asyncio.wait_for(proc.wait(), timeout=clone_timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            elapsed = time.monotonic() - start
            stderr_tail = ""
            if proc.stderr:
                stderr_data = await proc.stderr.read()
                stderr_tail = stderr_data.decode(errors="replace")[-200:]
            return GitCloneResult(
                success=False,
                elapsed=elapsed,
                exit_code=-1,
                stderr_tail=f"TIMEOUT after {clone_timeout}s\n{stderr_tail}",
            )

        elapsed = time.monotonic() - start
        stderr_data = b""
        if proc.stderr:
            stderr_data = await proc.stderr.read()
        stderr_tail = stderr_data.decode(errors="replace")[-200:]

        success = proc.returncode == 0
        return GitCloneResult(
            success=success,
            elapsed=elapsed,
            exit_code=proc.returncode or 0,
            stderr_tail=stderr_tail,
            clone_dir=clone_dir if success else None,
        )
    except Exception as e:
        elapsed = time.monotonic() - start
        return GitCloneResult(
            success=False,
            elapsed=elapsed,
            exit_code=-1,
            stderr_tail=str(e),
        )


async def run_git_round(
    concurrency: int,
    round_num: int,
    clone_timeout: float = 60.0,
) -> RoundReport:
    """一轮 git clone 测试：并发 clone 同一仓库"""
    # 创建临时目录存放 clones
    base_dir = tempfile.mkdtemp(prefix=f"git_504_round{round_num}_")

    print(f"\n{'='*60}")
    print(f"  Round {round_num} [GIT] | concurrency={concurrency} | clone_timeout={clone_timeout}s")
    print(f"  📁 Cloning into: {base_dir}")
    print(f"{'='*60}")

    tasks = []
    for i in range(concurrency):
        clone_dir = os.path.join(base_dir, f"clone_{i}")
        tasks.append(git_clone_one(REPO_URL, clone_dir, clone_timeout))

    report = RoundReport(mode="git", concurrency=concurrency, total_requests=concurrency)
    report.start_time = time.monotonic()
    results = await asyncio.gather(*tasks)
    report.end_time = time.monotonic()
    report.results = list(results)

    # 清理 clone 目录
    try:
        shutil.rmtree(base_dir)
    except Exception:
        pass

    # 汇总
    stats = report.git_stats
    print(f"\n  ⏱  Duration: {report.duration:.2f}s")
    print(f"  ✅ Successful clones: {stats.get('success', 0)}")
    print(f"  ❌ Failed clones: {stats.get('failed', 0)}")
    if stats.get("avg_elapsed"):
        print(f"  📊 Avg elapsed: {stats['avg_elapsed']:.2f}s | Max: {stats['max_elapsed']:.2f}s")

    # 打印失败详情
    failed = [r for r in results if isinstance(r, GitCloneResult) and not r.success]
    if failed:
        print(f"\n  🔴 Failed clones ({len(failed)}):")
        for r in failed[:10]:
            print(f"     exit={r.exit_code} | {r.elapsed:.2f}s | {r.stderr_tail[:100]}")
        if len(failed) > 10:
            print(f"     ... and {len(failed) - 10} more")

        # 检查是否包含超时（模拟 504 场景）
        timeouts = [r for r in failed if "TIMEOUT" in r.stderr_tail or "timed out" in r.stderr_tail.lower()]
        if timeouts:
            print(f"\n  ⚠️  {len(timeouts)} clones timed out — this simulates a 504 Gateway Timeout scenario!")

    return report


# ── 模式 3: 模拟代理超时（极短客户端超时）──────────────

async def run_proxy_round(
    concurrency: int,
    headers: dict,
    round_num: int,
    proxy_timeout: float,
) -> RoundReport:
    """用极短超时模拟中间代理层超时 → 容易触发客户端侧 504"""
    print(f"\n{'='*60}")
    print(f"  Round {round_num} [PROXY-SIM] | concurrency={concurrency} | simulated_proxy_timeout={proxy_timeout}s")
    print(f"  💡 This mode sets a very short client timeout to simulate")
    print(f"     what happens when a gateway/proxy times out before GitHub responds.")
    print(f"{'='*60}")

    return await run_api_round(concurrency, headers, round_num, client_timeout=proxy_timeout)


# ── 主流程 ────────────────────────────────────────────

def _print_final_summary(all_reports: list[RoundReport]):
    print(f"\n{'='*60}")
    print("  FINAL SUMMARY")
    print(f"{'='*60}\n")

    for r in all_reports:
        if r.mode == "git":
            stats = r.git_stats
            flag = "🔴 TIMEOUT" if stats.get("failed", 0) > 0 else "🟢 OK"
            print(
                f"  Round | mode=GIT | concurrency={r.concurrency:3d} | "
                f"duration={r.duration:.2f}s | success={stats.get('success',0)} failed={stats.get('failed',0)} | {flag}"
            )
        else:
            counter = r.status_counter
            has_504 = 504 in counter
            has_429 = 429 in counter
            has_403 = 403 in counter
            flag = "🔴 504" if has_504 else ("🟡 429/403" if (has_429 or has_403) else "🟢 OK")
            print(
                f"  Round | mode={r.mode.upper():9s} | concurrency={r.concurrency:3d} | "
                f"duration={r.duration:.2f}s | {flag} | {dict(counter)}"
            )

    # 关键发现
    first_504 = next(
        (r for r in all_reports if (isinstance(res, RequestResult) and res.status == 504 for res in r.results) and any(isinstance(res, RequestResult) and res.status == 504 for res in r.results)), None
    )
    first_git_timeout = next(
        (r for r in all_reports if r.mode == "git" and any(isinstance(res, GitCloneResult) and not res.success for res in r.results)), None
    )

    print(f"\n  🏁 Key findings:")
    api_504_reports = [r for r in all_reports if r.mode != "git" and 504 in r.status_counter]
    if api_504_reports:
        print(f"     ✅ 504 Gateway Timeout detected at concurrency={api_504_reports[0].concurrency}")
    elif first_git_timeout:
        print(f"     ✅ Git clone timeout detected at concurrency={first_git_timeout.concurrency} (simulates 504)")
    else:
        rate_limited = [r for r in all_reports if r.mode != "git" and (403 in r.status_counter or 429 in r.status_counter)]
        if rate_limited:
            print(f"     🟡 GitHub rate-limited (403/429) before 504 appeared at concurrency={rate_limited[0].concurrency}")
            print(f"     → Tip: Use --mode proxy with very short timeout to simulate 504")
            print(f"     → Tip: Use --token to increase rate limit and push concurrency higher")
        else:
            print(f"     ❌ No 504, 429, or 403 detected")
            print(f"     → Tip: Increase --max-concurrency or try --mode proxy")


async def run_mode_api(args):
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "ARC-test-504-reproducer",
    }
    if args.token:
        headers["Authorization"] = f"Bearer {args.token}"
        print(f"\n🔑 Using GitHub Token (rate limit: 5,000/hr)")
    else:
        print(f"\n🔓 No Token — unauthenticated rate limit: 60/hr")

    print(f"\n🎯 Target repo: {REPO_OWNER}/{REPO_NAME}")
    print(f"📈 Concurrency: {args.start_concurrency} → {args.max_concurrency} (step={args.step})")
    print(f"🔄 Max rounds: {args.rounds}")

    all_reports: list[RoundReport] = []
    concurrency = args.start_concurrency
    round_num = 0

    while concurrency <= args.max_concurrency and round_num < args.rounds:
        round_num += 1
        report = await run_api_round(concurrency, headers, round_num)
        all_reports.append(report)

        if 504 in report.status_counter:
            print(f"\n  ✅ Detected 504 at concurrency={concurrency}!")

        concurrency += args.step
        if concurrency <= args.max_concurrency and round_num < args.rounds:
            print(f"\n  ⏳ Waiting {args.delay}s before next round...")
            await asyncio.sleep(args.delay)

    return all_reports


async def run_mode_git(args):
    print(f"\n🎯 Target repo: {REPO_URL}")
    print(f"📈 Git clone concurrency: {args.git_count}")
    print(f"🔄 Max rounds: {args.rounds}")
    print(f"⏱  Clone timeout: {args.git_timeout}s")

    # 确认 git 可用
    proc = await asyncio.create_subprocess_exec("git", "--version", stdout=asyncio.subprocess.PIPE)
    await proc.wait()
    print(f"  ✅ git available")

    all_reports: list[RoundReport] = []
    concurrency = args.git_count
    round_num = 0

    while round_num < args.rounds:
        round_num += 1
        report = await run_git_round(concurrency, round_num, args.git_timeout)
        all_reports.append(report)

        # 逐步加压
        concurrency = min(concurrency + args.step, args.max_concurrency)

        if round_num < args.rounds:
            print(f"\n  ⏳ Waiting {args.delay}s before next round...")
            await asyncio.sleep(args.delay)

    return all_reports


async def run_mode_proxy(args):
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "ARC-test-504-reproducer",
    }
    if args.token:
        headers["Authorization"] = f"Bearer {args.token}"

    proxy_timeout = args.proxy_timeout
    print(f"\n🎯 Target repo: {REPO_OWNER}/{REPO_NAME}")
    print(f"⚡ Simulated proxy timeout: {proxy_timeout}s")
    print(f"💡 This is the MOST EFFECTIVE way to reproduce 504 behavior")
    print(f"   A real proxy/gateway with a short read_timeout would return 504")
    print(f"   when GitHub's backend takes longer than {proxy_timeout}s to respond.")
    print(f"📈 Concurrency: {args.start_concurrency} → {args.max_concurrency} (step={args.step})")
    print(f"🔄 Max rounds: {args.rounds}")

    all_reports: list[RoundReport] = []
    concurrency = args.start_concurrency
    round_num = 0

    while concurrency <= args.max_concurrency and round_num < args.rounds:
        round_num += 1
        report = await run_proxy_round(concurrency, headers, round_num, proxy_timeout)
        all_reports.append(report)

        if 504 in report.status_counter:
            print(f"\n  ✅ Simulated 504 (proxy timeout) at concurrency={concurrency}!")

        concurrency += args.step
        if concurrency <= args.max_concurrency and round_num < args.rounds:
            print(f"\n  ⏳ Waiting {args.delay}s before next round...")
            await asyncio.sleep(args.delay)

    return all_reports


async def main(args):
    if args.mode == "all":
        all_reports: list[RoundReport] = []
        print("\n" + "="*60)
        print("  MODE 1: API Concurrent Pressure")
        print("="*60)
        all_reports.extend(await run_mode_api(args))

        print("\n" + "="*60)
        print("  MODE 2: Concurrent Git Clone")
        print("="*60)
        all_reports.extend(await run_mode_git(args))

        print("\n" + "="*60)
        print("  MODE 3: Simulated Proxy Timeout")
        print("="*60)
        all_reports.extend(await run_mode_proxy(args))

        _print_final_summary(all_reports)
    elif args.mode == "api":
        reports = await run_mode_api(args)
        _print_final_summary(reports)
    elif args.mode == "git":
        reports = await run_mode_git(args)
        _print_final_summary(reports)
    elif args.mode == "proxy":
        reports = await run_mode_proxy(args)
        _print_final_summary(reports)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Reproduce GitHub 504 Gateway Timeout",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # API 并发加压（默认模式）
  python3 reproduce_504.py --mode api --start-concurrency 10 --max-concurrency 50

  # 并发 git clone
  python3 reproduce_504.py --mode git --git-count 15

  # 模拟代理超时（最易复现 504）
  python3 reproduce_504.py --mode proxy --proxy-timeout 3

  # 三种模式依次运行
  python3 reproduce_504.py --mode all

  # 带 Token（提高限流阈值，可以打更高并发）
  python3 reproduce_504.py --mode api --token ghp_xxxxxxxx --max-concurrency 200
""",
    )

    parser.add_argument("--mode", choices=["api", "git", "proxy", "all"], default="api",
                        help="Test mode: api=API concurrent, git=concurrent clone, proxy=simulated proxy timeout, all=all three")
    parser.add_argument("--start-concurrency", type=int, default=10, help="Initial concurrency (default: 10)")
    parser.add_argument("--max-concurrency", type=int, default=80, help="Max concurrency (default: 80)")
    parser.add_argument("--step", type=int, default=10, help="Concurrency step per round (default: 10)")
    parser.add_argument("--rounds", type=int, default=6, help="Max rounds per mode (default: 6)")
    parser.add_argument("--token", type=str, default=None, help="GitHub personal access token")
    parser.add_argument("--delay", type=float, default=5.0, help="Delay between rounds (seconds)")

    # git mode options
    parser.add_argument("--git-count", type=int, default=10, help="Concurrent git clones per round (default: 10)")
    parser.add_argument("--git-timeout", type=float, default=30.0, help="Timeout per git clone (seconds, default: 30)")

    # proxy mode options
    parser.add_argument("--proxy-timeout", type=float, default=3.0,
                        help="Simulated proxy/gateway timeout in seconds (default: 3.0). Shorter = more 504s")

    args = parser.parse_args()
    asyncio.run(main(args))
