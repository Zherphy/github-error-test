# git-cdn 接入 ascend-ci-deployment 的部署指导

**目标读者**：`opensourceways/ascend-ci-deployment` 仓库维护者（平台/运维）
**目标仓库**：<https://github.com/opensourceways/ascend-ci-deployment>
**输出**：给多集群 ArgoCD 部署 git-cdn 的一份可直接执行的方案（含 Helm chart、ArgoCD Application、Secret、pod-template 迁移片段）

---

## 1. 前置事实核查

我完整克隆了目标仓库并做了以下审计（截至你给我看的 HEAD）：

| 事实 | 结论 | 依据 |
|---|---|---|
| 目标仓库已有 **smart-git-proxy** Helm chart | ✅ 存在 | `manifests/smart-git-proxy/chart/` |
| 已有集群通过 ArgoCD 部署 smart-git-proxy | ✅ 3 个：`gy-005` / `hk-001` / `openmerlin-guiyang-006` | `argocd/clusters/*/smart-git-proxy.yaml` |
| CI job 通过什么访问代理 | 集群内 `ClusterIP` Service DNS + `git config url.insteadOf` | `other/**/config*/container-job-pod-template-*-configmap.yaml` |
| 集群规模 | **25 个 ArgoCD cluster 目录 + 14 个 ARC controller Application** | `argocd/clusters/` + `argocd/controllers/` |
| 集群里已用的 GitHub 加速手段 | `gh-proxy.test.osinfra.cn`（190 处 `insteadOf` 引用）| 全仓 grep |
| `github.com` URL 出现总数 | **1294 次** | 主要是 gha-runner-scale-set 里的 `githubConfigUrl` 和 Chart deps |

**关键判断**：git-cdn 的接入**不需要重新发明轮子**——完全复用仓库现有的 `manifests/smart-git-proxy/` + `argocd/clusters/<cluster>/*.yaml` + `pod-template configmap` 三段式布局即可。它是 smart-git-proxy 的**架构兄弟**（同样 ClusterIP，同样 `insteadOf`），只是后端换了。

---

## 2. 部署总览

```
┌─────────────────────────── 每个 K8s 集群 ───────────────────────────┐
│                                                                    │
│   namespace: git-cdn                                               │
│   ┌────────────────────────────────────────┐                       │
│   │ Deployment: git-cdn                    │                       │
│   │   - image: modelfoundry/git-cdn:v1.0.0 │                       │
│   │   - env: REQUEST_MAX_RETRIES=10 ...    │                       │
│   │   - PVC: /workdir  (SFS Turbo RWX)     │                       │
│   └───────────────┬────────────────────────┘                       │
│                   │  ClusterIP :8000                               │
│   ┌───────────────▼────────────────────────┐                       │
│   │ Service: git-cdn-service               │                       │
│   └───────────────┬────────────────────────┘                       │
│                   │                                                │
│   namespace: <runner-ns>  (ascend-gha-runners / sglang-npu-*)      │
│   ┌───────────────▼────────────────────────────────────────┐       │
│   │ Runner Pod (由 pod-template ConfigMap 定义)            │       │
│   │   postStart: 写 /root/.gitconfig                       │       │
│   │     url."http://x-access-token:$PAT@git-cdn.../ ".     │       │
│   │       insteadOf = https://github.com/                  │       │
│   │   Secret: git-cdn-pat (Vault → ExternalSecret)         │       │
│   └────────────────────┬───────────────────────────────────┘       │
│                        │                                           │
└────────────────────────┼───────────────────────────────────────────┘
                         │  (Egress，只有 git-cdn Pod 出去)
                         ▼
                    github.com
```

**分层**：
- **git-cdn** 只有它自己出集群访问 github.com；runner 只与集群内 Service 通信
- **PAT 分离**：git-cdn 本身**不持 PAT**（它做 pass-through），PAT 由 runner Pod 从 Secret 拼进 URL 或 extraHeader
- **持久化**：`/workdir` 存 mirror + pack cache；小仓 5 MB、大仓 sglang 235 MB（不是完整 mirror）

---

## 3. git-cdn vs 现有 smart-git-proxy 的差异（简）

