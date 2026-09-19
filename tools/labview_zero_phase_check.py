"""LabVIEW 零相位(filtfilt)思路 vs 引擎 wms_harmonic_lockin 对照实测。"""
import io
import sys
import os

import numpy as np
from scipy.signal import butter, lfilter
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tdlas_sim as ts

FS, FM = 240000.0, 30000.0
N = 2000
T = np.arange(N) / FS
WIN = int(round(FS / FM))
N_STAGES = 2
CR = 0.25


def odd_pad(x, L):
    x = np.asarray(x, float)
    left = 2.0 * x[0] - x[1:1 + L][::-1]
    right = 2.0 * x[-1] - x[-1 - L:-1][::-1]
    return np.concatenate([left, x, right])


def lp_boxcar(X, cycles):
    ker = np.ones(WIN) / WIN
    L = WIN * 3
    out = np.asarray(X, float)
    for _ in range(cycles):
        p = odd_pad(out, L)
        f1 = np.convolve(p, ker, mode="full")[:p.size]
        f1 = f1[L:p.size - L]
        r = f1[::-1]
        p2 = odd_pad(r, L)
        f2 = np.convolve(p2, ker, mode="full")[:p2.size]
        f2 = f2[L:p2.size - L]
        out = f2[::-1]
    return out


def lp_butter(X):
    b, a = butter(N_STAGES, CR * FM / (0.5 * FS), btype="low")
    L = 3 * max(len(a), len(b))
    out = np.asarray(X, float)
    p = odd_pad(out, L)
    f1 = lfilter(b, a, p)
    f1 = f1[L:p.size - L]
    r = f1[::-1]
    p2 = odd_pad(r, L)
    f2 = lfilter(b, a, p2)
    f2 = f2[L:p2.size - L]
    out = f2[::-1]
    return out


def gain(lp, f):
    x = np.cos(2 * np.pi * f * T)
    y = lp(x)
    lo, hi = N // 5, 4 * N // 5
    return np.max(np.abs(y[lo:hi])) / np.max(np.abs(x[lo:hi]))


def lockin_labview(S, n, kind, cycles=1):
    X = S * np.cos(n * 2 * np.pi * FM * T)
    Y = S * np.sin(n * 2 * np.pi * FM * T)
    if kind == "boxcar":
        X, Y = lp_boxcar(X, cycles), lp_boxcar(Y, cycles)
    else:
        X, Y = lp_butter(X), lp_butter(Y)
    return np.sqrt(X ** 2 + Y ** 2)


