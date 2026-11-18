#!/usr/bin/env bash
# voip-demo 开发板只读自检；不启动服务、不修改系统。
set -u

CONF="${HOME}/.config/voipd/voipd.conf"
while (($#)); do
  case "$1" in
    --config) [[ $# -ge 2 ]] || { echo "--config 缺少参数" >&2; exit 2; }; CONF="$2"; shift 2 ;;
    -h|--help) echo "用法：$0 [--config FILE]"; exit 0 ;;
    *) echo "未知参数：$1" >&2; exit 2 ;;
  esac
done

PASS=0
WARN=0
FAIL=0
ok() { printf '  [OK] %s\n' "$*"; PASS=$((PASS + 1)); }
warn() { printf '  [提示] %s\n' "$*"; WARN=$((WARN + 1)); }
bad() { printf '  [失败] %s\n' "$*"; FAIL=$((FAIL + 1)); }

echo "voip-demo 开发板自检"
case "$(dpkg --print-architecture 2>/dev/null || uname -m)" in
  amd64|arm64|armhf|x86_64|aarch64) ok "CPU 架构受支持" ;;
  *) bad "CPU 架构未验证：$(uname -m)" ;;
esac

for cmd in g++ pkg-config python3 dbus-run-session pactl; do
  command -v "$cmd" >/dev/null 2>&1 && ok "命令存在：$cmd" || bad "缺少命令：$cmd"
done

BIN="$HOME/.local/bin/voipd"
if [[ -x "$BIN" ]]; then
  ok "voipd 已安装：$BIN"
  if ldd "$BIN" 2>/dev/null | grep -q 'not found'; then
    bad "voipd 存在未解析的动态库（执行 ldd $BIN 查看）"
  else
    ok "voipd 动态库完整"
  fi
else
  bad "找不到 $BIN"
fi

if python3 -c "import gi; gi.require_version('Gtk','4.0'); gi.require_version('Adw','1'); from gi.repository import Gtk,Adw" 2>/dev/null; then
  ok "GTK4 / libadwaita Python 绑定可用"
else
  bad "GTK4 / libadwaita Python 绑定不可用"
fi

if [[ -f "$CONF" ]]; then
  ok "配置存在：$CONF"
  if grep -qE '^server=sip:.+' "$CONF" && grep -qE '^user=.+' "$CONF"; then
    ok "SIP 服务器和账号已配置"
  else
    warn "SIP 账号尚未配置；首次启动后请在登录页填写"
  fi
  PERM="$(stat -c '%a' "$CONF" 2>/dev/null || true)"
  [[ "$PERM" == 600 ]] && ok "配置权限为 0600" || warn "配置含密码，建议 chmod 600 '$CONF'（当前 $PERM）"
else
  bad "找不到配置：$CONF"
fi

if [[ -n "${DBUS_SESSION_BUS_ADDRESS:-}" ]]; then
  ok "当前终端有用户 D-Bus 会话"
else
  warn "当前终端没有用户 D-Bus；SSH 中常见，请在图形桌面会话启动应用"
fi
if pactl info >/dev/null 2>&1; then
  ok "PipeWire/PulseAudio 音频会话可访问"
  pactl get-default-source >/dev/null 2>&1 && ok "存在默认录音设备" || warn "未设置默认录音设备"
  pactl get-default-sink >/dev/null 2>&1 && ok "存在默认播放设备" || warn "未设置默认播放设备"
else
  warn "当前终端无法访问音频会话；在图形桌面登录后复查"
fi

printf '\n结果：%d 通过，%d 提示，%d 失败\n' "$PASS" "$WARN" "$FAIL"
((FAIL == 0))

