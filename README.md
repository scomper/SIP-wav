# SIP-wav

SIP 语音异常检测工具。通过波形分析和 ASR 内容比对，快速筛选通话录音中的质量问题——静音、截断、纯音、内容漂移、时间轴偏移。

三层管线逐层过滤，越早发现问题越省算力。

## 检测能力

四种模式，逐步深入：

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

### 模式 C — 内容匹配验证

语音通知场景下，录音时长不固定（用户在不同时间点挂断）。使用 `--mode notification` 启用**头部匹配 + 滑动窗口搜索**双层检测，识别正常送达、部分送达、吞字（录音开头丢失）等情况：

```bash
sipcheck task --dir ./records/ --sample notify.wav --mode notification
sipcheck task --dir ./records/ --sample notify.wav --mode notification --head-seconds 10
```

**检测流程：**
1. 提取参考样本和录音的前 N 秒 MFCC 特征（头部匹配）
2. 相似度 ≥ 0.9 → 匹配成功，逐窗口计算送达比例
3. 相似度 < 0.9 → 进入滑动窗口搜索：在参考样本中逐段滑动，定位录音内容出现的位置
4. 找到位置 → 判定为**吞字**（录音开头丢失），报告截断起始点
5. 未找到 → 判定为未匹配

**输出示例：**
```
文件名                时长    相似度   送达度   挂断点   状态
1380001_0618.wav      65s     1.00     100%     600s    ✅ 已送达
1380002_0618.wav      18s     0.85      25%      15s    ⚠️ 部分送达
1380003_0618.wav      35s     -         0%       -     ⚠️ 吞字@200s
1380004_0618.wav      12s     0.12       0%       -     ❌ 未匹配
```

**状态说明：**
| 状态 | 含义 |
|------|------|
| ✅ 已送达 | 通知完整播放（≥95%） |
| ⚠️ 部分送达 | 通知播放了一部分，用户提前挂断 |
| ⚠️ 吞字@Xs | 录音开头丢失（线路异常），内容从参考样本第 X 秒处开始匹配 |
| ❌ 未匹配 | 录音内容与参考通知无关 |
| ❌ 无语音 | 未检测到语音内容 |

### 模式 A+B+C — 全管线

组合三层检测，逐层过滤：波形筛查 → 样本锚定 → 内容匹配 → ASR 内容分析。越早发现问题越省算力。

| 层 | 检测项 | 规则 | 阈值 |
|----|--------|------|------|
| A | 静音/截断/纯音/能量 | 波形特征 | 见模式 A |
| B | 时长/包络/DTW/VAD 节奏 | 样本锚定 | 见模式 B |
| C | 头部匹配/送达度/吞字 | 内容匹配 | 见模式 C |
| C+ | ASR 文本 diff | 内容比对 | 匹配率 < 1.0 |
| C+ | 时间轴漂移 | 数字锚点逐个比对 | > 1.0s |
| C+ | 无语音 | ASR 返回空 | — |

ASR 控制：候选 ≤10 全部分析，11~100 抽样 10 个，>100 抽样 100 个。>180s 长录音自动切片。

## 管线架构

```
模式 A：波形筛查 (numpy + webrtcvad, ~0.04s/文件)
  ↓ 异常直接报，不进后续
模式 B：样本比对 (librosa DTW, ~20s/文件)
  ↓ 异常直接报，不进内容匹配
模式 C：内容匹配 (头部匹配 + 送达度 + 吞字检测)
  ↓ 匹配后可选 ASR
模式 A+B+C：全管线 (波形 + 锚定 + 内容匹配 + ASR 内容分析)
```

前三项可独立使用，第四项组合全部。

## 快速开始

### 安装

```bash
git clone https://github.com/scomper/SIP-wav.git
cd SIP-wav                              # ⚠️ 必须在项目根目录，不要 cd 进 sipwav/ 子目录
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[full]"
```

> **⚠️ 常见错误**：项目根目录下有个 `sipwav/` 子目录（Python 包），`pip install` 必须在根目录执行。如果你已经在 `sipwav/` 子目录里，先 `cd ..` 回到根目录。

### 基本使用

```bash
# Interactive mode
sipcheck

# Command line: L1 waveform screening
sipcheck task --dir ./records/

# Command line: full pipeline (L1 + L2 + L3)
sipcheck task --dir ./records/ --sample ref.wav --silence 2
```

## 命令一览

