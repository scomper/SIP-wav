"""Layer 1: 快速波形特征提取 (numpy + webrtcvad, 毫秒级)"""

import warnings
import wave
import struct
import math

import numpy as np
import webrtcvad

warnings.filterwarnings("ignore", category=DeprecationWarning)


def load_wav(path: str, sr: int = 8000) -> tuple[np.ndarray, int]:
    """加载 WAV 文件，返回 (信号数组, 实际采样率)
    
    支持标准 PCM 和压缩格式（ADPCM 等），会自动解码。
    """
    try:
        # 先用标准 wave 模块尝试（最快，支持 PCM）
        with wave.open(str(path), "rb") as wf:
            fs = wf.getframerate()
            frames = wf.readframes(wf.getnframes())
            dtype = np.int16 if wf.getsampwidth() == 2 else np.int8
            y = np.frombuffer(frames, dtype=dtype).astype(np.float64) / (32768.0 if dtype == np.int16 else 128.0)
    except (wave.Error, Exception):
        # 标准 wave 失败 → 用 soundfile 解码（支持 ADPCM 等压缩格式）
        try:
            import soundfile as sf
            y, fs = sf.read(str(path), dtype="float64")
            if y.ndim > 1:
                y = np.mean(y, axis=1)  # 多声道转单声道
        except Exception:
            # 最后尝试 librosa
            import librosa
            y, fs = librosa.load(str(path), sr=None, mono=True)
            y = y.astype(np.float64)

    # 统一转单声道
    if y.ndim > 1:
        y = np.mean(y, axis=1)

    # 如果采样率不匹配，用 librosa 重采样（比简单降采样质量好）
    if sr and fs != sr:
        import librosa
        y = librosa.resample(y, orig_sr=fs, target_sr=sr)
        fs = sr
    return y, fs


# ─── 快速能量分析 ───────────────────────────────────────────────

def frame_rms(y: np.ndarray, frame_len: int, hop_len: int) -> np.ndarray:
    """帧级 RMS 能量 (矢量运算, 极快)"""
    frames = np.lib.stride_tricks.sliding_window_view(y, frame_len)[::hop_len]
    return np.sqrt(np.mean(frames ** 2, axis=1))


def compute_energy_profile(y: np.ndarray, sr: int) -> dict:
    """能量包络分析 — 检测断流/静音/截断"""
    frame_ms = 30
    frame_len = int(sr * frame_ms / 1000)
    hop_len = frame_len
    rms = frame_rms(y, frame_len, hop_len)

    profile = {
        "rms_mean": float(np.mean(rms)),
        "rms_std": float(np.std(rms)),
        "rms_max": float(np.max(rms)),
        "rms_min": float(np.min(rms)),
        "rms_variation": float(np.std(rms) / (np.mean(rms) + 1e-8)),
        "peak": float(np.max(np.abs(y))),
        "duration_s": len(y) / sr,
    }

    # 判断是否几乎无声 (no voice / pure tone)
    profile["likely_no_voice"] = profile["rms_variation"] < 0.05 and profile["rms_mean"] < 0.01

    return profile


# ─── VAD 静音检测 ───────────────────────────────────────────────

def vad_segments(y: np.ndarray, sr: int, aggressiveness: int = 1) -> list[dict]:
    """VAD 分析，返回说话段和静音段的起止时间"""
    vad = webrtcvad.Vad(aggressiveness)
    # webrtcvad 要求 16-bit PCM, 仅支持 8/16/32/48kHz
    frame_ms = 30
    frame_len = int(sr * frame_ms / 1000)
    pcm = (y * 32768.0).astype(np.int16).tobytes()

    voiced = []
    for i in range(0, len(y) - frame_len + 1, frame_len):
        chunk = pcm[i * 2 : (i + frame_len) * 2]
        if len(chunk) < frame_len * 2:
            break
        try:
            is_speech = vad.is_speech(chunk, sr)
        except Exception:
            is_speech = False
        t_start = i / sr
        t_end = (i + frame_len) / sr
        voiced.append(is_speech)

    # 合并连续段
    segments = []
    in_speech = False
    seg_start = 0
    for i, v in enumerate(voiced):
        t = i * frame_ms / 1000
        if v and not in_speech:
            in_speech = True
            seg_start = t
        elif not v and in_speech:
            in_speech = False
            segments.append({"type": "speech", "start": seg_start, "end": t})
        elif i == len(voiced) - 1 and in_speech:
            segments.append({"type": "speech", "start": seg_start, "end": t + frame_ms / 1000})
    # 反向推静音段
    silent = []
    prev_end = 0.0
    for seg in segments:
        if seg["start"] - prev_end > 0.1:
            silent.append({"type": "silence", "start": prev_end, "end": seg["start"]})
        prev_end = seg["end"]
    total_dur = len(y) / sr
    if total_dur - prev_end > 0.1:
        silent.append({"type": "silence", "start": prev_end, "end": total_dur})

    return {"segments": sorted(segments + silent, key=lambda x: x["start"]), "voiced_ratio": sum(1 for v in voiced if v) / len(voiced) if voiced else 0}


