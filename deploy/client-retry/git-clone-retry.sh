#!/bin/bash
# git-clone-retry: git clone 幂等重试壳，用来吸收 smart-git-proxy 或直连 GitHub 的短暂 5xx
#
# 用法：
#   git-clone-retry [--depth 1] <repo-url> [<dest>]
# 环境变量：
#   GCR_MAX_ATTEMPTS   最大尝试次数（默认 4）
#   GCR_BACKOFF_BASE   指数退避基数秒（默认 2 → 2,4,8,16s）
#   GCR_RETRY_ON       正则匹配 stderr，命中则重试（默认覆盖常见 5xx/网络错误）
#   GCR_JITTER_MAX     退避上叠加的随机抖动上限秒（默认 3）
#
# 特点：
#   - stderr 匹配才重试，鉴权/仓库不存在等错误立即返回，不做无意义重试
#   - 每次重试前清理不完整目录
#   - 打印进度到 stderr，退出码保留最后一次尝试的返回码

set -eu
MAX_ATTEMPTS="${GCR_MAX_ATTEMPTS:-4}"
BACKOFF_BASE="${GCR_BACKOFF_BASE:-2}"
JITTER_MAX="${GCR_JITTER_MAX:-3}"
RETRY_ON="${GCR_RETRY_ON:-HTTP/[0-9.]+ 5[0-9][0-9]|502|503|504|early EOF|Connection reset|Connection refused|timed out|Failed to connect|RPC failed|SSL_ERROR|network is unreachable|temporary failure}"

# 通过尝试解析最后一个 non-flag 参数猜 dest（供失败时清理）
guess_dest() {
    local last=""
    for arg in "$@"; do
        case "$arg" in
            -*) ;;
            *) last="$arg" ;;
        esac
    done
    echo "$last"
}

repo=""
for arg in "$@"; do
    case "$arg" in
        http*|git@*|ssh://*) repo="$arg"; break ;;
    esac
done
[ -n "$repo" ] || { echo "git-clone-retry: cannot find repo URL in args" >&2; exit 2; }

# 猜 dest 目录（clone 的默认行为：拿 basename 去 .git）
dest="$(guess_dest "$@")"
if [ "$dest" = "$repo" ]; then
    dest="$(basename "$repo" .git)"
fi

attempt=1
while true; do
    tmp_err="$(mktemp)"
    # 不完整目录先清（clone 失败通常会留半个目录，重试会 clash）
    if [ -n "$dest" ] && [ -e "$dest" ]; then
        rm -rf -- "$dest"
    fi
    echo "git-clone-retry: attempt $attempt/$MAX_ATTEMPTS: git clone $*" >&2
    if git clone "$@" 2> "$tmp_err"; then
        cat "$tmp_err" >&2
        rm -f "$tmp_err"
        exit 0
    fi
    rc=$?
    err_content="$(cat "$tmp_err")"
    cat "$tmp_err" >&2
    rm -f "$tmp_err"

    # 是否命中可重试模式
    if ! echo "$err_content" | grep -qE "$RETRY_ON"; then
        echo "git-clone-retry: error not retryable, giving up (rc=$rc)" >&2
        exit "$rc"
    fi
    if [ "$attempt" -ge "$MAX_ATTEMPTS" ]; then
        echo "git-clone-retry: exhausted $MAX_ATTEMPTS attempts (rc=$rc)" >&2
        exit "$rc"
    fi

    # 指数退避 + 抖动
    backoff=$((BACKOFF_BASE ** attempt))
    jitter=$((RANDOM % (JITTER_MAX + 1)))
    sleep_for=$((backoff + jitter))
    echo "git-clone-retry: retryable failure detected, sleeping ${sleep_for}s ..." >&2
    sleep "$sleep_for"
    attempt=$((attempt + 1))
done
