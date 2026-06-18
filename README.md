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

### 模式 B — 样本锚定比对

拿一条正常录音做锚，比对待检文件的波形一致性。自动过滤与样本特征不匹配的文件（时长/采样率/能量差异过大）。

| 检测项 | 规则 | 阈值 |
|--------|------|------|
| 时长偏移 | 待检/参考 时长比 | < 0.7 或 > 1.3 |
| 包络不匹配 | 能量包络余弦相似度 | < 0.6 |
| DTW 漂移 | MFCC 对齐路径偏离 | > 0.3 |
| VAD 节奏偏移 | 静音段数量差异 | > 20% |

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
L1 波形筛查 (numpy + webrtcvad, ~0.04s/文件)
  ↓ 异常直接报，不进后续
L2 样本比对 (librosa DTW, ~20s/文件)
  ↓ 异常直接报，不进 ASR
L3 ASR 确认 (funasr 本地 / 阿里云, ~15s/60s 录音)
```

## 快速开始

### 安装

```bash
git clone https://github.com/scomper/SIP-wav.git
cd SIP-wav
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[full]"
```

### 基本使用

```bash
# 交互模式（引导选择）
sipwav

# 命令行：L1 波形检测
sipwav task --dir ./录音/

# 命令行：全管线（L1 + L2 + L3）
sipwav task --dir ./录音/ --sample 参考.wav --silence 2
```

## 命令一览

| 命令 | 功能 |
|------|------|
| `sipwav` | 交互模式（选模式 → 输目录 → 自动跑） |
| `sipwav doctor` | 环境诊断 |
| `sipwav task --dir ./录音/` | 任务模式（支持断点续跑） |
| `sipwav scan --dir ./录音/` | 简单批量扫描 |
| `sipwav info 文件.wav` | 查看单文件详情 |
| `sipwav view 文件.wav -d 60 --open` | 波形标记 SVG 可视化 |
| `sipwav gen "文本内容" --output ref.wav` | TTS 生成参考语音 |

### 常用参数

| 参数 | 说明 |
|------|------|
| `--sample ref.wav` | 参考样本（模式 B/C） |
| `--silence 2` | 静音阈值（秒），默认 2.0 |
| `--asr` | 启用 ASR 内容分析 |
| `--asr-mode aliyun` | ASR 模式：`local` / `aliyun` / `auto` |
| `-p 1` | 仅 L1 波形检测 |
| `-p 12` | L1 + L2 样本比对 |
| `-p 123` | 全管线（默认） |
| `--no-resume` | 不恢复旧任务 |

## 部署

### 方案 A：完整部署（本地 ASR + 云端回退）

```bash
pip install -e ".[full]"
```

需要 PyTorch + FunASR（约 2GB），首次加载模型 ~10s。ASR 默认本地推理，无结果时自动回退阿里云。

### 方案 B：轻量化部署（仅云端 ASR）⭐ 推荐

```bash
pip install -e ".[server]"
```

仅需 dashscope + librosa，**不安装 torch/funasr**，体积小、启动快。

使用时指定 `--asr-mode aliyun`：
```bash
sipwav scan --dir ./录音/ --asr --asr-mode aliyun
sipwav scan --dir ./录音/ --sample ref.wav --asr --asr-mode aliyun
```

> 环境检测会自动选择推荐模式（无 funasr 时自动切 aliyun）。

### 阿里云百炼 API Key 配置

轻量化部署需要阿里云百炼 ASR 的 API Key：

1. 登录 [阿里云百炼控制台](https://bailian.console.aliyun.com/)
2. 左侧菜单 → **API Key 管理** → **创建 API Key**
3. 复制生成的 Key（格式 `sk-ws-...`）
4. 在项目根目录创建 `.env` 文件：

```bash
echo "DASHSCOPE_API_KEY=sk-ws-你的Key" > .env
```

> **定价参考**：Paraformer 模型 ¥0.003/秒（约 ¥0.18/分钟），新用户有免费额度。

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

| 问题 | 解决 |
|------|------|
| venv 未生效 | `unalias python && source .venv/bin/activate` |
| ModuleNotFoundError | `pip install -e .` 重装 |
| No module named 'funasr' | 用 `--asr-mode aliyun`，轻量部署不需要 funasr |
| venv 卡死（import 超时） | `rm -ri .venv && python3 -m venv .venv && source .venv/bin/activate && pip install -e ".[server]"` |
| 彻底重来 | `rm -ri .venv && python3 -m venv .venv && source .venv/bin/activate && pip install -e ".[full]"` |

## License

[MIT](LICENSE)
