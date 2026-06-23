"""CLI 入口 — sipcheck 命令行工具"""

import argparse
import json
import sys
import os
import time
import pathlib
import shutil

# 延迟导入重依赖（features/webrtcvad 等），doctor/env 不需要它们
scanner = feat = aligner = asr = report = shenv = TaskManager = None
ENV_INFO = None


def _lazy_imports():
    """按需导入重依赖，doctor/env 可跳过"""
    global scanner, feat, aligner, asr, report, shenv, TaskManager, ENV_INFO
    if scanner is not None:
        return True  # 已导入
    try:
        from . import scanner as _scanner
        from . import features as _feat
        from . import aligner as _aligner
        from . import asr_handler as _asr
        from . import report as _report
        from . import env as _shenv
        from .task import TaskManager as _TM
        scanner, feat, aligner, asr, report, shenv, TaskManager = (
            _scanner, _feat, _aligner, _asr, _report, _shenv, _TM
        )
        ENV_INFO = shenv.detect()
        return True
    except ImportError as e:
        print(f"⚠️  核心依赖缺失: {e}")
        print(f"   修复: pip install -e .")
        print()
        return False


# ─── 启动环境自检 ───

def _check_python_env():
    """启动时快速自检：Python 环境是否正确

    常见问题：
    1. pip install --break-system-packages 装到系统 Python，venv 里找不到模块
    2. /opt/homebrew/bin/sipcheck 残留脚本指向系统 Python
    3. venv 未激活，用的是系统 Python
    """
    exe = pathlib.Path(sys.executable)
    running_in_venv = hasattr(sys, 'real_prefix') or (
        hasattr(sys, 'base_prefix') and sys.base_prefix != sys.prefix
    )

    # 检查 1：脚本自身路径 vs 当前 Python
    script = pathlib.Path(sys.argv[0]).resolve() if sys.argv[0] else None
    if script and str(script) != '-c':
        script_dir = script.parent
        # 如果脚本在 /opt/homebrew/bin 但 Python 在 .venv → 冲突
        if 'opt/homebrew' in str(script_dir) and running_in_venv:
            print("⚠️  sipcheck 脚本指向系统 Python，但当前是 venv")
            print(f"   脚本: {script}")
            print(f"   Python: {exe}")
            print(f"   修复: rm {script} && pip install -e .")
            print()
            return False

    # 检查 2：包是否真的能导入
    try:
        import sipwav
        pkg_dir = pathlib.Path(sipwav.__file__).parent
        # 包路径应该在 site-packages 里
        if 'site-packages' not in str(pkg_dir):
            print(f"⚠️  sipcheck 包路径异常: {pkg_dir}")
    except ImportError:
        print("❌ 无法导入 sipcheck 包")
        if running_in_venv:
            print(f"   Python: {exe}")
            print(f"   修复: pip install -e .")
        else:
            print(f"   Python: {exe} (未在 venv 中)")
            print(f"   修复: source .venv/bin/activate && pip install -e .")
        print()
        return False

    # 检查 3：系统目录残留
    homebrew_sipcheck = pathlib.Path("/opt/homebrew/bin/sipcheck")
    if homebrew_sipcheck.exists() and running_in_venv:
        # 不报错，但 doctor 会提示
        pass

    return True


def _check_python_env_quiet():
    """安静模式自检 — 只在有严重问题时输出"""
    exe = pathlib.Path(sys.executable)
    running_in_venv = hasattr(sys, 'real_prefix') or (
        hasattr(sys, 'base_prefix') and sys.base_prefix != sys.prefix
    )
    script = pathlib.Path(sys.argv[0]).resolve() if sys.argv[0] else None

    # 严重问题：脚本指向系统但我们在 venv
    if script and 'opt/homebrew' in str(script.parent) and running_in_venv:
        print(f"⚠️  环境冲突: sipcheck 脚本在 /opt/homebrew/bin 但 Python 在 venv")
        print(f"   修复: rm {script} && pip install -e .")
        print()
        return

    # 严重问题：包导入失败
    try:
        import sipwav
    except ImportError:
        if running_in_venv:
            print(f"❌ sipcheck 未安装到当前 venv ({exe})")
            print(f"   修复: pip install -e .")
        else:
            print(f"❌ 未在 venv 中运行 ({exe})")
            print(f"   修复: source .venv/bin/activate && pip install -e .")
        print()


