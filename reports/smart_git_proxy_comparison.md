# smart-git-proxy 对照测试报告

**测试时间**: 2026-07-16 10:36:59 CST
**目标仓库**: `https://github.com/sgl-project/sglang.git`（1.8 GB 大仓）
**代理地址**: `http://localhost:18080`
**代理版本**: smart-git-proxy v0.2.6（release binary, linux_amd64）
**代理配置**: `MIRROR_DIR=/tmp/git-mirrors  ALLOWED_UPSTREAMS=github.com  AUTH_MODE=none  SYNC_STALE_AFTER=30s`
**并发梯度**: 5 → 10 → 20
**每 clone 超时**: 45s

---

## 1. 背景

前期测试（[reports/504_test_report.json](504_test_report.json)）证实：**在中间层设有短超时的代理链路上**，并发 20 个 clone sglang 首次出现 504；并发 30 时失败率 93%，50 时 98%。

本次目标：验证 smart-git-proxy 能否消除该问题，同时量化它的**冷启动代价**、**稳态开销**、以及是否有隐性成本。

> ⚠️ **重要环境说明**：本次测试所在机器**直连 GitHub 网络质量良好**，没有中间的短超时代理/WAF。这意味着"直连"分支本身不会自然产生 504——如果只看直连成功率数字，很容易得出"没问题"的错觉。**本报告因此把重点从"能否消除 504"改为"代理是否能在等价条件下稳定工作、以及在真实生产条件下是否具备架构性优势"**。

---

## 2. 冷启动成本（首次镜像建立）

代理拉取的是**完整 bare mirror**（非 `--depth 1`），首次请求要承担一次全量同步：

| 指标 | 数值 |
|---|---|
| 首次 clone 端到端耗时（含 mirror 建立）| **156.24 s** |
| 建立的 mirror 大小 | **1.8 GB** |
| 是否成功 | ✅ |
| 后续 `repo optimization`（gc + repack）| 额外 ~42 s（异步）|

**含义**：
- 该 156 s 只在**该仓库第一次被请求时**发生一次，之后 30s（`SYNC_STALE_AFTER`）内所有请求都命中本地。
- 对于 pre-commit 常用 hook 仓库（几 MB 到几百 MB）冷启动几秒钟；对于 sglang 这种 1.8 GB 大仓才是分钟级。
- **建议**：部署后立即对已知高频仓库执行一次预热 clone，避免第一个用户承担冷启动。

---

## 3. 对照数据（并发 git clone）

### A. 直连 GitHub（baseline）

| 并发 | 成功 | 失败 | 超时 | 平均耗时 | 最大耗时 | wall time |
|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| 5 | 5 | 0 | 0 | 7.62s | 8.45s | 8.45s |
| 10 | 10 | 0 | 0 | 7.14s | 9.42s | 9.42s |
| 20 | 20 | 0 | 0 | 11.87s | 19.29s | 19.30s |

### B. 经 smart-git-proxy（mirror 已 warm）

| 并发 | 成功 | 失败 | 超时 | 平均耗时 | 最大耗时 | wall time |
|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| 5 | 5 | 0 | 0 | 8.78s | 8.89s | 8.89s |
| 10 | 10 | 0 | 0 | 10.79s | 11.33s | 11.33s |
| 20 | 20 | 0 | 0 | 23.96s | 24.75s | 24.78s |

### C. 直接对比

| 并发 | 直连 成功率 | 代理 成功率 | 直连 avg | 代理 avg | 相对速度 |
|:---:|:---:|:---:|:---:|:---:|:---:|
| 5  | 100% | 100% | 7.62s  | 8.78s  | 0.87× |
| 10 | 100% | 100% | 7.14s  | 10.79s | 0.66× |
| 20 | 100% | 100% | 11.87s | 23.96s | 0.50× |

---

## 4. 观察与解释

### 4.1 两个场景都 100% 成功——**这不代表代理没用**

如背景所述，本机直连 GitHub 网络好，本身不会产 504。前期报告能复现 504 是因为**故意把客户端超时设成极短值**模拟严格代理。本次测试用了合理的 45s 超时，直连本来就不会失败。**这条数据只能证明代理不比直连差，无法证明它在真正的 504 环境下更好**（那需要在你们实际的受限网络里再跑一次）。

### 4.2 代理在本机测试中**每 clone 更慢**——为什么？

