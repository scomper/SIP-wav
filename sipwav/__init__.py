"""SIP-wav — SIP 语音异常检测工具"""

import warnings
import logging
import os

# 抑制 webrtcvad 的 pkg_resources 弃用警告
warnings.filterwarnings("ignore", message="pkg_resources is deprecated", category=UserWarning)

# 抑制 funasr/modelscope 的冗余输出
os.environ.setdefault("MODELSCOPE_LOG_LEVEL", "40")
os.environ.setdefault("FUNASR_LOG_LEVEL", "40")
logging.getLogger().setLevel(logging.ERROR)          # root logger
logging.getLogger("modelscope").setLevel(logging.ERROR)
logging.getLogger("funasr").setLevel(logging.ERROR)
_jieba_logger = logging.getLogger("jieba")
_jieba_logger.handlers.clear()
_jieba_logger.propagate = False
_jieba_logger.setLevel(logging.CRITICAL)

# 抑制 tqdm 进度条（funasr 推理时）
os.environ.setdefault("TQDM_DISABLE", "1")
