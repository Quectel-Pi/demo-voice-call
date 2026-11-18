#!/usr/bin/env bash
# run-app.sh —— 前台管理整套 VoIP 应用的生命周期。
# 启动：voipd -> GTK UI。SIP 服务器使用外部已有服务器。
# 关闭窗口或按 Ctrl+C 后，反向停止全部由本脚本启动的进程。
# 不使用 systemd，不全屏，不设置开机自启动。
set -uo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SESSION_DIR="${VOIP_SESSION_DIR:-${XDG_RUNTIME_DIR:-/tmp}/voip-demo}"
CONFIG="${VOIP_CONFIG:-}"

mkdir -p "$SESSION_DIR"

if [[ -z "$CONFIG" ]]; then
  if [[ -f "${XDG_CONFIG_HOME:-$HOME/.config}/voipd/voipd.conf" ]]; then
    CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}/voipd/voipd.conf"
  elif [[ -f "$PROJECT_DIR/.runtime/voipd.conf" ]]; then
    CONFIG="$PROJECT_DIR/.runtime/voipd.conf"
  fi
fi

find_voipd() {
  local candidate
  for candidate in \
    "$PROJECT_DIR/build/voipd" \
    "$HOME/.local/bin/voipd" \
    "$HOME/voip-local/bin/voipd"; do
    if [[ -x "$candidate" ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  command -v voipd 2>/dev/null || return 1
}

VOIPD_BIN="${VOIPD_BIN:-$(find_voipd)}"
UI_SCRIPT="${VOIP_UI_SCRIPT:-$PROJECT_DIR/src/ui/voipapp.py}"
VOIP_LOG="${VOIP_LOG:-0}"
LOG_TO_TERMINAL=0
case "${VOIP_LOG,,}" in 1|true|yes|on) LOG_TO_TERMINAL=1 ;; esac
VOIPD_PID=""
UI_PID=""

stop_pid() {
  local pid="$1"
  [[ "$pid" =~ ^[0-9]+$ ]] || return 0
  kill "$pid" 2>/dev/null || return 0
  for _ in {1..20}; do
    kill -0 "$pid" 2>/dev/null || return 0
    sleep 0.1
  done
  # 正常退出超时后强制回收，避免下次启动误判为旧实例仍在运行。
  kill -KILL "$pid" 2>/dev/null || true
  wait "$pid" 2>/dev/null || true
}

cleanup() {
  trap - EXIT INT TERM HUP
  stop_pid "$UI_PID"
  stop_pid "$VOIPD_PID"
  echo "[voip] UI 和 voipd 已关闭"
}

trap cleanup EXIT
trap 'exit 130' INT TERM HUP

[[ -x "$VOIPD_BIN" ]] || { echo "找不到 voipd，请先安装或设置 VOIPD_BIN" >&2; exit 1; }
[[ -f "$UI_SCRIPT" ]] || { echo "找不到 UI: $UI_SCRIPT" >&2; exit 1; }
[[ -f "$CONFIG" ]] || { echo "找不到配置: $CONFIG" >&2; exit 1; }

# 兼容旧部署：旧的用户服务会让 voipd 常驻，与本脚本的统一生命周期冲突。
if command -v systemctl >/dev/null 2>&1 && \
   systemctl --user is-active --quiet voipd.service 2>/dev/null; then
  echo "[voip] 停止并禁用旧的 voipd/voip-ui 用户服务"
  systemctl --user disable --now voip-ui.service voipd.service 2>/dev/null || true
  sleep 1
fi

if gdbus call --session --dest org.example.Voip1 \
     --object-path /org/example/Voip1 \
     --method org.example.Voip1.GetSnapshot >/dev/null 2>&1; then
  echo "已有 voipd 正在运行，请先关闭旧实例" >&2
  exit 1
fi

echo "[voip] 启动通话守护进程"
# voipd 链接的 liblinphone 会把好友/聊天室/通话记录数据库写到固定的
# $HOME/.local/share/linphone/linphone.db，不受 --data-dir 控制。
# 如果同一账号下还跑着别的 SIP 客户端（如 Linphone 官方 App/AppImage），
# 会共用这个库文件；版本不同的 liblinphone 读到对方写入的新 schema 记录时，
# 可能因为缺少语法解析资源直接 abort。这里给 voipd 分配独立的 HOME，彻底隔离。
VOIPD_HOME="$SESSION_DIR/voipd-home"
mkdir -p "$VOIPD_HOME/.local/share/linphone" "$VOIPD_HOME/.config"
if ((LOG_TO_TERMINAL)); then
  HOME="$VOIPD_HOME" "$VOIPD_BIN" --config "$CONFIG" --data-dir "$SESSION_DIR/data" \
    > >(tee -a "$SESSION_DIR/voipd.log") 2>&1 &
else
  HOME="$VOIPD_HOME" "$VOIPD_BIN" --config "$CONFIG" --data-dir "$SESSION_DIR/data" \
    >"$SESSION_DIR/voipd.log" 2>&1 &
fi
VOIPD_PID=$!
sleep 1
if ! kill -0 "$VOIPD_PID" 2>/dev/null; then
  echo "voipd 启动失败，日志：$SESSION_DIR/voipd.log" >&2
  exit 1
fi

echo "[voip] 启动窗口；关闭窗口将停止全部相关进程"
if ((LOG_TO_TERMINAL)); then
  VOIP_LOG=1 python3 "$UI_SCRIPT" > >(tee -a "$SESSION_DIR/ui.log") 2>&1 &
else
  python3 "$UI_SCRIPT" >"$SESSION_DIR/ui.log" 2>&1 &
fi
UI_PID=$!
wait "$UI_PID"
UI_STATUS=$?
UI_PID=""
exit "$UI_STATUS"
