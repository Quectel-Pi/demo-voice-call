#!/usr/bin/env bash
# 在可联网的 Debian/Ubuntu 开发板上原生编译并部署 voip-demo。
# 请以将要运行桌面程序的普通用户执行，不要 sudo 整个脚本。
set -Eeuo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVER=""
SIP_USER=""
SIP_SECRET="${SIP_PASSWORD:-}"
DISPLAY_NAME="开发板话机"
SIP_PORT="5060"
AUTOSTART=0
SKIP_APT=0
KEEP_CONFIG=0

usage() {
  cat <<'EOF'
用法：
  ./scripts/deploy-board.sh [选项]

选项：
  --server ADDR       可选预配置 SIP 服务器，例如 192.168.1.10:5060
  --user USER         可选预配置 SIP 账号
  --password PASS     可选预配置 SIP 密码（也可用 SIP_PASSWORD 环境变量）
  --name NAME         界面显示名（默认“开发板话机”）
  --sip-port PORT     本机 SIP UDP 端口（默认 5060）
  --autostart         登录图形桌面后自动启动应用
  --skip-apt          跳过 apt 更新和依赖安装（适合已准备好的系统）
  --keep-config       保留现有 ~/.config/voipd/voipd.conf
  -h, --help          显示帮助

环境要求：Debian/Ubuntu，amd64/arm64/armhf，可联网，有 sudo 权限和图形桌面。
EOF
}

die() { printf '错误：%s\n' "$*" >&2; exit 1; }
step() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }

while (($#)); do
  case "$1" in
    --server) [[ $# -ge 2 ]] || die "--server 缺少参数"; SERVER="$2"; shift 2 ;;
    --user) [[ $# -ge 2 ]] || die "--user 缺少参数"; SIP_USER="$2"; shift 2 ;;
    --password) [[ $# -ge 2 ]] || die "--password 缺少参数"; SIP_SECRET="$2"; shift 2 ;;
    --name) [[ $# -ge 2 ]] || die "--name 缺少参数"; DISPLAY_NAME="$2"; shift 2 ;;
    --sip-port) [[ $# -ge 2 ]] || die "--sip-port 缺少参数"; SIP_PORT="$2"; shift 2 ;;
    --autostart) AUTOSTART=1; shift ;;
    --skip-apt) SKIP_APT=1; shift ;;
    --keep-config) KEEP_CONFIG=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "未知参数：$1（使用 --help 查看帮助）" ;;
  esac
done

[[ ${EUID:-$(id -u)} -ne 0 ]] || die "请以普通用户运行；脚本会在需要时调用 sudo"
if [[ -n "$SIP_USER" ]]; then
  [[ "$SIP_USER" =~ ^[A-Za-z0-9_.-]+$ ]] || die "账号只能包含字母、数字、点、下划线和连字符"
fi
[[ "$DISPLAY_NAME" != *$'\n'* && "$DISPLAY_NAME" != *$'\r'* ]] || die "显示名不能包含换行"

SERVER_HOST=""
SERVER_PORT="5060"
if [[ -n "$SERVER" ]]; then
  SERVER="${SERVER#sip:}"
  SERVER="${SERVER%/}"
  [[ "$SERVER" != *$'\n'* && "$SERVER" != *$'\r'* && "$SERVER" != *' '* ]] || die "服务器地址格式无效"
  SERVER_HOST="$SERVER"
  if [[ "$SERVER" =~ ^([^:]+):([0-9]+)$ ]]; then
    SERVER_HOST="${BASH_REMATCH[1]}"
    SERVER_PORT="${BASH_REMATCH[2]}"
  fi
  [[ "$SERVER_HOST" =~ ^[A-Za-z0-9._-]+$ ]] \
    || die "服务器仅支持 IPv4 或普通域名；当前值无效：$SERVER_HOST"
fi
[[ "$SERVER_PORT" =~ ^[0-9]+$ ]] && ((SERVER_PORT >= 1 && SERVER_PORT <= 65535)) \
  || die "服务器端口无效：$SERVER_PORT"

[[ "$SIP_SECRET" != *$'\n'* && "$SIP_SECRET" != *$'\r'* ]] || die "密码不能包含换行"
if [[ -n "$SERVER" || -n "$SIP_USER" || -n "$SIP_SECRET" ]]; then
  [[ -n "$SERVER" && -n "$SIP_USER" ]] \
    || die "预配置时 --server 和 --user 必须同时提供；也可以不传参数，启动后在界面填写"
fi
[[ "$SIP_PORT" =~ ^[0-9]+$ ]] && ((SIP_PORT >= 1 && SIP_PORT <= 65535)) \
  || die "本机 SIP 端口无效：$SIP_PORT"

[[ -r /etc/os-release ]] || die "无法识别系统（缺少 /etc/os-release）"
# shellcheck disable=SC1091
source /etc/os-release
case "${ID:-}" in debian|ubuntu) ;; *) die "目前只支持 Debian/Ubuntu，检测到 ${ID:-unknown}" ;; esac
command -v dpkg >/dev/null || die "缺少 dpkg"
ARCH="$(dpkg --print-architecture)"
case "$ARCH" in amd64|arm64|armhf) ;; *) die "暂未验证的 CPU 架构：$ARCH" ;; esac
command -v sudo >/dev/null || die "缺少 sudo，请先以 root 安装 sudo 并授权当前用户"

