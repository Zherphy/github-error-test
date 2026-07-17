#!/usr/bin/env python3
"""
Task R1: git-cdn 内建重试机制的验证
—— 与 smart-git-proxy Task 1 / Task 5 场景完全对齐，方便对比

git-cdn 源码里的两层重试（无需自己打补丁）：
  A) ClientSessionWithRetry (client_session.py): 10 次，backoff 0.1s→51.2s（总 ~100s+）
  B) repo_cache.fetch/clone: 5 次 git 子命令级重试（默认 BACKOFF_START=0.5, COUNT=5 → 0.5/1/2/4/8s）

场景（每次跑前清 mirror 保证是首次 clone）：
  Phase 1: baseline，无 upstream 阻断 → 期望成功
  Phase 2: iptables REJECT 全程阻断，客户端 30s 超时 → 期望客户端超时；观察 git-cdn 是否在重试
  Phase 3: iptables REJECT 阻断 5s 后放开，客户端 60s 超时 → 期望 git-cdn 内建 retry 吸收阻断，客户端成功
  Phase 4: iptables REJECT 阻断 15s 后放开，客户端 60s 超时 → 期望 retry 内建 backoff（5×指数）能覆盖
  Phase 5: iptables DROP（静默丢包），客户端 45s 超时 → 期望 git-cdn 端到端连接超时 + retry；对比 smart-git-proxy 的 130s 慢失败
"""
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
GH_PAT = os.environ.get("GH_PAT")
if not GH_PAT:
    print("❌ GH_PAT env var not set", file=sys.stderr)
    sys.exit(1)

# 每 phase 用不同仓库以确保是"首次 clone"路径
# 都选小仓库控制 warm-clone 耗时；PyCQA/ruff 已改名到 astral-sh/ruff
REPOS = {
    "1_baseline":        "pre-commit/pre-commit-hooks",
    "2_full_block_30s":  "psf/black",
    "3_reject_5s_60t":   "astral-sh/ruff",
    "4_reject_15s_60t":  "PyCQA/isort",
    "5_drop_45s":        "PyCQA/flake8",
}


def gh_ip() -> str:
    r = subprocess.run(["getent", "ahostsv4", "github.com"], capture_output=True, text=True)
    return r.stdout.split()[0]


def iptables_apply(action: str, ip: str, mode: str):
    """action: -I DOCKER-USER 1 or -D DOCKER-USER
    DOCKER-USER chain is used because git-cdn runs in a container; OUTPUT
    chain wouldn't affect container egress traffic.
    """
    parts = action.split()
    for _ in range(3):
        r = subprocess.run(["iptables"] + parts + ["-d", ip, "-p", "tcp",
                                                    "--dport", "443", "-j", mode],
                           capture_output=True)
        if parts[0] == "-D" and r.returncode != 0:
            break


def clear_mirror(repo: str):
    """清 mirror + bundle + auth_cache，保证是首次 clone"""
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


def container_log_offset() -> int:
    """记录当前 log 长度（byte offset），用于取新增部分"""
    r = subprocess.run(["docker", "logs", CONTAINER], capture_output=True)
    return len(r.stdout) + len(r.stderr)


def container_log_slice(from_offset: int) -> str:
    r = subprocess.run(["docker", "logs", CONTAINER], capture_output=True, text=True)
    combined = (r.stdout or "") + (r.stderr or "")
    return combined[from_offset:]


def scan_retry_events(log_slice: str) -> dict:
    """从 log 里提取重试相关事件"""
    events = {
        "http_retry_count": 0,
        "clone_retry_count": 0,
        "fetch_retry_count": 0,
        "connection_errors": 0,
        "final_response_statuses": [],
    }
    for line in log_slice.split("\n"):
        # 剥掉 ANSI 转义
        line = re.sub(r"\x1b\[[0-9;]*m", "", line)
        if "upstream wrong return, retry" in line:
            events["http_retry_count"] += 1
        if "Client connection error" in line:
            events["connection_errors"] += 1
        if "clone failed, trying again" in line:
            events["clone_retry_count"] += 1
        if "fetch failed, trying again" in line:
            events["fetch_retry_count"] += 1
        m = re.search(r"response_status=(\d+)", line)
        if m:
            events["final_response_statuses"].append(int(m.group(1)))
    return events


