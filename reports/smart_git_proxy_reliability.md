# smart-git-proxy 上游抖动/504 缓解方案

**背景**：生产使用 smart-git-proxy 后，仍会因上游（GitHub）网络问题偶发 504；smart-git-proxy 自身**不做自动重试**，客户端直接看到失败。本报告分析根因、列出所有可行方案、并明确指出**哪些方案能在本机复现测试、哪些必须到集群实测**。

**场景约束**：拉取的仓库以**公开仓库**为主（pre-commit hooks、开源依赖等）。

---

## 1. 现有代码行为（源码验证）

来自 [runs-on/smart-git-proxy `internal/mirror/mirror.go:121-129`](https://github.com/runs-on/smart-git-proxy/blob/main/internal/mirror/mirror.go)：

```go
if err != nil {
    if m.requiresAuth(repoPath) {
        return "", "", fmt.Errorf("authentication required: %w", err)
    }
    m.log.Warn("sync failed, serving stale", ...)
    return repoPath, StatusHit, nil   // 公开仓库：serve stale
}
```

| 场景 | 现有行为 | 是否会 504 |
|---|---|:---:|
| **公开仓库**，mirror 已存在，upstream sync 失败 | serve stale，返回 200 | ❌ 不会 |
| **公开仓库**，**首次拉取**（mirror 不存在），upstream 失败 | `cloneRepo` 直接失败，无重试 | ✅ 会 |
| **公开仓库**，客户端在 sync 完成前先超时 | 客户端看到 `early EOF` / timeout | ✅ 类似 504 |
| **私有仓库** sync 失败 | 一律报"authentication required"（无法区分网络故障 vs 鉴权） | ✅ 会 |

**结论**：`serve-stale-on-error` 已经覆盖了"日常巡查同步失败"这类最常见场景。剩下的 504 主要来自**首次拉取遇到上游故障**、以及**客户端超时**这两条链路。**根源是"没有任何一层做重试"**。

---

## 2. 缓解方案（按落地成本从低到高）

### 方案 A：客户端重试壳

**思路**：让 client 在 clone 失败时重试。适用所有场景，改动最小。

**pre-commit / 开发机 / self-hosted runner**：包一层 shell 脚本

```bash
#!/bin/bash
# /usr/local/bin/git-clone-retry
for i in 1 2 3 4; do
  git clone "$@" && exit 0
  echo "clone failed (attempt $i), retrying in $((2**i))s..." >&2
  sleep $((2**i))
done
exit 1
```

**GitHub Actions**：用 `nick-fields/retry@v3` 包裹关键步骤

```yaml
- uses: nick-fields/retry@v3
  with:
    timeout_minutes: 5
    max_attempts: 3
    retry_wait_seconds: 10
    command: |
      git -c url."http://smart-git-proxy/github.com/".insteadOf="https://github.com/" \
          clone --depth 1 https://github.com/owner/repo
```

**pre-commit hook 场景**：pre-commit 内部自己调 git（不好插入 retry）。最省心的做法是**先手动把 hook 仓库预 clone 到 `PRE_COMMIT_HOME` 缓存**（配合方案 B 的预热 cron），下发到开发机镜像和 CI base image。

**优点**：0 服务端改动、立刻见效、透明可回退
**局限**：重试的还是同一个失败源；如果上游真的挂了 5 分钟，重试无用

### 方案 B：拉长 `SYNC_STALE_AFTER` + 主动预热

**思路**：让绝大部分客户端请求走 mirror-hit（完全不碰上游），把上游抖动收敛到**代理自身的预热 job** 里。

**改配置**（一行 env）：
```bash
SYNC_STALE_AFTER=600s   # 或更长，看新鲜度要求
```

之前测试用 `30s` 意味着几乎每次请求都会去 upstream 探一次头，每一次探头都是潜在 504 触发点。拉到 `600s` 后 99% 请求走 mirror-hit。

**加预热 cron**（在代理机内部执行，自带重试）：

```bash
# /etc/cron.d/sgp-warmup — 每 5 分钟对高频仓库刷新
*/5 * * * * root /usr/local/bin/sgp-warmup >/var/log/sgp-warmup.log 2>&1
```

```bash
#!/bin/bash
# /usr/local/bin/sgp-warmup
REPOS=(
  "pre-commit/pre-commit-hooks"
  "psf/black"
  "PyCQA/ruff"
  "PyCQA/isort"
  # ...
)
for repo in "${REPOS[@]}"; do
  for i in 1 2 3; do
    curl -sf -m 60 "http://localhost:18080/github.com/${repo}/info/refs?service=git-upload-pack" \
      -o /dev/null && break
    sleep $((5*i))
  done
done
```

**效果预估**：
- 上游抖动只影响这个 warmup job（自带重试就能吸收）
- 客户端请求 99% 命中 mirror-hit，与上游解耦

**优点**：无需代码改动、直接解决 90%+ 的 504
**局限**：新加入的仓库首次仍会 miss（要么客户端加重试，要么把预热列表动态化）

### 方案 C：nginx 在 smart-git-proxy 前置做 5xx 重试

**思路**：把 smart-git-proxy 当成后端，在前面架一层 nginx 做 502/503/504 重试。

```nginx
upstream sgp_pool {
    server 127.0.0.1:18080;
    server 127.0.0.1:18080 backup;  # 同一 backend 多写几次
    server 127.0.0.1:18080 backup;  # nginx 才会当作"重试目标"
}

server {
    listen 8080;
    location / {
        proxy_pass http://sgp_pool;
        proxy_next_upstream error timeout http_502 http_503 http_504;
        proxy_next_upstream_tries 3;
        proxy_next_upstream_timeout 60s;
        proxy_read_timeout 300s;     # 给 sync 留时间
        proxy_send_timeout 300s;
        proxy_http_version 1.1;
    }
}
```

**注意**：
- 单实例 smart-git-proxy 下这个重试帮助有限——第 2、3 次重试打的是**同一个后端进程**，如果故障是持续性（网络分区），重试无用；如果是**瞬时**故障（几秒 TCP 抖动），有效。
- **真正发挥价值**是当你部署了**HA 版**（多实例 smart-git-proxy），nginx 能路由到健康节点。

**优点**：不改代理代码，收敛偶发抖动
**局限**：单实例 gain 有限；HA 部署要解决 mirror 一致性（各实例独立存 mirror，first-miss 会重复冷 clone）

### 方案 D：给 smart-git-proxy 打上游重试补丁

**思路**：**根治**。在 `syncRepo` 和 `cloneRepo` 外层加重试 + 网络错误 vs 鉴权错误的判定。

```go
// 伪代码，加在 internal/mirror/mirror.go
func (m *Mirror) syncRepoWithRetry(ctx context.Context, ...) error {
    const maxAttempts = 3
    var lastErr error
    for i := 0; i < maxAttempts; i++ {
        if i > 0 {
            backoff := time.Duration(1<<i) * time.Second // 2s, 4s
            select {
            case <-ctx.Done(): return ctx.Err()
            case <-time.After(backoff):
            }
            m.log.Warn("retry sync", "repo", repoPath, "attempt", i+1)
        }
        err := m.syncRepo(ctx, repoPath, upstreamURL, authHeader)
        if err == nil { return nil }
        lastErr = err
        if isAuthError(err) { return err }  // 鉴权错误立即返回，别重试
    }
    return lastErr
}
```

配套加两个 env：
- `UPSTREAM_RETRY_COUNT=3`
- `UPSTREAM_RETRY_BACKOFF=2s`

**优点**：根治，所有上游故障场景都被吸收
**局限**：需维护 fork，或等上游合入 PR（值得同时向上游提 PR）

---

## 3. 我的推荐组合

| 优先级 | 动作 | 预期效果 |
|:---:|---|---|
| P0（今天）| `SYNC_STALE_AFTER=600s` + Actions 里加 `nick-fields/retry` | 覆盖 80%+ 场景 |
| P1（本周）| 部署预热 cron | 首次 miss 场景大幅收敛 |
| P2（下周）| 打上游重试补丁（可以我先给你写好 diff） | 根治剩余长尾 |
| P3（长期）| 前置 nginx + HA 部署 | 单点故障保护 |

---

## 4. 本地可复现 vs 集群必须实测

### 4.1 本地可复现（我能立即帮你跑）

复用之前 [reproduce_504.py](../reproduce_504.py) 的框架，加上 `iptables` / `tc netem` **人工制造上游抖动**：

| 方案 | 本地怎么测 | 需要的额外工具 |
|---|---|---|
| **A 客户端重试** | 起 proxy，`iptables -A OUTPUT -d github.com -j DROP` 制造上游中断 → 直连 clone 一个"新"仓库（无 mirror）→ 走 A 的 retry wrapper → 恢复 iptables → 应恢复成功 | iptables |
| **B `SYNC_STALE_AFTER` + 预热效果** | 起两组 proxy（`SYNC_STALE_AFTER=2s` vs `600s`），用 `tc netem` 加入 10% 丢包 + 50ms 抖动，持续跑 clone，统计错误率对比 | tc（netem） |
| **验证源码里的 serve-stale-on-error 是否真的生效** | 起 proxy，warm mirror，iptables 阻塞出站，clone 应仍成功（走 stale 路径），log 里应看到 `sync failed, serving stale` | iptables |
| **D 补丁编译 + 验证** | fork 源码，加 retry patch，需要 **Go 1.25+**（当前只有 1.23.1，要装或用 docker），本地 iptables 抖动测重试次数是否符合预期 | Go 1.25+，可能要 docker |
| **C 单实例 nginx 重试** | 本地起 nginx + 单 proxy，iptables 抖动测**瞬时**故障（<3s）能否被 nginx 重试吸收；持续故障肯定不行 | nginx |

**能覆盖的问题**：验证机制是否按设计工作、量化各方案在人工抖动下的错误率下降

**本地测不了的**：真实故障模式（部分包丢、TLS 握手失败、DNS 抽风、GitHub 限流慢响应等），本地只能"制造"近似效果

### 4.2 必须在集群/生产测（本地测不了或没意义）

| 场景 | 为什么必须集群测 |
|---|---|
| **真实上游抖动下的错误率下降** | 本地 iptables/netem 只是近似；真实抖动模式多样（partial packet loss、TLS handshake timeout、GitHub 限流慢响应、DNS 抽风、CDN 边缘漂移…），本地不可能完整复现 |
| **HA 多实例部署 + nginx 前置的真实收益** | 单机 nginx 重试打回自己意义有限；HA 场景要看**多实例间 mirror 一致性、singleflight 是否跨实例**（不跨），这些必须在真实拓扑下测 |
| **生产 CI 流量图谱下的错误率** | 你们真实的 CI 并发时序、仓库分布、失败重试次数只有生产才有；预热清单和 `SYNC_STALE_AFTER` 值的最优取值只能靠生产数据反推 |
| **和现有 WAF/企业代理链路的兼容性** | 如果 smart-git-proxy 前面还有企业 WAF 或 SSL 中间人，其超时和重试策略与本方案的相互作用只能实测 |
| **规模测试**（几十上百台 runner 同时打）| 本地机器只有 15GB RAM、双核，之前 c=100 就打到 OOM floor 了；生产代理机的真实容量曲线只能生产上测 |

---

## 5. 建议的下一步

**你可以让我立刻在本地做的（不需要额外授权）**：

1. **验证 serve-stale-on-error 是否真生效**（iptables 阻上游 + warm mirror + clone）——1 小时内出结果
2. **对比 `SYNC_STALE_AFTER=2s vs 600s` 在抖动下的错误率**——tc netem 加抖动，跑量化压测——2 小时
3. **写方案 A 的 retry wrapper 和 Actions workflow 示例**（不涉及 proxy 内部）——0.5 小时
4. **写方案 B 的 warmup cron 脚本 + 部署示例**（systemd timer 版本更稳）——0.5 小时
5. **给 smart-git-proxy 写 D 方案的补丁 + 起草上游 PR**（需要装 Go 1.25 或用 docker）——1-2 小时

**必须你或运维同事在集群做的**：

1. 部署一次带方案 A+B 的组合，观察实际 CI 错误率变化——**这是最重要的一步**
2. 打 D 方案的 fork 补丁到生产实例灰度
3. HA 部署 + nginx 前置的容量/一致性测试
4. 与你们现有企业代理/WAF 链路的兼容性验证

---

## 6. 参考

- 上游代码：[runs-on/smart-git-proxy `internal/mirror/mirror.go`](https://github.com/runs-on/smart-git-proxy/blob/main/internal/mirror/mirror.go)
- 前次对照测试：[smart_git_proxy_comparison.md](smart_git_proxy_comparison.md)
- 前次高并发压测：[smart_git_proxy_high_concurrency.json](smart_git_proxy_high_concurrency.json)
- `nick-fields/retry` action：https://github.com/nick-fields/retry
