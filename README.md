# GitHub 504 Gateway Timeout 复现测试

复现 GitHub 并发访问时出现 **504 Gateway Timeout** 的测试工具集，包含渐进加压脚本、多模式测试框架，以及真实测试报告。

## 背景

504 Gateway Timeout 是 HTTP 协议中网关/代理层返回的错误码，表示代理服务器等待上游（GitHub 后端）响应超时。在实际生产环境中，常见于：

- 企业代理/WAF 后方访问 GitHub，代理 `proxy_read_timeout` 设置过短
- GitHub 服务降级或故障期间
- 高并发 git clone / API 请求导致 GitHub 后端响应延迟
- 网络质量差区域（跨国访问、CDN 覆盖不足）

## 文件说明

| 文件 | 说明 |
|---|---|
| `reproduce_504.py` | 完整版测试框架 — 3 种模式（api / git / proxy），全参数化配置 |
| `stress_test_504.py` | 渐进加压版 — 5 轮自动递增，直接运行即可复现 504 |
| `reports/504_test_report.json` | JSON 格式原始测试数据 |

## 快速开始

### 安装依赖

```bash
pip install aiohttp
```

### 1. 渐进加压复现（最简单）

```bash
python3 stress_test_504.py
```

自动运行 5 轮测试：并发从 5→50 递增，超时从 30s→8s 递减，观察 504 何时出现。

### 2. 完整框架 — API 加压

```bash
# 未认证（限流 60 次/小时，易触发 403）
python3 reproduce_504.py --mode api --start-concurrency 10 --max-concurrency 80

# 带 Token（限流 5000 次/小时，可打更高并发）
python3 reproduce_504.py --mode api --token ghp_xxxx --max-concurrency 200
```

### 3. 完整框架 — 并发 Git Clone

```bash
# 默认：并发 clone sglang 仓库
python3 reproduce_504.py --mode git --git-count 20 --git-timeout 15

# 指定大仓库（clone 更慢 → 更易超时）
# 修改脚本中 REPO_URL 即可，如：
#   REPO_URL = "https://github.com/kubernetes/kubernetes.git"
#   REPO_URL = "https://github.com/torvalds/linux.git"
```

### 4. 完整框架 — 模拟代理超时

```bash
# 极短超时模拟 WAF/代理层行为（最容易触发 504 机制）
python3 reproduce_504.py --mode proxy --proxy-timeout 3

# 更激进
python3 reproduce_504.py --mode proxy --proxy-timeout 1
```

### 5. 三种模式依次运行

```bash
python3 reproduce_504.py --mode all
```

## 参数说明

### reproduce_504.py

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--mode` | `api` | 测试模式：`api` / `git` / `proxy` / `all` |
| `--start-concurrency` | 10 | 初始并发量 |
| `--max-concurrency` | 80 | 最大并发量 |
| `--step` | 10 | 每轮并发增量 |
| `--rounds` | 6 | 最大测试轮数 |
| `--token` | None | GitHub Personal Access Token |
| `--delay` | 5.0 | 轮间等待秒数 |
| `--git-count` | 10 | 每轮并发 git clone 数 |
| `--git-timeout` | 30.0 | git clone 超时秒数 |
| `--proxy-timeout` | 3.0 | 模拟代理超时秒数（越短越易出 504） |

## 测试报告

### 测试环境

- **目标仓库**：`https://github.com/sgl-project/sglang.git`（大型仓库，`--depth 1` clone 约 5-14s）
- **策略**：渐进加压 — 并发递增 + 超时递减
- **超时含义**：模拟中间代理/网关的 `proxy_read_timeout` 设置

### 测试结果

| 轮次 | 并发数 | 代理超时 | 成功 | 失败 | 超时(=504) | 平均耗时 | 最大耗时 | 结果 |
|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---|
| 1 | 5 | 30s | 5 | 0 | 0 | 4.82s | 7.04s | 🟢 正常 |
| 2 | 10 | 20s | 10 | 0 | 0 | 7.98s | 13.49s | 🟢 正常 |
| 3 | **20** | **15s** | 15 | 5 | **5** | 11.19s | 15.04s | **🔴 504 首次出现** |
| 4 | 30 | 10s | 2 | 28 | **28** | 9.93s | 10.25s | 🔴 大规模超时 |
| 5 | 50 | 8s | 1 | 49 | **49** | 8.34s | 8.50s | 🔴 几乎全部超时 |

### 关键发现

1. **504 首次触发**：并发 20 + 代理超时 15s 时首次出现 504，5/20 个 clone 超时
2. **急剧恶化**：并发 30 时超时率升至 93%（28/30），并发 50 时达 98%（49/50）
3. **典型错误信息**：`fatal: early EOF` — git 协议层在连接被中途切断时的表现，与真实 504 场景一致
4. **阈值规律**：当并发请求导致 GitHub 响应时间 > 代理超时阈值时，504 必然出现

### 504 原因分析

```
客户端 → [代理/WAF/CDN] → GitHub 后端

正常：客户端请求 → 代理转发 → GitHub 5s 内响应 → 代理返回 200 ✓
504：  客户端请求 → 代理转发 → GitHub 响应耗时 15s → 代理超时(10s) → 返回 504 ✗
```

504 不是 GitHub 主动返回的状态码，而是**中间代理层**在等待 GitHub 响应超时后生成的。核心触发条件：

| 因素 | 影响 |
|---|---|
| **并发量过高** | GitHub 后端排队处理，单请求响应时间延长 |
| **代理超时阈值过短** | 如企业 WAF 设 `proxy_read_timeout=10s`，正常 clone 15s 就被截断 |
| **仓库体积大** | clone 耗时更长，更容易超过超时阈值 |
| **网络质量差** | 跨国/跨区域访问增加传输时间 |
| **GitHub 服务降级** | 响应时间整体延长 |

### 修复建议

| 场景 | 建议 |
|---|---|
| 企业代理后方 | 调大 `proxy_read_timeout` 至 60s+，或对 git 域名单设规则 |
| CI/CD 管道 | 控制并发 ≤ 10，使用 `--depth 1` 浅克隆，加 `retry` 机制 |
| API 调用 | 使用 Token 认证提升限流阈值，指数退避重试 |
| 大仓库 clone | 浅克隆 `--depth 1`，分批拉取，避免全量 clone |

---

## 许可证

MIT License
