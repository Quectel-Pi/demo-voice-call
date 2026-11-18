#!/usr/bin/env bash
# install-nosudo.sh —— 在没有 root / 没有 liblinphone-dev 的机器上安装本项目
#
# 适用：Ubuntu 22.04 等只有运行时库、缺开发包的桌面系统。
# 做法：把 liblinphone 及其依赖的 deb 解包到本地 sysroot（不写入系统目录），
#       用 sysroot 里的头文件/库编译 voipd，装到本地 prefix。
#
#   ./install-nosudo.sh              # 安装到 ~/voip-local
#   PREFIX=~/opt/voip ./install-nosudo.sh
#   ./install-nosudo.sh --service    # 额外安装 systemd 用户服务
#
# 如果本机已经有 liblinphone-dev（apt 装过），请直接用 scripts/install.sh。
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
PREFIX="${PREFIX:-$HOME/voip-local}"
SYSROOT="$PREFIX/sysroot"
DEBDIR="$PREFIX/debs"
BIN="$PREFIX/bin"
SHARE="$PREFIX/share/voip"
CONF="${CONF:-$PREFIX/etc/voipd.conf}"
UNITS="${UNITS:-$HOME/.config/systemd/user}"
WITH_SERVICE=0
[[ "${1:-}" == "--service" ]] && WITH_SERVICE=1

say() { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
die() { printf '\033[1;31m!! %s\033[0m\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- 1. 依赖探测
say "1. 探测依赖"
HAVE_SYSDEV=0
# 4.4 的聊天室需要 linphone-common 提供语法资源；只有头文件并不足够。
if [[ -f /usr/include/linphone/core.h ]] && \
   { [[ -f /usr/include/linphone/api/c-account-params.h ]] || \
     [[ -f /usr/share/belr/grammars/identity_grammar ]]; }; then
  HAVE_SYSDEV=1
fi
echo "  liblinphone 开发包及聊天资源: $([[ $HAVE_SYSDEV == 1 ]] && echo 系统已就绪 || echo 不完整，将用本地 sysroot)"

for c in g++ make; do command -v $c >/dev/null || die "缺少 $c（apt install build-essential）"; done
python3 -c "import gi; gi.require_version('Gtk','4.0'); gi.require_version('Adw','1')" 2>/dev/null \
  || die "缺少 python3-gi / gir1.2-gtk-4.0 / gir1.2-adw-1"

# ---------------------------------------------------------------- 2. sysroot
if [[ $HAVE_SYSDEV == 0 ]]; then
  say "2. 准备本地 sysroot（$SYSROOT）"
  mkdir -p "$DEBDIR" "$SYSROOT"
  # 链接期需要的：-dev 包（头文件+so 链接）
  DEV_PKGS="liblinphone-dev liblinphone10 liblinphone++10 libbelcard-dev libbellesip-dev
            libbelr-dev libmediastreamer-dev libortp-dev libbctoolbox-dev
            libglib2.0-dev libmount-dev libffi-dev libpcre2-dev zlib1g-dev"
  # 运行期需要的：-dev 包不带的实体库
  RUN_PKGS="linphone-common libbctoolbox1 libbelcard1 libbellesip1 libbelr1 libmediastreamer11 libortp15
            libsoci-core4.0 libsoci-sqlite3-4.0
            libbzrtp0 libsrtp2-1 libglew2.2 libturbojpeg libantlr3c-3.4-0"
  cd "$DEBDIR"
  # shellcheck disable=SC2086
  apt-get download $DEV_PKGS $RUN_PKGS 2>&1 | tail -3
  for d in *.deb; do dpkg-deb -x "$d" "$SYSROOT" 2>/dev/null || true; done
  # 去掉 glib 的静态库和悬空 .so 链接，让链接器改用系统运行时 .so.N
  ( cd "$SYSROOT/usr/lib/x86_64-linux-gnu" && \
    rm -f libgio-2.0.a libglib-2.0.a libgobject-2.0.a \
          libgio-2.0.so libglib-2.0.so libgobject-2.0.so 2>/dev/null || true )
  [[ -f $SYSROOT/usr/include/linphone/core.h ]] || die "sysroot 准备失败"
  echo "  OK: $(ls "$DEBDIR"/*.deb | wc -l) 个包已解包"
else
  say "2. 跳过 sysroot（用系统开发包）"
fi

# ---------------------------------------------------------------- 3. 编译
say "3. 编译 voipd"
BUILD="$PREFIX/build"; mkdir -p "$BUILD"
if [[ $HAVE_SYSDEV == 1 ]]; then
  make -B -C "$HERE/src/voipd"
  cp "$HERE/build/voipd" "$BUILD/voipd"
else
  L="$SYSROOT/usr/lib/x86_64-linux-gnu"
  g++ -O2 -Wno-deprecated-declarations -std=c++17 \
    -I"$SYSROOT/usr/include" \
    -I"$SYSROOT/usr/include/glib-2.0" \
    -I"$L/glib-2.0/include" \
    -DVOIP_WITH_BELR \
    -o "$BUILD/voipd" "$HERE/src/voipd/voipd.cpp" \
    -L"$L" -Wl,--disable-new-dtags -Wl,-rpath,"$L" \
    -llinphone -lmediastreamer -lortp -lbelcard -lbctoolbox -lbellesip -lbelr \
    -l:libsoci_core.so.4.0 -l:libsoci_sqlite3.so.4.0 \
    -l:libgio-2.0.so.0 -l:libgobject-2.0.so.0 -l:libglib-2.0.so.0
fi
[[ -x $BUILD/voipd ]] || die "编译失败"
echo "  $BUILD/voipd"

# ---------------------------------------------------------------- 4. 安装
say "4. 安装到 $PREFIX"
install -d "$BIN" "$SHARE/ui" "$SHARE/assets" "$(dirname "$CONF")"
install -m 0755 "$BUILD/voipd" "$BIN/voipd"
install -m 0644 "$HERE/src/ui/voipapp.py" "$SHARE/ui/voipapp.py"
install -m 0644 "$HERE/src/ui/style.css"   "$SHARE/ui/style.css"
install -m 0644 "$HERE/assets/ring.wav" "$SHARE/assets/ring.wav"
install -m 0755 "$HERE/scripts/run-app.sh" "$SHARE/run-app.sh"
cat > "$BIN/voip-ui" <<EOF
#!/bin/sh
exec python3 "$SHARE/ui/voipapp.py" "\$@"
EOF
chmod 0755 "$BIN/voip-ui"
cat > "$BIN/voip-app" <<EOF
#!/bin/sh
VOIPD_BIN="$BIN/voipd" VOIP_UI_SCRIPT="$SHARE/ui/voipapp.py" \
VOIP_CONFIG="$CONF" exec "$SHARE/run-app.sh"
EOF
chmod 0755 "$BIN/voip-app"
echo "  $BIN/voipd"
echo "  $BIN/voip-ui"
echo "  $BIN/voip-app"

# ---------------------------------------------------------------- 5. 配置
say "5. 生成配置（$CONF）"
DATA_RESOURCES_DIR=""
[[ $HAVE_SYSDEV == 0 ]] && DATA_RESOURCES_DIR="$SYSROOT/usr/share"
SERVER="${SERVER:-10.66.84.129:5060}"
USER_NAME="${USER_NAME:-1002}"
if [[ -f $CONF ]]; then
  echo "  已存在，保留"
else
  cat > "$CONF" <<EOF
# voipd 配置（本机）。改完重启: systemctl --user restart voipd
[account]
user=$USER_NAME
secret=pass$USER_NAME
server=sip:$SERVER
# 本机 SIP 监听端口；确保没有其他程序占用
sip_port=5060
display_name=本机

# 音频设备：留空=系统默认
capture_device=
playback_device=

# 非系统安装（sysroot）必须指定，否则 belr 语法/资源找不到
data_resources_dir=$DATA_RESOURCES_DIR

[general]
auto_answer=false
verbose=false
EOF
  echo "  $CONF"
fi

# liblinphone 4.4 把用户数据库固定在 XDG 目录
[[ $HAVE_SYSDEV == 0 ]] && mkdir -p "$HOME/.local/share/linphone"

if ((WITH_SERVICE)); then
  say "6. 清理旧 systemd 自动启动项"
  install -d "$UNITS"
  sed -e "s|%h/.local/bin/voipd|$BIN/voipd|" \
      -e "s|%h/.config/voipd/voipd.conf|$CONF|" \
      "$HERE/scripts/voipd.service" > "$UNITS/voipd.service"
  sed -e "s|%h/.local/bin/voip-app|$BIN/voip-app|" \
      "$HERE/scripts/voip-ui.service" > "$UNITS/voip-ui.service"
  systemctl --user daemon-reload
  systemctl --user disable --now voip-ui.service voipd.service 2>/dev/null || true
  echo "  服务文件已保留兼容，但处于禁用状态"
else
  say "6. 不安装 systemd 自动启动项"
fi

say "完成"
cat <<EOF
启动：
  $BIN/voip-app

自检：
  gdbus call --session --dest org.example.Voip1 --object-path /org/example/Voip1 \\
             --method org.example.Voip1.GetSnapshot
EOF
