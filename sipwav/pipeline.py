"""管线引擎 — 分阶段批量处理"""

import time
import os


def _format_elapsed(s: float) -> str:
    if s < 60:
        return f"{s:.1f}s"
    return f"{int(s//60)}m{int(s%60)}s"


class Pipeline:
    """分阶段管线引擎

    用法:
        pipe = Pipeline(files, ref_profile, ..., phases="123")
        pipe.run_phase1()  # L1 全部
        pipe.run_phase2()  # L2 筛选后
        pipe.run_phase3()  # L3 ASR
        results = pipe.get_combined_results()
    """

    def __init__(self, files: list[str], ref_profile=None, ref_vad_segments=None,
                 ref_asr_text=None, enable_asr=False, asr_mode="auto",
                 phases: str = "123", silence_threshold: float = 2.0,
                 ref_path: str | None = None, ref_numbers: list[dict] | None = None,
                 max_asr_files: int = 100, asr_sample_size: int = 10,
                 interactive: bool = False, ref_sr: int = 8000):
        from . import features as feat
        # 参考样本单独处理（跑 L1 拿基准指标，但不进 L2/L3 检测）
        self.ref_path = ref_path
        self.files = [f for f in files if f != ref_path]
        self.total = len(self.files)
        self.ref_profile = ref_profile
        self._ref_sr = ref_sr
        self.ref_vad_segments = ref_vad_segments
        self.ref_asr_text = ref_asr_text
        self.ref_numbers = ref_numbers or []
        self.ref_l1 = None  # 参考样本的 L1 分析结果（基准）
        self.enable_asr = enable_asr
        self.asr_mode = asr_mode
        self.phases = phases
        self.silence_threshold = silence_threshold
        self.max_asr_files = max_asr_files
        self.asr_sample_size = asr_sample_size
        self.interactive = interactive
        self.feat = feat

        self.l1_results: dict[str, dict] = {}
        self.l2_results: dict[str, dict] = {}
        self.l3_results: dict[str, dict] = {}
        self.errors: dict[str, str] = {}
        self.phase_times = {}
        self.filtered_files: list[str] = []  # 被样本预过滤的文件

    def _prefilter_by_sample(self, files: list[str]) -> list[str]:
        """样本预过滤：只读 WAV 文件头（时长/采样率），不加载波形"""
        import wave as _wave
        if not self.ref_profile or not files:
            return files

        ref_dur = self.ref_profile.get("duration_s", 0)
        ref_sr = 8000  # 参考样本采样率（从 ref_profile 推断）

        # 从 l1_results 或 ref_profile 推断采样率
        if hasattr(self, '_ref_sr'):
            ref_sr = self._ref_sr

        kept = []
        for fpath in files:
            if fpath == self.ref_path:
                continue
            try:
                # 只读文件头：帧数、采样率 → 时长
                with _wave.open(str(fpath), "rb") as wf:
                    sr = wf.getframerate()
                    n_frames = wf.getnframes()
                    dur = n_frames / sr if sr > 0 else 0

                # 时长比检查
                if ref_dur > 0:
                    ratio = dur / ref_dur
                    if ratio < 0.5 or ratio > 2.0:
                        self.filtered_files.append(fpath)
                        continue

                # 采样率检查
                if abs(sr - ref_sr) > 1000:
                    self.filtered_files.append(fpath)
                    continue

                kept.append(fpath)
            except Exception:
                kept.append(fpath)  # 读头失败的保留，让 L1 处理

        if self.filtered_files:
            print(f"  📋 样本预过滤: 排除 {len(self.filtered_files)} 个特征不匹配文件")

        return kept

    # ─── Phase 1: L1 快速筛查 ─────────────────

    def run_phase1(self) -> dict[str, dict]:
        if "1" not in self.phases:
            print("\n  跳过 Phase 1")
            return {}
        t0 = time.time()
        import numpy as np

        # 参考样本先跑 L1，拿到基准指标
        if self.ref_path and os.path.exists(self.ref_path):
            try:
                y_ref, sr_ref = self.feat.load_wav(self.ref_path)
                self.ref_l1 = self.feat.layer1_fast_scan(y_ref, sr_ref, self.silence_threshold)
                self._ref_y = y_ref
                self._ref_sr_loaded = sr_ref
            except Exception:
                self.ref_l1 = None

        # 样本预过滤：有参考样本时，排除特征不匹配的文件
        if self.ref_profile:
            self.files = self._prefilter_by_sample(self.files)
            self.total = len(self.files)

        results = {}

        phase_label = "L1 波形筛查" if "2" in self.phases or "3" in self.phases else "扫描"
        print(f"  {phase_label} ({self.total} 文件)...", end="", flush=True)

        for fpath in self.files:
            try:
                y, sr = self.feat.load_wav(fpath)
                l1 = self.feat.layer1_fast_scan(y, sr, self.silence_threshold)
                results[fpath] = {"y": y, "sr": sr, "l1": l1}
            except Exception as e:
                self.errors[fpath] = str(e)

        elapsed = time.time() - t0
        abnormal = sum(1 for r in results.values() if r["l1"]["verdict"] == "abnormal")
        print(f" {abnormal} 异常 / {_format_elapsed(elapsed)}")

        self.l1_results = results
        self.phase_times["l1"] = elapsed
        return results

    # ─── Phase 2: L2 样本比对 ─────────────────

    def run_phase2(self) -> dict[str, dict]:
        if "2" not in self.phases or self.ref_profile is None:
            return {}
        t0 = time.time()
        from . import aligner
        results = {}

        # 只比对 L1 正常且不在过滤列表中的文件
        candidates = [
            fpath for fpath, r in self.l1_results.items()
            if r["l1"]["verdict"] == "normal" and fpath not in self.errors and fpath not in self.filtered_files
        ]

        # 时长预筛选：差异太大的直接跳过 DTW（省算力）
        ref_dur = self.ref_profile.get("duration_s", 0)
        fast_skip = []
        real_candidates = []
        for fpath in candidates:
            r = self.l1_results[fpath]
            test_dur = len(r["y"]) / r["sr"]
            ratio = test_dur / ref_dur if ref_dur > 0 else 1.0
            if ratio < 0.3 or ratio > 3.0:
                # 时长差异过大，直接标记，不做 DTW
                flags = ["too_short"] if ratio < 0.7 else ["too_long"]
                results[fpath] = {
                    "verdict": "abnormal",
                    "flags": flags,
                    "duration": {"test_s": round(test_dur, 1), "ref_s": round(ref_dur, 1), "ratio": round(ratio, 2)},
                }
                fast_skip.append(fpath)
            else:
                real_candidates.append(fpath)

        if fast_skip:
            print(f"  L2 样本比对 ({len(real_candidates)} 文件, {len(fast_skip)} 时长差异跳过)...", end="", flush=True)
        else:
            print(f"  L2 样本比对 ({len(candidates)} 文件)...", end="", flush=True)

        for fpath in real_candidates:
            try:
                r = self.l1_results[fpath]
                test_vad = self.feat.vad_segments(r["y"], r["sr"])
                l2 = aligner.layer2_sample_compare(
                    r["y"], r["sr"], self.ref_profile,
                    test_vad_segments=test_vad.get("segments"),
                    ref_vad_segments=self.ref_vad_segments,
                )
                results[fpath] = l2
            except Exception as e:
                self.errors[fpath] = str(e)

        elapsed = time.time() - t0
        flagged = sum(1 for r in results.values() if r.get("flags"))
        print(f" {flagged} 异常 / {_format_elapsed(elapsed)}")

        self.l2_results = results
        self.phase_times["l2"] = elapsed
        return results

    # ─── Phase 3: L3 ASR ─────────────────

    def _slice_at_silence(self, y, sr, max_duration: float = 180.0) -> list:
        """在静音点切片，每段不超过 max_duration 秒

        返回: [(start_sample, end_sample), ...]
        """
        dur = len(y) / sr
        if dur <= max_duration:
            return [(0, len(y))]

        vad = self.feat.vad_segments(y, sr)
        silences = [s for s in vad["segments"] if s["type"] == "silence" and s["end"] - s["start"] > 1.0]

        slices = []
        slice_start = 0
        for sil in silences:
            sil_sample = int(sil["start"] * sr)
            current_dur = (sil_sample - slice_start) / sr
            if current_dur >= max_duration * 0.5:
                slices.append((slice_start, sil_sample))
                slice_start = int(sil["end"] * sr)

        # 最后一段
        if slice_start < len(y):
            slices.append((slice_start, len(y)))

        return slices if len(slices) > 1 else [(0, len(y))]

    def run_phase3(self) -> dict[str, dict]:
        if "3" not in self.phases or not self.enable_asr:
            return {}
        t0 = time.time()
        from . import asr_handler as asr

        # 复用 get_asr_candidates 的过滤逻辑（时长+静音，L1 其他标记不排除）
        candidate_pairs = self.get_asr_candidates()
        candidates = [f for f, _ in candidate_pairs]

        # 抽样控制
        max_asr = getattr(self, 'max_asr_files', 100)
        sample_size = getattr(self, 'asr_sample_size', 10)
        original_count = len(candidates)

        if len(candidates) > max_asr:
            # 超过上限：抽样 max_asr 个
            candidates_with_dur = []
            for f in candidates:
                dur = len(self.l1_results[f]["y"]) / self.l1_results[f]["sr"]
                candidates_with_dur.append((f, dur))
            candidates_with_dur.sort(key=lambda x: x[1])
            step = len(candidates_with_dur) / max_asr
            candidates = [candidates_with_dur[int(i * step)][0] for i in range(max_asr)]
            print(f"  L3 ASR 分析: 抽样 {max_asr}/{original_count} 文件...", end="", flush=True)
        elif len(candidates) > sample_size:
            # 按文件大小排序，均匀抽样
            candidates_with_dur = []
            for f in candidates:
                dur = len(self.l1_results[f]["y"]) / self.l1_results[f]["sr"]
                candidates_with_dur.append((f, dur))
            candidates_with_dur.sort(key=lambda x: x[1])
            step = len(candidates_with_dur) / sample_size
            candidates = [candidates_with_dur[int(i * step)][0] for i in range(sample_size)]
            print(f"  L3 ASR 分析: 抽样 {len(candidates)}/{original_count} 文件...", end="", flush=True)
        else:
            print(f"  L3 ASR 分析: {len(candidates)} 文件...", end="", flush=True)

        # 逐文件 ASR
        found_anomaly = False
        for fpath in candidates:
            if found_anomaly and self.interactive:
                break  # 交互模式下发现异常后暂停

            try:
                r = self.l1_results[fpath]
                y, sr = r["y"], r["sr"]
                is_long = len(y) / sr > 180

                if is_long:
                    # 长文件：切片 ASR
                    l3 = self._asr_sliced(y, sr, asr)
                else:
                    # 短文件：直接 ASR
                    l3 = self._asr_single(y, sr, asr)

                self.l3_results[fpath] = l3

                # 发现异常 → 交互模式下询问是否继续
                if l3.get("verdict") == "abnormal":
                    basename = os.path.basename(fpath)
                    flags = ", ".join(l3.get("flags", []))
                    print(f"\n  ⚠️  {basename}: {flags}")
                    found_anomaly = True

            except Exception as e:
                self.errors[fpath] = str(e)

        elapsed = time.time() - t0
        print(f" 完成 / {_format_elapsed(elapsed)}")
        self.phase_times["l3"] = elapsed
        return self.l3_results

    def _asr_single(self, y, sr, asr) -> dict:
        """单文件 ASR"""
        if self.asr_mode == "local":
            return asr.layer3_asr_check(y, sr, self.ref_asr_text, use_fallback=False,
                                         ref_numbers=self.ref_numbers)
        elif self.asr_mode == "aliyun":
            from . import asr_aliyun
            api_key = asr_aliyun._get_api_key()
            ali_result = asr_aliyun.transcribe(y, sr, api_key=api_key)
            l3 = {"transcribed": ali_result}
            if self.ref_asr_text and ali_result.get("has_content"):
                l3["diff"] = asr.text_diff(self.ref_asr_text, ali_result["text"])
                flags = []
                if l3["diff"]["has_missing"]: flags.append("missing_words")
                if l3["diff"]["has_extra"]: flags.append("extra_words")
                l3["verdict"] = "abnormal" if flags else "normal"
                l3["flags"] = flags
            elif self.ref_asr_text:
                l3["verdict"] = "abnormal"
                l3["flags"] = ["no_speech"]
            else:
                l3["verdict"] = "normal"
                l3["flags"] = []
            return l3
        else:
            return asr.layer3_asr_check(y, sr, self.ref_asr_text, use_fallback=True,
                                         ref_numbers=self.ref_numbers)

    def _asr_sliced(self, y, sr, asr) -> dict:
        """长文件切片 ASR"""
        slices = self._slice_at_silence(y, sr, max_duration=180)
        all_text, all_numbers, slice_results = [], [], []

        for i, (start, end) in enumerate(slices):
            y_slice = y[start:end]
            time_offset = start / sr
            if self.asr_mode == "local":
                t = asr.transcribe(y_slice, sr, use_fallback=False)
            else:
                t = asr.transcribe(y_slice, sr, use_fallback=True)

            if t.get("has_content"):
                all_text.append(t["text"])
                for n in t.get("numbers", []):
                    all_numbers.append({**n, "start": round(n["start"] + time_offset, 2), "end": round(n["end"] + time_offset, 2)})
            slice_results.append({"slice": i + 1, "start_s": round(time_offset, 1), "end_s": round(time_offset + len(y_slice) / sr, 1), "text": t.get("text", "")[:50]})

        merged_text = "".join(all_text)
        l3 = {"transcribed": {"text": merged_text, "has_content": bool(merged_text.strip()), "numbers": all_numbers}, "slices": slice_results}

        if self.ref_asr_text and merged_text.strip():
            l3["diff"] = asr.text_diff(self.ref_asr_text, merged_text)
            flags = []
            if l3["diff"]["has_missing"]: flags.append("missing_words")
            if l3["diff"]["has_extra"]: flags.append("extra_words")
            if l3["diff"]["similarity"] < 0.5: flags.append("content_mismatch")
            l3["verdict"] = "abnormal" if flags else "normal"
            l3["flags"] = flags
            if self.ref_numbers and all_numbers:
                from .numbers import detect_drift
                drift = detect_drift(self.ref_numbers, all_numbers, threshold=1.0)
                l3["drift"] = drift
                if drift.get("total_drift", 0) > 0:
                    l3["flags"].append("drift_detected")
                    l3["verdict"] = "abnormal"
        elif self.ref_asr_text:
            l3["verdict"] = "abnormal"
            l3["flags"] = ["no_speech"]
        else:
            l3["verdict"] = "normal"
            l3["flags"] = []
        return l3

    # ─── ASR 预估 ─────────────────

    def get_asr_candidates(self) -> list[tuple[str, float]]:
        """返回 Phase 3 的候选文件列表 (path, duration_s)

        过滤条件（只看时长和静音，L1 其他标记不排除）：
        1. 时长比：与参考样本差异 <0.5 或 >2.0 → 排除
        2. 静音段：静音占比过高 → 排除
        3. L2 时长/包络不匹配 → 排除
        4. 出错/预过滤的文件 → 排除
        """
        ref_dur = self.ref_profile.get("duration_s", 0) if self.ref_profile else 0
        candidates = []
        for fpath, r in self.l1_results.items():
            if fpath in self.errors or fpath in self.filtered_files:
                continue
            dur = len(r["y"]) / r["sr"]
            # 条件1: 时长比（有参考样本时）
            if ref_dur > 0:
                ratio = dur / ref_dur
                if ratio < 0.5 or ratio > 2.0:
                    continue
            # 条件2: 静音占比过高（有参考样本时检查 L2 flags）
            if fpath in self.l2_results:
                l2 = self.l2_results[fpath]
                # L2 时长不匹配或包络不匹配 → 排除
                if any(f in l2.get("flags", []) for f in ("too_short", "too_long", "envelope_mismatch")):
                    continue
            # L1 的截断/纯音/能量异常不排除，只在报告里展示
            candidates.append((fpath, dur))
        return candidates

    def estimate_asr_time(self) -> dict:
        """预估 ASR 分析耗时

        返回:
            {"files": int, "total_dur_s": float, "est_time_s": float, "est_time_str": "..."}
        """
        candidates = self.get_asr_candidates()
        if not candidates:
            return {"files": 0, "total_dur_s": 0, "est_time_s": 0, "est_time_str": "0s"}

        total_dur = sum(dur for _, dur in candidates)
        # ASR 估算：本地 ~0.3x 实时，云端 ~0.5x 实时 + 网络开销
        if self.asr_mode == "aliyun":
            est_time = total_dur * 0.5 + len(candidates) * 2  # 每文件 2s 网络开销
        else:
            est_time = total_dur * 0.3

        return {
            "files": len(candidates),
            "total_dur_s": round(total_dur, 1),
            "est_time_s": round(est_time, 0),
            "est_time_str": _format_elapsed(est_time),
        }

    # ─── 结果汇总 ─────────────────

    def get_combined_results(self) -> list[dict]:
        """合并各阶段结果，输出统一格式"""
        results = []
        for fpath in self.files:
            basename = os.path.basename(fpath)

            if fpath in self.errors:
                results.append({
                    "file": fpath,
                    "verdict": "failed",
                    "flags": [],
                    "error": self.errors[fpath],
                })
                continue

            r = self.l1_results.get(fpath)
            if not r:
                continue

            l1 = r["l1"]
            l2 = self.l2_results.get(fpath)
            l3 = self.l3_results.get(fpath)

            # 合并 flags
            flags = list(l1.get("flags", []))
            if l2:
                flags += l2.get("flags", [])
            if l3:
                flags += l3.get("flags", [])

            verdict = "abnormal" if flags else "normal"

            entry = {
                "file": fpath,
                "duration_s": round(len(r["y"]) / r["sr"], 1) if "y" in r else None,
                "l1": l1,
                "l2": l2,
                "l3": l3,
                "verdict": verdict,
                "flags": list(dict.fromkeys(flags)),  # 去重保序
            }
            results.append(entry)

        return results
