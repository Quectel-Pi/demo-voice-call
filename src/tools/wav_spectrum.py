#!/usr/bin/env python3
"""频谱诊断：找出 wav 里的主频、直流偏置、每帧能量

用法: wav_spectrum.py file.wav [file2.wav ...]
"""
import sys
import wave

import numpy as np


def analyze(path):
    w = wave.open(path)
    n, ch, sr = w.getnframes(), w.getnchannels(), w.getframerate()
    raw = w.readframes(n)
    w.close()
    if n == 0:
        print(f"{path}: 空")
        return
    a = np.frombuffer(raw, dtype="<i2").reshape(-1, ch).astype(np.float64)
    mono = a.mean(axis=1)
    peak = np.abs(mono).max()
    dc = mono.mean()
    print(f"\n=== {path} ===")
    print(f"  时长 {n/sr:.2f}s  采样率 {sr}  声道 {ch}")
    print(f"  peak={peak:.0f} ({20*np.log10(max(peak,1)/32768):.1f} dBFS)  "
          f"rms={np.sqrt((mono**2).mean()):.1f}  直流偏置={dc:.1f}")

    # 取能量最大的一段做 FFT
    win = min(sr * 2, len(mono))
    if win < 1024:
        print("  样本太少")
        return
    # 滑动找最响的窗
    best_off, best_e = 0, -1.0
    step = max(win // 8, 1)
    for off in range(0, len(mono) - win + 1, step):
        e = (mono[off:off + win] ** 2).mean()
        if e > best_e:
            best_e, best_off = e, off
    seg = mono[best_off:best_off + win]
    seg = seg - seg.mean()  # 去直流
    spec = np.abs(np.fft.rfft(seg * np.hanning(len(seg))))
    freqs = np.fft.rfftfreq(len(seg), 1 / sr)
    # 找前 5 个峰（排除近 0Hz）
    mask = freqs > 60
    sp, fr = spec[mask], freqs[mask]
    idx = np.argsort(sp)[-5:][::-1]
    print(f"  最响窗 @{best_off/sr:.2f}s，主频：")
    for i in idx:
        if sp[i] > 0:
            print(f"    {fr[i]:7.1f} Hz  幅度 {sp[i]:10.0f}")
    # 1kHz 与邻频比值
    def band(f, tol=25):
        m = (freqs >= f - tol) & (freqs <= f + tol)
        return spec[m].max() if m.any() else 0.0
    e1k, e700 = band(1000), band(700)
    print(f"  1kHz={e1k:.0f}  700Hz={e700:.0f}  比值={e1k/max(e700,1):.1f}")


for p in sys.argv[1:]:
    try:
        analyze(p.split("=", 1)[-1])
    except Exception as exc:
        print(f"{p}: {exc}")
