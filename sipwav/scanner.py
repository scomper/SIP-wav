"""文件扫描器 — 递归扫描目录，只保留 WAV 文件"""

import os


WAV_EXTENSIONS = {".wav", ".WAV"}


def find_wav_files(directory: str) -> list[str]:
    """递归扫描目录，返回所有 WAV 文件路径（排除 macOS 元数据文件 ._xxx）"""
    result = []
    for root, _, files in os.walk(directory):
        for f in files:
            if f.startswith("._"):
                continue
            ext = os.path.splitext(f)[1]
            if ext in WAV_EXTENSIONS:
                result.append(os.path.join(root, f))
    return sorted(result)