def cmd_doctor(args):
    """环境诊断 — 全面检查 Python 环境、依赖、残留脚本"""
    exe = pathlib.Path(sys.executable)
    running_in_venv = hasattr(sys, 'real_prefix') or (
        hasattr(sys, 'base_prefix') and sys.base_prefix != sys.prefix
    )
    pkg_dir = None
    try:
        import sipwav
        pkg_dir = pathlib.Path(sipwav.__file__).parent
    except ImportError:
        pass

    print("🔬 sipcheck 环境诊断")
    print("=" * 50)

    # 1. Python 环境
    print(f"\n📦 Python 环境")
    print(f"   可执行文件: {exe}")
    print(f"   版本: {sys.version.split()[0]}")
    print(f"   sys.prefix: {sys.prefix}")
    print(f"   sys.base_prefix: {sys.base_prefix}")
    print(f"   venv: {'✅ 是' if running_in_venv else '❌ 否 (建议使用 venv)'}")

    # 1b. Shell 配置检查（alias/PATH 导致 venv 失效）
    if not running_in_venv:
        import subprocess
        # 检查 python alias
        try:
            r = subprocess.run(
                ["zsh", "-ic", "alias python 2>/dev/null || true"],
                capture_output=True, text=True, timeout=5
            )
            alias_out = r.stdout.strip()
            if "alias python=" in alias_out or "python: aliased to" in alias_out:
                print(f"\n   ⚠️  Shell alias 冲突:")
                print(f"      {alias_out}")
                print(f"      alias 优先级高于 venv PATH，导致 venv 失效")
                print(f"      修复: unalias python   # 当前会话生效")
                print(f"      永久: 编辑 ~/.zshrc 移除 alias python=...")
        except Exception:
            pass
        # 检查 .venv 是否存在但未生效
        local_venv = pathlib.Path.cwd() / ".venv" / "bin" / "python"
        if local_venv.exists():
            print(f"\n   💡 当前目录有 .venv 但未生效:")
            print(f"      source .venv/bin/activate")
            print(f"      或直接用: {local_venv}")

    # 2. sipcheck 包
    print(f"\n📦 sipcheck 包")
    if pkg_dir:
        print(f"   路径: {pkg_dir}")
        in_site = 'site-packages' in str(pkg_dir)
        in_editable = '.egg-link' in str(pkg_dir) or 'sipwav-v' in str(pkg_dir) or 'SIP-wav' in str(pkg_dir)
        print(f"   安装方式: {'editable (-e)' if in_editable else 'regular'}")
        print(f"   位置正确: {'✅' if in_site or in_editable else '⚠️ 异常'}")
    else:
        print(f"   状态: ❌ 未安装")
        print(f"   修复: pip install -e .")

    # 3. 脚本路径检查
    print(f"\n📂 CLI 脚本")
    script = pathlib.Path(sys.argv[0]).resolve() if sys.argv[0] else None
    if script and str(script) != '-c':
        print(f"   路径: {script}")
        # 检查是否有多个 sipcheck
        which_results = shutil.which('sipcheck')
        if which_results:
            print(f"   which: {which_results}")

    # 检查系统目录残留
    stale_paths = [
        pathlib.Path("/opt/homebrew/bin/sipcheck"),
        pathlib.Path("/usr/local/bin/sipcheck"),
    ]
    stale_found = []
    for p in stale_paths:
        if p.exists():
            stale_found.append(p)

    if stale_found:
        print(f"\n⚠️  发现残留脚本 (可能导致环境冲突):")
        for p in stale_found:
            print(f"   {p}")
            if p.exists():
                try:
                    content = p.read_text()
                    # 找 shebang 行
                    for line in content.split('\n')[:3]:
                        if line.startswith('#!'):
                            print(f"     → {line.strip()}")
                except Exception:
                    pass
        if running_in_venv:
            print(f"\n   修复命令:")
            for p in stale_found:
                print(f"   rm {p}")
            print(f"   pip install -e .")

    # 4. 依赖检查
    print(f"\n📚 依赖检查")
    deps_core = ["numpy", "scipy", "webrtcvad", "soundfile"]
    deps_full = ["librosa", "torch", "funasr", "modelscope", "dashscope"]
    deps_server = ["dashscope"]

    def _check_dep(name):
        """检查依赖状态: (状态, 版本, 修复建议)"""
        try:
            mod = __import__(name)
            ver = getattr(mod, '__version__', '?')
            return "ok", ver, None
        except ImportError as e:
            err = str(e)
            # 已安装但导入失败（如 pkg_resources 缺失）
            if "pkg_resources" in err or "No module named" not in err:
                return "broken", None, f"导入失败: {err}。修复: pip install 'setuptools<81'"
            # 真的没装
            try:
                import importlib.metadata
                importlib.metadata.distribution(name)
                return "broken", None, f"已安装但导入失败。修复: pip install --force-reinstall {name}"
            except Exception:
                return "missing", None, None

    all_ok = True
    for dep in deps_core:
        status, ver, fix = _check_dep(dep)
        if status == "ok":
            print(f"   {dep}: ✅ {ver}")
        elif status == "broken":
            print(f"   {dep}: ⚠️  {fix}")
            all_ok = False
        else:
            print(f"   {dep}: ❌ 未安装 (必需)")
            all_ok = False

    print(f"\n   --- 可选 (full) ---")
    full_ok = 0
    for dep in deps_full:
        status, ver, fix = _check_dep(dep)
        if status == "ok":
            print(f"   {dep}: ✅ {ver}")
            full_ok += 1
        elif status == "broken":
            print(f"   {dep}: ⚠️  {fix}")
        else:
            print(f"   {dep}: ⬜ 未安装")

    # 5. 推荐操作
    print(f"\n💡 推荐操作")
    if not running_in_venv:
        print(f"   1. 创建并激活 venv:")
        print(f"      python3 -m venv .venv && source .venv/bin/activate")
        print(f"   2. 安装 sipcheck:")
        print(f"      pip install -e .")
        print(f"      # 或完整安装: pip install -e '.[full]'")
    elif not pkg_dir:
        print(f"   pip install -e .")
    elif stale_found and running_in_venv:
        print(f"   清除残留脚本: rm {' '.join(str(p) for p in stale_found)}")
        print(f"   重新安装: pip install -e .")
    elif full_ok < len(deps_full):
        print(f"   如需 ASR 能力: pip install -e '.[full]'")
    else:
        print(f"   ✅ 环境正常，无需修复")

    print()


def _load_dotenv():
    """加载 .env 文件到环境变量（如存在）"""
    import pathlib
    env_path = pathlib.Path(__file__).parent.parent / ".env"
    if env_path.exists():
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key, val = key.strip(), val.strip().strip("\"'")
                if not os.environ.get(key):
                    os.environ[key] = val


def _apply_env_defaults(args):
    """根据环境自动调整默认参数"""
    env = ENV_INFO
    # 如果启用了 ASR 但没指定模式，用环境推荐的
    if getattr(args, 'asr', False) and getattr(args, 'asr_mode', 'auto') == 'auto':
        args.asr_mode = env['recommended_asr_mode']
    # 如果没指定管线，用环境推荐的
    if not hasattr(args, 'phases') or not args.phases or args.phases == '123':
        args.phases = env['recommended_phases']
    return env


def _format_elapsed(s: float) -> str:
    """格式化耗时"""
    if s < 60:
        return f"{s:.1f}s"
    return f"{int(s//60)}m{int(s%60)}s"


def _parse_recording_filename(filename: str) -> dict:
    """解析录音文件名中的元数据（主叫/被叫/IP/时间等）

    常见格式：
      13800138000_10086_20240618_143022.wav
      13800138000-10086-20240618-143022-192.168.1.1.wav
      REC_13800138000_10086_20240618143022.wav
    """
    import re
    stem = pathlib.Path(filename).stem

    # 提取所有数字串
    parts = re.split(r'[-_\s]+', stem)

    result = {
        "caller": None,      # 主叫
        "callee": None,      # 被叫
        "ip": None,          # IP 地址
        "datetime": None,    # 日期时间
        "raw_parts": parts,
    }

    for p in parts:
        # IP 地址
        if re.match(r'\d+\.\d+\.\d+\.\d+', p):
            result["ip"] = p
        # 日期时间 (20240618 或 20240618143022)
        elif re.match(r'20\d{6,}', p):
            result["datetime"] = p
        # 手机号 (11位，1开头)
        elif re.match(r'1\d{10}$', p):
            if result["caller"] is None:
                result["caller"] = p
            else:
                result["callee"] = p
        # 座机号 (区号+号码，如 01012345678)
        elif re.match(r'0\d{9,11}$', p):
            if result["caller"] is None:
                result["caller"] = p
            else:
                result["callee"] = p
        # 短号/特服号 (如 10086, 95588, 400xxxx)
        elif re.match(r'(10\d{3,}|95\d{3}|400\d{7})', p):
            if result["caller"] is None:
                result["caller"] = p
            else:
                result["callee"] = p

    return result


