#!/bin/sh
# detect-audio.sh —— 用系统默认设备（排除虚拟节点）生成 voipd 的设备配置
#
# 为什么需要：liblinphone 在未指定设备时会选它自己列表里的第一个，往往不是
# 系统默认（PC 上曾选到未接线的 S/PDIF，板子上会选到 mic-ns 回声消除虚拟节点）。
# 这里把"系统默认 + 排除虚拟"的结果写死进配置，避免每次踩坑。
#
# 用法: ./detect-audio.sh [配置文件]
set -eu
CONF="${1:-$HOME/.config/voipd/voipd.conf}"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

virtual_re='monitor|null|dummy|mic-ns|echo|空输出|空输入'
digital_re='spdif|hdmi|digital|数字'

pick() { # $1 = sinks|sources
  def=$(pactl get-default-"$1" 2>/dev/null | sed 's/^sink(s)$//' || true)
  [ "$1" = "sinks" ] && def=$(pactl get-default-sink 2>/dev/null || true)
  [ "$1" = "sources" ] && def=$(pactl get-default-source 2>/dev/null || true)
  if [ -n "$def" ] && ! echo "$def" | grep -qiE "$virtual_re"; then
    echo "$def"; return
  fi
  # 默认是虚拟节点：从硬件设备里挑，先排除数字口
  list=$(pactl list short "$1" 2>/dev/null | cut -f2)
  c=$(echo "$list" | grep -viE "$virtual_re" | grep -viE "$digital_re" | head -1)
  [ -z "$c" ] && c=$(echo "$list" | grep -viE "$virtual_re" | head -1)
  echo "$c"
}

PLAY=$(pick sinks)
CAP=$(pick sources)
[ -z "$PLAY" ] && { echo "!! 找不到可用播放设备"; exit 1; }
[ -z "$CAP" ] && { echo "!! 找不到可用采集设备"; exit 1; }

echo "推荐播放设备: $PLAY"
echo "推荐采集设备: $CAP"

mkdir -p "$(dirname "$CONF")"
[ -f "$CONF" ] || : > "$CONF"
python3 - "$CONF" "$PLAY" "$CAP" <<'PY'
import sys, re
conf, play, cap = sys.argv[1], sys.argv[2], sys.argv[3]
# liblinphone 的设备名是 "PulseAudio: <描述> [Unknown]" 形式，
# 这里用 PulseAudio 后端前缀 + 描述（去掉 .sink/.source 后缀的推断交给用户核对）
def describe(name):
    return "PulseAudio: " + name
text = open(conf).read() if conf else ""
if "[account]" not in text:
    text = "[account]\nuser=1001\nsecret=pass1001\nserver=sip:127.0.0.1:5060\nsip_port=5061\ndisplay_name=话机\n" + text
text = re.sub(r"(?m)^capture_device=.*$", f"capture_device={describe(cap)}", text) if re.search(r"(?m)^capture_device=", text) else text + f"\ncapture_device={describe(cap)}\n"
text = re.sub(r"(?m)^playback_device=.*$", f"playback_device={describe(play)}", text) if re.search(r"(?m)^playback_device=", text) else text + f"\nplayback_device={describe(play)}\n"
open(conf, "w").write(text)
print(f"已写入 {conf}")
PY
echo
echo "注意：liblinphone 需要完整设备名（形如 'PulseAudio: <系统描述> [Unknown]'），"
echo "上面的名字可能需要按 voipd 日志里的 'capture device -> ...' 校准。"
echo "若设备名不匹配，voipd 会打印 set capture/playback device failed 并回退。"
