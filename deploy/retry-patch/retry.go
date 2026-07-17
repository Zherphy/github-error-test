package mirror

import (
	"context"
	"os"
	"strconv"
	"strings"
	"time"
)

// UpstreamRetryPolicy 控制 git fetch/clone 遇到暂态网络失败时的重试行为。
//
// 通过环境变量配置（在 mirror.New 时读取）：
//   UPSTREAM_RETRY_COUNT      重试次数，默认 3（含首次共尝试 3+1 = 4 次；配 0 关闭重试）
//   UPSTREAM_RETRY_BACKOFF    初始退避秒数，默认 1s，指数递增（1s, 2s, 4s ...）
//   UPSTREAM_ATTEMPT_TIMEOUT  单次 git 子命令超时，默认 60s（不设时依赖上游 TCP 默认 130s）
type UpstreamRetryPolicy struct {
	MaxRetries     int
	InitialBackoff time.Duration
	AttemptTimeout time.Duration
}

func LoadPolicyFromEnv() UpstreamRetryPolicy {
	p := UpstreamRetryPolicy{
		MaxRetries:     3,
		InitialBackoff: 1 * time.Second,
		AttemptTimeout: 60 * time.Second,
	}
	if v := os.Getenv("UPSTREAM_RETRY_COUNT"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n >= 0 {
			p.MaxRetries = n
		}
	}
	if v := os.Getenv("UPSTREAM_RETRY_BACKOFF"); v != "" {
		if d, err := time.ParseDuration(v); err == nil && d > 0 {
			p.InitialBackoff = d
		}
	}
	if v := os.Getenv("UPSTREAM_ATTEMPT_TIMEOUT"); v != "" {
		if d, err := time.ParseDuration(v); err == nil && d > 0 {
			p.AttemptTimeout = d
		}
	}
	return p
}

// isRetryableUpstreamError 判断 git fetch/clone 的错误是否值得重试。
// 目的：只对网络类错误重试；鉴权 / 仓库不存在等错误立即返回。
func isRetryableUpstreamError(err error) bool {
	if err == nil {
		return false
	}
	msg := strings.ToLower(err.Error())

	// 明确不该重试的（避免把授权失败误当成网络问题）
	nonRetryablePatterns := []string{
		"authentication failed",
		"could not read username",
		"repository not found",
		"access denied",
		"permission denied",
		"invalid credentials",
		"remote: forbidden",
		"http 401",
		"http 403",
		"http 404",
	}
	for _, p := range nonRetryablePatterns {
		if strings.Contains(msg, p) {
			return false
		}
	}

	// 网络类错误 —— 值得重试
	retryablePatterns := []string{
		"connection refused",
		"connection reset",
		"connection timed out",
		"timeout",
		"timed out",
		"network is unreachable",
		"no route to host",
		"temporary failure",
		"early eof",
		"the remote end hung up unexpectedly",
		"rpc failed",
		"http 5", // 5xx from upstream
		"tls handshake",
		"i/o timeout",
		"broken pipe",
		"failed to connect",
	}
	for _, p := range retryablePatterns {
		if strings.Contains(msg, p) {
			return true
		}
	}

	// 未识别的错误 —— 默认重试一次（安全 net）
	// 若担心过度重试，把返回值改成 false 即可
	return true
}

// runWithRetry 用统一的重试策略跑一个 fn。fn 应尊重 ctx 的 deadline。
// 每次尝试用一个独立的、带 AttemptTimeout 的子 ctx，避免慢失败拖死整个请求。
func (m *Mirror) runWithRetry(ctx context.Context, opName, repoKey string, fn func(ctx context.Context) error) error {
	var lastErr error
	total := m.retry.MaxRetries + 1
	for i := 0; i < total; i++ {
		attemptCtx, cancel := context.WithTimeout(ctx, m.retry.AttemptTimeout)
		attemptStart := time.Now()
		err := fn(attemptCtx)
		cancel()
		if err == nil {
			if i > 0 {
				m.log.Info("upstream op succeeded after retry",
					"op", opName, "repo", repoKey,
					"attempt", i+1, "duration_ms", time.Since(attemptStart).Milliseconds())
			}
			return nil
		}
		lastErr = err
		if !isRetryableUpstreamError(err) {
			m.log.Warn("upstream op non-retryable, giving up",
				"op", opName, "repo", repoKey,
				"attempt", i+1, "err", err.Error())
			return err
		}
		if i == total-1 {
			// 用完所有尝试
			m.log.Warn("upstream op exhausted retries",
				"op", opName, "repo", repoKey,
				"attempts", total, "err", err.Error())
			return err
		}
		// 指数退避 backoff = InitialBackoff * 2^i
		backoff := m.retry.InitialBackoff << i
		m.log.Warn("upstream op failed, will retry",
			"op", opName, "repo", repoKey,
			"attempt", i+1, "backoff_ms", backoff.Milliseconds(), "err", err.Error())
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(backoff):
		}
	}
	return lastErr
}
