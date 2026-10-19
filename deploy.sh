#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$APP_DIR"

if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
  echo "Docker Engine 和 Docker Compose 插件尚未安装。请先安装 Docker，再重新运行本脚本。" >&2
  exit 1
fi

read_env_value() {
  local key="$1"
  if [[ -f .env ]]; then
    sed -n "s/^${key}=//p" .env | tail -n 1
  fi
}

GOODSTACK_API_KEY="${GOODSTACK_API_KEY:-$(read_env_value GOODSTACK_API_KEY)}"
GOODSTACK_PROXY_URL="${GOODSTACK_PROXY_URL:-$(read_env_value GOODSTACK_PROXY_URL)}"
GOODSTACK_PROXY_LIST_URL="${GOODSTACK_PROXY_LIST_URL:-$(read_env_value GOODSTACK_PROXY_LIST_URL)}"
GOODSTACK_PROXY_TOKEN="${GOODSTACK_PROXY_TOKEN:-$(read_env_value GOODSTACK_PROXY_TOKEN)}"
GOODSTACK_PROXY_REFRESH_SECONDS="${GOODSTACK_PROXY_REFRESH_SECONDS:-$(read_env_value GOODSTACK_PROXY_REFRESH_SECONDS)}"

if [[ -z "$GOODSTACK_API_KEY" ]]; then
  read -r -s -p "Goodstack API key (pk_ 或 sk_): " GOODSTACK_API_KEY
  echo
fi
if [[ -z "$GOODSTACK_API_KEY" ]]; then
  echo "未提供 GOODSTACK_API_KEY，已退出。" >&2
  exit 1
fi

if [[ -z "$GOODSTACK_PROXY_URL" && -z "$GOODSTACK_PROXY_LIST_URL" ]]; then
  echo "动态代理配置："
  echo "  1) 轮换网关：服务商提供一个固定代理 URL，每次连接由服务商换 IP"
  echo "  2) 代理池：填写服务商的代理列表 API，脚本下载并轮换池中代理"
  echo "  3) 暂不使用代理"
  read -r -p "请选择 [1/2/3，默认 1]: " proxy_mode
  case "${proxy_mode:-1}" in
    1)
      read -r -p "轮换代理 URL（例如 http://user:pass@gateway.example:8000）: " GOODSTACK_PROXY_URL
      ;;
    2)
      read -r -p "代理列表 API URL: " GOODSTACK_PROXY_LIST_URL
      read -r -s -p "代理列表 API Bearer token（没有则直接回车）: " GOODSTACK_PROXY_TOKEN
      echo
      GOODSTACK_PROXY_REFRESH_SECONDS="${GOODSTACK_PROXY_REFRESH_SECONDS:-300}"
      ;;
    3) ;;
    *) echo "无效选项。" >&2; exit 2 ;;
  esac
fi

if [[ -n "$GOODSTACK_PROXY_URL" && -n "$GOODSTACK_PROXY_LIST_URL" ]]; then
  echo "GOODSTACK_PROXY_URL 与 GOODSTACK_PROXY_LIST_URL 只能选一种。" >&2
  exit 2
fi
GOODSTACK_PROXY_REFRESH_SECONDS="${GOODSTACK_PROXY_REFRESH_SECONDS:-300}"

umask 077
{
  printf 'GOODSTACK_API_KEY=%s\n' "$GOODSTACK_API_KEY"
  printf 'GOODSTACK_PROXY_URL=%s\n' "$GOODSTACK_PROXY_URL"
  printf 'GOODSTACK_PROXY_LIST_URL=%s\n' "$GOODSTACK_PROXY_LIST_URL"
  printf 'GOODSTACK_PROXY_TOKEN=%s\n' "$GOODSTACK_PROXY_TOKEN"
  printf 'GOODSTACK_PROXY_REFRESH_SECONDS=%s\n' "$GOODSTACK_PROXY_REFRESH_SECONDS"
} > .env
chmod 600 .env

docker compose build
if [[ "${1:-}" == "--pilot" ]]; then
  docker compose run --rm goodstack-check --max-records 20
else
  docker compose run --rm goodstack-check
fi
