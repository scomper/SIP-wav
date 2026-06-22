"""中文数字 → 阿拉伯数字 转换器"""

# 基础映射
_DIGITS = {
    "零": 0, "一": 1, "二": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
    "两": 2,
}
_UNITS = {"十": 10, "百": 100, "千": 1000, "万": 10000, "亿": 100000000}


def chinese_to_number(text: str) -> int:
    """中文数字字符串 → int

    支持:
        零一二三...九 (0-9)
        十一、十二...十九 (11-19)
        二十、三十...九十 (20-90)
        二十一...九十九 (21-99)
        一百、一百零一... (100+)
        一千二百三十四 = 1234
        一万两千 = 12000

    用法:
        >>> chinese_to_number("十四")
        14
        >>> chinese_to_number("一百二十三")
        123
    """
    if not text:
        return 0

    # 纯数字: "一二三" = 123 (逐位)
    if all(c in _DIGITS for c in text):
        result = 0
        for c in text:
            result = result * 10 + _DIGITS[c]
        return result

    # 含单位的标准数字
    result = 0
    current = 0
    for c in text:
        if c in _DIGITS:
            current = _DIGITS[c]
        elif c in _UNITS:
            unit = _UNITS[c]
            if unit >= 10 and current == 0:
                current = 1  # "十" = 10 (而不是 0)
            if unit >= 10000:
                result = (result + current) * unit
                current = 0
            else:
                result += current * unit
                current = 0
    result += current
    return result


def split_by_timing(text: str, timestamps: list) -> list[dict]:
    """按字符时间戳间隔分割 → 逐数字分段

    参数:
        text: ASR 文本（如 "一二三四"）
        timestamps: 字符级时间戳 [[start_ms, end_ms], ...]

    返回:
        [{"text": "14", "start": 0.17, "end": 9.69, "chinese": "十四"}, ...]
    """
    CHARS = set(_DIGITS.keys()) | {"十", "百", "千", "万", "亿"}
    gap_threshold = 0.35  # 秒

    groups = []
    current_chars = []
    start_time = 0.0
    prev_end = 0.0

    for i, (c, ts_data) in enumerate(zip(text, timestamps)):
        st = ts_data[0] / 1000 if isinstance(ts_data, (list, tuple)) else ts_data
        et = ts_data[1] / 1000 if isinstance(ts_data, (list, tuple)) else ts_data

        # 跳过非数字字符（标点、空格等）
        if c not in CHARS:
            if current_chars:
                chinese = "".join(current_chars)
                groups.append({
                    "chinese": chinese,
                    "arabic": chinese_to_number(chinese),
                    "start": round(start_time, 2),
                    "end": round(prev_end, 2),
                })
                current_chars = []
            continue

        gap = st - prev_end if prev_end > 0 else 0
        if gap > gap_threshold and current_chars:
            chinese = "".join(current_chars)
            groups.append({
                "chinese": chinese,
                "arabic": chinese_to_number(chinese),
                "start": round(start_time, 2),
                "end": round(prev_end, 2),
            })
            current_chars = []
            start_time = st

        if not current_chars:
            start_time = st
        current_chars.append(c)
        prev_end = et

    if current_chars:
        chinese = "".join(current_chars)
        groups.append({
            "chinese": chinese,
            "arabic": chinese_to_number(chinese),
            "start": round(start_time, 2),
            "end": round(prev_end, 2),
        })

    return groups


def format_timeline(text: str, timestamps: list) -> str:
    """格式化 ASR 数字时间轴

    输入: text="十四", timestamps=[[170,410], ...]
    输出: "1: 14  (0.2s→9.7s)"
    """
    groups = split_by_timing(text, timestamps)
    lines = []
    for i, g in enumerate(groups, 1):
        lines.append(f"  [{i:3d}] {g['arabic']:>4d}  {g['start']:.1f}s → {g['end']:.1f}s  ({g['chinese']})")
    return "\n".join(lines)


