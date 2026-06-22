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
# 可选模型：
#   qwen3-asr-flash-filetrans — Qwen3-ASR（默认，精度最高，嘈杂/中英混合场景优势大）
#   paraformer-8k-v2          — Paraformer（8k 电话录音专用，支持热词）
#   fun-asr                   — Fun-ASR（工业级，支持热词）
DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com"
ASR_MODEL = os.environ.get("SIPWAV_ASR_MODEL", "qwen3-asr-flash-filetrans")


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
    import httpx

    try:
        t_start = time.time()

        # Step 1: 保存为临时 WAV
        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        try:
            sf.write(tmp.name, y, sr, subtype="PCM_16")
            tmp.close()

            # Step 2: 用 SDK 上传（兼容性最好）
            upload_resp = Files.upload(file_path=tmp.name, purpose="inference")
            if upload_resp.status_code != 200:
                return {"text": "", "error": f"上传失败: {upload_resp}", "provider": "aliyun"}
            file_id = upload_resp.output["uploaded_files"][0]["file_id"]
            file_info = Files.get(file_id)
            if file_info.status_code != 200:
                return {"text": "", "error": "获取文件信息失败", "provider": "aliyun"}
            file_url = file_info.output["url"]

            # Step 3: 提交转写（Qwen3 用 file_url 单数，其他用 file_urls 复数）
            if "qwen3" in ASR_MODEL:
                task_body = {"model": ASR_MODEL, "input": {"file_url": file_url}}
            else:
                task_body = {"model": ASR_MODEL, "input": {"file_urls": [file_url]}}

            submit_resp = httpx.post(
                "https://dashscope.aliyuncs.com/api/v1/services/audio/asr/transcription",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", "X-DashScope-Async": "enable"},
                json=task_body,
                timeout=30,
            )
            if submit_resp.status_code != 200:
                return {"text": "", "error": f"提交失败: {submit_resp.status_code}", "elapsed_s": round(time.time() - t_start, 2), "provider": "aliyun"}

            task_id = submit_resp.json().get("output", {}).get("task_id", "")
            if not task_id:
                return {"text": "", "error": "获取 task_id 失败", "provider": "aliyun"}

            # Step 4: 轮询结果（最长 5 分钟）
            headers_poll = {"Authorization": f"Bearer {api_key}"}
            for _ in range(60):
                time.sleep(5)
                poll = httpx.get(f"https://dashscope.aliyuncs.com/api/v1/tasks/{task_id}", headers=headers_poll, timeout=15)
                if poll.status_code != 200:
                    continue
                output = poll.json().get("output", {})
                status = output.get("task_status", "")
                if status == "SUCCEEDED":
                    break
                elif status in ("FAILED", "UNKNOWN"):
                    return {"text": "", "error": f"转写失败: {output.get('message', status)}", "elapsed_s": round(time.time() - t_start, 2), "provider": "aliyun"}
            else:
                return {"text": "", "error": "转写超时（5分钟）", "provider": "aliyun"}

            elapsed = time.time() - t_start

            # Step 5: 获取结果
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
