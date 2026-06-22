"""报告输出 — 按模式分表 + JSON 详细报告"""

import json
import os


_FLAG_ZH = {
    "pure_tone": "纯音",
    "long_silence": "静音",
    "likely_no_voice": "无声",
    "truncated": "截断",
    "abnormal_energy": "能量异常",
    "too_short": "过短",
    "too_long": "过长",
    "envelope_mismatch": "包络不匹配",
    "high_drift": "时间漂移",
    "high_dtw_cost": "波形不似",
    "missing_words": "吞字",
    "extra_words": "多余",
    "content_mismatch": "内容不匹配",
    "no_speech": "无语音",
    "drift_detected": "时间漂移",
}

_MODE_NAMES = {
    "A": "模式 A — 波形异常检测",
    "B": "模式 B — 样本锚定比对",
    "C": "模式 C — ASR 内容检测",
}


def _flag_desc(flags: list[str]) -> str:
    """flags → 简短中文描述"""
    unique = list(dict.fromkeys(flags))
    return " / ".join(_FLAG_ZH.get(f, f) for f in unique)


def _silence_detail(entry: dict, threshold: float) -> str | None:
    """静音统计描述"""
    sil = entry.get("l1", {}).get("vad", {}).get("silence_gt_threshold", [])
    if not sil:
        return None
    longest = max(sil, key=lambda s: s["duration"])
    return f">{threshold:.0f}s 静音 {len(sil)} 段 · 最长 {longest['duration']:.1f}s ({longest['start']:.0f}s-)"


