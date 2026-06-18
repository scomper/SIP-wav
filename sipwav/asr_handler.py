"""Layer 3: ASR 内容分析 — funasr 本地推理 + 文本 diff"""

import warnings
import threading
import os
import sys
import contextlib

import numpy as np
import librosa

warnings.filterwarnings("ignore")


_ASR_MODEL = None
_ASR_LOCK = threading.Lock()


@contextlib.contextmanager
def _suppress_output():
    """临时抑制 stdout/stderr（funasr 模型加载时的冗余输出）"""
    with open(os.devnull, "w") as devnull:
        old_stdout, old_stderr = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = devnull, devnull
        try:
            yield
        finally:
            sys.stdout, sys.stderr = old_stdout, old_stderr


def _get_asr_model():
    """单例加载 ASR 模型"""
    global _ASR_MODEL
    if _ASR_MODEL is not None:
        return _ASR_MODEL
    with _ASR_LOCK:
        if _ASR_MODEL is not None:
            return _ASR_MODEL
        os.environ["FUNASR_LOG_LEVEL"] = "ERROR"
        os.environ["MODELSCOPE_LOG_LEVEL"] = "ERROR"

        with _suppress_output():
            from funasr import AutoModel
            _ASR_MODEL = AutoModel(
                model="iic/speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
                vad_model="iic/speech_fsmn_vad_zh-cn-16k-common-pytorch",
                punc_model="iic/punc_ct-transformer_cn-en-common-vocab471067-large",
                model_hub="modelscope",
                trust_remote_code=True,
                disable_update=True,
                disable_log=True,
                log_level="ERROR",
            )
    return _ASR_MODEL


def _local_transcribe(y: np.ndarray, sr: int) -> dict:
    """funasr 本地 ASR 转写，返回带时间戳的分段结果"""
    model = _get_asr_model()
    if sr != 16000:
        y = librosa.resample(y, orig_sr=sr, target_sr=16000)
    raw = model.generate(input=y)
    text = ""
    segments = []
    numbers = []

    if raw and len(raw) > 0:
        texts = []
        for seg in raw:
            seg_text = seg.get("text", "").strip()
            if seg_text:
                texts.append(seg_text)
            # 提取时间戳 + 按标点分句
            ts = seg.get("timestamp", [])
            if ts and seg_text:
                # 中文数字 → 阿拉伯数字
                from .numbers import split_by_timing
                try:
                    numbers = split_by_timing(seg_text, ts)
                except Exception:
                    numbers = []

                import re
                chars = list(seg_text)
                ts_len = len(ts)
                punct_positions = [i for i, c in enumerate(chars) if c in "。！？，、；："]
                if punct_positions:
                    prev = 0
                    for pp in punct_positions:
                        frag = "".join(chars[prev:pp+1]).strip()
                        if frag and prev < ts_len and pp < ts_len:
                            st = ts[prev][0]/1000 if isinstance(ts[prev], (list,tuple)) else prev*0.03
                            et = ts[pp][1]/1000 if isinstance(ts[pp], (list,tuple)) else pp*0.03
                            segments.append({"text": frag, "start": round(st,2), "end": round(et,2)})
                        prev = pp + 1
                    # 尾部残片
                    if prev < len(chars) and prev < ts_len:
                        frag = "".join(chars[prev:]).strip()
                        if frag:
                            st = ts[prev][0]/1000 if isinstance(ts[prev],(list,tuple)) else prev*0.03
                            et_ix = min(ts_len-1, len(chars)-1)
                            et = ts[et_ix][1]/1000 if isinstance(ts[et_ix],(list,tuple)) else et_ix*0.03
                            segments.append({"text": frag, "start": round(st,2), "end": round(et,2)})
                else:
                    st = ts[0][0]/1000 if isinstance(ts[0],(list,tuple)) else 0
                    et = ts[-1][1]/1000 if isinstance(ts[-1],(list,tuple)) else len(seg_text)*0.03
                    segments.append({"text": seg_text, "start": round(st,2), "end": round(et,2)})

        text = "".join(texts)

    return {
        "text": text,
        "has_content": bool(text),
        "segments": segments,
        "numbers": numbers,
        "provider": "funasr",
    }


