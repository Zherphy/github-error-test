# 客户端重试壳（方案 A）

覆盖 pre-commit、GitHub Actions、开发机三个入口。

## 1. `git-clone-retry.sh` shim

指数退避重试 `git clone`，只对可重试错误重试（502/504/early EOF/Connection reset 等），鉴权/仓库不存在等错误立即返回。

### 部署到开发机 / self-hosted runner base image

```bash
sudo install -m 0755 git-clone-retry.sh /usr/local/bin/git-clone-retry
```

### 使用

```bash
GCR_MAX_ATTEMPTS=4 git-clone-retry --depth 1 https://github.com/owner/repo
```

### 端到端本地验证结果

在本仓库测试中（[reports/task1_serve_stale.json](../../reports/task1_serve_stale.json) 揭示了首次 clone 无 stale 会返 502），把 upstream REJECT 8 秒后放开，wrapper 表现：

```
attempt 1/5 → 502 → sleep 2s
attempt 2/5 → 502 → sleep 7s  (2s + jitter)
attempt 3/5 → ✅ success
```

**结论**：只要 upstream 中断窗口 < `sum(backoff)` 就能被吸收，本例中 4 次重试可覆盖累计 30s 左右的中断。

## 2. GitHub Actions workflow 集成

见 [actions-workflow-example.yml](actions-workflow-example.yml)。核心两点：
- **必须 self-hosted runner**（GitHub-hosted runner 访问不到内网 proxy）
- 用 `nick-fields/retry@v3` 包 `git clone` / `git submodule` 等步骤

## 3. pre-commit 集成

pre-commit 自己会调 `git clone` 拉 hook 仓库（`.pre-commit-config.yaml` 里 `repo:` 字段）。它内部**不暴露重试接口**，因此有三种落地路径：

### 3.a 全局 `insteadOf` + PATH 前置一个 git 包装

pre-commit 只认 PATH 里第一个 `git`。做一个包装脚本：

```bash
sudo tee /usr/local/bin/git-wrapped >/dev/null <<'SH'
#!/bin/bash
# 只对 clone 命令走 retry，其他直通
if [ "${1:-}" = "clone" ]; then
    shift
    exec git-clone-retry "$@"
fi
exec /usr/bin/git "$@"
SH
sudo chmod +x /usr/local/bin/git-wrapped
```

然后在 pre-commit CI job 里：

```yaml
env:
  PATH: /usr/local/bin:/opt/gitwrap:/usr/bin  # 保证 git-wrapped 优先
```

或直接在 `.pre-commit-config.yaml` **不改动**的情况下，用 `git config --global url.insteadOf` 让所有 clone 走 proxy：

```bash
git config --global "url.http://smart-git-proxy.internal:8080/github.com/.insteadOf" "https://github.com/"
```

### 3.b 预置 `PRE_COMMIT_HOME` 缓存

配合方案 B 的 warmup cron：预热 job 在代理机内部把常用 hook 仓库拉到本地 mirror，同时把 `PRE_COMMIT_HOME` 的 `repos/` 结构预写好放到 base image / runner cache 里。这样 pre-commit 启动几乎不 clone。

### 3.c 依赖 smart-git-proxy 自身的 serve-stale

Task 1 已证明：**mirror 已 warm** + **upstream 快速失败**（REJECT） → serve-stale 在 14ms 内生效，pre-commit 感知不到 upstream 故障。所以只要 3.b 或方案 B（warmup cron）跑起来，pre-commit 场景的 504 会被 serve-stale 直接吸收。

## 4. 关键配置

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `GCR_MAX_ATTEMPTS` | 4 | 最大尝试次数（含首次）|
| `GCR_BACKOFF_BASE` | 2 | 指数退避基数（2 → 2/4/8/16s）|
| `GCR_JITTER_MAX` | 3 | 每次退避额外叠加 0-3s 抖动 |
| `GCR_RETRY_ON` | 内置正则 | stderr 命中即重试；覆盖 5xx / early EOF / TCP 相关错误 |

## 5. 什么情况下 retry 帮不上

- upstream 长时间（> 累计退避时间）不恢复
- 出错原因不匹配 `GCR_RETRY_ON`（如鉴权失败、仓库不存在）
- 服务端返 4xx 类不可重试错误

对这些情况的解法看方案 B（warmup + `SYNC_STALE_AFTER=600s`）和方案 D（proxy 侧补丁）。
