#!/usr/bin/env python3
"""音频自检工具：确认"能听到自己说的"、"对方能收到"

解决的问题：通话没声音时，快速区分是
  (A) 播放硬件/路由问题     -> selftest speaker
  (B) 麦克风/采集问题       -> selftest mic
  (C) 通话链路问题          -> 需要两端配合（见 --call 说明）

用法（在设备本机运行，需要 XDG_RUNTIME_DIR 指向用户运行时目录）：
    ./audio_selftest.py speaker         放 1kHz 音到你指定的播放设备
    ./audio_selftest.py mic             录 3 秒，报告电平（对着麦克风说话）
    ./audio_selftest.py loop            放音同时录音，测"声学/串扰"耦合度
    ./audio_selftest.py devices         列出所有设备及虚拟节点标记

关键判据：
  - 播放：`speaker` 后你能亲耳听到"嘟——"声（1kHz 持续 3 秒）。听不到就是
    播放设备选错（PC 上曾路由到未接线的 S/PDIF）或硬件静音（板子看 Speaker Switch）。
  - 采集：`mic` 时对着麦克风说话，RMS 应显著高于底噪（>20 dB 差值）。
    板子的 mic-ns 虚拟源实测 RMS ≈ -76 dBFS（哑的），必须用原始 ALSA 设备。
"""
import argparse
import os
import subprocess
import sys
import tempfile
import time
import wave
import struct
import math

try:
    import numpy as np
except ImportError:
    np = None


def sh(cmd, **kw):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, **kw)


def pactl(*args):
    return sh("pactl " + " ".join(f"'{a}'" for a in args)).stdout.strip()


def list_devices():
    sinks = []
    for line in pactl("list", "short", "sinks").splitlines():
        f = line.split("\t")
        if len(f) >= 2:
            sinks.append(f[1])
    sources = []
    for line in pactl("list", "short", "sources").splitlines():
        f = line.split("\t")
        if len(f) >= 2:
            sources.append(f[1])
    return sinks, sources


VIRTUAL_MARKS = ("monitor", "null", "dummy", "mic-ns", "echo", "空输出", "空输入")


def is_virtual(name):
    low = name.lower()
    return any(m in low for m in VIRTUAL_MARKS)


def show_devices():
    sinks, sources = list_devices()
    print("=== 播放设备（sink）===")
    for s in sinks:
        print(f"  {'[虚拟]' if is_virtual(s) else '[硬件]'} {s}")
    print("\n=== 采集设备（source）===")
    for s in sources:
        print(f"  {'[虚拟]' if is_virtual(s) else '[硬件]'} {s}")
    ds = pactl("get-default-sink")
    dc = pactl("get-default-source")
    print(f"\n默认播放: {ds}")
    print(f"默认采集: {dc}")
    if is_virtual(ds):
        print("  ⚠ 默认播放是虚拟设备，通话必须有声音的话请显式指定硬件设备！")
    if is_virtual(dc):
        print("  ⚠ 默认采集是虚拟设备，可能采集不到声音（板子的 mic-ns 就是这种）。")


def make_tone(path, seconds=3.0, freq=1000.0, amp=0.35, sr=48000):
    n = int(sr * seconds)
    frames = bytearray()
    for i in range(n):
        # 前后 30 ms 淡入淡出，避免爆音
        t = i / sr
        env = min(1.0, t / 0.03, max(0.0, (seconds - t)) / 0.03)
        v = amp * env * math.sin(2 * math.pi * freq * t)
        frames += struct.pack("<h", int(max(-1.0, min(1.0, v)) * 32767))
    w = wave.open(path, "wb")
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(sr)
    w.writeframes(bytes(frames))
    w.close()


