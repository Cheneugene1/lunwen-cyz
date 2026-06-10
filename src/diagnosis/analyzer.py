"""
运行诊断 — 从 JSONL 事件构建规则摘要（不调用 LLM）
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_events(path: str | Path) -> list[dict[str, Any]]:
    """读取诊断 JSONL，返回事件列表（跳过坏行）。"""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(str(path))
    events: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


def summarize(events: list[dict[str, Any]]) -> dict[str, Any]:
    """聚合统计，供终端表格或后续报告层使用。"""
    by_type: dict[str, list[dict[str, Any]]] = {}
    for e in events:
        t = e.get("type", "")
        by_type.setdefault(t, []).append(e)

    spans = by_type.get("phase_span", [])
    total_phase_ms = sum(float(s.get("duration_ms") or 0) for s in spans)

    evals = by_type.get("evaluation", [])
    last_eval = evals[-1] if evals else None

    return {
        "session_id": events[0].get("session_id") if events else None,
        "n_events": len(events),
        "phase_spans": spans,
        "total_phase_ms": round(total_phase_ms, 2),
        "parse": by_type.get("parse_complete", [{}])[-1] if by_type.get("parse_complete") else None,
        "plan": by_type.get("plan_complete", [{}])[-1] if by_type.get("plan_complete") else None,
        "search": by_type.get("search_complete", [{}])[-1] if by_type.get("search_complete") else None,
        "draft": by_type.get("draft_complete", [{}])[-1] if by_type.get("draft_complete") else None,
        "evaluations": evals,
        "last_evaluation": last_eval,
        "revisions": by_type.get("revision_complete", []),
        "run_end": by_type.get("run_end", [{}])[-1] if by_type.get("run_end") else None,
    }
