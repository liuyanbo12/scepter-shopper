from __future__ import annotations

import copy
import json
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

TERMINAL = {"completed", "failed", "stopped"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    """Atomic, local JSON journals. Never stores the model API key."""

    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.runs: dict[str, dict] = {}
        for path in sorted(self.directory.glob("*.json")):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                if not re.fullmatch(r"[a-f0-9]{32}", record["id"]):
                    continue
                self.runs[record["id"]] = record
                if record.get("status") not in TERMINAL:
                    record.update(status="stopped", error="应用已重启；本次任务已停止。请检查购物车后新建任务。", updated_at=now())
                    self._save(record)
            except (OSError, ValueError, KeyError, TypeError):
                continue

    def _save(self, record: dict):
        path = self.directory / f'{record["id"]}.json'
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        tmp.replace(path)

    def create(self, prompt: str) -> dict:
        with self.lock:
            record = {
                "id": uuid.uuid4().hex, "prompt": prompt, "status": "planning", "phase": "理解购物需求",
                "created_at": now(), "updated_at": now(), "plan": None, "candidates": [], "events": [],
                "summary": "", "error": None, "step_count": 0,
                "usage": {"prompt_tokens": 0, "completion_tokens": 0}, "cart_items": [],
                "proposals": [], "default_proposal_id": None, "choice_deadline": None,
                "choice_policy": "recommend_only", "recovery": {}, "cart_deltas": {},
            }
            self.runs[record["id"]] = record
            self._save(record)
            return copy.deepcopy(record)

    def get(self, run_id: str) -> dict:
        with self.lock:
            if run_id not in self.runs:
                raise KeyError("任务不存在")
            return copy.deepcopy(self.runs[run_id])

    def update(self, run_id: str, **fields) -> dict:
        with self.lock:
            if "id" in fields or "events" in fields:
                raise ValueError("不可直接更改任务 ID 或事件日志")
            self.runs[run_id].update(copy.deepcopy(fields), updated_at=now())
            self._save(self.runs[run_id])
            return self.get(run_id)

    def event(self, run_id: str, message: str, level: str = "info") -> None:
        with self.lock:
            record = self.runs[run_id]
            record.setdefault("events", []).append({"time": now(), "level": level, "message": str(message)[:4000]})
            record["events"] = record["events"][-400:]
            record["updated_at"] = now()
            self._save(record)

    def history(self) -> list[dict]:
        with self.lock:
            return [copy.deepcopy(r) for r in sorted(self.runs.values(), key=lambda r: r["created_at"], reverse=True)[:50]]

    def active(self) -> dict | None:
        return next((r for r in self.history() if r["status"] not in TERMINAL), None)
