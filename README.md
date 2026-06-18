# SIP-wav

SIP 语音异常检测工具。通过波形分析和 ASR 内容比对，快速筛选通话录音中的质量问题——静音、截断、纯音、内容漂移、时间轴偏移。

三层管线逐层过滤，越早发现问题越省算力。

## 检测能力

### 模式 A — 波形异常检测

逐文件快速筛查，不需要参考样本。

| 检测项 | 规则 | 阈值 |
|--------|------|------|
| 静音超长 | VAD 连续非语音帧 | > 2s（可配） |
| 通话截断 | 尾部能量梯度骤降 | 梯度 ≥ 3σ |
| 纯音/忙音 | 频谱主频能量占比 > 60% | 输出主频 Hz |
| 空录音 | RMS 均值低且变化率趋零 | RMS < 0.01 |
| 能量异常 | RMS 变化率异常 | 变化率 < 0.05 |

```
⚠️  不正常的.wav    1513s  静音 / 截断 | >2s 静音 9 段 · 最长 30.6s (251s-)
⚠️  持续嘟声.wav     960s  纯音
```

### 模式 B — 样本锚定比对

拿一条正常录音做锚，比对待检文件的波形一致性。自动过滤与样本特征不匹配的文件（时长/采样率/能量差异过大）。

| 检测项 | 规则 | 阈值 |
|--------|------|------|
| 时长偏移 | 待检/参考 时长比 | < 0.7 或 > 1.3 |
| 包络不匹配 | 能量包络余弦相似度 | < 0.6 |
| DTW 漂移 | MFCC 对齐路径偏离 | > 0.3 |
| VAD 节奏偏移 | 静音段数量差异 | > 20% |

```
⚠️  number.wav    600s  过短 / 包络不匹配 / 波形不似 | 时长比 0.40, 包络 0.01, DTW 176.08
```

### 模式 C — ASR 内容检测

ASR 识别内容后与参考文本比对，检测吞字、多余内容、时间轴漂移。支持数字时间轴对齐——找到第一个相同数字作为锚点，逐个检测后续 >1s 的时间偏移。

| 检测项 | 规则 | 阈值 |
|--------|------|------|
| 吞字/缺字 | ASR 文本 diff | 匹配率 < 1.0 |
| 多余内容 | ASR 文本 diff | 匹配率 < 1.0 |
| 内容不匹配 | 整体文本相似度 | < 50% |
| 无语音 | ASR 返回空 | — |
| 时间轴漂移 | 数字时间轴逐个比对 | > 1.0s |

ASR 控制：候选 ≤10 全部分析，11~100 抽样 10 个，>100 抽样 100 个。>180s 长录音自动切片。

## 管线架构

```
L1 波形筛查 (0.04s/文件)
  ↓ 异常直接报，不进后续
L2 样本比对 (20s/文件)
  ↓ 异常直接报，不进 ASR
L3 ASR 确认 (15s/60s 录音)
```

## 快速开始

```bash
# 安装
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[full]"

# 交互模式（引导选择）
sipwav

# 命令行模式
sipwav task --dir ./录音/ --sample 正常.wav --silence 2
```

## 命令

| 命令 | 功能 |
|------|------|
| `sipwav` | 交互模式（选模式→输目录→自动跑） |
| `sipwav doctor` | 环境诊断 |
| `sipwav task --dir ./录音/` | 任务模式（断点续跑） |
| `sipwav info 异常.wav` | 查看单文件详情 |
| `sipwav view 异常.wav -d 60 --open` | 波形标记 SVG |
| `sipwav gen "验证码123456" --output ref.wav` | TTS 生成参考语音 |

## 参数

```bash
--sample ref.wav   # 参考样本（模式 B/C）
--silence 2        # 静音阈值（秒），默认 2.0
--no-asr           # 关闭 ASR（加速）
-p 1               # 仅 L1
-p 12              # L1 + L2
-p 123             # 全管线（默认）
--no-resume        # 不恢复旧任务
```

## 部署

### 方案 A：完整部署（本地 ASR + 云端回退）

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[full]"
```

需要 PyTorch + FunASR（约 2GB），首次加载模型 ~10s。

### 方案 B：轻量化部署（仅云端 ASR）⭐ 推荐本机使用

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[server]"
```

仅需 dashscope，**不安装 torch/funasr**，体积小、启动快。

```bash
# 配置阿里云百炼 API Key
echo "DASHSCOPE_API_KEY=sk-ws-..." > .env
```

使用时指定 `--asr-mode aliyun`：
```bash
sipwav scan --dir ./录音/ --asr --asr-mode aliyun
sipwav scan --dir ./录音/ --sample ref.wav --asr --asr-mode aliyun
```

环境检测会自动选择推荐模式（无 funasr 时自动切 aliyun）。

### 依赖说明

| 层级 | 包 | 说明 |
|------|-----|------|
| 核心 | numpy, scipy, webrtcvad, soundfile, setuptools<81 | 必装 |
| L2 | librosa | 样本比对 DTW |
| L3 本地 | torch, torchaudio, funasr, modelscope | 完整模式 |
| L3 云端 | dashscope, httpx | 轻量模式 |

## 故障排除

```bash
sipwav doctor   # 遇到问题先跑这个
```

**venv 未生效**：`which python` 显示 `aliased to /opt/homebrew/bin/python3` → `unalias python && source .venv/bin/activate`

**ModuleNotFoundError**：`rm -f /opt/homebrew/bin/sipwav && pip install -e .`

**No module named 'funasr'**：使用 `--asr-mode aliyun` 而不是默认的 auto/auto 模式。轻量部署不需要 funasr。

**venv 卡死（import 超时）**：重建 venv：`rm -ri .venv && python3 -m venv .venv && source .venv/bin/activate && pip install -e ".[server]"`

**彻底重来**：`deactivate && rm -ri .venv && python3 -m venv .venv && source .venv/bin/activate && pip install -e ".[full]"`