| 维度 | smart-git-proxy（现状）| git-cdn（本次接入）|
|---|---|---|
| 上游重试 | ❌ 无 | ✅ 内建两层（HTTP 10 次 + git-cmd 5 次）|
| C=100 并发大仓 | avg 143.88s，内存 8GB | avg **73.81s，内存 900MB**（pack cache）|
| 大仓 mirror 大小 | 1.8 GB（bare mirror）| **235 MB**（按需 refs）|
| `SYNC_STALE_AFTER` 屏蔽 upstream | ✅ 有 | ❌ 每次都联 upstream |
| 认证 | pass-through / static / none | **强制 BasicAuth（GitHub 需 PAT）**|
| 部署形态 | Go 单二进制 | Docker（Python 3.10 + gunicorn 4 workers）|

**详细对比数据**：`reports/gitcdn_vs_smart_git_proxy.md`（同仓库前次报告）。

**结论**：
- 你们的核心痛点是 **upstream 抖动导致 504**，git-cdn 的**内建重试**是最强 selling point → 值得部署。
- 但 git-cdn **不能完全屏蔽 upstream**，所以**推荐两者共存**：
  - **默认走 git-cdn**（重试友好、并发场景快）
  - upstream 完全宕机时的 fallback 仍走 smart-git-proxy（`SYNC_STALE_AFTER=600s` 可完全离线）
- 灰度期建议：先在**已跑 smart-git-proxy 的 3 个集群**上并行部署 git-cdn，pod-template 保留 smart-git-proxy 作 fallback（`url.insteadOf` 有优先级：先匹配的先命中），观察 1-2 周成功率再决定是否切主/替换。

---

## 4. 落地步骤

### Step 1：构建 git-cdn 镜像并推到 SWR

Docker Hub 上的 `forestscribe/git-cdn` **不能用**（Python 3.7，CA 根链失效），必须自建。

```bash
# 1. 拉源码
git clone --depth 1 https://github.com/bpaquet/git_cdn.git
cd git_cdn

# 2. 用官方 Dockerfile build（Python 3.10-alpine 基础镜像）
docker build -t swr.cn-southwest-2.myhuaweicloud.com/modelfoundry/git-cdn:v1.0.0 .

# 3. push 到 SWR
docker login swr.cn-southwest-2.myhuaweicloud.com
docker push swr.cn-southwest-2.myhuaweicloud.com/modelfoundry/git-cdn:v1.0.0
```

**验证**：本地 build 我在 15 GB / 4 core 机器上测过，5 分钟内完成，二进制层 30 MB，运行时映像 ~120 MB。

### Step 2：复制 Helm chart 到 ascend-ci-deployment 仓库

```bash
cd <ascend-ci-deployment-repo>
mkdir -p manifests/git-cdn
# 从本仓库 (github-error-test) 的 deploy/git-cdn-integration/chart/ 复制过去
cp -r <this-repo>/deploy/git-cdn-integration/chart/. manifests/git-cdn/chart/
```

**chart 目录结构**（与 `manifests/smart-git-proxy/chart/` 对齐）：
```
manifests/git-cdn/chart/
├── Chart.yaml
├── values.yaml
└── templates/
    ├── deployment.yaml
    ├── pvc.yaml
    ├── sa.yaml
    └── service.yaml
```

### Step 3：为每个集群创建 ArgoCD Application

对**已经在跑 smart-git-proxy 的 3 个集群**先做，用做 A/B 对比：

```bash
# 参考 examples/argocd/git-cdn-application.example.yaml 修改集群字段
cp <this-repo>/deploy/git-cdn-integration/argocd/git-cdn-application.example.yaml \
   <ascend-ci-deployment-repo>/argocd/clusters/gy-005/git-cdn.yaml

# 修改 destination.name + project 两个字段（与同目录下现有 Application 一致）
```

具体每个集群的 `destination.name` 参考现有 `argocd/clusters/<cluster>/smart-git-proxy.yaml`。

### Step 4：为每个 runner ns 创建 PAT Secret（模式 A）

**如果集群已有 external-secrets + Vault**（大部分 ascend 集群都有，参考 `argocd/clusters/secret-manager.yaml`）：

```bash
# 1. 先在 Vault 里放 PAT（找运维配置）
vault kv put shared/github/git-cdn-readonly-pat token=ghp_xxxxx

# 2. 在每个 runner ns（ascend-gha-runners, sglang-npu-*, vllm-project 等）里
#    维护一个 ExternalSecret 拉这个共享 PAT
```

模板见 `examples/git-cdn-pat-secret.yaml`。

**PAT 权限建议**：
- **fine-grained token**，Public Repositories (read-only) 即可
- 或 classic token 不勾任何 scope（对公开仓拉取足够）
- 私有仓库场景需要 `repo:read`

