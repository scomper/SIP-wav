"""阿里云百炼 ASR 回退 — 上传文件到百炼 → 异步转写（带进度反馈）"""

import os
import sys
import time
import warnings
import tempfile
from typing import Optional

import numpy as np
import soundfile as sf

warnings.filterwarnings("ignore")


# 配置（可通过环境变量覆盖）
# 可选模型：
#   qwen3-asr-flash-filetrans — Qwen3-ASR（默认，精度最高，嘈杂/中英混合场景优势大）
#   paraformer-8k-v2          — Paraformer（8k 电话录音专用，支持热词）
#   fun-asr                   — Fun-ASR（工业级，支持热词）
DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com"
ASR_MODEL = os.environ.get("SIPWAV_ASR_MODEL", "paraformer-8k-v2")


def _get_api_key() -> str:
    """获取百炼 API Key：优先环境变量，其次 CSV 文件"""
    key = os.environ.get("DASHSCOPE_API_KEY") or os.environ.get("ALIYUN_ASR_API_KEY", "")
    if key:
        return key
    # 尝试从下载的 CSV 读取
    csv_path = os.path.expanduser("~/Downloads/主账号空间-***REMOVED***.csv")
    if os.path.exists(csv_path):
        with open(csv_path, encoding="utf-8-sig") as f:
            for line in f:
                parts = line.strip().split(",", 1)
                if len(parts) == 2 and parts[0] == "apiKey":
                    return parts[1]
    return ""


# ─── 热词管理 ─────────────────────────────────────────────────

# 默认热词：数字 1-10 + 常见电话术语（权重 5 = 最高）
DEFAULT_HOTWORDS = {
    "一": 5, "二": 5, "三": 5, "四": 5, "五": 5,
    "六": 5, "七": 5, "八": 5, "九": 5, "十": 5,
    "零": 5, "百": 3, "千": 3, "万": 3,
    "确认": 3, "取消": 3, "转接": 3, "人工": 3,
}

_PHRASE_ID = None  # 缓存热词 ID


def _get_phrase_id(api_key: str) -> str | None:
    """获取或创建热词表，返回 phrase_id"""
    global _PHRASE_ID
    if _PHRASE_ID is not None:
        return _PHRASE_ID

    try:
        import dashscope
        from dashscope.audio.asr import AsrPhraseManager
        dashscope.api_key = api_key

        # 查询已有热词
        result = AsrPhraseManager.list_phrases(model=ASR_MODEL)
        outputs = result.output.get("finetuned_outputs", []) if result.output else []
        if outputs:
            _PHRASE_ID = outputs[0].get("finetuned_output", "")
            return _PHRASE_ID

        # 没有则创建
        result = AsrPhraseManager.create_phrases(model=ASR_MODEL, phrases=DEFAULT_HOTWORDS)
        if result.output and hasattr(result.output, "finetuned_output"):
            _PHRASE_ID = result.output.finetuned_output
            return _PHRASE_ID
    except Exception:
        pass
    return None