def detect_drift(ref_numbers: list[dict], test_numbers: list[dict],
                 threshold: float = 1.0) -> dict:
    """检测两个数字时间轴之间的漂移

    策略：
    1. 找到第一个相同的数字作为对齐锚点
    2. 计算时间偏移量
    3. 检测后续数字是否漂移超过阈值

    Args:
        ref_numbers: 参考数字时间轴 [{"arabic": 14, "start": 0.2, "end": 9.7}, ...]
        test_numbers: 待检数字时间轴
        threshold: 漂移阈值（秒），默认 1.0s

    Returns:
        {
            "aligned": True/False,
            "anchor": {"ref_idx": 0, "test_idx": 0, "value": 14, "offset_s": 0.5},
            "drifts": [{"test_idx": 5, "value": 20, "ref_time": 10.2, "test_time": 11.8, "drift": 1.6}],
            "total_drift": 3,
            "summary": "对齐于数字 14，检测到 3 处漂移"
        }
    """
    if not ref_numbers or not test_numbers:
        return {"aligned": False, "drifts": [], "total_drift": 0, "summary": "无数字数据"}

    # Step 1: 找第一个相同数字对齐
    anchor_ref_idx = None
    anchor_test_idx = None
    for ri, rn in enumerate(ref_numbers):
        for ti, tn in enumerate(test_numbers):
            if rn["arabic"] == tn["arabic"]:
                anchor_ref_idx = ri
                anchor_test_idx = ti
                break
        if anchor_ref_idx is not None:
            break

    if anchor_ref_idx is None:
        return {
            "aligned": False,
            "drifts": [],
            "total_drift": 0,
            "summary": "未找到相同数字，无法对齐"
        }

    # Step 2: 计算时间偏移
    offset = test_numbers[anchor_test_idx]["start"] - ref_numbers[anchor_ref_idx]["start"]

    # Step 3: 对齐后逐个比较
    drifts = []
    ri = anchor_ref_idx
    ti = anchor_test_idx
    while ri < len(ref_numbers) and ti < len(test_numbers):
        rn = ref_numbers[ri]
        tn = test_numbers[ti]

        if rn["arabic"] == tn["arabic"]:
            # 相同数字，检查时间漂移
            expected_time = rn["start"] + offset
            actual_drift = abs(tn["start"] - expected_time)
            if actual_drift > threshold:
                drifts.append({
                    "test_idx": ti,
                    "value": tn["arabic"],
                    "chinese": tn.get("chinese", ""),
                    "ref_time": round(rn["start"], 2),
                    "test_time": round(tn["start"], 2),
                    "expected_time": round(expected_time, 2),
                    "drift": round(actual_drift, 2),
                })
            ri += 1
            ti += 1
        elif rn["arabic"] < tn["arabic"]:
            # 参考有但测试跳过了（吞字）
            ri += 1
        else:
            # 测试有多余数字（插字）
            ti += 1

    summary = f"对齐于数字 {ref_numbers[anchor_ref_idx]['arabic']}"
    if drifts:
        summary += f"，检测到 {len(drifts)} 处漂移 (>{threshold}s)"
    else:
        summary += f"，时间轴一致 (偏移 {offset:+.1f}s)"

    return {
        "aligned": True,
        "anchor": {
            "ref_idx": anchor_ref_idx,
            "test_idx": anchor_test_idx,
            "value": ref_numbers[anchor_ref_idx]["arabic"],
            "offset_s": round(offset, 2),
        },
        "drifts": drifts,
        "total_drift": len(drifts),
        "summary": summary,
    }


def format_drift_report(drift_result: dict) -> str:
    """格式化漂移检测报告"""
    if not drift_result.get("aligned"):
        return f"  ⚠️ {drift_result.get('summary', '无法对齐')}"

    lines = []
    anchor = drift_result["anchor"]
    lines.append(f"  🔗 对齐: 数字 {anchor['value']} (偏移 {anchor['offset_s']:+.1f}s)")

    if drift_result["drifts"]:
        lines.append(f"  ⚠️  漂移 {len(drift_result['drifts'])} 处:")
        for d in drift_result["drifts"]:
            lines.append(f"     数字 {d['value']:>4d} ({d['chinese']})  "
                        f"期望 {d['expected_time']:.1f}s  实际 {d['test_time']:.1f}s  "
                        f"漂移 {d['drift']:.1f}s")
    else:
        lines.append(f"  ✅ 时间轴一致，无漂移")

    return "\n".join(lines)


# ─── 自检 ───────────────────────────────────────────────────────

def _selftest():
    """可运行的核心逻辑检查"""
    # chinese_to_number
    assert chinese_to_number("十四") == 14
    assert chinese_to_number("一百二十三") == 123
    assert chinese_to_number("一万两千") == 12000
    assert chinese_to_number("零") == 0
    assert chinese_to_number("十") == 10
    assert chinese_to_number("二十") == 20
    assert chinese_to_number("三百零五") == 305
    assert chinese_to_number("") == 0
    # 纯数字逐位
    assert chinese_to_number("一二三") == 123

    # split_by_timing
    ts = [[0, 500], [600, 1100]]
    groups = split_by_timing("十四", ts)
    assert len(groups) == 1
    assert groups[0]["arabic"] == 14

    # detect_drift
    ref = [{"arabic": 14, "start": 0.2}, {"arabic": 20, "start": 10.0}]
    test = [{"arabic": 14, "start": 0.3}, {"arabic": 20, "start": 10.1}]
    result = detect_drift(ref, test, threshold=1.0)
    assert result["aligned"] is True
    assert result["total_drift"] == 0

    # text_diff
    from .asr_handler import text_diff
    assert text_diff("abc", "abc")["similarity"] == 1.0
    d = text_diff("abc", "axc")
    assert d["similarity"] < 1.0
    assert d["has_missing"] is True

    print("  ✅ 自检通过")


if __name__ == "__main__":
    _selftest()
