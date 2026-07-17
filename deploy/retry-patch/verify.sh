#!/bin/bash
# Task 5 端到端验证：比较 v0.2.6 原版 vs 打了 retry patch 的版本
# 场景：首次拉取新仓库 + upstream REJECT 5 秒（然后放开）
# 期望：v0.2.6 立刻 502；patched 版本重试并成功
set -u
ORIG_BIN="${ORIG_BIN:-/tmp/sgp/smart-git-proxy}"
PATCHED_BIN="${PATCHED_BIN:-/root/sgp-fork/smart-git-proxy/bin/smart-git-proxy}"
MIRROR_DIR="${MIRROR_DIR:-/tmp/git-mirrors}"
LOG_DIR="/tmp/sgp-logs"
REPO="${REPO:-https://github.com/pre-commit/pre-commit.git}"
DEST_PREFIX="/tmp/task5_"
GH_IP=$(getent ahostsv4 github.com | awk '{print $1}' | head -1)

mkdir -p "$LOG_DIR"

start_proxy() {
    local bin="$1" tag="$2" extra_env="$3"
    env MIRROR_DIR="$MIRROR_DIR" LISTEN_ADDR=:18080 ALLOWED_UPSTREAMS=github.com \
        AUTH_MODE=none SYNC_STALE_AFTER=30s LOG_LEVEL=info $extra_env \
        "$bin" > "$LOG_DIR/task5-$tag.log" 2>&1 &
    echo $!
}

stop_proxy() {
    local pid="$1"
    kill "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
    sleep 1
}

clear_mirror() {
    rm -rf "$MIRROR_DIR/github.com/pre-commit/pre-commit.git"
}

block_upstream() {
    iptables -I OUTPUT 1 -d "$GH_IP" -p tcp --dport 443 -j REJECT 2>/dev/null
}

unblock_upstream() {
    for _ in 1 2 3; do
        iptables -D OUTPUT -d "$GH_IP" -p tcp --dport 443 -j REJECT 2>/dev/null || break
    done
}

run_case() {
    local bin="$1" tag="$2" extra_env="$3"
    local dest="${DEST_PREFIX}${tag}"
    clear_mirror
    rm -rf "$dest"
    unblock_upstream   # 兜底
    local pid; pid=$(start_proxy "$bin" "$tag" "$extra_env")
    sleep 1.5
    if ! curl -sf http://localhost:18080/healthz > /dev/null; then
        echo "  [$tag] proxy not healthy, skip" >&2
        stop_proxy "$pid"
        return
    fi

    # 上阻断，5 秒后放开
    block_upstream
    ( sleep 5 && unblock_upstream ) &
    local bg_pid=$!

    local t0; t0=$(date +%s.%N)
    local rc=0
    local out
    out=$(timeout 90 git -c "url.http://localhost:18080/github.com/.insteadOf=https://github.com/" \
              clone --depth 1 "$REPO" "$dest" 2>&1) || rc=$?
    local t1; t1=$(date +%s.%N)
    local elapsed; elapsed=$(awk "BEGIN{printf \"%.2f\", $t1 - $t0}")

    wait "$bg_pid" 2>/dev/null || true
    unblock_upstream
    stop_proxy "$pid"
    rm -rf "$dest"

    echo "=== $tag ==="
    echo "  rc=$rc  elapsed=${elapsed}s"
    echo "  client stderr tail:"
    echo "$out" | tail -3 | sed 's/^/    /'
    echo "  proxy log retry events:"
    grep -E "retry|attempt|serving stale|request failed|cloning mirror|clone complete" "$LOG_DIR/task5-$tag.log" | sed 's/^/    /'
    echo ""
}

# Case A: v0.2.6 原版
run_case "$ORIG_BIN" "orig_v0.2.6" ""

# Case B: patched 版本，默认 retry policy（3 次 + 1s 起步 backoff）
run_case "$PATCHED_BIN" "patched_default" ""

# Case C: patched 版本，更激进（5 次 + 500ms 起步）
run_case "$PATCHED_BIN" "patched_aggressive" "UPSTREAM_RETRY_COUNT=5 UPSTREAM_RETRY_BACKOFF=500ms"

unblock_upstream
echo "iptables final:"
iptables -L OUTPUT -n | head -3
