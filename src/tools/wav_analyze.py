#!/usr/bin/env python3
"""分析 wav：峰值、RMS、指定频率能量（Goertzel）"""
import math
import struct
import sys
import wave


def goertzel(x, f, sr):
    w0 = 2 * math.pi * f / sr
    c = 2 * math.cos(w0)
    s1 = s2 = 0.0
    for v in x:
        s0 = v + c * s1 - s2
        s2 = s1
        s1 = s0
    return math.sqrt(max(0.0, s1 * s1 + s2 * s2 - c * s1 * s2)) / max(1, len(x))


def main():
    for spec in sys.argv[1:]:
        if "=" in spec:
            tag, path = spec.split("=", 1)
        else:
            tag, path = "", spec
        try:
            w = wave.open(path)
        except Exception as exc:
            print(f"{tag}: 打不开 {path}: {exc}")
            continue
        n, ch, sr = w.getnframes(), w.getnchannels(), w.getframerate()
        raw = w.readframes(n)
        w.close()
        s = struct.unpack("<%dh" % (len(raw) // 2), raw)
        mono = [sum(s[i * ch:(i + 1) * ch]) / ch for i in range(n)]
        if not mono:
            print(f"{tag}: 空文件")
            continue
        peak = max(abs(v) for v in mono)
        rms = math.sqrt(sum(v * v for v in mono) / len(mono))
        g1k = goertzel(mono, 1000, sr)
        dbfs = 20 * math.log10(max(peak, 1) / 32768)
        print(f"{tag}: 时长{n/sr:.1f}s peak={peak:6.0f} ({dbfs:6.1f} dBFS) "
              f"rms={rms:7.1f} 1kHz={g1k:8.1f}")


if __name__ == "__main__":
    main()
