#!/usr/bin/env bash
# install.sh —— 把 voipd + UI 装到用户目录，并可选安装 systemd 用户服务
#
#   ./install.sh            # 编译并安装到 ~/.local/bin，生成配置模板
#   ./install.sh --service  # 仅兼容旧参数；服务保持禁用，不设置开机自启
#
# 只装用户级文件，不需要 root（音频/D-Bus 都在用户会话内）。
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
BIN="${BIN:-$HOME/.local/bin}"
APPDIR="${APPDIR:-$HOME/.local/share/voip}"
CONF="${CONF:-$HOME/.config/voipd/voipd.conf}"
UNITS="${UNITS:-$HOME/.config/systemd/user}"
WITH_SERVICE=0
[[ "${1:-}" == "--service" ]] && WITH_SERVICE=1

say() { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }

say "1. 检查依赖"
missing=()
for c in g++ pkg-config; do command -v $c >/dev/null || missing+=($c); done
pkg-config --exists glib-2.0 gio-2.0 || missing+=(libglib2.0-dev)
[[ -f /usr/include/linphone/core.h ]] || missing+=(liblinphone-dev)
# 4.4 版聊天初始化需要 linphone-common 中的 identity_grammar（缺失时库会 abort）。
if [[ ! -f /usr/include/linphone/api/c-account-params.h ]]; then
  [[ -f /usr/share/belr/grammars/identity_grammar ]] || missing+=(linphone-common)
fi
python3 -c "import gi; gi.require_version('Gtk','4.0'); gi.require_version('Adw','1')" 2>/dev/null \
  || missing+=(python3-gi gir1.2-gtk-4.0 gir1.2-adw-1)
if ((${#missing[@]})); then
  echo "缺少依赖: ${missing[*]}"
  echo "请先执行: sudo apt install -y liblinphone-dev linphone-common libmediastreamer-dev libglib2.0-dev \\"
  echo "            python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 gir1.2-gstreamer-1.0 gstreamer1.0-plugins-base"
  exit 1
fi
echo "  OK"

say "2. 编译 voipd"
# 部署目录可能带有其他架构的旧产物，始终在目标设备重新编译。
make -B -C "$HERE/src/voipd"

say "3. 安装到 $BIN 与 $APPDIR"
install -d "$BIN" "$APPDIR/ui" "$APPDIR/assets"
install -m 0755 "$HERE/build/voipd" "$BIN/voipd"
install -m 0644 "$HERE/src/ui/voipapp.py" "$APPDIR/ui/voipapp.py"
install -m 0644 "$HERE/src/ui/style.css" "$APPDIR/ui/style.css"
install -m 0644 "$HERE/assets/ring.wav" "$APPDIR/assets/ring.wav"
install -m 0755 "$HERE/scripts/run-app.sh" "$APPDIR/run-app.sh"
# 启动器：固定 UI 资源路径
cat > "$BIN/voip-ui" <<EOF
#!/bin/sh
exec python3 "$APPDIR/ui/voipapp.py" "\$@"
EOF
chmod 0755 "$BIN/voip-ui"
cat > "$BIN/voip-app" <<EOF
#!/bin/sh
VOIPD_BIN="$BIN/voipd" VOIP_UI_SCRIPT="$APPDIR/ui/voipapp.py" \
VOIP_CONFIG="$CONF" exec "$APPDIR/run-app.sh"
EOF
chmod 0755 "$BIN/voip-app"
echo "  $BIN/voipd"
echo "  $BIN/voip-ui"
echo "  $BIN/voip-app"

say "4. 生成配置"
if [[ -f $CONF ]]; then
  echo "  已存在，保留: $CONF"
else
  install -d "$(dirname "$CONF")"
  cat > "$CONF" <<'EOF'
# voipd 配置。改完执行: systemctl --user restart voipd
[account]
# 首次启动后在登录页输入外部 SIP 服务器和账号
user=
secret=
server=
# 本机 SIP 监听端口；确保没有其他程序占用
sip_port=5060
display_name=会议室话机

# 音频设备：留空 = 系统默认（PipeWire 默认源/汇）
# 默认源 mic-ns 已带 AEC/降噪，推荐留空。
# 若要改用应用层 AEC，填 raw 设备名，例如：
#   capture_device=PulseAudio: 内置音频 Analog Stereo
# 可用 `voipd --list-devices` 之外的方式查看：运行后调 D-Bus GetDevices
capture_device=
playback_device=

[general]
# 仅联调/演示用：自动接听
auto_answer=false
verbose=false
EOF
  echo "  $CONF"
fi

if ((WITH_SERVICE)); then
  say "5. 清理旧 systemd 自动启动项"
  install -d "$UNITS"
  install -m 0644 "$HERE/scripts/voipd.service" "$UNITS/voipd.service"
  install -m 0644 "$HERE/scripts/voip-ui.service" "$UNITS/voip-ui.service"
  systemctl --user daemon-reload
  systemctl --user disable --now voip-ui.service voipd.service 2>/dev/null || true
  echo "  服务文件已保留兼容，但处于禁用状态"
else
  say "5. 不安装 systemd 自动启动项"
fi

say "完成"
echo "启动（关闭窗口会同时停止 voipd）："
echo "  $BIN/voip-app"
echo
echo "在登录页输入已有 SIP 服务器及账号"