def do_clone(repo: str, dest: str, timeout: float) -> dict:
    shutil.rmtree(dest, ignore_errors=True)
    url = f"http://x-access-token:{GH_PAT}@localhost:18000/{repo}.git"
    start = time.monotonic()
    try:
        p = subprocess.run(
            ["git", "clone", "--depth", "1", url, dest],
            capture_output=True, text=True, timeout=timeout,
        )
        return {
            "success": p.returncode == 0,
            "elapsed": round(time.monotonic() - start, 2),
            "returncode": p.returncode,
            # 剥掉 token
            "stderr_tail": re.sub(r"ghp_[A-Za-z0-9]+", "ghp_XXX", (p.stderr or "")[-200:]).strip(),
        }
    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "elapsed": round(time.monotonic() - start, 2),
            "returncode": -1,
            "stderr_tail": f"CLIENT_TIMEOUT_{timeout}s",
        }
    finally:
        shutil.rmtree(dest, ignore_errors=True)


def run_phase(name: str, repo: str, mode_delay_seq: list, client_timeout: float,
              ip: str) -> dict:
    """
    mode_delay_seq: [("REJECT", 5.0), ("clear", 0)]
      表示先 REJECT 5s 后清除；或 [("REJECT", 999)] 表示全程 REJECT
    """
    print(f"\n{'='*68}\n Phase {name}: repo={repo}, timeout={client_timeout}s, "
          f"seq={mode_delay_seq}\n{'='*68}")
    clear_mirror(repo)
    log_off = container_log_offset()

    # 后台按 seq 施加/清除 iptables
    import threading
    events = []
    def apply_seq():
        for i, (mode, delay) in enumerate(mode_delay_seq):
            events.append((time.monotonic(), f"seq[{i}]:{mode}"))
            if mode == "clear":
                iptables_apply("-D DOCKER-USER", ip, "REJECT")
                iptables_apply("-D DOCKER-USER", ip, "DROP")
            else:
                iptables_apply("-I DOCKER-USER 1", ip, mode)
            if delay > 0:
                time.sleep(delay)
    t = threading.Thread(target=apply_seq)
    t.start()
    # 等 iptables 生效一下再打请求
    time.sleep(0.2)
    r = do_clone(repo, f"/tmp/gitcdn_r1_{name}", timeout=client_timeout)
    t.join(timeout=max(60, client_timeout))
    # 兜底清 iptables
    iptables_apply("-D DOCKER-USER", ip, "REJECT")
    iptables_apply("-D DOCKER-USER", ip, "DROP")

    log_slice = container_log_slice(log_off)
    retry_events = scan_retry_events(log_slice)
    # 脱敏
    log_slice_scrubbed = re.sub(r"ghp_[A-Za-z0-9]+", "ghp_XXX", log_slice)

    print(f"  client: success={r['success']}, elapsed={r['elapsed']}s, "
          f"stderr={r['stderr_tail'][:100]}")
    print(f"  retry events: http={retry_events['http_retry_count']}, "
          f"clone={retry_events['clone_retry_count']}, "
          f"fetch={retry_events['fetch_retry_count']}, "
          f"conn_err={retry_events['connection_errors']}")
    return {
        "phase": name, "repo": repo, "config_seq": mode_delay_seq,
        "client_timeout": client_timeout,
        **r,
        "retry_events": retry_events,
        "log_tail_scrubbed": log_slice_scrubbed[-1200:],
    }