| Command | Description |
|---------|-------------|
| `sipcheck` | 交互模式（选择模式 → 输入目录 → 自动运行） |
| `sipcheck doctor` | 环境诊断 |
| `sipcheck task --dir ./records/` | 任务模式（支持断点续扫） |
| `sipcheck scan --dir ./records/` | 简单批量扫描 |
| `sipcheck info file.wav` | 查看单文件详情 |
| `sipcheck view file.wav -d 60 --open` | 波形 SVG 可视化 |
| `sipcheck gen "Your verification code is 123456" --output ref.wav` | TTS 生成参考语音 |
| `sipcheck task --dir ./records/ --sample ref.wav --mode notification` | 内容匹配验证（头部匹配 + 送达度） |
| `sipcheck task --dir ./records/ --sample ref.wav --mode notification --head-seconds 10` | 内容匹配验证，头部匹配 10 秒 |

### 参数

| Parameter | Description |
|-----------|-------------|
| `--mode quality` | 质量检测模式（默认） |
| `--mode notification` | 内容匹配验证（头部匹配 + 送达度 + 吞字检测） |
| `--sample ref.wav` | 参考样本（模式 B/C） |
| `--head-seconds 5` | 内容匹配头部匹配时长（默认 5 秒） |
| `--silence 2` | 静音阈值（秒），默认 2.0 |
| `--asr` | 启用 ASR 内容分析 |
| `--asr-mode aliyun` | ASR 模式：`local` / `aliyun` / `auto` |
| `--asr-model paraformer-8k-v2` | 云端 ASR 模型：`qwen3-asr-flash-filetrans`（默认）/ `paraformer-8k-v2` / `fun-asr` |
| `-p 1` | 仅 L1 波形筛查 |
| `-p 12` | L1 + L2 样本对比 |
| `-p 123` | 完整流水线（默认） |
| `--no-resume` | 不恢复上次任务 |

## Deployment

### 方式 A：完整部署（本地 ASR + 云端兜底）

```bash
pip install -e ".[full]"
```

需要 PyTorch + FunASR（约 2GB），首次模型加载约 10 秒。默认本地 ASR，失败时自动回退阿里云。

### 方式 B：轻量化部署（仅云端 ASR）⭐ 推荐

```bash
pip install -e ".[server]"
```

仅需 dashscope + librosa，**无需 torch/funasr**，体积小，启动快。

配合 `--asr-mode aliyun` 使用：
```bash
sipcheck scan --dir ./records/ --asr --asr-mode aliyun
sipcheck scan --dir ./records/ --sample ref.wav --asr --asr-mode aliyun
```

### ASR Model Selection

Default model is `qwen3-asr-flash-filetrans` (best accuracy, supports any sample rate).

Switch model via CLI:
```bash
# Use Qwen3-ASR (default, best accuracy)
sipcheck scan --dir ./records/ --asr --asr-model qwen3-asr-flash-filetrans

# Use Paraformer (supports hotwords, 8kHz optimized)
sipcheck scan --dir ./records/ --asr --asr-model paraformer-8k-v2

# Use Fun-ASR (industrial grade, supports hotwords)
sipcheck scan --dir ./records/ --asr --asr-model fun-asr
```

Or via environment variable:
```bash
export SIPWAV_ASR_MODEL=paraformer-8k-v2
sipcheck scan --dir ./records/ --asr
```

| Model | Accuracy | Hotwords | Sample Rate | Speed |
|-------|----------|----------|-------------|-------|
| `qwen3-asr-flash-filetrans` | Best | No | Any | ~17s/10min |
| `paraformer-8k-v2` | Good | Yes | 8kHz | ~16s/10min |
| `fun-asr` | Good | Yes | Any | ~15s/10min |

> 环境检测会自动选择推荐模式（无 funasr 时自动切 aliyun）。

### ASR Models

| Model | ID | Best For | Hotword | 8kHz |
|-------|-----|----------|---------|------|
| **Qwen3-ASR** ⭐ | `qwen3-asr-flash-filetrans` | General, noisy audio, mixed zh/en | ❌ | ✅ |
| **Paraformer** | `paraformer-8k-v2` | Telephone recordings, hotword support | ✅ | ✅ |
| **Fun-ASR** | `fun-asr` | Industrial, multi-language | ✅ | ✅ |

Switch model via CLI:
```bash
# Use Paraformer (with hotword support)
sipcheck scan --dir ./records/ --asr-model paraformer-8k-v2

# Use Qwen3 (default, best accuracy)
sipcheck scan --dir ./records/ --asr-model qwen3-asr-flash-filetrans

# Or via environment variable
export SIPWAV_ASR_MODEL=paraformer-8k-v2
sipcheck scan --dir ./records/
```

### 阿里云百炼 API Key 配置

轻量化部署需要阿里云百炼 ASR 的 API Key：