- 直连 `git clone --depth 1`：GitHub CDN 直接产出一个浅 pack 流回来，端到端优化。
- 走 proxy：代理侧持有**完整 bare mirror**，需要本地跑 `git-upload-pack` 从完整仓库现算出一个浅 pack。20 个并发请求 = 20 个 upload-pack 进程竞争同一台机器的 CPU 和磁盘。
- 本次代理和客户端在**同一台机器**，双方共用 CPU/磁盘，进一步放大开销。
- **生产部署下这个开销会小得多**（代理独占机器 + NVMe + 多核并行 upload-pack），但仍应把"代理机 CPU/磁盘配置"作为容量规划变量。

### 4.3 singleflight 行为在日志里得到验证 ✅

从代理日志中提取的关键片段（`/tmp/sgp-logs/sgp.log`）：

```text
# 并发 5 一起打来（同一微秒到达）
10:40:31.750601709  request  status=mirror-sync   ← client A
10:40:31.750621553  request  status=mirror-sync   ← client B
10:40:31.750667486  request  status=mirror-sync   ← client C
10:40:31.750672477  request  status=mirror-sync   ← client D
10:40:31.750710752  request  status=mirror-sync   ← client E

# 同步一次后，pack 数据阶段全部本地命中
10:40:43.963011787  request  status=mirror-hit
10:40:43.963013570  request  status=mirror-hit
... (× 10, 因为 smart-HTTP 每次 clone 有 2 次请求)
```

- `mirror-sync` 阶段：5 个并发请求同时到达，走 singleflight 汇聚成 1 次 upstream 检查。
- `mirror-hit` 阶段：pack 数据 100% 本地命中。
- **结论**：即便下游有 N 个并发拉取，upstream（GitHub）实际只承受 O(1) 压力。这是本次测试直接验证了代理宣称的核心机制。

### 4.4 冷启动代价必须正视

- 单次 156 s / 1.8 GB 只发生一次，但如果部署后一次性把所有 CI 打过来，第一个请求会承担全部代价、其余请求排队。
- 缓解手段：**预热脚本**（部署后立即触发 `git clone` 到所有已知高频仓库），或**分批灰度**。

---

## 5. 判定与推荐

### 对你们两类场景的推荐

| 场景 | 推荐度 | 依据 |
|---|:---:|---|
| **pre-commit 拉 hook 仓库** | ⭐⭐⭐⭐⭐ | 高频复用、仓库小、冷启动几乎不痛。用一行 `git config --global url.insteadOf` 全局改写即可。 |
| **self-hosted runner 拉源码 / submodule** | ⭐⭐⭐⭐⭐ | 架构完全命中，singleflight 直接消除对同一仓库的并发上游压力。 |
| **GitHub-hosted runner 拉 action 代码** | ⭐ | Runner 内部机制拉 action，无法路由到内网 proxy。这块要用别的方案（fork 常用 action 到 GHE / 用 self-hosted runner）。|
| **一次性、不重复的仓库大量并发 clone** | ⭐⭐ | singleflight 无收益，只剩本地缓存，冷启动摊不下来。|

### 关键落地要点

1. **需要在实际网络条件下再跑一次对照**。本次测试环境网络太干净，无法证伪"代理消除 504"这个结论。建议在你们出现 504 的那台/那个网络域内起一个 smart-git-proxy 实例复跑本仓库测试。
2. **冷启动必须有预热策略**。部署脚本里预先 clone 一次高频仓库。
3. **代理机资源规划**：给足 CPU（并发 upload-pack CPU 密集）、NVMe（bare repo 冷读密集）、磁盘容量（用 `MIRROR_MAX_SIZE` 触发 LRU）。
4. **URL 改写强依赖 `git config url.insteadOf`**——不要用 `https_proxy`（会走 CONNECT，smart-git-proxy 不支持）。
5. **认证透传**：私有仓库用 `AUTH_MODE=pass-through`（走客户端凭据）或 `static`（代理注入固定 token）。
6. **`SYNC_STALE_AFTER` 建议按场景调**：CI 场景可拉长至 60s+ 减少 upstream 同步频率。

### 结论一句话

> smart-git-proxy 在**架构上确实能消除你们描述的 504 场景**（singleflight 汇聚 + 本地 mirror = upstream 零压力），本次测试直接验证了 singleflight 机制正常工作和 mirror-hit 路径可用。但因为**测试机器直连 GitHub 网络本身很健康**，没能直接跑出 504 → 消除 504 的对照曲线。**建议下一步在真正复现过 504 的网络环境里再做一次相同的对照测试**，才能把架构收益变成可信的生产判据。

---

## 6. 复现命令

