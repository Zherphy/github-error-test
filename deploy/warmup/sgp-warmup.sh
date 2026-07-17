#!/bin/bash
# sgp-warmup: 定时对 smart-git-proxy 的高频仓库触发 info/refs 请求，让 mirror 常驻 warm
#
# 部署位置：与 smart-git-proxy 同机（走 localhost，绕过所有外部网络）
# 频率建议：与 SYNC_STALE_AFTER 匹配（例如 SYNC_STALE_AFTER=600s → 每 5 分钟跑一次）
#
# 环境变量：
#   SGP_URL      smart-git-proxy 地址（默认 http://localhost:8080）
#   SGP_REPOS    仓库列表文件路径（每行一个 owner/repo）（默认 /etc/smart-git-proxy/warmup-repos.txt）
#   MAX_ATTEMPTS 单仓库最大尝试次数（默认 3）
#   REQ_TIMEOUT  单请求超时秒数（默认 60）
#   LOG_JSON     是否输出 JSON 行日志（默认 1）
#
# 退出码：始终 0（避免 systemd 因单个仓库失败反复重启）
#         详细结果落到 /var/log/sgp-warmup.log 或 stdout

set -u
SGP_URL="${SGP_URL:-http://localhost:8080}"
SGP_REPOS="${SGP_REPOS:-/etc/smart-git-proxy/warmup-repos.txt}"
MAX_ATTEMPTS="${MAX_ATTEMPTS:-3}"
REQ_TIMEOUT="${REQ_TIMEOUT:-60}"
LOG_JSON="${LOG_JSON:-1}"

if [ ! -f "$SGP_REPOS" ]; then
    echo "sgp-warmup: repo list not found: $SGP_REPOS" >&2
    exit 0
fi

log() {
    if [ "$LOG_JSON" = "1" ]; then
        local ts; ts=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
        printf '{"time":"%s","level":"%s","repo":"%s","attempt":%d,"status":"%s","http_code":"%s","duration_ms":%d}\n' \
            "$ts" "$1" "$2" "$3" "$4" "${5:-}" "${6:-0}"
    else
        echo "[$(date +%H:%M:%S)] $1 repo=$2 attempt=$3 status=$4 http=${5:-} dur_ms=${6:-0}"
    fi
}

overall_ok=0
overall_fail=0

while IFS= read -r repo || [ -n "$repo" ]; do
    # 支持注释和空行
    case "$repo" in
        ""|\#*) continue ;;
    esac

    url="${SGP_URL}/github.com/${repo}/info/refs?service=git-upload-pack"
    attempt=1
    success=0
    while [ "$attempt" -le "$MAX_ATTEMPTS" ]; do
        start_ns=$(date +%s%N)
        # 只关心 header 层；-o /dev/null 丢弃 body；抓 http_code
        http_code=$(curl -sf -m "$REQ_TIMEOUT" -o /dev/null -w "%{http_code}" \
                    -H "User-Agent: git/2.34.1" "$url" || true)
        elapsed_ms=$(( ($(date +%s%N) - start_ns) / 1000000 ))
        if [ "$http_code" = "200" ]; then
            log "info" "$repo" "$attempt" "ok" "$http_code" "$elapsed_ms"
            success=1
            overall_ok=$((overall_ok + 1))
            break
        fi
        log "warn" "$repo" "$attempt" "fail" "${http_code:-timeout}" "$elapsed_ms"
        # 指数退避 2/4/8s
        sleep_for=$((2 ** attempt))
        [ "$attempt" -lt "$MAX_ATTEMPTS" ] && sleep "$sleep_for"
        attempt=$((attempt + 1))
    done
    if [ "$success" = "0" ]; then
        log "error" "$repo" "$MAX_ATTEMPTS" "exhausted" "" 0
        overall_fail=$((overall_fail + 1))
    fi
done < "$SGP_REPOS"

log "info" "SUMMARY" 0 "done" "$overall_ok/$((overall_ok + overall_fail))" 0
exit 0