def _group_files_by_metadata(files: list[str]) -> list[list[str]]:
    """按文件名元数据分组：同主叫+同时间段的文件放一起

    返回分组后的文件列表，每组内按时间排序
    """
    import pathlib

    groups = {}
    for f in files:
        meta = _parse_recording_filename(pathlib.Path(f).name)
        # 分组键：主叫号码 + 日期（去掉时间部分）
        caller = meta["caller"] or "unknown"
        dt = meta["datetime"][:8] if meta["datetime"] else "unknown"
        key = f"{caller}_{dt}"
        groups.setdefault(key, []).append(f)

    # 每组内按时间排序
    result = []
    for key in sorted(groups.keys()):
        group = sorted(groups[key], key=lambda f: _parse_recording_filename(pathlib.Path(f).name).get("datetime") or "")
        result.append(group)

    return result


def _auto_find_reference(directory: str) -> str | None:
    """自动查找目录中的参考样本（文件名含 '正常' 或 'ref'）"""
    import pathlib
    exact = []      # 完全匹配 "正常"/"ref"/"normal"
    partial = []    # 包含这些关键词
    for f in pathlib.Path(directory).glob("*.wav"):
        name = f.stem
        if name in ("正常", "ref", "normal"):
            exact.append(str(f))
        elif "正常" in name or "ref" in name or "normal" in name:
            partial.append(str(f))
    if exact:
        return exact[0]
    if partial:
        # 排除 "不正常" 等否定词
        clean = [p for p in partial if "不正常" not in p and "abnormal" not in p]
        return clean[0] if clean else partial[0]
    return None


def cmd_task(args):
    """任务模式 — 分阶段管线处理，各阶段独立进度"""
    if not _lazy_imports():
        return

    # 通知模式：直接走 cmd_notification
    if getattr(args, 'mode', 'quality') == 'notification':
        return cmd_notification(args)

    from .pipeline import Pipeline
    import numpy as np

    _apply_env_defaults(args)
    work_dir = args.work_dir or args.dir
    tm = TaskManager(work_dir)

    # 检查是否有可恢复的任务
    resume_task = tm.find_resume_task()
    if resume_task and args.resume is not False:
        task = resume_task
        print(f"🔄 恢复未完成任务 ({task['task_id']})")
        print(tm.format_progress(task))
    else:
        old = tm.load_task()
        if old:
            if old["status"] == "completed":
                print("📗 上次已完成，开始新的")
            elif args.resume is False:
                print("♻️  放弃旧任务，重新开始")
            else:
                print("⚠️  已有任务，使用 --resume 恢复")
                return
        tm.clear_task()
        # 自动查找参考样本：目录中有 "正常"/"ref" 命名的 WAV 文件
        if not args.sample:
            args.sample = _auto_find_reference(args.dir)
        task = tm.create_task(args.dir, args.sample, "anchor" if args.sample else "scan")

    # 加载参考样本
    ref_profile = ref_vad_segments = ref_asr_text = None
    ref_numbers = []
    enable_asr = getattr(args, 'asr', False)
    asr_mode = getattr(args, 'asr_mode', 'auto')
    codec = getattr(args, 'codec', 'auto')

    # 应用 --asr-model 参数
    asr_model_override = getattr(args, 'asr_model', None)
    if asr_model_override:
        from . import asr_aliyun
        asr_aliyun.ASR_MODEL = asr_model_override

    # 编码检测（auto 模式从参考样本或首个文件判断）
    if codec == 'auto' and task["config"]["sample"]:
        try:
            y_check, sr_check = feat.load_wav(task["config"]["sample"])
            codec_info = feat.detect_codec(y_check, sr_check)
            codec = codec_info["codec"]
            print(f"🎙 编码检测: {codec.upper()} (置信度 {codec_info['confidence']:.0%}, 静音RMS={codec_info['silence_rms']})")
        except Exception:
            codec = "g711"

    if task["config"]["sample"]:
        sample_path = task["config"]["sample"]
        if not os.path.exists(sample_path):
            print(f"  [X] 参考样本不存在: {sample_path}")
            return
        print(f"  [*] 参考样本: {os.path.basename(sample_path)}")
        y_ref, sr_ref = feat.load_wav(sample_path)
        ref_profile = aligner.extract_reference_profile(y_ref, sr_ref)
        ref_vad = feat.vad_segments(y_ref, sr_ref)
        ref_vad_segments = ref_vad.get("segments")
        # 显示参考样本完整参数
        print(f"      时长:   {ref_profile['duration_s']:.1f}s")
        print(f"      能量:   {ref_profile['rms_mean']:.4f}")
        print(f"      采样率: {sr_ref} Hz")
        print(f"      编码:   {codec.upper()}")
        # ASR 参考文本延迟到 L1+L2 完成后提取

    task["status"] = "running"
    tm.save_task(task)
    files = tm.get_pending_files(task)
    t_start = time.time()

    phases = getattr(args, 'phases', "123")
    silence_threshold = getattr(args, 'silence', 2.0)

    # ─── 分阶段管线 ───
    sample_path = task["config"].get("sample")
    pipe = Pipeline(files, ref_profile, ref_vad_segments, ref_asr_text,
                    enable_asr, asr_mode, phases=phases,
                    silence_threshold=silence_threshold,
                    ref_path=sample_path,
                    ref_numbers=ref_numbers if enable_asr else [],
                    interactive=getattr(args, 'interactive', False),
                    ref_sr=sr_ref)

    pipe.run_phase1()

    # 展示参考样本 L1 基准指标
    if pipe.ref_l1:
        rl = pipe.ref_l1
        re = rl.get("energy", {})
        rv = rl.get("vad", {})
        print(f"\n  参考样本基准:")
        print(f"    RMS均值: {re.get('rms_mean', 0):.4f}  标准差: {re.get('rms_std', 0):.4f}  变化率: {re.get('rms_variation', 0):.3f}")
        print(f"    语音占比: {rv.get('voiced_ratio', 0):.1%}  静音段: {len(rv.get('silence_gt_threshold', []))} 段")
        rt = rl.get("tone", {})
        if rt.get("is_pure_tone"):
            print(f"    主频: {rt.get('dominant_freqs', [])[0] if rt.get('dominant_freqs') else '?'}Hz (纯音)")

    if ref_profile is not None:
        pipe.run_phase2()

    # ─── 候选文件列表 + 基本参数 ───
    candidates = pipe.get_asr_candidates()

    if not candidates:
        # 无匹配文件，结束
        if enable_asr:
            print(f"\n  [i] L1+L2 筛选后无匹配文件，跳过 ASR")
    else:
        # 列出候选文件 + 基本参数
        print(f"\n  匹配文件 ({len(candidates)} 个):")
        print(f"  {'文件名':30s}  {'时长':>8s}  {'能量':>8s}  {'采样率':>8s}")
        print(f"  {'-'*30}  {'-'*8}  {'-'*8}  {'-'*8}")
        for fpath, dur in candidates:
            r = pipe.l1_results.get(fpath, {})
            y_f = r.get("y")
            sr_f = r.get("sr", 0)
            rms_f = float(np.sqrt(np.mean(y_f**2))) if y_f is not None else 0
            name = os.path.basename(fpath)[:30]
            print(f"  {name:30s}  {dur:>7.0f}s  {rms_f:>8.4f}  {sr_f:>7d}Hz")

        if enable_asr:
            # 预估 ASR 时间
            est = pipe.estimate_asr_time()
            print(f"\n  [ASR] 预估耗时: {est['est_time_str']} (总录音 {_format_elapsed(est['total_dur_s'])})")

            # 交互模式询问是否继续
            if getattr(args, 'interactive', False):
                confirm = _prompt("  继续 ASR 分析？[Y/n]: ").lower()
                if confirm in ("n", "no"):
                    print("  跳过 ASR")
                    enable_asr = False

            if enable_asr:
                # 提取参考样本 ASR 文本
                if ref_asr_text is None and sample_path:
                    print(f"\n  [ASR] 识别参考样本...")
                    if asr_mode == "aliyun":
                        from . import asr_aliyun
                        api_key = asr_aliyun._get_api_key()
                        ref_asr = asr_aliyun.transcribe(y_ref, sr_ref, api_key=api_key)
                    else:
                        ref_asr = asr.transcribe(y_ref, sr_ref)
                    ref_asr_text = ref_asr["text"]
                    ref_numbers = ref_asr.get("numbers", [])
                    pipe.ref_asr_text = ref_asr_text
                    pipe.ref_numbers = ref_numbers or []
                    if ref_asr_text:
                        excerpt = ref_asr_text[:80]
                        suffix = "..." if len(ref_asr_text) > 80 else ""
                        print(f"  [ASR] 参考内容节选: [{excerpt}{suffix}]")
                    else:
                        print(f"  [ASR] 参考样本无语音内容")

                # 执行 ASR 分析
                pipe.run_phase3()

    # ─── 汇总 ───
    elapsed_total = time.time() - t_start
    combined = pipe.get_combined_results()

    # 更新任务状态
    for entry in combined:
        fpath = entry["file"]
        if entry["verdict"] == "failed":
            tm.mark_failed(task, fpath, entry.get("error", ""), entry.get("elapsed_s", 0))
        else:
            tm.mark_done(task, fpath, entry["verdict"], entry["flags"], entry.get("elapsed_s", 0))

    # 报告
    results_path = os.path.join(work_dir, "sipcheck_report.json")
    report.save_json_report(combined, len(files), results_path)
    tm.complete_task(task, results_path)

    filtered_count = len(getattr(pipe, 'filtered_files', []))
    print(report.format_report(combined, len(files), elapsed_total, silence_threshold, results_path, filtered_count))