def analyze_silence(segments: list, threshold: float = 1.0) -> list[dict]:
    """检测超过 threshold 秒的静音段"""
    long_silence = []
    for seg in segments:
        if seg["type"] == "silence":
            dur = seg["end"] - seg["start"]
            if dur > threshold:
                long_silence.append({**seg, "duration": round(dur, 3)})
    return long_silence


# ─── 纯音/忙音检测 ───────────────────────────────────────────────

def detect_pure_tone(y: np.ndarray, sr: int, top_n: int = 3) -> dict:
    """检测是否为纯音信号（忙音/回铃音）"""
    from scipy import signal as sp_signal

    # 短时 FFT
    f, t, Zxx = sp_signal.stft(y[:min(len(y), sr * 5)], fs=sr, nperseg=256)
    mean_mag = np.mean(np.abs(Zxx), axis=1)

    top_idx = np.argsort(mean_mag)[-top_n:][::-1]
    top_freqs = f[top_idx]
    top_energy = mean_mag[top_idx]

    total_energy = np.sum(mean_mag)
    dominant_ratio = float(np.sum(top_energy) / (total_energy + 1e-8))

    # 检测周期性脉冲（忙音特征）
    envelope = np.abs(y[::int(sr * 0.01)])  # 10ms 粒度
    envelope = envelope - np.mean(envelope)
    peaks, props = sp_signal.find_peaks(envelope, height=np.std(envelope) * 2, distance=int(1.0 / 0.01))

    result = {
        "dominant_freqs": [round(f, 1) for f in top_freqs],
        "dominant_ratio": round(dominant_ratio, 4),
        "peak_count": len(peaks),
    }
    if len(peaks) > 1:
        intervals = np.diff(peaks) * 0.01
        result["peak_interval_mean"] = round(float(np.mean(intervals)), 3)
        result["peak_interval_std"] = round(float(np.std(intervals)), 3)

    result["is_pure_tone"] = dominant_ratio > 0.6
    return result


# ─── 过零率 ──────────────────────────────────────────────────────

def compute_zcr(y: np.ndarray, sr: int) -> dict:
    """过零率分析，辅助判断语音 vs 纯音 vs 杂音"""
    frame_len = int(sr * 0.03)
    frames = np.lib.stride_tricks.sliding_window_view(y, frame_len)[::frame_len]
    zcr_frames = np.sum(np.diff(np.sign(frames), axis=1) != 0, axis=1) / (2 * frame_len)

    return {
        "zcr_mean": float(np.mean(zcr_frames)),
        "zcr_std": float(np.std(zcr_frames)),
        "zcr_max": float(np.max(zcr_frames)),
    }


# ─── 截断检测 ────────────────────────────────────────────────────