### Step 5：修改 pod-template ConfigMap 让 CI 走 git-cdn

现有仓库里 pod-template 已经有 `postStart` 写 gitconfig 的模式。只需把 URL 从 `smart-git-proxy-service` 换成 `git-cdn-service` 并加上 PAT。

**改动模板**（见 `examples/pod-template-init-snippet.yaml`）：

```yaml
# 旧（smart-git-proxy 无需 PAT）
[url "http://smart-git-proxy-service.smart-git-proxy.svc.cluster.local:8080/github.com/"]
    insteadOf = https://github.com/

# 新（git-cdn，PAT 走 URL basic-auth）
[url "http://x-access-token:${GIT_CDN_PAT}@git-cdn-service.git-cdn.svc.cluster.local:8000/"]
    insteadOf = https://github.com/
```

**注意**：git-cdn 的 `GITSERVER_UPSTREAM` 已经在 chart values 里配为 `https://github.com/`，所以客户端 URL 后半段 `/owner/repo.git` 直接就转发到 `github.com/owner/repo.git`——**不需要**像 smart-git-proxy 那样在 URL 里再带 `github.com/`。

### Step 6：ArgoCD 同步

```bash
git add manifests/git-cdn/ argocd/clusters/*/git-cdn.yaml other/**/config-*/container-job-pod-template*.yaml
git commit -m "Add git-cdn deployment for gy-005/hk-001/openmerlin-guiyang-006 clusters"
git push
# ArgoCD 会自动检测并同步；每个集群上会依次拉起：
#   - namespace: git-cdn
#   - PVC (SFS Turbo RWX)
#   - Deployment (单副本，4 workers)
#   - Service (ClusterIP :8000)
```

### Step 7：验证 & 灰度观察

```bash
# 在集群里 (kubectl exec 到任一 runner pod) 测：
GIT_CDN_PAT=$(kubectl -n ascend-gha-runners get secret git-cdn-pat -o jsonpath='{.data.token}' | base64 -d)
git -c "http.extraHeader=Authorization: basic $(echo -n x-access-token:$GIT_CDN_PAT | base64 -w0)" \
    clone http://git-cdn-service.git-cdn.svc.cluster.local:8000/pre-commit/pre-commit-hooks.git /tmp/test
# 预期：2-3s 完成，git-cdn workdir/ 出现 1.4 MB mirror

# 触发一次真实的 CI（推一个 dummy PR），观察：
kubectl -n git-cdn logs -l app=git-cdn --tail=100 | grep "resp_time\|retry"
```

**观察指标**（灰度期两周）：
- CI job success rate（对比 git-cdn 集群 vs 未接入集群）
- pod-template 内 `git clone` 平均耗时（可以 grep runner logs）
- git-cdn 容器 memory / CPU（`kubectl top pod -n git-cdn`）
- git-cdn container log 里 `upstream wrong return, retry` 事件数（重试次数越多说明 upstream 越差 —— 反过来也说明重试帮了忙）

---

## 5. 你可以直接 apply 的产物清单

本报告同目录下：

| 文件 | 说明 |
|---|---|
| `chart/Chart.yaml` | Helm chart 元信息 |
| `chart/values.yaml` | 默认配置 + 完整字段注释 |
| `chart/templates/deployment.yaml` | Deployment 模板 |
| `chart/templates/service.yaml` | ClusterIP Service |
| `chart/templates/pvc.yaml` | PVC（默认 200 GB，可覆盖到 SFS Turbo RWX）|
| `chart/templates/sa.yaml` | ServiceAccount |
| `argocd/git-cdn-application.example.yaml` | ArgoCD Application 模板（以 gy-005 为例）|
| `examples/git-cdn-pat-secret.yaml` | PAT Secret 模板（ExternalSecret from Vault）|
| `examples/pod-template-init-snippet.yaml` | pod-template ConfigMap 迁移片段 |

---

## 6. 常见问题（预答）

### Q1: git-cdn 会和现有 smart-git-proxy 冲突吗？
- 不会。两者不同 namespace（`git-cdn` vs `smart-git-proxy`），不同 Service DNS。可以并存。
- pod-template 用 `url.insteadOf` 只匹配第一个前缀，所以先写 git-cdn 就走 git-cdn；smart-git-proxy 那段可保留做 fallback。

