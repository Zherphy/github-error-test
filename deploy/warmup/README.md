# 预热 cron / systemd timer（方案 B 配套）

配合 `SYNC_STALE_AFTER=600s`，让高频仓库常驻 warm，客户端请求 99% 走 `mirror-hit`。

## 部署（systemd timer 方式，推荐）

```bash
# 1. 部署脚本和 unit
sudo install -m 0755 sgp-warmup.sh /usr/local/bin/sgp-warmup
sudo install -m 0644 sgp-warmup.service /etc/systemd/system/
sudo install -m 0644 sgp-warmup.timer /etc/systemd/system/

# 2. 部署仓库列表
sudo mkdir -p /etc/smart-git-proxy
sudo install -m 0644 warmup-repos.txt.example /etc/smart-git-proxy/warmup-repos.txt
sudo vi /etc/smart-git-proxy/warmup-repos.txt   # 按需增删

# 3. 启动 timer
sudo systemctl daemon-reload
sudo systemctl enable --now sgp-warmup.timer

# 4. 查看状态和日志
sudo systemctl list-timers | grep sgp-warmup
sudo tail -f /var/log/sgp-warmup.log
sudo systemctl status sgp-warmup.service
```

## 部署（cron 方式，作为 fallback）

```bash
# /etc/cron.d/sgp-warmup
*/5 * * * * gitproxy /usr/local/bin/sgp-warmup >> /var/log/sgp-warmup.log 2>&1
```

## smart-git-proxy 侧的建议改动

配合本方案，把代理的 env 改一下：

```bash
# /etc/smart-git-proxy/env
SYNC_STALE_AFTER=600s     # 从 2s 拉长到 10 分钟
MIRROR_MAX_SIZE=80%       # LRU 阈值
LOG_LEVEL=info
```

**效果**：
- warmup 每 5 分钟触发一次 sync（在 SYNC_STALE_AFTER=600s 内）
- 客户端请求命中 mirror-hit 的概率大幅提高
- upstream 抖动只影响 warmup job，用户请求几乎不受影响

## 本仓库测试数据支持

见 [reports/task2_stale_after.json](../../reports/task2_stale_after.json)：
- `stale=2s` + tc netem 10% loss / 500ms delay：avg 每次 **2.61s**（每次都触发 sync，被抖动拖累）
- `stale=600s` + 同抖动条件：avg 每次 **0.05s**（全 mirror-hit，与 upstream 完全解耦）

**52× 提速 + 完全解耦于 upstream 抖动**——这是方案 B 的核心价值。

## 监控建议

warmup 日志按行 JSON 输出，接入你们的日志系统后可以拉两条告警：

- **单次 warmup 全部失败** → upstream 或代理有问题，立即告警
- **单个仓库连续 3 次 warmup 失败** → 该仓库有问题（重命名？归档？鉴权变了？）

Prometheus 指标（可选）：可用 `mtail` 或直接改 `sgp-warmup.sh` 输出 `.prom` 文件供 node-exporter textfile collector 读取。
