"""文件扫描器 — 递归扫描目录，只保留 WAV 文件"""

import os
import fnmatch


WAV_EXTENSIONS = {".wav", ".WAV"}


def find_wav_files(directory: str) -> list[str]:
    """递归扫描目录，返回所有 WAV 文件路径"""
    result = []
    for root, _, files in os.walk(directory):
        for f in files:
            ext = os.path.splitext(f)[1]
            if ext in WAV_EXTENSIONS:
                result.append(os.path.join(root, f))
    return sorted(result)


def filter_audio(files: list[str]) -> list[str]:
    """按扩展名过滤，排除非音频文件"""
    return [f for f in files if os.path.splitext(f)[1] in WAV_EXTENSIONS]
