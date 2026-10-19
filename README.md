# Goodstack 浏览器批量核验

用 Playwright 启动 Chromium，自动打开 Goodstack 的公开认领/搜索页面，选择加拿大并逐家搜索名单中的机构。脚本读取页面可见的组织名称、地址、登记编号和页面提示；不会点击“认领组织”、创建组织或提交表单，也不需要 Goodstack API key。

## 一键部署

在 Ubuntu / Debian VPS 上以 root 运行：

```bash
git clone https://github.com/zijin-c/goodstack-server-checker.git && cd goodstack-server-checker && chmod +x deploy.sh && ./deploy.sh --pilot
```

`--pilot` 先搜索 20 家；确认结果后执行 `./deploy.sh` 开始全量名单。断点会保存在 `data/results/checkpoint.jsonl`，重跑时自动跳过已完成项。脚本会在 Ubuntu / Debian 上通过 Docker 官方 apt 仓库安装 Docker 与 Compose，并构建带 Chromium 的运行镜像。

## 日期字段

公开搜索卡片本身不显示 `createdAt` 和 `claimedAt`。为保留原先需要的日期筛选，脚本只观察 Chromium 在提交公开搜索表单后，由网页自身加载的搜索结果响应；它不自己发起 API 查询、不使用 Goodstack API key。只有搜索响应包含这两个日期且候选与名单精确匹配时，才写入 `both_dates.json`。如果 Goodstack 改变网页或响应不再包含日期，日期结果会为空，页面可见结果仍保留在完整结果文件中。

## 动态代理

首次运行时可选择代理商轮换网关、代理池列表 API，或直连。支持 HTTP(S) 代理；代理池列表可为纯文本或常见 JSON 格式。动态代理需要你已有代理服务的地址/令牌；仓库不含代理凭据，也不会尝试绕过 Goodstack 的真人验证。若出现验证码，程序会停止并保留断点，等待人工处理。

## 输出

- `data/results/summary.csv`：逐机构摘要，包括页面搜索状态和两个日期。
- `data/results/both_dates.json`：名称/登记号/官网精确匹配，且 `createdAt` 与 `claimedAt` 都有值的结果。
- `data/results/checkpoint.jsonl`：每次搜索的完整记录，中断续跑依据。
- `data/results/summary.json`：总体计数和结果索引。

页面显示“未找到”仅代表该次公开页面搜索无结果，不代表机构不存在或不符合 CRA 资格。名称候选应结合登记编号人工复核。

## 常用命令

```bash
# 完整批次
./deploy.sh

# 仅在需要时无视断点重查
docker compose run --rm goodstack-check --no-resume

# 查看进度摘要
cat data/results/summary.json
```

默认每次页面搜索间隔 2 秒；2300 条记录可能运行数小时。需要后台执行时可用 `nohup ./deploy.sh > deploy.log 2>&1 &`，之后查看 `tail -f deploy.log`。