def cmd_notification(args):
    """模式 C — 内容匹配验证（头部匹配 + 送达度 + 吞字检测）"""
    if not _lazy_imports():
        return
    from .pipeline import Pipeline

    work_dir = args.work_dir or args.dir
    head_seconds = getattr(args, 'head_seconds', 5.0)

    # 查找参考样本
    sample_path = getattr(args, 'sample', None)
    if not sample_path:
        sample_path = _auto_find_reference(args.dir)
    if not sample_path:
        print("  ❌ 通知模式需要参考样本 (--sample 或目录中含 '正常'/'ref' 文件)")
        return

    if not os.path.exists(sample_path):
        print(f"  ❌ 参考样本不存在: {sample_path}")
        return

    # 查找录音文件
    files = scanner.find_wav_files(args.dir)
    if not files:
        print(f"  ⚠️ 在 {args.dir} 中未找到 WAV 文件")
        return

    # 排除参考样本自身
    files = [f for f in files if os.path.abspath(f) != os.path.abspath(sample_path)]
    if not files:
        print(f"  ⚠️ 除参考样本外没有其他 WAV 文件")
        return

    print(f"\n  📢 内容匹配验证")
    print(f"  参考样本: {os.path.basename(sample_path)}")
    print(f"  待检文件: {len(files)} 个")
    print(f"  头部匹配: {head_seconds}s")
    print()

    pipe = Pipeline(files, ref_path=sample_path)
    t0 = time.time()
    notif_result = pipe.run_notification_mode(head_seconds=head_seconds)
    elapsed = time.time() - t0

    # 输出报告
    json_path = getattr(args, 'output', None) or os.path.join(work_dir, "sipcheck_notification.json")
    report.save_notification_json_report(notif_result, json_path)
    print(report.format_notification_report(notif_result, elapsed, json_path))


def cmd_status(args):
    """查看任务状态"""
    if not _lazy_imports():
        return
    work_dir = args.dir
    tm = TaskManager(work_dir)
    task = tm.load_task()
    if not task:
        print("📭 当前目录没有活跃任务")
        return
    print(f"📊 任务状态: {task['status']}")
    print(f"  任务 ID: {task['task_id']}")
    print(f"  创建时间: {task['created_at']}")
    print(f"  目录: {task['config']['directory']}")
    if task["config"]["sample"]:
        print(f"  参考样本: {task['config']['sample']}")
    if task["status"] != "completed":
        print(tm.format_progress(task))
    else:
        s = task["stats"]
        print(f"  结果: 总计 {s['total']} | 异常 {s['abnormal']} | 失败 {s['failed']}")
        if task["output"].get("report_path"):
            print(f"  报告: {task['output']['report_path']}")