1. 登录 [阿里云百炼控制台](https://bailian.console.aliyun.com/)
2. 左侧菜单 → **API Key 管理** → **创建 API Key**
3. 复制生成的 Key（格式 `sk-ws-...`）
4. 配置方式（二选一）：

```bash
# 方式 A：环境变量（推荐）
export DASHSCOPE_API_KEY=sk-ws-你的Key

# 方式 B：配置文件（持久化）
mkdir -p ~/.config/sipwav
echo "sk-ws-你的Key" > ~/.config/sipwav/api-key
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
sipcheck doctor   # 遇到问题先跑这个
```

| 问题 | 解决 |
|------|------|
| `does not appear to be a Python project` | 当前在 `sipwav/` 子目录，`cd ..` 回到项目根目录再执行 |
| `command not found: sipcheck` | 未安装或 venv 未激活，`source .venv/bin/activate && pip install -e .` |
| venv 未生效 | `unalias python && source .venv/bin/activate` |
| ModuleNotFoundError | `pip install -e .` 重装 |
| No module named 'funasr' | 用 `--asr-mode aliyun`，轻量部署不需要 funasr |
| No module named 'librosa' | 轻量部署：`pip install -e ".[server]"`，或全量：`pip install -e ".[full]"` |
| venv 卡死（import 超时） | `rm -ri .venv && python3 -m venv .venv && source .venv/bin/activate && pip install -e ".[server]"` |
| 彻底重来 | `rm -ri .venv && python3 -m venv .venv && source .venv/bin/activate && pip install -e ".[full]"` |

### CentOS 7 部署

CentOS 7 自带 Python 2.7 + OpenSSL 1.0.2 + GCC 4.8 + GLIBC 2.17，项目需要 Python 3.10+。
直接装会遇到多个兼容性问题，以下是实测验证过的步骤。

```bash
# 1. 安装编译依赖
yum install -y gcc gcc-c++ make zlib-devel bzip2 bzip2-devel \
  readline-devel sqlite sqlite-devel openssl-devel libffi-devel \
  xz-devel libsndfile git

# 2. 编译 OpenSSL 1.1.1（CentOS 7 自带 1.0.2，Python 3.10+ 要求 1.1.1+）
curl -sL https://www.openssl.org/source/openssl-1.1.1w.tar.gz -o /tmp/openssl.tar.gz
cd /tmp && tar xzf openssl.tar.gz && cd openssl-1.1.1w
./config --prefix=/usr/local/ssl --openssldir=/usr/local/ssl shared
make -j$(nproc) && make install
echo '/usr/local/ssl/lib' > /etc/ld.so.conf.d/openssl-1.1.conf && ldconfig

# 3. 编译 Python 3.12（必须带 --with-openssl 指向新版 OpenSSL）
curl -sL https://www.python.org/ftp/python/3.12.13/Python-3.12.13.tgz -o /tmp/Python.tgz
cd /tmp && tar xzf Python.tgz && cd Python-3.12.13
export LDFLAGS="-L/usr/local/ssl/lib"
export CPPFLAGS="-I/usr/local/ssl/include"
./configure --prefix=/usr/local --with-openssl=/usr/local/ssl
make -j$(nproc) && make altinstall
# 验证: /usr/local/bin/python3.12 -c "import ssl; print(ssl.OPENSSL_VERSION)"

# 4. 克隆项目 + 安装
git clone https://github.com/scomper/SIP-wav.git /opt/sipwav
cd /opt/sipwav
/usr/local/bin/python3.12 -m venv .venv && source .venv/bin/activate
pip install 'numpy>=1.24,<2.0'   # 先锁定 numpy 版本（2.x 需要 GCC≥9.3）
pip install -e ".[server]"        # 轻量（云端 ASR）
```

> **⚠️ CentOS 7 常见坑：**
> - **OpenSSL 太旧**：不编译 1.1.1 的话，Python 的 `ssl` 模块不会编进去，pip 无法联网
> - **GCC 太旧**：numpy 2.x / scipy 新版需要 GCC≥9.3，锁定 `numpy<2.0` 用预编译 wheel
> - **GLIBC 太旧**：Miniconda/Anaconda 的最新版要求 GLIBC≥2.28，CentOS 7 只有 2.17，不可用
> - **GitHub 连不上**：如果服务器无法访问 github.com，本地打包后 scp 上传
>
> CentOS 8 / Stream / AlmaLinux 自带 Python 3.6，同样需要编译 3.10+。
> Ubuntu 22.04+ 自带 Python 3.10+，可直接用。

## License

[MIT](LICENSE)