def format_report(results: list[dict], total: int, time_s: float,
                  silence_threshold: float = 2.0,
                  json_path: str | None = None,
                  filtered_count: int = 0) -> str:
    """终端简洁报告 — 按模式 A/B/C 分表"""
    lines = []

    abnormal = [r for r in results if r.get("verdict") == "abnormal"]
    normal_cnt = total - len(abnormal) - sum(1 for r in results if r.get("verdict") == "failed")
    fail_cnt = sum(1 for r in results if r.get("verdict") == "failed")

    if not abnormal and fail_cnt == 0:
        lines.append(f"\n  ✅ 全部正常 ({total} 文件, {time_s:.1f}s)\n")
        return "\n".join(lines)

    # 汇总行（含过滤信息）
    parts = [f"{len(abnormal)} 异常", f"{normal_cnt} 正常", f"{total} 总计"]
    if filtered_count > 0:
        parts.append(f"{filtered_count} 不匹配")
    parts.append(f"{time_s:.1f}s")
    lines.append(f"\n  ⚠️  {' · '.join(parts)}\n")

    # ─── 模式 A — L1 波形检测 ───
    l1_abnormal = [r for r in results if (r.get("l1") or {}).get("verdict") == "abnormal"]
    if l1_abnormal:
        lines.append(f"  {'━' * 68}")
        lines.append(f"  模式 A — 波形异常检测 (>{silence_threshold:.0f}s 静音 / 纯音 / 截断 / 能量)")
        lines.append(f"  {'━' * 68}")
        for r in l1_abnormal:
            basename = os.path.basename(r.get("file", ""))
            dur = r.get("duration_s")
            dur_str = f"{dur:.0f}s" if dur else "?"
            l1_flags = r.get("l1", {}).get("flags", [])
            desc = _flag_desc(l1_flags)
            extra = _silence_detail(r, silence_threshold)
            if extra:
                desc += f" | {extra}"
            lines.append(f"  ⚠️  {basename[:28]:28s}  {dur_str:>6s}  {desc}")
        lines.append("")

    # ─── 模式 B — L2 样本比对 ───
    l2_abnormal = [r for r in results if (r.get("l2") or {}).get("flags")]
    if l2_abnormal:
        lines.append(f"  {'━' * 68}")
        lines.append(f"  模式 B — 样本锚定比对 (时长 / 包络 / DTW / VAD)")
        lines.append(f"  {'━' * 68}")
        for r in l2_abnormal:
            basename = os.path.basename(r.get("file", ""))
            dur = r.get("duration_s")
            dur_str = f"{dur:.0f}s" if dur else "?"
            l2 = r["l2"]
            flags = l2.get("flags", [])
            desc = _flag_desc(flags)
            # 补充关键数值
            extras = []
            d = l2.get("duration", {})
            if d.get("ratio") and ("too_short" in flags or "too_long" in flags):
                extras.append(f"时长比 {d['ratio']:.2f}")
            env = l2.get("envelope", {})
            if "envelope_mismatch" in flags and env.get("cosine_similarity") is not None:
                extras.append(f"包络 {env['cosine_similarity']:.2f}")
            dtw = l2.get("dtw", {})
            if ("high_dtw_cost" in flags or "high_drift" in flags) and dtw.get("dtw_cost") is not None:
                extras.append(f"DTW代价 {dtw['dtw_cost']:.2f}")
            if extras:
                desc += " | " + ", ".join(extras)
            lines.append(f"  ⚠️  {basename[:28]:28s}  {dur_str:>6s}  {desc}")
        lines.append("")

    # ─── 模式 C — L3 ASR 内容 ───
    l3_files = [r for r in results if r.get("l3")]
    if l3_files:
        lines.append(f"  {'━' * 68}")
        lines.append(f"  模式 C — ASR 内容检测")
        lines.append(f"  {'━' * 68}")
        for r in l3_files:
            basename = os.path.basename(r.get("file", ""))
            dur = r.get("duration_s")
            dur_str = f"{dur:.0f}s" if dur else "?"
            l3 = r["l3"]
            flags = l3.get("flags", [])
            transcribed = l3.get("transcribed", {})
            asr_text = transcribed.get("text", "")

            if flags:
                desc = _flag_desc(flags)
                diff = l3.get("diff", {})
                if diff.get("similarity") is not None:
                    desc += f" | 相似度 {diff['similarity']:.2f}"
                if diff.get("missing"):
                    desc += f" | 缺: {diff['missing'][:30]}"
                if diff.get("extra"):
                    desc += f" | 多: {diff['extra'][:30]}"
                drift = l3.get("drift", {})
                if drift.get("total_drift", 0) > 0:
                    desc += f" | 漂移 {drift['total_drift']} 处"
                lines.append(f"  ⚠️  {basename[:28]:28s}  {dur_str:>6s}  {desc}")
            else:
                diff = l3.get("diff", {})
                sim = diff.get("similarity", 0)
                lines.append(f"  ✅  {basename[:28]:28s}  {dur_str:>6s}  相似度 {sim:.2f}")

            # 展示 ASR 识别内容节选
            if asr_text:
                excerpt = asr_text[:100]
                suffix = "..." if len(asr_text) > 100 else ""
                lines.append(f"       ASR: [{excerpt}{suffix}]")
        lines.append("")

    # ─── 失败文件 ───
    if fail_cnt:
        lines.append(f"  ❌ 失败 {fail_cnt} 文件:")
        for r in results:
            if r.get("verdict") == "failed":
                basename = os.path.basename(r.get("file", ""))
                lines.append(f"     {basename}: {r.get('error', '未知错误')[:50]}")
        lines.append("")

    # ─── 正常文件 ───
    normal_files = [r for r in results if r.get("verdict") == "normal"]
    if normal_files:
        names = [os.path.basename(r["file"]) for r in normal_files]
        lines.append(f"  ✅ 正常: {', '.join(names)}")

    if json_path:
        lines.append(f"\n  📄 JSON → {json_path}")
    lines.append("")

    return "\n".join(lines)


def save_json_report(results: list[dict], total: int, output_path: str):
    """保存详细 JSON 报告（包含完整时间轴和 ASR 数据）"""
    abnormal = [r for r in results if r.get("verdict") == "abnormal"]
    clean_results = []
    for r in results:
        entry = {k: v for k, v in r.items() if k not in ("y", "sr")}
        clean_results.append(entry)

    report = {
        "total": total,
        "normal": total - len(abnormal) - sum(1 for r in results if r.get("verdict") == "failed"),
        "abnormal": len(abnormal),
        "summary": {
            "abnormal_types": list(set(
                f for r in abnormal for f in r.get("flags", [])
            )),
        },
        "results": clean_results,
    }
    with open(output_path, "w") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)


# ─── 通知送达报告 ────────────────────────────────────────────────

_STATUS_ICON = {
    "delivered":       "✅ 已送达",
    "partial":         "⚠️ 部分送达",
    "truncated_start": "⚠️ 吞字",
    "no_match":        "❌ 未匹配",
    "no_voice":        "❌ 无语音",
    "error":           "❌ 错误",
}