def cmd_scan(args):
    """简单扫描模式 — 不保留任务状态，走 Pipeline"""
    if not _lazy_imports():
        return

    # 通知模式：直接走 cmd_notification
    if getattr(args, 'mode', 'quality') == 'notification':
        return cmd_notification(args)

    _apply_env_defaults(args)
    from .pipeline import Pipeline

    files = scanner.find_wav_files(args.dir)
    if not files:
        print(f"⚠️  在 {args.dir} 中未找到 WAV 文件")
        return

    # 加载参考样本（如有）
    ref_profile = ref_vad_segments = ref_asr_text = None
    ref_numbers = []

    # 应用 --asr-model 参数
    asr_model_override = getattr(args, 'asr_model', None)
    if asr_model_override:
        from . import asr_aliyun
        asr_aliyun.ASR_MODEL = asr_model_override
    sr_ref = 8000
    enable_asr = getattr(args, 'asr', False)
    asr_mode = getattr(args, 'asr_mode', 'auto')

    if args.sample:
        print(f"📌 参考样本: {args.sample}")
        try:
            y_ref, sr_ref = feat.load_wav(args.sample)
            ref_profile = aligner.extract_reference_profile(y_ref, sr_ref)
            ref_vad = feat.vad_segments(y_ref, sr_ref)
            ref_vad_segments = ref_vad.get("segments")
            print(f"   时长: {ref_profile['duration_s']:.1f}s | 能量均值: {ref_profile['rms_mean']:.4f}")
            # ASR 参考文本延迟到 L1+L2 完成后提取
            print()
        except Exception as e:
            print(f"❌ 参考样本加载失败: {e}")
            return

    print(f"📁 待检: {len(files)} 个文件")
    phases = getattr(args, 'phases', '123')
    silence = getattr(args, 'silence', 2.0)

    pipe = Pipeline(files, ref_profile, ref_vad_segments, ref_asr_text,
                    enable_asr, asr_mode, phases=phases,
                    silence_threshold=silence,
                    ref_path=args.sample,
                    ref_numbers=ref_numbers,
                    ref_sr=sr_ref)

    t0 = time.time()
    pipe.run_phase1()
    if ref_profile is not None:
        pipe.run_phase2()

    if enable_asr:
        est = pipe.estimate_asr_time()
        if est["files"] == 0:
            print(f"\n  [i] L1+L2 筛选后无匹配文件，跳过 ASR")
        else:
            # 延迟提取参考样本 ASR 文本
            if ref_asr_text is None and args.sample:
                print(f"\n  [ASR] 识别参考样本文本...")
                ref_asr = asr.transcribe(y_ref, sr_ref)
                ref_asr_text = ref_asr["text"]
                ref_numbers = ref_asr.get("numbers", [])
                pipe.ref_asr_text = ref_asr_text
                pipe.ref_numbers = ref_numbers or []
                if ref_asr_text:
                    print(f"  [ASR] 参考: [{ref_asr_text[:60]}{'...' if len(ref_asr_text)>60 else ''}]")
                else:
                    print(f"  [ASR] 参考样本无语音内容")
            print(f"\n  [ASR] 候选: {est['files']} 文件 · "
                  f"总录音 {_format_elapsed(est['total_dur_s'])} · "
                  f"预估 {est['est_time_str']}")
            pipe.run_phase3()

    total_time = time.time() - t0
    combined = pipe.get_combined_results()

    filtered_count = len(getattr(pipe, 'filtered_files', []))
    print(report.format_report(combined, len(files), total_time, silence, filtered_count=filtered_count))

    if args.output:
        report.save_json_report(combined, len(files), args.output)
        print(f"\n💾 报告: {args.output}")


def cmd_info(args):
    """查看单个 WAV 文件的详细特征"""
    if not _lazy_imports():
        return
    y, sr = feat.load_wav(args.file)
    l1 = feat.layer1_fast_scan(y, sr)
    vad = feat.vad_segments(y, sr)

    if args.json:
        info = {
            "file": args.file,
            "duration": round(len(y) / sr, 2),
            "sr": sr,
            "l1": l1,
            "vad_overview": {
                "voiced_ratio": round(vad["voiced_ratio"], 4),
                "silence_count": len([s for s in vad["segments"] if s["type"] == "silence"]),
                "speech_count": len([s for s in vad["segments"] if s["type"] == "speech"]),
            },
        }
        print(json.dumps(info, ensure_ascii=False, indent=2, default=str))
        return

    print(f"📄 {args.file}")
    print(f"⏱ 时长: {len(y)/sr:.2f}s | 采样率: {sr} Hz")
    print(f"\n--- Layer 1: 快速筛查 ---")
    print(f"判决: {'⚠️ 异常' if l1['verdict']=='abnormal' else '✅ 正常'}")
    print(f"详情: {l1.get('details', '')}")

    e = l1["energy"]
    print(f"\n能量:")
    print(f"  RMS 均值: {e['rms_mean']:.5f} | 标准差: {e['rms_std']:.5f} | 变化率: {e['rms_variation']:.4f}")
    print(f"  峰值: {e['peak']:.5f} | 时长: {e['duration_s']:.1f}s")

    v = l1.get("vad", {})
    print(f"\nVAD: 语音占比 {v.get('voiced_ratio', 0):.1%}")
    for s in v.get("silence_gt_threshold", []):
        print(f"  ⚠️ 超长静音 {s['duration']}s @ {s['start']:.1f}s~{s['end']:.1f}s")

    t = l1.get("tone", {})
    if t:
        freqs = ", ".join([str(f) for f in t.get("dominant_freqs", [])])
        print(f"\n频谱: 主频 [{freqs}] Hz | 纯音: {'是' if t.get('is_pure_tone') else '否'}")
        if "peak_interval_mean" in t:
            print(f"  脉冲间隔: {t['peak_interval_mean']}s ± {t['peak_interval_std']}s")

    if l1.get("truncation", {}).get("is_truncated"):
        print(f"\n截断检测: ⚠️ {l1['truncation'].get('reason', '是')}")
    else:
        print(f"\n截断检测: 否")