def analyze(path):
    w = wave.open(path)
    n, ch, sr = w.getnframes(), w.getnchannels(), w.getframerate()
    raw = w.readframes(n)
    w.close()
    if n == 0:
        return None
    if np is not None:
        a = np.frombuffer(raw, dtype="<i2").reshape(-1, ch).astype(float).mean(axis=1)
        rms = float(np.sqrt((a ** 2).mean()))
        peak = float(np.abs(a).max())
        dom = ""
        if n > sr // 2:
            seg = a[: sr].astype(float)
            seg = seg - seg.mean()
            spec = np.abs(np.fft.rfft(seg * np.hanning(len(seg))))
            fr = np.fft.rfftfreq(len(seg), 1 / sr)
            mask = fr > 60
            if mask.any():
                i = int(np.argmax(spec * mask))
                dom = f" 主频 {fr[i]:.0f} Hz"
    else:
        vals = struct.unpack("<%dh" % (len(raw) // 2), raw)
        mono = [sum(vals[i * ch:(i + 1) * ch]) / ch for i in range(n)]
        rms = math.sqrt(sum(v * v for v in mono) / len(mono))
        peak = max(abs(v) for v in mono)
        dom = ""
    db = lambda x: 20 * math.log10(max(x, 1) / 32768)
    return dict(rms=rms, peak=peak, rms_db=db(rms), peak_db=db(peak), dom=dom,
                dur=n / sr)


def cmd_speaker(dev):
    sinks, _ = list_devices()
    dev = dev or pactl("get-default-sink")
    if dev not in sinks:
        print(f"!! 找不到播放设备: {dev}")
        print("可用：")
        for s in sinks:
            print("   ", s)
        return 1
    print(f"播放设备: {dev}")
    if is_virtual(dev):
        print("  ⚠ 这是虚拟设备，可能听不到声音")
    print("3 秒内应能听到 1kHz 的“嘟——”声…")
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        tone = f.name
    make_tone(tone, 3.0)
    r = sh(f"paplay --device='{dev}' '{tone}'")
    os.unlink(tone)
    if r.returncode != 0:
        print(f"!! 播放失败: {r.stderr.strip()}")
        return 1
    print("播放完成。若没听到：")
    print("  1) 检查该设备的音量/静音: pactl get-sink-volume/-mute")
    print("  2) 板子检查功放开关: amixer -c 0 cget numid=60  (Speaker Switch)")
    print("  3) 确认音箱/耳机插在对应接口（PC 上 S/PDIF 是数字口，接模拟音箱没声音）")
    return 0


def cmd_mic(dev, seconds):
    _, sources = list_devices()
    dev = dev or pactl("get-default-source")
    if dev not in sources:
        print(f"!! 找不到采集设备: {dev}")
        print("可用：")
        for s in sources:
            print("   ", s)
        return 1
    print(f"采集设备: {dev}")
    if is_virtual(dev):
        print("  ⚠ 这是虚拟设备（如回声消除节点），可能采集不到声音")
    print(f"请对着麦克风说话，录 {seconds} 秒…")
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        out = f.name
    sh(f"timeout -s INT {seconds + 2} parecord --device='{dev}' "
       f"--file-format=wav --rate=48000 --channels=1 '{out}'")
    a = analyze(out)
    os.unlink(out)
    if not a:
        print("!! 没录到数据")
        return 1
    print(f"  RMS {a['rms_db']:.1f} dBFS   peak {a['peak_db']:.1f} dBFS"
          f"   时长 {a['dur']:.1f}s{a['dom']}")
    if a["rms_db"] < -60:
        print("  ✗ 电平极低，基本等于静音。换一个采集设备再试：")
        for s in sources:
            if not is_virtual(s):
                print(f"     --device {s}")
        return 2
    if a["rms_db"] < -40:
        print("  ⚠ 偏低，建议提高麦克风增益（板子: amixer -c 0 sset 'ADCL PGA' 14）")
    else:
        print("  ✓ 电平正常")
    return 0


def cmd_loop(play, cap, seconds):
    print(f"播放 {play or '(默认)'}，同时采集 {cap or '(默认)'} —— 测耦合度")
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        tone = f.name
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        rec = f.name
    make_tone(tone, seconds)
    p = subprocess.Popen(
        f"timeout -s INT {seconds + 2} parecord --device='{cap or pactl('get-default-source')}' "
        f"--file-format=wav --rate=48000 --channels=2 '{rec}'",
        shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.5)
    sh(f"paplay --device='{play or pactl('get-default-sink')}' '{tone}'")
    p.wait()
    a = analyze(rec)
    os.unlink(tone)
    os.unlink(rec)
    if not a:
        print("!! 没录到数据")
        return 1
    print(f"  采集到 RMS {a['rms_db']:.1f} dBFS  peak {a['peak_db']:.1f} dBFS{a['dom']}")
    if a["rms_db"] > -35:
        print("  ✓ 耦合明显（扬声器确实在发声，或存在较强电气串扰）")
    elif a["rms_db"] > -55:
        print("  ⚠ 耦合较弱：可能扬声器音量小、距离远，或麦克风增益低")
    else:
        print("  ✗ 几乎无耦合：扬声器可能没发声，或采集设备不对")
    return 0


def main():
    ap = argparse.ArgumentParser(formatter_class=argparse.RawDescriptionHelpFormatter,
                                 description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p1 = sub.add_parser("speaker", help="放 1kHz 音测试播放")
    p1.add_argument("--device", default="")
    p2 = sub.add_parser("mic", help="录音并报告电平")
    p2.add_argument("--device", default="")
    p2.add_argument("--seconds", type=int, default=3)
    p3 = sub.add_parser("loop", help="放音同时录音，测耦合")
    p3.add_argument("--play", default="")
    p3.add_argument("--capture", default="")
    p3.add_argument("--seconds", type=int, default=3)
    sub.add_parser("devices", help="列出设备并标记虚拟节点")

    a = ap.parse_args()
    if not os.environ.get("XDG_RUNTIME_DIR"):
        os.environ["XDG_RUNTIME_DIR"] = f"/run/user/{os.getuid()}"
    if a.cmd == "devices":
        show_devices()
        return 0
    if a.cmd == "speaker":
        return cmd_speaker(a.device)
    if a.cmd == "mic":
        return cmd_mic(a.device, a.seconds)
    if a.cmd == "loop":
        return cmd_loop(a.play, a.capture, a.seconds)
    return 0


if __name__ == "__main__":
    sys.exit(main())
