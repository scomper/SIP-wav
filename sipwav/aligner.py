"""Layer 2: 样本锚定比对 — 与参考语音做对齐和相似度计算"""

import numpy as np
import librosa


def extract_reference_profile(y: np.ndarray, sr: int) -> dict:
    """从参考语音提取锚定特征"""
    profile = {}

    # 时长
    profile["duration_s"] = len(y) / sr

    # 能量包络（降采样到 ~10fps 用于快速比对）
    frame_len = int(sr * 0.1)
    frames = np.lib.stride_tricks.sliding_window_view(y, frame_len)[::frame_len]
    profile["energy_envelope"] = np.sqrt(np.mean(frames ** 2, axis=1)).astype(np.float64)

    # MFCC（音色特征，用于 DTW 对齐）
    mfcc = librosa.feature.mfcc(y=y, sr=sr, n_mfcc=13, hop_length=int(sr * 0.03))
    profile["mfcc"] = mfcc.astype(np.float64)

    # RMS 统计
    profile["rms_mean"] = float(np.mean(profile["energy_envelope"]))
    profile["rms_std"] = float(np.std(profile["energy_envelope"]))

    return profile


# ─── 快速能量包络相似度 ────────────────────────────────────────

def envelope_similarity(ref_env: np.ndarray, test_env: np.ndarray) -> dict:
    """计算能量包络的相似度 — 用互相关对齐后算余弦相似度"""
    # 统一长度到较短的那个
    min_len = min(len(ref_env), len(test_env))
    r = ref_env[:min_len]
    t = test_env[:min_len]

    # 归一化
    r_norm = (r - np.mean(r)) / (np.std(r) + 1e-8)
    t_norm = (t - np.mean(t)) / (np.std(t) + 1e-8)

    # 余弦相似度
    cos_sim = float(np.dot(r_norm, t_norm) / (np.linalg.norm(r_norm) * np.linalg.norm(t_norm) + 1e-8))

    # 互相关峰值（用于检测整体偏移）
    corr = np.correlate(r_norm, t_norm, mode="full")
    peak_corr = float(np.max(corr)) / min_len

    return {
        "cosine_similarity": round(cos_sim, 4),
        "peak_cross_correlation": round(peak_corr, 4),
    }


# ─── DTW 对齐 ──────────────────────────────────────────────────

def dtw_align(ref_mfcc: np.ndarray, test_mfcc: np.ndarray) -> dict:
    """DTW 对齐，返回对齐路径和归一化代价"""
    D, wp = librosa.sequence.dtw(X=ref_mfcc, Y=test_mfcc, subseq=True)
    # 归一化代价（除以路径长度）
    cost = float(D[-1, -1] / max(wp.shape[0], 1))

    # 计算偏移量：路径偏离对角线的程度
    path_len = wp.shape[0]
    diag = np.arange(path_len) * (test_mfcc.shape[1] - 1) / max(path_len - 1, 1)
    drift = float(np.mean(np.abs(wp[:, 1] - diag)) / (test_mfcc.shape[1] + 1e-8))

    return {
        "dtw_cost": round(cost, 4),
        "drift": round(drift, 4),
        "path_length": path_len,
    }


# ─── VAD 节奏模式对比 ───────────────────────────────────────────

def vad_pattern_similarity(ref_segments: list, test_segments: list) -> dict:
    """对比说话段和静音段的节奏模式"""
    # 提取静音段列表
    ref_sil = [s for s in ref_segments if s["type"] == "silence"]
    test_sil = [s for s in test_segments if s["type"] == "silence"]

    if not ref_sil and not test_sil:
        return {"silence_pattern_match": 1.0, "note": "无静音段"}

    # 如果静音段数量不同，说明节奏差异大
    gap = abs(len(ref_sil) - len(test_sil))
    if len(ref_sil) > 0:
        match = max(0, 1 - gap / len(ref_sil))
    else:
        match = 1.0 if gap == 0 else 0.0

    # 静音段位置偏移
    pos_drift = 0
    for rs in ref_sil:
        # 找测试文件中最接近的静音段位置
        closest = min(test_sil, key=lambda s: abs(s["start"] - rs["start"]), default=None)
        if closest:
            pos_drift += abs(closest["start"] - rs["start"])
    if ref_sil:
        pos_drift /= len(ref_sil)

    return {
        "silence_count_match": round(match, 4),
        "silence_position_drift_s": round(pos_drift, 3),
        "ref_silence_count": len(ref_sil),
        "test_silence_count": len(test_sil),
    }


# ─── Layer 2 主入口 ─────────────────────────────────────────────

def layer2_sample_compare(
    y_test: np.ndarray,
    sr: int,
    ref_profile: dict,
    test_vad_segments: list | None = None,
    ref_vad_segments: list | None = None,
) -> dict:
    """Layer 2: 与参考样本对比，输出相似度评分"""
    result = {}

    # 时长比
    test_dur = len(y_test) / sr
    ref_dur = ref_profile["duration_s"]
    dur_ratio = test_dur / ref_dur if ref_dur > 0 else 0
    result["duration"] = {
        "test_s": round(test_dur, 3),
        "ref_s": round(ref_dur, 3),
        "ratio": round(dur_ratio, 4),
        "flag_too_short": dur_ratio < 0.7,
        "flag_too_long": dur_ratio > 1.3,
    }

    # 能量包络相似度
    frame_len = int(sr * 0.1)
    frames = np.lib.stride_tricks.sliding_window_view(y_test, frame_len)[::frame_len]
    test_env = np.sqrt(np.mean(frames ** 2, axis=1)).astype(np.float64)
    result["envelope"] = envelope_similarity(ref_profile["energy_envelope"], test_env)

    # DTW 对齐（MFCC 层面）
    try:
        test_mfcc = librosa.feature.mfcc(y=y_test, sr=sr, n_mfcc=13, hop_length=int(sr * 0.03))
        result["dtw"] = dtw_align(ref_profile["mfcc"], test_mfcc)
    except Exception as e:
        result["dtw"] = {"dtw_cost": -1, "drift": -1, "error": str(e)}

    # VAD 节奏对比
    if test_vad_segments and ref_vad_segments:
        result["vad_pattern"] = vad_pattern_similarity(ref_vad_segments, test_vad_segments)

    # ─── 综合判断 ───
    flags = []

    if result["duration"]["flag_too_short"]:
        flags.append("too_short")
    if result["duration"]["flag_too_long"]:
        flags.append("too_long")
    if result["envelope"]["cosine_similarity"] < 0.6:
        flags.append("envelope_mismatch")

    # DTW 漂移：只有当对齐代价不是趋近 0 时才判断漂移
    dtw = result.get("dtw", {})
    if dtw.get("dtw_cost", 0) < 0.01:
        # 代价趋近 0 = 完全一致，忽略漂移
        pass
    elif dtw.get("drift", 0) > 0.3:
        flags.append("high_drift")
    if dtw.get("dtw_cost", 0) > 0.5 and dtw.get("dtw_cost", -1) > 0:
        flags.append("high_dtw_cost")

    result["flags"] = flags
    result["verdict"] = "abnormal" if flags else "normal"
    return result