def main():
    REPORTS.mkdir(exist_ok=True)
    ip = gh_ip()
    print(f"github.com IP: {ip}")

    # 兜底
    iptables_apply("-D DOCKER-USER", ip, "REJECT")
    iptables_apply("-D DOCKER-USER", ip, "DROP")

    results = {
        "meta": {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "gitcdn_container": CONTAINER,
            "gitcdn_url": GITCDN_URL,
            "github_ip": ip,
            "retry_env": {
                "REQUEST_MAX_RETRIES": os.environ.get("REQUEST_MAX_RETRIES", "container-default"),
                "BACKOFF_START": os.environ.get("BACKOFF_START", "container-default"),
                "BACKOFF_COUNT": os.environ.get("BACKOFF_COUNT", "container-default"),
            },
        },
        "phases": [],
    }

    try:
        # Phase 1: baseline
        results["phases"].append(run_phase(
            "1_baseline", REPOS["1_baseline"],
            [("clear", 30.0)], 30.0, ip,
        ))
        time.sleep(2)

        # Phase 2: 全程 REJECT, 30s 客户端超时（应超时）
        results["phases"].append(run_phase(
            "2_full_block_30s", REPOS["2_full_block_30s"],
            [("REJECT", 40.0)], 30.0, ip,
        ))
        time.sleep(2)

        # Phase 3: REJECT 5s 后放开, 60s 超时（期望内建 retry 吸收）
        results["phases"].append(run_phase(
            "3_reject_5s_60t", REPOS["3_reject_5s_60t"],
            [("REJECT", 5.0), ("clear", 0)], 60.0, ip,
        ))
        time.sleep(2)

        # Phase 4: REJECT 15s 后放开, 60s 超时（考察是否 backoff 序列能等到）
        results["phases"].append(run_phase(
            "4_reject_15s_60t", REPOS["4_reject_15s_60t"],
            [("REJECT", 15.0), ("clear", 0)], 60.0, ip,
        ))
        time.sleep(2)

        # Phase 5: DROP 全程, 45s 客户端超时 —— smart-git-proxy 场景是 130s 才失败
        results["phases"].append(run_phase(
            "5_drop_45s", REPOS["5_drop_45s"],
            [("DROP", 60.0)], 45.0, ip,
        ))
    finally:
        iptables_apply("-D DOCKER-USER", ip, "REJECT")
        iptables_apply("-D DOCKER-USER", ip, "DROP")

    # Summary
    print(f"\n{'='*68}\nSUMMARY\n{'='*68}")
    for p in results["phases"]:
        icon = "✅" if p["success"] else "❌"
        print(f"  {icon} {p['phase']:<22} elapsed={p['elapsed']}s  "
              f"retries: http={p['retry_events']['http_retry_count']} "
              f"clone={p['retry_events']['clone_retry_count']} "
              f"conn_err={p['retry_events']['connection_errors']}")

    # 判定
    ph = {p["phase"]: p for p in results["phases"]}
    results["verdict"] = {
        "baseline_ok": ph["1_baseline"]["success"],
        "reject_5s_absorbed_by_retry": ph["3_reject_5s_60t"]["success"],
        "reject_15s_absorbed_by_retry": ph["4_reject_15s_60t"]["success"],
        "drop_failure_before_130s": ph["5_drop_45s"]["elapsed"] < 130,
        "retry_events_observed_during_blocks": (
            ph["3_reject_5s_60t"]["retry_events"]["clone_retry_count"] +
            ph["3_reject_5s_60t"]["retry_events"]["connection_errors"] +
            ph["4_reject_15s_60t"]["retry_events"]["clone_retry_count"] +
            ph["4_reject_15s_60t"]["retry_events"]["connection_errors"]
        ) > 0,
    }
    print(f"\nVerdict: {json.dumps(results['verdict'], indent=2, ensure_ascii=False)}")

    out = REPORTS / "gitcdn_task_r1_retry.json"
    out.write_text(json.dumps(results, indent=2, ensure_ascii=False))
    print(f"\n📄 JSON: {out}")


if __name__ == "__main__":
    main()