def cmd_view(args):
    """生成波形标记 SVG — 可视化异常位置"""
    if not _lazy_imports():
        return
    import numpy as np

    y, sr = feat.load_wav(args.file)
    dur = len(y) / sr
    if args.duration and dur > args.duration:
        seg = y[:int(args.duration * sr)]
        dur = args.duration
    else:
        seg = y

    vad = feat.vad_segments(seg, sr)
    l1 = feat.layer1_fast_scan(seg, sr)
    silences = [s for s in vad["segments"] if s["type"] == "silence" and s["end"]-s["start"] > 1.0]

    W, H, mid, scale = 900, 320, 200, 80
    step = max(1, len(seg) // 700)
    wav = seg[::step]
    fl = int(sr * 0.03)
    frames = np.lib.stride_tricks.sliding_window_view(seg, fl)[::fl*4]
    rms = np.sqrt(np.mean(frames**2, axis=1))
    n_wav = len(wav)

    lines = []
    lines.append(f'<svg width="100%" viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg">')
    lines.append('<style>text{font-family:system-ui,sans-serif}</style>')
    lines.append('<rect width="100%" height="100%" fill="transparent"/>')

    for s in silences:
        x = s["start"] / dur * W
        w = (s["end"] - s["start"]) / dur * W
        lines.append(f'<rect x="{x:.1f}" y="0" width="{max(w,2):.1f}" height="{H}" fill="#e24b4a" opacity="0.10" rx="2"/>')
        lines.append(f'<text x="{x+3:.1f}" y="14" font-size="10" fill="#e24b4a" opacity="0.8">静音{s["end"]-s["start"]:.1f}s</text>')

    env_pts = " ".join(f"{i*4*fl/sr/dur*W:.1f},{240 - min(v*800, 140):.1f}" for i, v in enumerate(rms))
    lines.append(f'<polyline points="{env_pts}" fill="none" stroke="#639922" stroke-width="1" opacity="0.5"/>')

    pts = " ".join(f"{i/n_wav*W:.1f},{mid - wav[i]*scale:.1f}" for i in range(n_wav))
    lines.append(f'<polyline points="{pts}" fill="none" stroke="var(--primary)" stroke-width="1" opacity="0.75"/>')
    lines.append(f'<line x1="0" y1="{mid}" x2="{W}" y2="{mid}" stroke="var(--border)" stroke-width="0.5" stroke-dasharray="4 4"/>')

    for t in range(0, int(dur)+1, max(1, int(dur)//12*5)):
        x = t / dur * W
        lines.append(f'<line x1="{x:.1f}" y1="290" x2="{x:.1f}" y2="298" stroke="var(--border)" stroke-width="0.5"/>')
        lines.append(f'<text x="{x:.1f}" y="308" font-size="10" fill="var(--muted-foreground)" text-anchor="middle">{t}s</text>')

    lines.append('<text x="10" y="190" font-size="11" fill="var(--muted-foreground)">波形</text>')
    lines.append('<text x="10" y="60" font-size="11" fill="#639922" opacity="0.7">能量</text>')

    flags = list(set(l1.get("flags", [])))
    for i, f in enumerate(flags):
        lines.append(f'<rect x="{W-130}" y="{10+i*20}" width="8" height="8" rx="1" fill="#e24b4a"/>')
        lines.append(f'<text x="{W-118}" y="{18+i*20}" font-size="11" fill="var(--foreground)">{f}</text>')

    lines.append(f'<text x="{W/2}" y="318" font-size="11" fill="var(--muted-foreground)" text-anchor="middle">{dur:.0f}秒波形 | 静音段: {len(silences)} | {", ".join(flags)}</text>')
    lines.append("</svg>")

    svg_path = args.output or os.path.splitext(args.file)[0] + ".svg"
    with open(svg_path, "w") as f:
        f.write("\n".join(lines))
    print(f"✅ 波形标记图: {svg_path}")
    if args.open:
        import subprocess
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        subprocess.run([opener, svg_path])


def cmd_env(args):
    """查看当前环境检测结果"""
    from . import env as _env
    print(_env.summary())


def cmd_gen(args):
    """用 alma tts 生成参考语音"""
    if not _lazy_imports():
        return
    import subprocess

    out = args.output or os.path.join(os.getcwd(), "output", "ref_voice.wav")
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    voice = args.voice or "vivian"
    cmd = ["alma", "tts", args.text, "--voice", voice, "--output", out]
    print(f"🔊 生成参考语音...")
    print(f"  文本: {args.text}")
    print(f"  音色: {voice}")
    print(f"  输出: {out}")
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode == 0:
        print(f"✅ 生成完成")
        y, sr = feat.load_wav(out)
        print(f"  时长: {len(y)/sr:.1f}s | 采样率: {sr} Hz | 文件大小: {os.path.getsize(out)/1024:.0f}KB")
    else:
        print(f"❌ 生成失败: {r.stderr}")


def _resolve_sample(user_input: str, work_dir: str) -> str | None:
    """解析用户输入的样本路径，按优先级查找

    查找顺序：
    1. 完整路径（用户输入了绝对路径）
    2. 项目 sample/ 目录（sipcheck 自带测试样本）
    3. 工作目录（用户指定的录音目录）
    """
    import pathlib

    s = user_input.strip()
    if not s:
        return None

    # 1. 绝对路径
    p = pathlib.Path(os.path.expanduser(s))
    if p.is_absolute() and p.exists():
        return str(p)

    # 2. 项目 sample/ 目录
    pkg_sample = pathlib.Path(__file__).parent.parent / "sample"
    candidate = pkg_sample / s
    if candidate.exists():
        return str(candidate)

    # 3. 工作目录
    candidate = pathlib.Path(work_dir) / s
    if candidate.exists():
        return str(candidate)

    # 4. 文件名匹配（用户输入了 number.wav，目录里可能有 number.wav）
    name_lower = pathlib.Path(s).stem.lower()
    for search_dir in [pkg_sample, pathlib.Path(work_dir)]:
        if search_dir.exists():
            for f in search_dir.glob("*.wav"):
                if f.stem.lower() == name_lower:
                    return str(f)

    return None


def _pick_sample_interactive(work_dir: str) -> str | None:
    """交互式选择参考样本 — 列出候选，用户编号选择或输入路径/文件名"""
    import pathlib

    # 收集候选：项目 sample/ 目录 + 工作目录中含关键词的文件
    pkg_sample = pathlib.Path(__file__).parent.parent / "sample"
    candidates = []  # (显示名, 完整路径, 来源)

    # 1. 项目 sample/ 目录（排除 ._ 元数据文件）
    if pkg_sample.is_dir():
        for f in sorted(pkg_sample.glob("*.wav")):
            if not f.name.startswith("._"):
                candidates.append((f.name, str(f), "sample/"))

    # 2. 工作目录中含 "正常"/"ref"/"normal" 的文件
    for f in sorted(pathlib.Path(work_dir).glob("*.wav")):
        name_lower = f.stem.lower()
        if any(kw in name_lower for kw in ("正常", "ref", "normal")):
            if "不正常" not in name_lower and "abnormal" not in name_lower:
                candidates.append((f.name, str(f), "录音目录"))

    if not candidates:
        # 无候选 → 直接让用户输入
        s = _prompt("  参考样本路径: ")
        if not s:
            print("  [X] 未指定参考样本")
            return None
        resolved = _resolve_sample(s, work_dir)
        if not resolved:
            print(f"  [X] 文件不存在: {s}")
            return None
        print(f"  参考样本: {os.path.basename(resolved)}")
        return resolved

    # 列出候选
    print()
    print("  选择参考样本:")
    print()
    for i, (name, _, source) in enumerate(candidates, 1):
        print(f"    {i:2d}  {name}  ({source})")
    print()
    print("    或直接输入文件路径/文件名")
    print()

    raw = _prompt(f"  选择 [1-{len(candidates)} / 路径 / 回车=1]: ")

    # 空回车 → 默认第 1 个
    if not raw:
        raw = "1"

    # 数字选择
    if raw.isdigit():
        idx = int(raw)
        if 1 <= idx <= len(candidates):
            chosen = candidates[idx - 1]
            print(f"  参考样本: {chosen[0]}")
            return chosen[1]
        else:
            print(f"  [X] 超出范围: {idx}")
            return None

    # 文件路径/名称输入
    resolved = _resolve_sample(raw, work_dir)
    if not resolved:
        print(f"  [X] 文件不存在: {raw}")
        return None
    print(f"  参考样本: {os.path.basename(resolved)}")
    return resolved


def _prompt(msg: str) -> str:
    """带 flush 的 input，修复 CentOS 7 SSH 终端渲染问题"""
    sys.stdout.write(msg)
    sys.stdout.flush()
    return sys.stdin.readline().strip()


def cmd_interactive(args):
    """交互模式 — 最少交互，直接开干"""
    if not _lazy_imports():
        return
    try:
        import readline  # noqa: F401 — 修复远程终端 input 行为
    except ImportError:
        pass

    print()
    print("  +-----------------------------------------------+")
    print("  |         SIP-wav 语音异常检测工具               |")
    try:
        from importlib.metadata import version as _get_ver
        _ver = _get_ver("sipwav")
    except Exception:
        _ver = "dev"
    print(f"  |         v{_ver} · 三层管线 · 样本锚定           |")
    print("  +-----------------------------------------------+")
    print()
    print("    1  模式 A -- 波形快速筛查（静音/纯音/截断/能量）")
    print("    2  模式 A+B -- 波形筛查 + 样本锚定比对")
    print("    3  模式 D -- 内容匹配验证（头部匹配 + 送达度 + 吞字检测）")
    print("    4  模式 A+B+C -- 全管线（波形 + 锚定 + ASR 内容）")
    print()
    print("    r  恢复上次任务  d  环境诊断  q  退出")
    print()

    choice = _prompt("  选择 [1/2/3/4/r/d/q]: ").lower()
    if choice == "q":
        return
    elif choice == "d":
        cmd_doctor(args)
        return
    elif choice == "r":
        _interactive_resume()
        return
    elif choice not in ("1", "2", "3", "4"):
        print("  [X] 无效选择")
        return

    # 输入目录
    print()
    dir_path = _prompt("  目录路径: ")
    if not dir_path:
        print("  [X] 未输入目录")
        return
    dir_path = os.path.expanduser(dir_path)
    if not os.path.isdir(dir_path):
        print(f"  [X] 目录不存在: {dir_path}")
        return

    files = scanner.find_wav_files(dir_path)
    if not files:
        print(f"  [!] 没有 WAV 文件")
        return
    print(f"  [*] {len(files)} 个 WAV 文件")

    # 模式 D：内容匹配验证
    if choice == "3":
        sample_path = _pick_sample_interactive(dir_path)
        if sample_path is None:
            return
        import argparse as _ap
        notif_args = _ap.Namespace(
            dir=dir_path,
            sample=sample_path,
            head_seconds=5.0,
            work_dir=dir_path,
            output=None,
        )
        cmd_notification(notif_args)
        return

    # 模式 A+B / A+B+C：选择参考样本
    sample_path = None
    asr_mode = "auto"
    if choice in ("2", "4"):
        sample_path = _pick_sample_interactive(dir_path)
        if sample_path is None:
            return

    # 直接开跑（默认参数：静音 2s，ASR auto）
    silence = 2.0
    mode_names = {"1": "A", "2": "A+B", "4": "A+B+C"}
    print(f"\n  ▶ 模式 {mode_names[choice]} · {len(files)} 文件 · 静音 >{silence}s")
    if sample_path:
        print(f"    参考: {os.path.basename(sample_path)}")
    print()

    import argparse as _ap
    task_args = _ap.Namespace(
        dir=dir_path,
        sample=sample_path,
        mode="quality",
        head_seconds=5.0,
        asr=(choice == "4"),
        asr_mode=asr_mode,
        phases="123",
        codec="auto",
        silence=silence,
        work_dir=None,
        verbose=False,
        resume=False,
        interactive=True,
    )
    cmd_task(task_args)


def _interactive_resume():
    """交互式恢复上次任务"""
    import pathlib
    import json

    search_dirs = []
    cwd = pathlib.Path.cwd()
    cwd_task = cwd / ".sipcheck_task.json"
    if cwd_task.exists():
        search_dirs.append(str(cwd))

    if not search_dirs:
        print("  ⚠️  没有找到未完成的任务")
        return

    # 显示任务详情
    print()
    for i, d in enumerate(search_dirs, 1):
        task_file = pathlib.Path(d) / ".sipcheck_task.json"
        try:
            with open(task_file) as f:
                task = json.load(f)
            cfg = task.get("config", {})
            stats = task.get("stats", {})
            status = task.get("status", "unknown")
            sample = cfg.get("sample")
            sample_name = os.path.basename(sample) if sample else "无"
            total = stats.get("total", 0)
            done = stats.get("normal", 0) + stats.get("abnormal", 0) + stats.get("failed", 0)
            status_icon = {"completed": "✅", "running": "🔄", "pending": "⏳"}.get(status, "❓")
            print(f"    {i}  {status_icon} {d}")
            print(f"       样本: {sample_name} | 进度: {done}/{total} | 状态: {status}")
        except Exception:
            print(f"    {i}  {d}")
    print()

    choice = input(f"  选择 [1-{len(search_dirs)}]: ").strip()
    try:
        idx = int(choice) - 1
        if 0 <= idx < len(search_dirs):
            import argparse as _ap
            # 读取原任务配置，恢复完整参数
            task_file = pathlib.Path(search_dirs[idx]) / ".sipcheck_task.json"
            with open(task_file) as f:
                old_task = json.load(f)
            cfg = old_task.get("config", {})
            task_args = _ap.Namespace(
                dir=cfg.get("directory", search_dirs[idx]),
                sample=cfg.get("sample"),
                asr=cfg.get("asr", False),
                asr_mode=cfg.get("asr_mode", "auto"),
                phases=cfg.get("phases", "123"),
                codec="auto",
                silence=cfg.get("silence_threshold", 2.0),
                work_dir=search_dirs[idx],
                verbose=False,
                resume=True,
            )
            cmd_task(task_args)
        else:
            print("  ❌ 无效选择")
    except ValueError:
        print("  ❌ 无效输入")


def main():
    # 启动环境自检（安静模式，只在有问题时输出）
    _check_python_env_quiet()

    parser = argparse.ArgumentParser(
        prog="sipcheck",
        description="SIP-wav 语音异常检测工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
用法:
  sipcheck                    交互模式（引导选择）
  sipcheck task ./录音         命令行模式
  sipcheck doctor             环境诊断

示例:
  sipcheck task ./录音/ -s ref.wav
  sipcheck task ./录音/ -m n -s notify.wav
  sipcheck scan ./录音/ -A aliyun -v
  sipcheck info 异常.wav
  sipcheck view 异常.wav -d 60 --open
  sipcheck gen "您的验证码是123456" -o ref.wav
        """,
    )
    # 加载 .env 文件（如存在）
    _load_dotenv()
    sub = parser.add_subparsers(dest="cmd")

    p_task = sub.add_parser("task", help="任务模式 — 支持中断恢复")
    p_task.add_argument("dir", nargs="?", default=".", help="待检目录 (默认当前目录)")
    p_task.add_argument("--sample", "-s", help="参考样本 WAV (样本锚定模式)")
    p_task.add_argument("--mode", "-m", choices=["quality", "notification"], default="quality",
                        help="检测模式: quality=异常检测(默认), notification=内容匹配验证")
    p_task.add_argument("--head-seconds", type=float, default=5.0, metavar="SEC",
                        help="内容匹配头部匹配秒数 (默认 5s)")
    p_task.add_argument("--asr", action=argparse.BooleanOptionalAction, default=True,
                        help="启用 ASR 内容分析 (默认开，--no-asr 关闭)")
    p_task.add_argument("--asr-mode", "-A", choices=["local", "mlx", "aliyun", "auto"], default="auto",
                        help="ASR 模式: local/aliyun/auto (默认 auto)")
    p_task.add_argument("--asr-model", default=None,
                        help="云端 ASR 模型: qwen3-asr-flash-filetrans (默认) / paraformer-8k-v2 / fun-asr")
    p_task.add_argument("--phases", "-p", default="123",
                        help="指定管线阶段: 1=L1, 2=L2, 3=L3, 可组合如 12, 23 (默认 123)")
    p_task.add_argument("--codec", choices=["g711", "g729", "auto"], default="auto",
                        help="SIP 编码: g711/g729, auto=自动检测 (默认 auto)")
    p_task.add_argument("--silence", type=float, default=2.0, metavar="SEC",
                        help="静音检测阈值（秒），超过此值才报异常 (默认 2.0)")
    p_task.add_argument("--work-dir", "-w", help="工作目录 (默认同 dir)")
    p_task.add_argument("--verbose", "-v", action="store_true", help="显示正常结果")
    p_task.add_argument("--resume", action=argparse.BooleanOptionalAction, default=True, help="自动恢复未完成任务 (默认 true)")

    p_status = sub.add_parser("status", help="查看任务状态")
    p_status.add_argument("--dir", required=True, help="工作目录")

    p_scan = sub.add_parser("scan", help="简单批量扫描（无任务状态）")
    p_scan.add_argument("dir", nargs="?", default=".", help="待检目录 (默认当前目录)")
    p_scan.add_argument("--sample", "-s", help="参考样本 WAV")
    p_scan.add_argument("--mode", "-m", choices=["quality", "notification"], default="quality",
                        help="检测模式: quality=异常检测(默认), notification=内容匹配验证")
    p_scan.add_argument("--head-seconds", type=float, default=5.0, metavar="SEC",
                        help="内容匹配头部匹配秒数 (默认 5s)")
    p_scan.add_argument("--asr", action="store_true", help="启用 ASR 内容分析")
    p_scan.add_argument("--asr-mode", "-A", choices=["local", "mlx", "aliyun", "auto"], default="auto",
                        help="ASR 模式: local/aliyun/auto (默认 auto)")
    p_scan.add_argument("--asr-model", default=None,
                        help="云端 ASR 模型: qwen3-asr-flash-filetrans (默认) / paraformer-8k-v2 / fun-asr")
    p_scan.add_argument("--silence", type=float, default=2.0, metavar="SEC",
                        help="静音检测阈值（秒） (默认 2.0)")
    p_scan.add_argument("--phases", "-p", default="123",
                        help="指定管线阶段 (默认 123)")
    p_scan.add_argument("--output", "-o", help="输出 JSON 报告路径")
    p_scan.add_argument("--verbose", "-v", action="store_true", help="显示正常结果")

    p_info = sub.add_parser("info", help="查看单个文件详情")
    p_info.add_argument("file", help="WAV 文件路径")
    p_info.add_argument("--json", "-j", action="store_true", help="输出 JSON")

    p_env = sub.add_parser("env", help="查看环境检测和推荐配置")

    p_view = sub.add_parser("view", help="生成波形标记 SVG 可视化")
    p_view.add_argument("file", help="WAV 文件路径")
    p_view.add_argument("--duration", "-d", type=int, default=60, help="截取前 N 秒 (默认 60)")
    p_view.add_argument("--output", "-o", help="SVG 输出路径 (默认同文件名)")
    p_view.add_argument("--open", action="store_true", help="自动用浏览器打开")

    p_gen = sub.add_parser("gen", help="用 TTS 生成参考语音")
    p_gen.add_argument("text", help="语音文本内容")
    p_gen.add_argument("--output", "-o", default=None, help="输出路径 (默认 output/ref_voice.wav)")
    p_gen.add_argument("--voice", default="vivian", help="TTS 音色 (默认 vivian)")

    sub.add_parser("doctor", help="环境诊断 — 排查 Python/依赖/安装问题")

    args = parser.parse_args()
    if args.cmd == "task":
        cmd_task(args)
    elif args.cmd == "status":
        cmd_status(args)
    elif args.cmd == "scan":
        cmd_scan(args)
    elif args.cmd == "info":
        cmd_info(args)
    elif args.cmd == "view":
        cmd_view(args)
    elif args.cmd == "env":
        cmd_env(args)
    elif args.cmd == "gen":
        cmd_gen(args)
    elif args.cmd == "doctor":
        cmd_doctor(args)
    else:
        # 无子命令 → 交互模式
        cmd_interactive(args)


if __name__ == "__main__":
    main()
