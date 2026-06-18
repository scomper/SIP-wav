"""任务管理 — 支持中断恢复、去重、进度追踪"""

import json
import os
import time
import uuid
from datetime import datetime, timezone, timedelta


TZ = timezone(timedelta(hours=8))


class TaskManager:
    """任务管理器，管理任务的创建、执行、中断恢复"""

    def __init__(self, work_dir: str):
        self.work_dir = work_dir
        self.task_file = os.path.join(work_dir, ".sipcheck_task.json")
        os.makedirs(work_dir, exist_ok=True)

    # ─── 任务文件读写 ────────────────────────────────────────

    def load_task(self) -> dict | None:
        """加载当前任务"""
        if not os.path.exists(self.task_file):
            return None
        with open(self.task_file) as f:
            return json.load(f)

    def save_task(self, task: dict):
        """保存任务状态"""
        task["updated_at"] = datetime.now(TZ).isoformat()
        with open(self.task_file, "w") as f:
            json.dump(task, f, ensure_ascii=False, indent=2, default=str)

    def clear_task(self):
        """清除任务文件"""
        if os.path.exists(self.task_file):
            os.remove(self.task_file)

    # ─── 创建任务 ────────────────────────────────────────────

    def create_task(self, directory: str, sample: str | None = None, mode: str = "scan") -> dict:
        """创建新任务，扫描目录生成文件列表"""
        from .scanner import find_wav_files

        files = find_wav_files(directory)
        task = {
            "task_id": uuid.uuid4().hex[:12],
            "created_at": datetime.now(TZ).isoformat(),
            "updated_at": datetime.now(TZ).isoformat(),
            "status": "pending",
            "config": {
                "directory": directory,
                "sample": sample,
                "mode": mode,
            },
            "stats": {
                "total": len(files),
                "completed": 0,
                "failed": 0,
                "abnormal": 0,
                "normal": 0,
                "elapsed_seconds": 0,
            },
            "files": [
                {"path": f, "status": "pending", "verdict": None, "elapsed_s": 0, "flags": []}
                for f in files
            ],
            "output": {},
        }
        self.save_task(task)
        return task

    # ─── 执行进度 ────────────────────────────────────────────

    def get_progress(self, task: dict) -> dict:
        """获取当前进度摘要"""
        s = task["stats"]
        done = s["completed"] + s["failed"]
        total = s["total"]
        pct = done / total * 100 if total > 0 else 0
        return {
            "total": total,
            "completed": s["completed"],
            "failed": s["failed"],
            "abnormal": s["abnormal"],
            "normal": s["normal"],
            "progress_pct": round(pct, 1),
            "done": done,
            "remaining": total - done,
            "elapsed_s": s["elapsed_seconds"],
        }

    def format_progress(self, task: dict) -> str:
        """格式化进度显示"""
        p = self.get_progress(task)
        bar_len = 30
        filled = int(bar_len * p["done"] / p["total"]) if p["total"] > 0 else 0
        bar = "█" * filled + "░" * (bar_len - filled)
        eta = ""
        if p["done"] > 0 and p["elapsed_s"] > 0:
            rate = p["done"] / p["elapsed_s"]
            remaining = p["remaining"] / rate if rate > 0 else 0
            eta = f" | 估计剩余: {remaining:.0f}s"
        return (
            f"  [{bar}] {p['progress_pct']:.0f}% "
            f"({p['done']}/{p['total']})"
            f" | 异常: {p['abnormal']} | 失败: {p['failed']}"
            f" | 耗时: {p['elapsed_s']:.0f}s{eta}"
        )

    # ─── 任务恢复 ────────────────────────────────────────────

    def find_resume_task(self) -> dict | None:
        """查找可恢复的任务"""
        task = self.load_task()
        if not task:
            return None
        if task["status"] in ("completed",):
            return None
        # 检查目录是否还存在
        if not os.path.isdir(task["config"]["directory"]):
            return None
        return task

    def get_pending_files(self, task: dict) -> list[str]:
        """获取尚未处理的文件列表"""
        return [f["path"] for f in task["files"] if f["status"] == "pending"]

    # ─── 文件处理回调 ────────────────────────────────────────

    def mark_processing(self, task: dict, file_path: str):
        """标记文件为处理中"""
        for f in task["files"]:
            if f["path"] == file_path:
                f["status"] = "processing"
                break
        self.save_task(task)

    def mark_done(self, task: dict, file_path: str, verdict: str, flags: list, elapsed_s: float):
        """标记文件处理完成"""
        for f in task["files"]:
            if f["path"] == file_path:
                f["status"] = "done"
                f["verdict"] = verdict
                f["flags"] = flags
                f["elapsed_s"] = round(elapsed_s, 3)
                break
        s = task["stats"]
        s["completed"] += 1
        if verdict == "abnormal":
            s["abnormal"] += 1
        else:
            s["normal"] += 1
        s["elapsed_seconds"] += elapsed_s
        self.save_task(task)

    def mark_failed(self, task: dict, file_path: str, error: str, elapsed_s: float):
        """标记文件处理失败"""
        for f in task["files"]:
            if f["path"] == file_path:
                f["status"] = "failed"
                f["verdict"] = "failed"
                f["error"] = error
                f["elapsed_s"] = round(elapsed_s, 3)
                break
        s = task["stats"]
        s["failed"] += 1
        s["elapsed_seconds"] += elapsed_s
        self.save_task(task)

    def complete_task(self, task: dict, report_path: str | None = None):
        """标记任务完成"""
        task["status"] = "completed"
        if report_path:
            task["output"]["report_path"] = report_path
        self.save_task(task)