def _get_aliyun_api_key() -> str:
    """获取百炼 API Key"""
    key = os.environ.get("DASHSCOPE_API_KEY") or os.environ.get("ALIYUN_ASR_API_KEY", "")
    if key:
        return key
    csv_path = os.path.expanduser("~/Downloads/主账号空间-***REMOVED***.csv")
    if os.path.exists(csv_path):
        with open(csv_path, encoding="utf-8-sig") as f:
            for line in f:
                parts = line.strip().split(",", 1)
                if len(parts) == 2 and parts[0] == "apiKey":
                    return parts[1]
    return ""


def transcribe(y: np.ndarray, sr: int, use_fallback: bool = False) -> dict:
    """ASR 转写 — 优先本地 funasr，失败时回退到阿里云百炼

    Args:
        y: 音频信号
        sr: 采样率
        use_fallback: 是否启用阿里云回退

    Returns:
        {"text": "...", "has_content": bool, "provider": "funasr|aliyun"}
    """
    result = _local_transcribe(y, sr)
    if result["has_content"] or not use_fallback:
        return result

    # 本地 ASR 无结果 → 尝试阿里云百炼
    api_key = _get_aliyun_api_key()
    if not api_key:
        result["fallback_skipped"] = "未配置百炼 API Key"
        return result

    from . import asr_aliyun
    ali_result = asr_aliyun.transcribe(y, sr, api_key=api_key)
    return ali_result if ali_result.get("has_content") else result


def layer3_asr_check(
    y: np.ndarray, sr: int, ref_text: str | None = None, use_fallback: bool = False,
    ref_numbers: list[dict] | None = None
) -> dict:
    """Layer 3: ASR 检测（支持阿里云回退 + 数字时间轴漂移检测）"""
    from .numbers import detect_drift

    result = {"transcribed": transcribe(y, sr, use_fallback=use_fallback)}

    if ref_text and result["transcribed"]["has_content"]:
        result["diff"] = text_diff(ref_text, result["transcribed"]["text"])
        flags = []
        if result["diff"]["has_missing"]:
            flags.append("missing_words")
        if result["diff"]["has_extra"]:
            flags.append("extra_words")
        if result["diff"]["similarity"] < 0.5 and result["transcribed"]["has_content"]:
            flags.append("content_mismatch")
        result["verdict"] = "abnormal" if flags else "normal"
        result["flags"] = flags

        # 数字时间轴漂移检测
        test_numbers = result["transcribed"].get("numbers", [])
        if ref_numbers and test_numbers:
            drift = detect_drift(ref_numbers, test_numbers, threshold=1.0)
            result["drift"] = drift
            if drift.get("total_drift", 0) > 0:
                if "drift_detected" not in flags:
                    flags.append("drift_detected")
                result["flags"] = flags
                result["verdict"] = "abnormal"

    elif ref_text and not result["transcribed"]["has_content"]:
        result["verdict"] = "abnormal"
        result["flags"] = ["no_speech"]
    else:
        result["verdict"] = "normal"
        result["flags"] = []

    return result


def text_diff(ref_text: str, test_text: str) -> dict:
    """文本 diff，检测吞字/多余内容"""
    import difflib

    matcher = difflib.SequenceMatcher(None, ref_text, test_text)
    ratio = matcher.ratio()

    # 提取差异
    missing = []  # 参考有但测试没有
    extra = []    # 测试有但参考没有
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "delete":
            missing.append(ref_text[i1:i2])
        elif tag == "insert":
            extra.append(test_text[j1:j2])
        elif tag == "replace":
            missing.append(ref_text[i1:i2])
            extra.append(test_text[j1:j2])

    return {
        "similarity": round(ratio, 4),
        "missing": "".join(missing) if missing else "",
        "extra": "".join(extra) if extra else "",
        "has_missing": len("".join(missing)) > 0,
        "has_extra": len("".join(extra)) > 0,
    }



