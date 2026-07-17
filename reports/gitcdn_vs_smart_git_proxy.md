# git-cdn vs smart-git-proxy 头对头对比报告

**执行日期**：2026-07-17
**测试机器**：15 GB RAM，同一台，同一网络
**上游**：`https://github.com/`（PAT 认证；session 内 env 变量，不写入任何提交）
**git-cdn**：从 [bpaquet/git_cdn](https://github.com/bpaquet/git_cdn) 源码 build（Python 3.10-alpine + aiohttp + gunicorn 4 workers）
**smart-git-proxy**：v0.2.6 release binary（数据来自前次报告 [reports/smart_git_proxy_comparison.json](smart_git_proxy_comparison.json)、[reports/smart_git_proxy_high_concurrency.json](smart_git_proxy_high_concurrency.json)、[reports/task1_serve_stale.json](task1_serve_stale.json)）

---

## TL;DR

| 维度 | git-cdn | smart-git-proxy v0.2.6 | 谁赢 |
|---|---|---|:---:|
| **上游 retry** | **原生内建**（HTTP 层 10 次 + git-cmd 层 5 次）| 无（需打补丁 ~200 行）| 🟢 git-cdn |
| REJECT 5s 后放开 首次 clone | ✅ **40s 内成功** | ❌ 立即 502（0.03s）| 🟢 git-cdn |
| REJECT 15s 后放开 首次 clone | ✅ 26.6s 成功 | ❌ 立即 502 | 🟢 git-cdn |
| 大仓 sglang **冷启动** | **35.78s / 235 MB mirror** | 156s / 1.8 GB mirror | 🟢 git-cdn（4.4× / 8× 小）|
| 大仓 sglang **warm clone** | **4.76s** | 8.78s | 🟢 git-cdn（1.8×）|
| **c=100 并发**大仓 avg | **73.81s，min_avail 7.7 GB** | 143.88s，min_avail 2.4 GB（触发 OOM）| 🟢 git-cdn（1.95× 快，无 OOM）|
| **完全解耦 upstream 的能力** | ❌ 每次请求都联 upstream | ✅ `SYNC_STALE_AFTER=600s` 可全 mirror-hit | 🟢 smart-git-proxy |
| **架构复杂度**（部署 / 依赖）| Docker + 4 workers + Redis-like pack cache + LFS 支持 | 单二进制 Go 服务 | 🟢 smart-git-proxy |
| **协议范围** | git v1 only（v2 不支持）、无 SSH | git smart-HTTP（v1+v2）| 🟢 smart-git-proxy |
| **强制认证** | ✅ 强制 BasicAuth（GitHub 需 PAT）| 支持 pass-through、static、none | 🟢 smart-git-proxy |
| **公开仓库匿名支持** | ❌ 必须 PAT | ✅ `AUTH_MODE=none` | 🟢 smart-git-proxy |

**结论**：**对于你们主要痛点（upstream 抖动 → 504）**，git-cdn 是显著更好的选择——重试机制内建、无需打补丁、并发场景压倒性优势。**但**如果你们需要 `SYNC_STALE_AFTER=600s` 那种"完全屏蔽 upstream"的模式、或者用无 PAT 的公开仓库场景，两者还是要**组合起来看**。

---

## 1. 源码层面的架构对比

### 1.1 git-cdn 的两层 retry（[git_cdn/client_session.py](https://github.com/bpaquet/git_cdn/blob/master/git_cdn/client_session.py)、[git_cdn/repo_cache.py](https://github.com/bpaquet/git_cdn/blob/master/git_cdn/repo_cache.py)）

**第一层——HTTP session retry**（`ClientSessionWithRetry`）：
- `REQUEST_MAX_RETRIES=10`（env 可调）
- backoff 序列：`0.1, 0.2, 0.4, 0.8, ..., 51.2s`，累计约 **100+ 秒**
- 触发：`aiohttp.ClientConnectionError` 或 HTTP 状态码在 `retry_on` 列表
- 用途：info/refs 转发、upload-pack 转发

**第二层——git 命令级 retry**（`repo_cache.fetch()` / `clone()`）：
- `BACKOFF_START=0.5`, `BACKOFF_COUNT=5`（env 可调）
- backoff 序列：`0.5, 1, 2, 4, 8s`，累计约 15.5 秒
- 触发：`git clone` / `git fetch` returncode != 0
- 用途：本地 mirror 建立、mirror sync

**pack cache**（[git_cdn/pack_cache.py](https://github.com/bpaquet/git_cdn/blob/master/git_cdn/pack_cache.py)）：
- 缓存 `git-upload-pack` 生成的 pack 结果
- 并发请求同一 pack 参数 → 单次生成，多 worker 共享（跟 singleflight 类似但更精细）
- `PACK_CACHE_SIZE_GB=20` 默认

### 1.2 smart-git-proxy 的机制（[internal/mirror/mirror.go](https://github.com/runs-on/smart-git-proxy/blob/v0.2.6/internal/mirror/mirror.go)）

- **零 retry**：`syncRepo` / `cloneRepo` 一次失败即返给客户端
- 有 **serve-stale-on-error**（公开仓库、mirror 存在时）——但触发要等 git-fetch 失败，DROP 场景要 130s
- singleflight 并发合并（clone/sync 阶段），但**没有 pack cache**：每个客户端请求触发一个独立的 `git-upload-pack` 进程
- 有 `SYNC_STALE_AFTER`：让请求走本地 mirror 而不联 upstream

**核心差异**：git-cdn 是**面向抖动网络设计**的（内建 retry 是第一优先级），smart-git-proxy 是**面向"上游 + 缓存"设计**的（`SYNC_STALE_AFTER` 完全屏蔽上游是第一优先级）。

---

## 2. Task R1：重试机制端到端验证（**用户核心关切**）

同样的 iptables REJECT/DROP 场景，与 smart-git-proxy [task1_serve_stale.json](task1_serve_stale.json) 完全对齐。

| Phase | 场景 | git-cdn 结果 | smart-git-proxy 结果 |
|---|---|:---:|:---:|
| 1 | baseline | ✅ 2.83s | ✅ 0.86s（快是因为它 mirror 已 warm）|
| 2 | REJECT **全程 30s** + client 30s 超时 | ❌ 客户端超时；内部 **9 次连接错误**（重试仍在跑）| ❌ 立即 502（0.03s）|
| 3 | REJECT **5s 后放开** + client 60s 超时（新仓库）| ✅ **40.05s 成功**（内建 retry 吸收）| ❌ 立即 502（无 stale 可 fallback）|
| 4 | REJECT **15s 后放开** + client 60s 超时（新仓库）| ✅ **26.61s 成功** | ❌ 立即 502 |
| 5 | DROP 全程 + client 45s 超时 | ❌ 45s 超时（TCP 卡 SYN）| ❌ 130s 后失败（比 client 超时更慢）|

**关键洞察**：
1. **首次 clone 遇到 upstream 短暂中断**——smart-git-proxy 立即 502，git-cdn **100% 吸收 5-15s 中断**。这是本次报告最有价值的对比。
2. **DROP 场景两者都不好**——git-cdn 的 aiohttp 层能捕获快速失败但不能识别静默丢包；smart-git-proxy 要等 git-fetch 的 130s 默认 TCP 超时。**给 smart-git-proxy 打 patch 加 attempt-timeout 后才能收敛**（见 [reports/task5_retry_patch.json](task5_retry_patch.json)）。git-cdn 需要自己配合调低 aiohttp connect timeout（默认没有）。

原始数据：[reports/gitcdn_task_r1_retry.json](gitcdn_task_r1_retry.json)

---

## 3. Task R2：冷启动 + Warm clone 性能

| 仓库 | 阶段 | git-cdn | smart-git-proxy |
|---|---|:---:|:---:|
| 小仓 pre-commit-hooks（5.8 MB）| **冷启动**（首次 clone，含 mirror 建立）| 2.77s，mirror **1.4 MB** | ~2s，mirror 5.8 MB |
| 小仓 | warm clone | 0.93s | ~0.05s（走本地 upload-pack）|
| **大仓 sglang** | **冷启动** | **35.78s，mirror 235 MB** | 156s，mirror 1.8 GB |
| 大仓 | warm clone | **4.76s** | 8.78s |

**为什么 git-cdn 的 mirror 只有 235 MB（vs smart-git-proxy 1.8 GB）？**
- smart-git-proxy 用 `git clone --bare --mirror`（**完整历史**，1.8 GB）
- git-cdn 只把客户端**当前请求需要的 refs pack 转发**并缓存 pack，本地 git 仓库只维持必要的 refs 数据

**代价**：git-cdn 的 mirror 结构不能满足"任意历史 clone"的需求，只能满足"客户端来什么就有什么"。对大部分场景（CI + pre-commit）足够。

原始数据：[reports/gitcdn_task_r234_perf.json](gitcdn_task_r234_perf.json)

---

## 4. Task R3：并发性能（sglang 1.8 GB 大仓，mirror 已 warm）

| 并发 | git-cdn 成功率 | git-cdn avg | git-cdn min MemAvail | smart-git-proxy 成功率 | smart-git-proxy avg | smart-git-proxy min MemAvail |
|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| 20 | 20/20 | 19.64s | 8093 MB | 20/20 | 23.96s | 5576 MB |
| 50 | 50/50 | 38.96s | 7996 MB | 50/50 | 68.03s | 5576 MB |
| **100** | 100/100 | **73.81s** | **7727 MB** | 100/100 | 143.88s | **2403 MB（触发 OOM 看门狗）** |

**git-cdn 在 c=100 大仓并发场景压倒性优势**：
- 快 **1.95×**（73.81s vs 143.88s）
- 内存几乎不消耗（8.6 GB → 7.7 GB，仅 900 MB），smart-git-proxy 消耗 8 GB
- **不触发 OOM 看门狗**，smart-git-proxy 触发了

**根本原因**：git-cdn 用 **pack cache**——100 个并发请求同一 refs → git-cdn 计算一次 pack、cache 到共享文件、100 个 aiohttp response 从同一文件读；smart-git-proxy 用 **进程级 singleflight**（refs 同步阶段有 singleflight，pack 生成阶段每个客户端 fork 一个 git-upload-pack 子进程，100 个进程各自 pack 一份，消耗 100× 内存和 CPU）。

原始数据：[reports/gitcdn_task_r234_perf.json](gitcdn_task_r234_perf.json)

---

## 5. Task R4：tc netem 抖动稳定性

| 场景 | git-cdn | smart-git-proxy（同参数）|
|---|:---:|:---:|
| **无 netem** | 15/15 avg 0.87s | `stale=2s` 15/15 avg **7.14s**；`stale=600s` 15/15 avg 0.05s |
| **netem 10% loss + 500ms delay** | 15/15 avg **3.37s** | `stale=2s` 15/15 avg 2.61s；`stale=600s` 15/15 avg 0.05s |

**关键差异 —— upstream 解耦能力**：
- git-cdn 每次请求都要联 upstream 拉最新 refs（**没有等价的 stale 阈值**）——抖动直接透传到客户端延迟
- smart-git-proxy 配 `SYNC_STALE_AFTER=600s` 后**完全屏蔽 upstream**（0.05s vs 3.37s，**63× 提速**）

如果你的 CI 场景对"最新 refs"要求不高（例如 pre-commit hook 仓库），smart-git-proxy 的 stale 模式反而更快、更稳。

---

## 6. 落地建议

### 6.1 对你们的两类场景

| 场景 | 推荐 | 理由 |
|---|---|---|
| **pre-commit hooks（小仓，高频）** | 🎯 **两者都可以** | 都秒级，网络抖动都能扛。**smart-git-proxy `stale=600s` 更快**，但 git-cdn 部署更简单（一个 docker 就行）|
| **CI 大仓 clone（sglang 类）** | 🎯 **首选 git-cdn** | pack cache 让 c=100 场景内存/时延压倒性优势；智能 retry 无需打补丁 |
| **上游偶发抖动/504** | 🎯 **首选 git-cdn** | 双层内建 retry 是设计出来的第一价值，架构对齐生产痛点 |
| **要求"离线可用"** | 🎯 **smart-git-proxy + SYNC_STALE_AFTER=600s + warmup cron** | git-cdn 没有等价机制 |
| **私有仓库 SSO/复杂鉴权** | ⚠️ 两者都要评估 | git-cdn 强制 BasicAuth；smart-git-proxy 支持 pass-through |
| **协议 v2 需求** | 🎯 **只能 smart-git-proxy** | git-cdn 只支持 git v1 |

### 6.2 组合方案（如果规模允许）

考虑 **两者叠加**部署：
```
[client] → nginx → git-cdn (有 retry, pack cache) → smart-git-proxy (有 stale 屏蔽) → github.com
```
- git-cdn 负责客户端并发和上游 retry
- smart-git-proxy 负责 mirror 冷藏，提供本地不联 upstream 的 fallback
- 但架构复杂度显著上升，建议先只上 git-cdn 试

### 6.3 关键落地要点

1. **必须 PAT 认证**——git-cdn 强制 BasicAuth，公开仓库也不例外。对每个 CI runner / dev machine 需要 token 分发方案（用组织级 token 或 GitHub App）
2. **`REQUEST_MAX_RETRIES` / `BACKOFF_START` / `BACKOFF_COUNT` 三个 env 可调**——生产建议保持默认或稍降（10 次太多可能拖累失败反馈）
3. **`PACK_CACHE_SIZE_GB=20` 默认，看仓库规模调**——如果你们高频仓库很多，可以升到 100 GB+
4. **无 stale 阈值**——git-cdn 每次都要访问 upstream 拉 refs。如果 upstream 完全宕机，客户端仍会失败（连 retry 都无解）。**这是 git-cdn 的架构死角**，必须在生产考虑到

---

## 7. git-cdn 的已知问题（部署时要注意）

1. **官方 Docker 镜像 `forestscribe/git-cdn` 是 7 年前的**（Python 3.7），CA 证书链缺失，无法连接现代 HTTPS 服务。必须从 [bpaquet/git_cdn](https://github.com/bpaquet/git_cdn) 源码自行 build（Python 3.10-alpine 基础镜像，5 分钟内完成）
2. **强制 BasicAuth**：即使 upstream 允许匿名（公开 GitHub 仓库），git-cdn 也返 401 强制客户端认证。**无 patch 可关闭**
3. **git 协议 v1 only**：git 2.26+ 默认 v2，客户端会自动降到 v1；但如果你们依赖 v2 特性（partial clone、大规模 tag negotiation 优化）会退化
4. **无 push 加速**：push 走裸转发，不适合 CI 场景大规模 push；但对拉取场景不影响
5. **强制走 `.git` 后缀 URL**：客户端 clone URL 必须以 `.git` 结尾，否则 308 redirect
6. **进程内 token 明文写入 log**：git-cdn 会把 `git clone https://x-access-token:$PAT@github.com/...` 完整命令 log 到 stdout。**必须清理 container log**（或用 systemd 的 SyslogFilter，或改用 stdout 到 logstash pipeline 前 mask）

---

## 8. 复现命令

```bash
# 1. 用源码 build 新镜像（老 Docker Hub 镜像不能用）
git clone --depth 1 https://github.com/bpaquet/git_cdn.git
cd git_cdn && docker build -t git-cdn:local .

# 2. 起 container（github 上游 + PAT 认证）
export GH_PAT='ghp_xxx'   # <-- 你自己的 token，绝不进任何提交
docker run -d --name git-cdn \
    -p 127.0.0.1:18000:8000 \
    -v /var/lib/gitcdn:/workdir \
    -e MAX_CONNECTIONS=100 \
    -e GITSERVER_UPSTREAM=https://github.com/ \
    -e WORKING_DIRECTORY=/workdir \
    -e GUNICORN_WORKER=4 \
    -e REQUEST_MAX_RETRIES=10 \
    -e BACKOFF_START=0.5 \
    -e BACKOFF_COUNT=5 \
    git-cdn:local

# 3. Client 走 URL 改写 + basic auth
git config --global "url.http://x-access-token:${GH_PAT}@localhost:18000/.insteadOf" "https://github.com/"
git clone https://github.com/owner/repo.git   # 会自动改写到 git-cdn

# 4. 本仓库测试脚本
python3 gitcdn_task_r1_retry.py       # Task R1 重试机制
python3 gitcdn_task_r234_perf.py     # Task R2/R3/R4 性能+并发+netem
```

---

## 9. 交付物

**报告**：
- `reports/gitcdn_vs_smart_git_proxy.md`（本文件）
- `reports/gitcdn_task_r1_retry.json`
- `reports/gitcdn_task_r234_perf.json`

**测试脚本**（无 token 泄漏，token 由 env 传入并在日志中被 scrub）：
- `gitcdn_task_r1_retry.py`
- `gitcdn_task_r234_perf.py`

**smart-git-proxy 前次数据引用**（用作对比 baseline）：
- `reports/task1_serve_stale.json` — REJECT/DROP + 首次 clone 行为
- `reports/smart_git_proxy_comparison.json` — 冷/warm 性能
- `reports/smart_git_proxy_high_concurrency.json` — c=50/100 并发数据
- `reports/task2_stale_after.json` — SYNC_STALE_AFTER 效果
- `reports/task5_retry_patch.json` — 打补丁后行为