step "目标环境：${PRETTY_NAME:-$ID} / $ARCH"
printf '安装用户：%s\n本机端口：%s/udp\n' "$USER" "$SIP_PORT"
if [[ -n "$SERVER_HOST" ]]; then
  printf '预配置服务器：%s:%s\n预配置账号：%s\n' "$SERVER_HOST" "$SERVER_PORT" "$SIP_USER"
else
  echo "SIP 账号：未预配置，首次启动后在登录页填写"
fi

if ((SKIP_APT == 0)); then
  step "联网安装编译、界面与音频依赖"
  sudo apt-get update
  CHAT_PKGS=()
  # Ubuntu 22.04 / liblinphone 4.4 needs this separate grammar package;
  # newer distributions may bundle the resources under a different package.
  if apt-cache show linphone-common >/dev/null 2>&1; then CHAT_PKGS=(linphone-common); fi
  sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y \
    build-essential pkg-config liblinphone-dev "${CHAT_PKGS[@]}" libmediastreamer-dev libglib2.0-dev \
    python3 python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 gir1.2-gstreamer-1.0 \
    gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
    pipewire pipewire-pulse wireplumber pulseaudio-utils dbus-user-session
fi

step "在开发板上原生编译并安装"
CONF="$HOME/.config/voipd/voipd.conf"
HAD_CONFIG=0
[[ -f "$CONF" ]] && HAD_CONFIG=1
# 旧版本用两个 systemd 用户服务分别管理进程，会与 run-app.sh 冲突。
systemctl --user disable --now voip-ui.service voipd.service 2>/dev/null || true
BIN="$HOME/.local/bin" APPDIR="$HOME/.local/share/voip" \
  CONF="$CONF" "$PROJECT_DIR/scripts/install.sh"

if ((KEEP_CONFIG == 0)); then
  if ((HAD_CONFIG)); then
    BACKUP="$CONF.bak.$(date +%Y%m%d-%H%M%S)"
    cp -a "$CONF" "$BACKUP"
    printf '原配置已备份：%s\n' "$BACKUP"
  fi
  install -d -m 0700 "$(dirname "$CONF")"
  umask 077
  CONFIG_SERVER=""
  [[ -z "$SERVER_HOST" ]] || CONFIG_SERVER="sip:$SERVER_HOST:$SERVER_PORT"
  cat >"$CONF" <<EOF
# 由 deploy-board.sh 生成。密码文件权限为 0600。
[account]
user=$SIP_USER
secret=$SIP_SECRET
server=$CONFIG_SERVER
sip_port=$SIP_PORT
display_name=$DISPLAY_NAME
capture_device=
playback_device=

[general]
auto_answer=false
no_answer_timeout=60
verbose=false
EOF
  chmod 0600 "$CONF"
else
  printf '保留已有配置：%s\n' "$CONF"
fi

if ((AUTOSTART)); then
  step "配置图形桌面登录后自动启动"
  [[ "$HOME" != *' '* ]] || die "HOME 路径含空格，无法安全生成 desktop Exec"
  install -d "$HOME/.config/autostart"
  cat >"$HOME/.config/autostart/voip-demo.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=VoIP Demo
Comment=开发板 SIP 语音通话应用
Exec=$PROJECT_DIR/scripts/run-app.sh
Terminal=false
X-GNOME-Autostart-enabled=true
EOF
fi

step "部署后静态自检"
"$PROJECT_DIR/scripts/board-check.sh" --config "$CONF"

cat <<EOF

部署完成。
在开发板图形桌面的终端运行：
  $PROJECT_DIR/scripts/run-app.sh

关闭窗口会同时停止本次启动的 voipd。日志目录：
  \${XDG_RUNTIME_DIR:-/tmp}/voip-demo/
EOF

