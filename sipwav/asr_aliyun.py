"""阿里云百炼 ASR 回退 — 上传文件到百炼 → 异步转写"""

import os
import time
import warnings
import tempfile
from typing import Optional

import numpy as np
import soundfile as sf

warnings.filterwarnings("ignore")


# 配置（可通过环境变量覆盖）
DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com"
ASR_MODEL = "paraformer-8k-v2"  # 8k 版，匹配电话录音采样率


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

    os.environ["DASHSCOPE_API_KEY"] = api_key

    from dashscope import Files
    from dashscope.audio.asr import Transcription
    import httpx

    try:
        t_start = time.time()

        # Step 1: 保存为临时 WAV
        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        try:
            sf.write(tmp.name, y, sr, subtype="PCM_16")
            tmp.close()

            # Step 2: 上传到百炼
            upload_resp = Files.upload(file_path=tmp.name, purpose="inference")
            if upload_resp.status_code != 200:
                return {"text": "", "error": f"上传失败: {upload_resp}", "provider": "aliyun"}

            file_id = upload_resp.output["uploaded_files"][0]["file_id"]

            # Step 3: 获取文件 URL
            file_info = Files.get(file_id)
            if file_info.status_code != 200:
                return {"text": "", "error": f"获取文件信息失败", "provider": "aliyun"}
            file_url = file_info.output["url"]

            # Step 4: 提交转写
            result = Transcription.call(model=ASR_MODEL, file_urls=[file_url])
            elapsed = time.time() - t_start

            if result.status_code != 200:
                return {"text": "", "error": f"转写请求失败: {result}", "elapsed_s": round(elapsed, 2), "provider": "aliyun"}

            output = result.output
            if output.get("task_status") != "SUCCEEDED":
                msg = output.get("message", "未知错误")
                return {"text": "", "error": f"转写失败: {msg}", "elapsed_s": round(elapsed, 2), "provider": "aliyun"}

            # Step 5: 获取转写结果
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
