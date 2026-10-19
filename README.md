# Goodstack 浏览器批量核验

用 Playwright 启动 Chromium，自动打开 Goodstack 的公开认领/搜索页面，选择加拿大并逐家搜索名单中的机构。脚本读取页面可见的组织名称、地址、登记编号和页面提示；不会点击“认领组织”、创建组织或提交表单，也不需要 Goodstack API key。

## 一键部署

在 Ubuntu / Debian VPS 上以 root 运行一键部署命令：

```bash
git clone https://github.com/zijin-c/goodstack-server-checker.git && cd goodstack-server-checker && chmod +x deploy.sh && ./deploy.sh
```

部署后浏览器打开 `http://<VPS 公网 IP>:8765`。脚本会生成 UI 登录用户名和密码并在终端显示；默认用户名为 `admin`。在页面中可点击“试跑 20 家”“全量续跑”“从头重查”或“停止任务”，也能查看进度、日志并下载筛选结果。首次运行会在 Ubuntu / Debian 上通过 Docker 官方 apt 仓库安装 Docker 与 Compose，并构建带 Chromium 和控制台的镜像。

如果云厂商安全组或 VPS 防火墙限制入站连接，请放行 TCP 8765 才能从浏览器访问。UI 端口可在部署目录的 `.env` 中通过 `UI_PORT` 修改。

## 日期字段

公开搜索卡片本身不显示 `createdAt` 和 `claimedAt`。脚本像本地工具一样监听 Chromium 的网络响应：先通过公开网页提交机构名称搜索，再读取该页面自行加载的搜索结果响应；不需要 Goodstack API key。只有候选与名单精确匹配、且响应中两个日期都非空的记录才会出现在 `both_dates.csv` 和 `both_dates.json`。如果网页改版或响应不再包含日期，这两个筛选文件会为空。

## 动态代理

首次运行时可选择代理商轮换网关、代理池列表 API，或直连。部署后也可直接在网页控制台的“动态代理”区域逐行添加 HTTP(S) 代理、开关代理、删除代理，并点击“检测 IP”查看代理当前出口 IP 和响应耗时。任务会按列表顺序轮换代理；单个轮换网关也可由代理服务商自行轮换出口。代理配置保存在 `data/proxy_settings.json`，仅返回脱敏地址到网页，账号密码文件权限限制为 600。代理地址支持 `http://user:password@host:port`、`http://host:port` 和 `host:port:user:password`。代理商网关/List API 的旧部署配置仍可继续使用。

代理 IP 检测通过代理访问 `https://api.ipify.org`，需确保 VPS 和代理都能访问该地址。仓库不含代理凭据，也不会尝试绕过 Goodstack 的真人验证。若出现验证码，程序会停止并保留断点，等待人工处理。

## 输出

- `data/results/both_dates.csv`：仅含精确匹配且 `createdAt`、`claimedAt` 都有值的机构。
- `data/results/both_dates.json`：同一筛选结果，包含完整 Goodstack 组织对象。
- `data/results/checkpoint.jsonl`：内部断点文件，保留所有搜索状态以便中断续跑；请用上面的双日期文件作为最终筛选结果。
- `data/results/summary.json`：总体计数和结果索引。

页面显示“未找到”仅代表该次公开页面搜索无结果，不代表机构不存在或不符合 CRA 资格。名称候选应结合登记编号人工复核。

## 常用命令

```bash
# 更新代码并重建控制台
git pull && ./deploy.sh

# 查看容器日志
docker compose logs -f goodstack-check

# 批次运行、暂停和下载结果均在网页控制台完成
# 查看进度摘要
cat data/results/summary.json
```

默认使用 3 个独立 Chromium 工作线程，可在 UI 里选择 1–6 个；每个线程使用独立 Playwright 驱动和浏览器，不共享页面对象。并发数越高越快，也越容易遇到网站限流，建议先用 2–3 个。每次网页搜索默认间隔 2 秒。控制台和批次任务会在 Docker 后台运行，关闭浏览器不影响任务。