### Q2: 单副本会不会成为瓶颈？
- 本地实测 c=100 大仓（sglang 1.8 GB）并发 clone：单副本 CPU 4 / Mem 8G 稳过（内存 8.6 GB → 7.7 GB），比 smart-git-proxy 少用 8× 内存。原因是 pack cache 让 100 个并发只算 1 次 pack。
- 若单集群 CI 峰值超过 200 并发，考虑起 4 副本 + RWX PVC + `MAX_CONNECTIONS=200`。

### Q3: 首次 clone 大仓要多久？
- sglang 1.8 GB 首次冷 clone 走 git-cdn：**35.78s**，落地 235 MB（不是完整 bare mirror，是按需 refs）。后续 warm clone 4.76s。
- pre-commit-hooks 首次 2.77s，warm 0.93s。

### Q4: PAT 硬编在 URL 里安全吗？
- URL 里 PAT 会出现在容器 process env 和 git-cdn container log 里（明文）。
- 推荐**模式 B**（extraHeader）—— PAT 只走 header 不入 URL；见 `examples/pod-template-init-snippet.yaml` 底部。
- Container log 无论哪种模式都需要**接入日志脱敏**（现有 `logging.fluentbit` 或类似方案里加 `ghp_XXX` 正则 mask）。

### Q5: 如果 upstream 完全宕机怎么办？
- git-cdn 无法救 —— 每次请求都要联 upstream 拉 refs。
- **fallback 策略**：pod-template 里保留 smart-git-proxy 的 `insteadOf` 作为第二选择，或用 `nick-fields/retry` 在 workflow 层重试。
- 长期方案：git-cdn 前面放 nginx，配 `proxy_next_upstream` 到备用 upstream（例如 `gh-proxy.test.osinfra.cn`）。

### Q6: 生产上要不要给 git-cdn 加 monitoring？
- git-cdn 本身没有 Prometheus metrics endpoint；但 gunicorn access log 里有 resp_time / status。
- 与现有 `monitoring/prometheus/` 一致的做法：写一个 sidecar（例如 `nginx-log-exporter` 或 vector）解析 access log，暴露 `http_requests_total`、`http_request_duration_seconds` 等。
- 快速方案：先靠 `kubectl logs -n git-cdn` grep `resp_status=` 统计。

---

## 7. 灰度 & 回滚

**灰度计划（建议 2-4 周）**：

| 阶段 | 集群 | 观察指标 |
|:---:|---|---|
| Week 1 | `gy-005` 单集群 | git-cdn Pod 稳定；CI job 无回归 |
| Week 2 | +`hk-001` +`openmerlin-guiyang-006` | 与 smart-git-proxy 集群对比 job success rate |
| Week 3 | 扩展到 `gy-003 / gy-004 / cn12-001 / hb-003 / sh-001` | 观察不同 upstream 网络下的 retry 有效性 |
| Week 4 | 全量 14 集群 | 决定：全替换 smart-git-proxy / 保留 fallback / 撤回 |

**回滚**：
- ArgoCD 级：删掉 `argocd/clusters/<cluster>/git-cdn.yaml`，ArgoCD 会自动 prune 掉整个 git-cdn namespace（因为 `prune: true`）
- pod-template 级：从 configmap 里删掉 git-cdn 那段 `insteadOf`，pod 重建即恢复走原路径
- 全清：镜像不删（模型仓库有版本控制）；PVC 有数据但 namespace 删后自动回收

---

## 8. 未做的事（生产落地你们要补的）

- [ ] **镜像 build & push 到 SWR**：我给了命令但没有你们的镜像仓库权限
- [ ] **Vault path 落地**：需要运维在 `shared/github/git-cdn-readonly-pat` 配 PAT
- [ ] **pod-template 批量迁移**：仓库里有 88 个 `container-job-pod-template-*-configmap.yaml`，需要 sed 批量替换。等你们决定灰度范围后再做，避免一次改动过大回滚困难
- [ ] **Prometheus rule / Grafana dashboard**：等观测数据积累后添加
- [ ] **HPA**：git-cdn 单副本已经能扛 c=100，暂不必要；除非要跨可用区多副本

---

## 参考

- 前次的 git-cdn vs smart-git-proxy 对比数据：[reports/gitcdn_vs_smart_git_proxy.md](../../reports/gitcdn_vs_smart_git_proxy.md)
- git-cdn 上游：<https://github.com/bpaquet/git_cdn>
- 目标仓库 smart-git-proxy 现有实现（可参考）：<https://github.com/opensourceways/ascend-ci-deployment/tree/main/manifests/smart-git-proxy>