def transcribe(y: np.ndarray, sr: int, api_key: Optional[str] = None) -> dict:
    """阿里云百炼 ASR 转写

    流程：上传到百炼 → 获取 OSS URL → 提交转写任务 → 获取结果

    Args:
        y: numpy 音频信号
        sr: 采样率
        api_key: 百炼 API Key（默认从环境变量读取）

    Returns:
        {"text": "...", "has_content": bool, "elapsed_s": float, "provider": "aliyun"}
    """
    api_key = api_key or _get_api_key()
    if not api_key:
        return {"text": "", "error": "未配置百炼 API Key", "provider": "aliyun"}

    import httpx

    def _progress(msg):
        sys.stdout.write(msg)
        sys.stdout.flush()

    try:
        t_start = time.time()
        headers = {"Authorization": f"Bearer {api_key}"}
        dur_s = len(y) / sr

        # Step 1: 保存为临时 WAV
        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        try:
            sf.write(tmp.name, y, sr, subtype="PCM_16")
            tmp.close()
            file_size_mb = os.path.getsize(tmp.name) / 1024 / 1024

            # Step 2: httpx 上传
            _progress(f"    上传中 ({file_size_mb:.1f}MB)...")
            t_upload = time.time()
            with open(tmp.name, "rb") as f:
                upload_resp = httpx.post(
                    "https://dashscope.aliyuncs.com/api/v1/files",
                    headers=headers,
                    files={"file": ("audio.wav", f, "audio/wav")},
                    data={"purpose": "file-extract"},
                    timeout=120,
                )
            if upload_resp.status_code != 200:
                _progress(f" 失败\n")
                return {"text": "", "error": f"上传失败: {upload_resp.status_code}", "provider": "aliyun"}

            file_id = upload_resp.json().get("data", {}).get("uploaded_files", [{}])[0].get("file_id", "")
            if not file_id:
                _progress(f" 失败\n")
                return {"text": "", "error": f"获取 file_id 失败", "provider": "aliyun"}

            _progress(f" {time.time()-t_upload:.1f}s\n")

            # Step 3: 获取下载 URL
            file_info = httpx.get(
                f"https://dashscope.aliyuncs.com/api/v1/files/{file_id}",
                headers=headers, timeout=30,
            )
            if file_info.status_code != 200:
                return {"text": "", "error": f"获取文件信息失败: {file_info.status_code}", "provider": "aliyun"}
            file_url = file_info.json().get("data", {}).get("url", "")
            if not file_url:
                return {"text": "", "error": "获取下载 URL 失败", "provider": "aliyun"}

            # Step 4: 提交转写（Paraformer 带热词）
            _progress(f"    提交转写 ({ASR_MODEL})...")
            if "qwen3" in ASR_MODEL:
                task_body = {"model": ASR_MODEL, "input": {"file_url": file_url}}
            else:
                task_body = {"model": ASR_MODEL, "input": {"file_urls": [file_url]}}
                # Paraformer 热词
                phrase_id = _get_phrase_id(api_key)
                if phrase_id:
                    task_body["parameters"] = {"phrase_id": phrase_id}

            submit_resp = httpx.post(
                "https://dashscope.aliyuncs.com/api/v1/services/audio/asr/transcription",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", "X-DashScope-Async": "enable"},
                json=task_body, timeout=30,
            )
            if submit_resp.status_code != 200:
                _progress(f" 失败\n")
                return {"text": "", "error": f"提交失败: {submit_resp.status_code}", "elapsed_s": round(time.time() - t_start, 2), "provider": "aliyun"}

            task_id = submit_resp.json().get("output", {}).get("task_id", "")
            _progress(f" OK\n")

            # Step 5: 轮询结果（带进度点）
            _progress(f"    识别中 ({dur_s:.0f}s 录音)")
            headers_poll = {"Authorization": f"Bearer {api_key}"}
            for i in range(120):
                time.sleep(5)
                poll = httpx.get(f"https://dashscope.aliyuncs.com/api/v1/tasks/{task_id}", headers=headers_poll, timeout=15)
                if poll.status_code != 200:
                    _progress(".")
                    continue
                output = poll.json().get("output", {})
                status = output.get("task_status", "")
                elapsed = time.time() - t_start
                _progress(".")
                if i > 0 and i % 6 == 0:
                    _progress(f" {elapsed:.0f}s")
                if status == "SUCCEEDED":
                    _progress(f" {elapsed:.0f}s\n")
                    break
                elif status in ("FAILED", "UNKNOWN"):
                    _progress(f" 失败: {output.get('message', '')}\n")
                    return {"text": "", "error": f"转写失败: {output.get('message', status)}", "elapsed_s": round(elapsed, 2), "provider": "aliyun"}
            else:
                _progress(f" 超时\n")
                return {"text": "", "error": "转写超时（10分钟）", "provider": "aliyun"}

            elapsed = time.time() - t_start

            # Step 6: 获取结果
            text = ""
            for r in output.get("results", []):
                tx_url = r.get("transcription_url", "")
                if tx_url:
                    tx_resp = httpx.get(tx_url, timeout=30)
                    if tx_resp.status_code == 200:
                        tx_data = tx_resp.json()
                        texts = [t.get("text", "") for t in tx_data.get("transcripts", [])]
                        text = " ".join(texts)

            return {
                "text": text.strip(),
                "has_content": bool(text.strip()),
                "elapsed_s": round(elapsed, 2),
                "provider": "aliyun",
            }

        finally:
            if os.path.exists(tmp.name):
                os.unlink(tmp.name)

    except Exception as e:
        return {"text": "", "error": str(e), "provider": "aliyun"}