def report():
    out = []
    out.append("=" * 72)
    out.append("LabVIEW 零相位(filtfilt)思路 vs 引擎 wms_harmonic_lockin 对照")
    out.append(f"fs={FS:.0f} fm={FM:.0f} fs/fm={FS/FM:.0f} N={N} WIN={WIN} n_stages={N_STAGES} cr={CR} fc={CR*FM:.0f}")
    out.append("=" * 72)
    out.append("\n[1] 低通幅频响应 |H(f)|（中段测量；boxcar 零点在 fm 整数倍）")
    out.append(f"{'方法':<28}{'|H(0)|':>10}{'|H(fm)|':>12}{'|H(2fm)|':>12}{'|H(3fm)|':>12}")
    g_bx2 = [gain(lambda x: lp_boxcar(x, 1), f) for f in (0, FM, 2 * FM, 3 * FM)]
    g_bx4 = [gain(lambda x: lp_boxcar(x, 2), f) for f in (0, FM, 2 * FM, 3 * FM)]
    g_bt = [gain(lp_butter, f) for f in (0, FM, 2 * FM, 3 * FM)]
    out.append(f"{'boxcar LabVIEW 1cyc(2遍=sinc^2)':<28}{g_bx2[0]:>10.2e}{g_bx2[1]:>12.2e}{g_bx2[2]:>12.2e}{g_bx2[3]:>12.2e}")
    out.append(f"{'boxcar LabVIEW 2cyc(4遍=sinc^4)':<28}{g_bx4[0]:>10.2e}{g_bx4[1]:>12.2e}{g_bx4[2]:>12.2e}{g_bx4[3]:>12.2e}")
    out.append(f"{'butter LabVIEW(2遍=4阶)':<28}{g_bt[0]:>10.2e}{g_bt[1]:>12.2e}{g_bt[2]:>12.2e}{g_bt[3]:>12.2e}")
    out.append("\n[2] 逐点一致性（引擎 vs LabVIEW 手动，同一输入）")
    rng = np.random.default_rng(0)
    S = (np.exp(-((np.arange(N) - 1000) ** 2) / (2 * 40 ** 2)) * np.cos(2 * np.pi * FM * T)
         + rng.standard_normal(N) * 0.05)
    for n in (1, 2):
        eng, _, _ = ts.wms_harmonic_lockin(S, T, FS, FM, n, 1, N_STAGES, lock_kind="boxcar", zero_phase=False)
        lab = lockin_labview(S, n, "boxcar", cycles=1)
        bulk = np.max(np.abs(eng[N // 5:4 * N // 5] - lab[N // 5:4 * N // 5]))
        full = np.max(np.abs(eng - lab))
        out.append(f"  boxcar 默认(2遍)  n={n}: bulk最大差={bulk:.2e}  全段最大差={full:.2e}")
    for n in (1, 2):
        eng, _, _ = ts.wms_harmonic_lockin(S, T, FS, FM, n, 1, N_STAGES, lock_kind="boxcar", zero_phase=True)
        lab = lockin_labview(S, n, "boxcar", cycles=2)
        bulk = np.max(np.abs(eng[N // 5:4 * N // 5] - lab[N // 5:4 * N // 5]))
        full = np.max(np.abs(eng - lab))
        out.append(f"  boxcar zp(4遍)    n={n}: bulk最大差={bulk:.2e}  全段最大差={full:.2e}")
    for n in (1, 2):
        eng, _, _ = ts.wms_harmonic_lockin(S, T, FS, FM, n, 1, N_STAGES, lock_kind="butter", cutoff_ratio=CR, zero_phase=True)
        lab = lockin_labview(S, n, "butter")
        bulk = np.max(np.abs(eng[N // 5:4 * N // 5] - lab[N // 5:4 * N // 5]))
        full = np.max(np.abs(eng - lab))
        out.append(f"  butter zp(2遍)    n={n}: bulk最大差={bulk:.2e}  全段最大差={full:.2e}")
    out.append("\n[3] 泄漏实测（输入=纯 2fm 正弦，锁相 n=1 -> 理想输出~0；基准=同信号锁相 n=2 的 2f 峰）")
    S2fm = np.cos(2 * np.pi * 2 * FM * T)

    def peak2f(sig):
        s2, _, _ = ts.wms_harmonic_lockin(sig, T, FS, FM, 2, 1, N_STAGES, lock_kind="boxcar", zero_phase=False)
        return np.max(s2[N // 5:4 * N // 5])
    base = peak2f(S2fm)
    for label, kind, zp, cycles in [("boxcar 默认(2遍)", "boxcar", False, 1),
                                    ("boxcar zp(4遍)", "boxcar", True, 2),
                                    ("butter zp(2遍)", "butter", True, 1)]:
        if kind == "boxcar":
            eng, _, _ = ts.wms_harmonic_lockin(S2fm, T, FS, FM, 1, 1, N_STAGES, lock_kind="boxcar", zero_phase=zp)
        else:
            eng, _, _ = ts.wms_harmonic_lockin(S2fm, T, FS, FM, 1, 1, N_STAGES, lock_kind="butter", cutoff_ratio=CR, zero_phase=zp)
        rms = np.sqrt(np.mean(eng[N // 5:4 * N // 5] ** 2))
        out.append(f"  {label:<16} 泄漏RMS={rms:.2e}  占2f峰比={rms/base:.2%}")
    out.append("\n[4] 峰位实测（高斯包络真值 k0=1000，锁相 n=1 -> 测得峰位应=1000）")
    k0 = 1000
    Sg = np.exp(-((np.arange(N) - k0) ** 2) / (2 * 30 ** 2)) * np.cos(2 * np.pi * FM * T)
    for label, kind, zp in [("boxcar 默认(2遍)", "boxcar", False),
                            ("boxcar zp(4遍)", "boxcar", True),
                            ("butter zp(2遍)", "butter", True)]:
        if kind == "boxcar":
            eng, _, _ = ts.wms_harmonic_lockin(Sg, T, FS, FM, 1, 1, N_STAGES, lock_kind="boxcar", zero_phase=zp)
        else:
            eng, _, _ = ts.wms_harmonic_lockin(Sg, T, FS, FM, 1, 1, N_STAGES, lock_kind="butter", cutoff_ratio=CR, zero_phase=zp)
        pk = int(np.argmax(eng))
        out.append(f"  {label:<16} 峰位={pk}  偏差={pk - k0} 样本")
    return "\n".join(out)


if __name__ == "__main__":
    txt = report()
    print(txt)
    with open("tmp_lvzp_out.txt", "w", encoding="utf-8") as fh:
        fh.write(txt)

