#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$APP_DIR"

if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
  if [[ "$(id -u)" -ne 0 ]]; then
    echo "Docker 未安装。请使用 root 运行本脚本，或先按 Docker 官方文档安装 Docker Engine 与 Compose。" >&2
    exit 1
  fi
  if [[ ! -r /etc/os-release ]]; then
    echo "无法识别 Linux 发行版，请先安装 Docker Engine 与 Compose。" >&2
    exit 1
  fi
  . /etc/os-release
  if [[ "${ID:-}" != "ubuntu" && "${ID:-}" != "debian" ]]; then
    echo "自动安装目前支持 Ubuntu / Debian；请先按 Docker 官方文档安装 Docker Engine 与 Compose。" >&2
    exit 1
  fi
  echo "正在通过 Docker 官方 apt 仓库安装 Docker Engine 和 Compose 插件……"
  apt-get update
  apt-get install -y ca-certificates curl
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL "https://download.docker.com/linux/${ID}/gpg" -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  codename="${UBUNTU_CODENAME:-${VERSION_CODENAME:-}}"
  if [[ -z "$codename" ]]; then
    echo "无法识别系统代号，Docker 仓库未配置。" >&2
    exit 1
  fi
  printf 'Types: deb\nURIs: https://download.docker.com/linux/%s\nSuites: %s\nComponents: stable\nArchitectures: %s\nSigned-By: /etc/apt/keyrings/docker.asc\n' \
    "$ID" "$codename" "$(dpkg --print-architecture)" > /etc/apt/sources.list.d/docker.sources
  apt-get update
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  systemctl enable --now docker
fi

read_env_value() {
  local key="$1"
  if [[ -f .env ]]; then
    sed -n "s/^${key}=//p" .env | tail -n 1
  fi
}

GOODSTACK_PROXY_URL="${GOODSTACK_PROXY_URL:-$(read_env_value GOODSTACK_PROXY_URL)}"
GOODSTACK_PROXY_LIST_URL="${GOODSTACK_PROXY_LIST_URL:-$(read_env_value GOODSTACK_PROXY_LIST_URL)}"
GOODSTACK_PROXY_TOKEN="${GOODSTACK_PROXY_TOKEN:-$(read_env_value GOODSTACK_PROXY_TOKEN)}"
GOODSTACK_PROXY_REFRESH_SECONDS="${GOODSTACK_PROXY_REFRESH_SECONDS:-$(read_env_value GOODSTACK_PROXY_REFRESH_SECONDS)}"
UI_PORT="${UI_PORT:-$(read_env_value UI_PORT)}"
UI_USERNAME="${UI_USERNAME:-$(read_env_value UI_USERNAME)}"
UI_PASSWORD="${UI_PASSWORD:-$(read_env_value UI_PASSWORD)}"
UI_PORT="${UI_PORT:-8765}"
UI_USERNAME="${UI_USERNAME:-admin}"
if [[ -z "$UI_PASSWORD" ]]; then
  UI_PASSWORD="$(od -An -N18 -tx1 /dev/urandom | tr -d ' \n')"
fi

if [[ -z "$GOODSTACK_PROXY_URL" && -z "$GOODSTACK_PROXY_LIST_URL" ]]; then
  echo "代理配置（Goodstack 页面可直连时可选 3）："
  echo "  1) 轮换网关：代理服务商提供一个固定 HTTP(S) 网关 URL"
  echo "  2) 代理池：代理服务商提供动态代理列表 API"
  echo "  3) 直连"
  read -r -p "请选择 [1/2/3，默认 1]: " proxy_mode
  case "${proxy_mode:-1}" in
    1) read -r -s -p "轮换网关 URL: " GOODSTACK_PROXY_URL; echo ;;
    2)
      read -r -p "代理列表 API URL: " GOODSTACK_PROXY_LIST_URL
      read -r -s -p "代理列表 Bearer token（没有则回车）: " GOODSTACK_PROXY_TOKEN
      echo
      GOODSTACK_PROXY_REFRESH_SECONDS="${GOODSTACK_PROXY_REFRESH_SECONDS:-300}"
      ;;
    3) ;;
    *) echo "无效选项。" >&2; exit 2 ;;
  esac
fi

if [[ -n "$GOODSTACK_PROXY_URL" && -n "$GOODSTACK_PROXY_LIST_URL" ]]; then
  echo "GOODSTACK_PROXY_URL 与 GOODSTACK_PROXY_LIST_URL 只能配置一个。" >&2
  exit 2
fi
GOODSTACK_PROXY_REFRESH_SECONDS="${GOODSTACK_PROXY_REFRESH_SECONDS:-300}"

umask 077
{
  printf 'GOODSTACK_PROXY_URL=%s\n' "$GOODSTACK_PROXY_URL"
  printf 'GOODSTACK_PROXY_LIST_URL=%s\n' "$GOODSTACK_PROXY_LIST_URL"
  printf 'GOODSTACK_PROXY_TOKEN=%s\n' "$GOODSTACK_PROXY_TOKEN"
  printf 'GOODSTACK_PROXY_REFRESH_SECONDS=%s\n' "$GOODSTACK_PROXY_REFRESH_SECONDS"
  printf 'UI_PORT=%s\n' "$UI_PORT"
  printf 'UI_USERNAME=%s\n' "$UI_USERNAME"
  printf 'UI_PASSWORD=%s\n' "$UI_PASSWORD"
} > .env
chmod 600 .env

docker compose build
docker compose up -d --force-recreate goodstack-check
echo
echo "Goodstack 网页控制台已启动："
echo "  浏览器地址: http://<VPS 公网 IP>:${UI_PORT}"
echo "  登录用户名: ${UI_USERNAME}"
echo "  登录密码: ${UI_PASSWORD}"
echo "  容器日志: docker compose logs -f goodstack-check"
echo "请在浏览器里打开控制台后点击「试跑 20 家」或「全量续跑」。"