def detect_truncation(y: np.ndarray, sr: int) -> dict:
    """检测尾部是否被截断（能量骤降）

    只检测真正的异常截断，电话录音尾部正常静音不报：
    - 需要尾部静音持续 >3s 才算截断（排除正常挂机前的短暂停顿）
    - 需要整段有语音内容（排除纯静音录音）
    - 断崖式下降需要在语音段内突然发生
    """
    frame_ms = 30
    frame_len = int(sr * frame_ms / 1000)
    rms = frame_rms(y, frame_len, frame_len)

    if len(rms) < 10:
        return {"is_truncated": False}

    total_dur = len(y) / sr
    rms_mean = float(np.mean(rms))

    # 纯静音录音不算截断（那是"无声"问题）
    if rms_mean < 0.001:
        return {"is_truncated": False}

    # 检查尾部静音：从末尾向前找最后一个有声帧
    silence_threshold = rms_mean * 0.05  # 低于均值 5% 视为静音
    last_sound_idx = len(rms) - 1
    while last_sound_idx > 0 and rms[last_sound_idx] < silence_threshold:
        last_sound_idx -= 1

    tail_silence_dur = (len(rms) - 1 - last_sound_idx) * frame_ms / 1000

    # 尾部静音 >3s 且占总时长 >10% → 可能截断（排除正常挂机）
    if tail_silence_dur > 3.0 and tail_silence_dur / total_dur > 0.1:
        # 检查是否有语音内容（有语音才可能是截断）
        voiced_frames = np.sum(rms > silence_threshold)
        if voiced_frames / len(rms) > 0.3:
            return {"is_truncated": True, "reason": f"尾部静音{tail_silence_dur:.0f}s"}

    # 检查最后 20% 是否有断崖式下降（比原来 10% 更严格）
    last_segment = rms[-len(rms) // 5:]
    if len(last_segment) > 5:
        segment_mean = np.mean(last_segment)
        overall_mean = np.mean(rms)
        # 断崖式下降：尾部均值 < 整体均值的 5%，且尾部静音 >2s
        if segment_mean < overall_mean * 0.05 and tail_silence_dur > 2.0:
            return {"is_truncated": True, "reason": "尾部断崖式下降"}

    return {"is_truncated": False}


# ─── 编码格式探测 ────────────────────────────────────────────────

def detect_codec(y: np.ndarray, sr: int) -> dict:
    """从波形特征推测 SIP 编码格式 (G.711 / G.729)"""
    frame_len = int(sr * 0.03)
    frames = np.lib.stride_tricks.sliding_window_view(y, frame_len)[::frame_len]
    rms = np.sqrt(np.mean(frames ** 2, axis=1))
    sorted_rms = np.sort(rms)
    silence_rms = float(np.mean(sorted_rms[:max(1, len(sorted_rms) // 10)]))

    if silence_rms < 0.001:
        return {"codec": "g711", "confidence": 0.85, "silence_rms": silence_rms}
    elif silence_rms < 0.003:
        return {"codec": "g711", "confidence": 0.65, "silence_rms": silence_rms}
    elif silence_rms < 0.01:
        return {"codec": "g729", "confidence": 0.75, "silence_rms": silence_rms}
    else:
        return {"codec": "unknown", "confidence": 0.3, "silence_rms": silence_rms}


# ─── Layer 1 主入口 ──────────────────────────────────────────────

def layer1_fast_scan(y: np.ndarray, sr: int, silence_threshold: float = 2.0) -> dict:
    """Layer 1 快速筛查 — 所有文件必经

    Args:
        silence_threshold: 静音检测阈值（秒），默认 2.0s
    """
    result = {}

    # 能量
    result["energy"] = compute_energy_profile(y, sr)
    # 快速退出：无声
    if result["energy"]["likely_no_voice"]:
        result["verdict"] = "abnormal"
        result["flags"] = ["likely_no_voice"]
        result["details"] = "全程能量极低且稳定，非语音信号"
        return result

    # VAD
    vad = vad_segments(y, sr)
    result["vad"] = {
        "voiced_ratio": round(vad["voiced_ratio"], 4),
        "silence_gt_threshold": analyze_silence(vad["segments"], silence_threshold),
    }

    # 纯音检测
    result["tone"] = detect_pure_tone(y, sr)

    # 过零率
    result["zcr"] = compute_zcr(y, sr)

    # 截断
    result["truncation"] = detect_truncation(y, sr)

    # ─── 综合判断 ───
    flags = []
    details = []

    # 纯音/忙音 → 非语音
    if result["tone"]["is_pure_tone"]:
        flags.append("pure_tone")
        details.append(f"纯音信号，主频 {result['tone']['dominant_freqs'][0]} Hz")

    # 静音过长（去重：只标记一次，汇总统计）
    silences = result["vad"]["silence_gt_threshold"]
    if silences:
        flags.append("long_silence")
        longest = max(silences, key=lambda s: s["duration"])
        total_sil = sum(s["duration"] for s in silences)
        details.append(f"{len(silences)}段静音(>1s)，总静音{total_sil:.1f}s，最长{longest['duration']:.1f}s@{longest['start']:.0f}s")

    # 截断
    if result["truncation"]["is_truncated"]:
        flags.append("truncated")
        details.append(result["truncation"]["reason"])

    # 声音矩形比异常
    if result["energy"]["rms_variation"] < 0.1 and not result["tone"]["is_pure_tone"]:
        flags.append("abnormal_energy")
        details.append("能量变化过小，疑似非语音信号")

    result["flags"] = flags
    result["verdict"] = "abnormal" if flags else "normal"
    result["details"] = "；".join(details) if details else "正常"
    return result