def format_notification_report(notif_result: dict, elapsed_s: float = 0,
                               json_path: str | None = None) -> str:
    """格式化通知送达报告（终端输出）

    Args:
        notif_result: run_notification_mode() 的返回值
        elapsed_s: 总耗时
        json_path: JSON 报告路径（可选）
    """
    lines = []
    ref_info = notif_result.get("ref_info", {})
    results = notif_result.get("results", [])
    summary = notif_result.get("summary", {})

    if notif_result.get("error"):
        lines.append(f"\n  ❌ {notif_result['error']}\n")
        return "\n".join(lines)

    ref_name = os.path.basename(ref_info.get("path", "?"))
    ref_notif_dur = ref_info.get("notification_duration_s", 0)

    lines.append(f"\n  {'━' * 68}")
    lines.append(f"  语音通知送达报告")
    lines.append(f"  {'━' * 68}")
    lines.append(f"  参考样本: {ref_name} (通知内容 {ref_notif_dur:.0f}s)")
    lines.append("")

    # 表头
    lines.append(f"  {'文件名':30s}  {'时长':>6s}  {'相似度':>6s}  {'送达度':>6s}  {'挂断点':>7s}  {'状态'}")
    lines.append(f"  {'-'*30}  {'-'*6}  {'-'*6}  {'-'*6}  {'-'*7}  {'-'*12}")

    for r in results:
        basename = os.path.basename(r.get("file", ""))[:30]
        dur = r.get("duration_s", 0)
        dur_str = f"{dur:.0f}s" if dur else "?"

        sim = r.get("head_similarity", 0)
        sim_str = f"{sim:.2f}" if r.get("head_match") else "-"

        delivery = r.get("delivery")
        if delivery:
            ratio = delivery["delivery_ratio"]
            ratio_str = f"{ratio:.0%}"
            hangup = delivery["hangup_s"]
            hangup_str = f"{hangup:.0f}s"
        else:
            ratio_str = "-"
            hangup_str = "-"

        status = r.get("status", "error")
        status_icon = _STATUS_ICON.get(status, "❓ 未知")

        # truncated_start 追加吞字位置
        extra = ""
        if status == "truncated_start" and r.get("match_position_s") is not None:
            extra = f" (吞字@{r['match_position_s']:.0f}s)"

        lines.append(
            f"  {basename:30s}  {dur_str:>6s}  {sim_str:>6s}  {ratio_str:>6s}  {hangup_str:>7s}  {status_icon}{extra}"
        )

    lines.append("")

    # 汇总
    d = summary.get("delivered", 0)
    p = summary.get("partial", 0)
    n = summary.get("no_match", 0)
    ts = summary.get("truncated_start", 0)
    err = summary.get("errors", 0)
    total = summary.get("total", 0)

    parts = [f"已送达 {d}"]
    if p > 0:
        parts.append(f"部分送达 {p}")
    if ts > 0:
        parts.append(f"吞字 {ts}")
    if n > 0:
        parts.append(f"未匹配 {n}")
    if err > 0:
        parts.append(f"错误 {err}")
    parts.append(f"共 {total}")

    if elapsed_s > 0:
        parts.append(f"{elapsed_s:.1f}s")

    icon = "✅" if n == 0 and err == 0 and p == 0 else "⚠️"
    lines.append(f"  {icon} {' · '.join(parts)}")

    if json_path:
        lines.append(f"  📄 JSON → {json_path}")
    lines.append("")

    return "\n".join(lines)


def save_notification_json_report(notif_result: dict, output_path: str):
    """保存通知送达 JSON 报告"""
    # 去掉 numpy 数组等不可序列化的数据
    clean = {k: v for k, v in notif_result.items() if k != "ref_info"}
    if "ref_info" in notif_result:
        clean["ref_info"] = {k: v for k, v in notif_result["ref_info"].items()}

    clean_results = []
    for r in notif_result.get("results", []):
        entry = {k: v for k, v in r.items() if k not in ("y", "sr")}
        clean_results.append(entry)
    clean["results"] = clean_results

    with open(output_path, "w") as f:
        json.dump(clean, f, ensure_ascii=False, indent=2, default=str)
