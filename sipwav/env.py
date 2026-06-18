"""环境检测 — 自适应本机(macOS) 和服务器(CentOS) 两种部署场景"""

import importlib
import platform
import sys


def detect() -> dict:
    """检测当前环境可用能力"""
    info = {
        "platform": platform.system().lower(),
        "python": sys.version,
    }

    # ASR 引擎
    info["has_funasr"] = _check("funasr")
    info["has_aliyun_sdk"] = _check("dashscope")

    # 信号处理
    info["has_librosa"] = _check("librosa")
    info["has_webrtcvad"] = _check("webrtcvad")
    info["has_scipy"] = _check("scipy.signal")

    # 深度学习
    info["has_torch"] = _check("torch")

    # 推荐模式
    info["recommended_asr_mode"] = _recommend_asr(info)
    info["recommended_phases"] = _recommend_phases(info)

    return info


def _check(module: str) -> bool:
    """检查模块是否可导入"""
    try:
        importlib.import_module(module)
        return True
    except ImportError:
        return False


def _recommend_asr(info: dict) -> str:
    """推荐 ASR 模式"""
    if info["has_funasr"] and info["has_aliyun_sdk"]:
        return "auto"       # 本机：本地优先 + 阿里云回退
    elif info["has_funasr"]:
        return "local"      # 仅本地
    elif info["has_aliyun_sdk"]:
        return "aliyun"     # 仅阿里云（CentOS 典型场景）
    else:
        return "disabled"   # 无 ASR 能力


def _recommend_phases(info: dict) -> str:
    """推荐管线阶段"""
    phases = ["1"]  # L1 numpy 是所有环境都能跑的
    if info["has_librosa"]:
        phases.append("2")  # L2 需要 librosa DTW
    # L3 取决于 ASR 能力
    if info["has_funasr"] or info["has_aliyun_sdk"]:
        phases.append("3")
    return "".join(phases)


def summary() -> str:
    """生成环境摘要"""
    e = detect()
    lines = [f"平台: {e['platform']} ({e['python'].split()[0]})"]
    lines.append(f"  funasr:    {'✅' if e['has_funasr'] else '❌'}  (本地 ASR)")
    lines.append(f"  dashscope: {'✅' if e['has_aliyun_sdk'] else '❌'}  (阿里云 ASR)")
    lines.append(f"  librosa:   {'✅' if e['has_librosa'] else '❌'}  (波形比对/DTW)")
    lines.append(f"  webrtcvad: {'✅' if e['has_webrtcvad'] else '❌'}  (静音检测)")
    lines.append(f"  torch:     {'✅' if e['has_torch'] else '❌'}  (深度学习)")
    lines.append(f"推荐 ASR 模式: {e['recommended_asr_mode']}")
    lines.append(f"推荐管线: Phase {' + '.join(list(e['recommended_phases']))}")
    return "\n".join(lines)


if __name__ == "__main__":
    print(summary())