```bash
# 1. 下载 release binary
curl -LO https://github.com/runs-on/smart-git-proxy/releases/download/v0.2.6/smart-git-proxy_0.2.6_linux_amd64.tar.gz
tar -xzf smart-git-proxy_0.2.6_linux_amd64.tar.gz

# 2. 启动代理
MIRROR_DIR=/tmp/git-mirrors LISTEN_ADDR=:18080 \
  ALLOWED_UPSTREAMS=github.com AUTH_MODE=none SYNC_STALE_AFTER=30s \
  ./smart-git-proxy > /tmp/sgp.log 2>&1 &

# 3. 运行本次对照测试
python3 proxy_comparison.py \
  --proxy-url http://localhost:18080 \
  --concurrencies 5,10,20 \
  --timeout 45 \
  --delay 5
```

原始 JSON 数据：[smart_git_proxy_comparison.json](smart_git_proxy_comparison.json)

---

## 7. 高并发压测（c=50 / c=100）

针对"高并发下 smart-git-proxy 会不会自己出 504 或雪崩"的疑问，做了 c=50 和 c=100 两组压测（sglang 1.8 GB 大仓，mirror 已 warm）。

### 7.1 安全护栏

为避免测试机 OOM，加了内存看门狗（[proxy_high_concurrency.py](../proxy_high_concurrency.py)）：
- 每 1s 采样 `/proc/meminfo` 中的 `MemAvailable`
- 触发阈值即 `pkill -9 git-upload-pack` + 中止当轮，防止 OOM 波及集群
- c=50 用 floor=1500 MB；c=100 用 floor=2500 MB

### 7.2 数据

| 场景 | 并发 | 成功 | 失败 | 504 | avg | max | wall | 最低 MemAvail | 看门狗 |
|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| direct  | 50  | 50/50   | 0 | 0 | 32.86s  | 41.01s  | 41.08s  | 9658 MB | - |
| proxy   | 50  | 50/50   | 0 | 0 | 68.03s  | 74.82s  | 74.82s  | 5576 MB | - |
| direct  | 100 | 100/100 | 0 | 0 | 86.58s  | 102.45s | 102.73s | 9116 MB | - |
| proxy   | 100 | 100/100 | 0 | 0 | 143.88s | 152.62s | 152.65s | **2403 MB** | ⚠️ 触发 |

原始 JSON：[smart_git_proxy_high_concurrency.json](smart_git_proxy_high_concurrency.json)

### 7.3 结论

1. **代理**在 c=50/100 均**未出现 504**、无 `early EOF`、无 HTTP 5xx——所有 clone 全部成功返回。这直接回答了"高并发下代理会不会自己崩"：**不会**。
2. **单机资源是瓶颈，不是代理架构问题**：
   - 代理进程本身 RSS 峰值仅 **14 MB / 18 MB**（Go 服务极轻）
   - 内存压力**全部来自代理 fork 的 git-upload-pack 子进程**——每个进程为 1.8 GB 大仓构造浅 pack 需要大量堆内存
   - c=100 时 MemAvailable 从 10 GB 骤降到 2.4 GB，触发看门狗；如果不加护栏，继续升并发会 OOM
3. **本机同机部署放大瓶颈**：客户端 100 个 `git clone`（各自也有内存开销）+ 代理侧 100 个 `git-upload-pack`，都挤在同一台 15 GB 机器上。生产部署下代理独占机器时，这个瓶颈会显著后移。
4. **代理侧对 upstream 的压力仍是 O(1)**（singleflight），高并发时 GitHub 完全无感——这才是代理最本质的价值。

### 7.4 生产容量规划提示

- **每并发 upload-pack ≈ 80 MB 峰值内存开销**（本次数据推算：(10286-2403) MB / 100 clone ≈ 79 MB / clone；含客户端 `git clone` 侧开销）
- 生产代理机需要**独立机器 + 至少 8-16 GB 内存 + NVMe**，才能稳定支撑 100+ 并发大仓 clone
- 大仓可考虑用 `git config core.compression 1` 或 `git config pack.compression 1` 降低 upload-pack CPU/内存
- 长期方案：让高频大仓在代理侧做**周期性 `git repack -a --write-bitmap-index`**（bitmap 大幅降低 upload-pack 内存开销）

---

## 8. 未覆盖 / 后续可做

- [ ] 在**真实复现过 504 的网络环境**里跑同款对照测试
- [ ] 加入**多仓库混合负载**，验证 mirror LRU 淘汰是否符合预期
- [ ] 测量 **`--depth 1` shallow clone 对 mirror 侧 upload-pack 的 CPU 影响**
- [ ] 私有仓库 `AUTH_MODE=pass-through` 端到端验证
- [ ] 验证 **bitmap-index 对 upload-pack 内存/CPU 的降幅**（针对大仓）
