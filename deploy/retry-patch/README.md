# 上游重试补丁（方案 D）

给 [runs-on/smart-git-proxy@v0.2.6](https://github.com/runs-on/smart-git-proxy/tree/v0.2.6) 打的补丁，让代理在 `git clone` / `git fetch` 遇到暂态网络错误时**自动重试**，并区分**网络错误 vs 鉴权错误**。

## 文件

| 文件 | 说明 |
|---|---|
| `retry.go` | 新加的重试策略实现（放到 `internal/mirror/retry.go`）|
| `0001-add-upstream-retry.patch` | 对 `internal/mirror/mirror.go` 的改动（4 处：Mirror struct 增字段、New() 加载策略、EnsureRepo 里 clone 和 sync 分别用 runWithRetry 包裹）|
| `verify.sh` | 端到端验证脚本，比较 v0.2.6 原版 vs 打补丁版本 |

## 新的环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `UPSTREAM_RETRY_COUNT` | 3 | 除首次外的最大重试次数（设 0 关闭）|
| `UPSTREAM_RETRY_BACKOFF` | 1s | 初始退避，指数递增（1s, 2s, 4s, 8s ...）|
| `UPSTREAM_ATTEMPT_TIMEOUT` | 60s | 单次 `git fetch/clone` 的最大等待时间（**关键**——原版无此上限，DROP 场景要等 130s）|

## 应用补丁

```bash
git clone --branch v0.2.6 https://github.com/runs-on/smart-git-proxy
cd smart-git-proxy
cp path/to/retry.go internal/mirror/retry.go
git apply path/to/0001-add-upstream-retry.patch
go build -o bin/smart-git-proxy ./cmd/proxy
```

需要 **Go 1.25+**。若本地无，可用 docker：

```bash
docker run --rm -v $PWD:/src -w /src golang:1.25 \
  go build -o /src/bin/smart-git-proxy ./cmd/proxy
```

## 本地端到端验证结果

场景：首次拉取一个 mirror 不存在的仓库；upstream REJECT 5 秒后放开。

见 [reports/task5_retry_patch.json](../../reports/task5_retry_patch.json)：

| 版本 | 结果 | 端到端耗时 | 说明 |
|---|:---:|:---:|---|
| **v0.2.6 原版** | ❌ HTTP 502 | 0.03s | 立即失败，无重试 |
| **patched（默认 3+1 次）** | ✅ 成功 | 9.36s | 4/4 次尝试成功；1s+2s+4s 退避 |
| **patched（激进 5+1 次）** | ✅ 成功 | 9.86s | 5/6 次尝试成功；500ms+1s+2s+4s 退避 |

**Log 里的重试事件示例**：

```json
{"level":"WARN","msg":"upstream op failed, will retry","op":"clone","attempt":1,"backoff_ms":1000,"err":"..."}
{"level":"WARN","msg":"upstream op failed, will retry","op":"clone","attempt":2,"backoff_ms":2000,"err":"..."}
{"level":"WARN","msg":"upstream op failed, will retry","op":"clone","attempt":3,"backoff_ms":4000,"err":"..."}
{"level":"INFO","msg":"upstream op succeeded after retry","op":"clone","attempt":4,"duration_ms":2200}
```

## 关键设计决策

1. **每次尝试独立 timeout**：用 `context.WithTimeout` 给每次 `git` 命令一个独立的、可控的超时，防止 DROP 类静默丢包让首次尝试就吃满 130s。
2. **网络 vs 鉴权分类**：`isRetryableUpstreamError` 里维护两个正则列表——
   - 命中"authentication failed / HTTP 403 / repository not found / permission denied ..." 立即返回，不重试
   - 命中"connection refused/reset/timed out / early EOF / RPC failed / HTTP 5xx / TLS handshake ..." 才重试
   - 未识别错误默认重试一次（安全 net，可按需改成 false）
3. **保留原有 serve-stale 行为**：EnsureRepo 里 sync 失败时依然会 serve stale（源码行为不变）。retry 只是在**触发失败判定之前**多试几次；serve-stale 仍是最后一道防线。
4. **backoff 上下文可取消**：`select { case <-ctx.Done() ...}`，客户端断开时立即停。

## 上游 PR 建议

这个补丁值得直接向 [runs-on/smart-git-proxy](https://github.com/runs-on/smart-git-proxy) 开一个 PR。要点：

- PR 标题建议：`feat: add configurable upstream retry with backoff for sync/clone`
- 强调"可通过 `UPSTREAM_RETRY_COUNT=0` 完全关闭，向后兼容"
- 强调"独立 attempt timeout 解决 upstream DROP 场景下 130s 慢失败问题"
- 用本次的 [task5_retry_patch.json](../../reports/task5_retry_patch.json) 做支撑数据
- 如果他们不合入，就长期维护 fork（本 patch 很小，跟主线合并成本低）

## 不覆盖的场景

- **私有仓库鉴权失败误判为网络错误**：源码里 `EnsureRepo` 有 `requiresAuth(repoPath)` 判断，失败时直接返 "authentication required"——本补丁没动这块。若上游 sync 时鉴权正常但网络失败，仍会被误报为鉴权错误。彻底修复要改 requiresAuth 后的分类逻辑（在 patch 里注掉 requiresAuth 分支，让所有错误都走 retry + serve-stale）。这属于**更激进的补丁**，需要私有仓库场景数据支持才做。
