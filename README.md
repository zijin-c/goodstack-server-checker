# Goodstack 批量机构核验（服务器版）

对加拿大候选机构名单进行 Goodstack 只读目录检索，保留组织名称、CRA 登记号、官网域名、Goodstack 组织 ID、注册号，以及 `createdAt` / `claimedAt`。脚本不会创建组织、认领资料或提交任何表单。

## 部署要求

- Docker Engine 与 Docker Compose 插件
- Goodstack API key（建议服务器端保存 `sk_` 密钥；脚本也接受 `pk_` key）
- 本目录 `data/input.json` 是本次加拿大可注册域名对应机构信息名单

Goodstack 的 Search Organisations API 是官方文档列出的 `GET /v1/organisations` 接口；检索使用 Canada 国家代码 `CAN`，先按 CRA 登记号检索，再按公开名称、法定名称和官网域名补查。脚本以每次请求至少间隔 1 秒的速度串行运行，遇到限流和服务端错误会退避重试，并逐家写入 checkpoint。

## 配置并运行

```bash
cp .env.example .env
# 编辑 .env，填入从 Goodstack 获取的 API key
chmod 600 .env
docker compose build

# 可先跑 20 家确认 API key 和输出格式；这 20 家会被断点记录
# 后续全量命令会自动跳过已完成记录
 docker compose run --rm goodstack-check --max-records 20

docker compose run --rm goodstack-check
```

### 从 GitHub 在 VPS 一键部署

仓库克隆到服务器后，运行部署脚本。脚本会创建权限为 `600` 的 `.env`，交互式收集 Goodstack key 和动态代理配置，构建容器，并启动批处理：

```bash
git clone https://github.com/zijin-c/goodstack-server-checker.git
cd goodstack-server-checker
chmod +x deploy.sh
./deploy.sh --pilot
```

`--pilot` 先检查 20 家机构；确认 API 返回和结果正常后，再运行 `./deploy.sh` 开始全量任务。两次运行共用断点文件，已完成记录会自动跳过。后台执行可使用 `nohup ./deploy.sh > deploy.log 2>&1 &`，完成后查看 `data/results/summary.json`。

部署脚本在 Ubuntu / Debian 上以 root 运行时，会通过 Docker 官方 apt 仓库自动安装 Docker Engine 与 Compose 插件；其他发行版需要先按 [Docker 官方文档](https://docs.docker.com/engine/install/) 安装运行环境。

### 动态代理

支持两种由代理服务商提供的 HTTP(S) 代理方式，在部署提示中选择一种：

- **轮换网关**：在 `GOODSTACK_PROXY_URL` 填入服务商的网关 URL。每次 API 请求都会经该网关发出，出口 IP 是否变化由服务商策略决定。
- **代理池 API**：设置 `GOODSTACK_PROXY_LIST_URL`，可选设置 `GOODSTACK_PROXY_TOKEN`；脚本会定期下载代理列表，并在请求和重试之间轮换代理。支持纯文本、一维 JSON 数组以及常见的 `data` / `proxies` JSON 格式。
- 没有代理服务时可选择直连。SOCKS 代理当前不支持。

脚本不会自动申请或猜测 Goodstack key、代理账号或代理 API 地址；这些值需从各自服务商获取。代理列表下载功能只会读取已配置的服务商接口。

> `docker compose run` 是一次性批处理，不会作为常驻服务自动重启。SSH 断开前可用 `tmux` / `screen` 保持会话；即使进程中断，重新运行相同命令和输出目录会从 `checkpoint.jsonl` 续跑。

若要从头核验，请指定一个全新的输出目录，例如：

```bash
docker compose run --rm goodstack-check --output-dir /data/results_rerun
```

如果在宿主机没有 Docker，可直接用 Python 3.10+ 运行：

```bash
export GOODSTACK_API_KEY='你的 Goodstack API key'
python3 goodstack_batch_verify.py --input data/input.json --output-dir data/results
```

PowerShell 示例：

```powershell
$env:GOODSTACK_API_KEY = '你的 Goodstack API key'
python goodstack_batch_verify.py --input data/input.json --output-dir data/results
```

## 输出

- `data/results/summary.csv`：每家机构一行，包含 Goodstack 检索状态、精确匹配数、两个日期是否齐全等摘要。
- `data/results/both_dates.json`：只收录 `createdAt` 与 `claimedAt` 均非空的 Goodstack 候选，同时保留匹配等级与原始组织对象。
- `data/results/checkpoint.jsonl`：逐条追加的完整检索结果；中断后用于续跑，也包含需要人工复核的候选。
- `data/results/summary.json`：运行计数和结果文件索引。

状态说明：`exact_match_both_dates` 表示登记号、官网域名或机构名称有精确匹配，且 Goodstack 记录同时含两项时间；`exact_match_dates_incomplete` 表示精确匹配但日期不齐；`candidates_need_review` 表示有搜索候选但无法精确对应；`not_found` 表示本次 API 搜索无候选；`query_error` 表示查询失败，不能据此判断未收录。Goodstack 的目录命中和时间字段只反映 API 返回记录，不替代 CRA 当前慈善资格核实。

## 速率与安全

默认每次 API 请求至少间隔 1 秒，最大每页 100 条，最多翻 10 页；遇到 HTTP 429 或 5xx 会按 `Retry-After` / 指数退避重试。不要把 `.env` 或 API key 放进代码仓库、聊天消息或日志。
