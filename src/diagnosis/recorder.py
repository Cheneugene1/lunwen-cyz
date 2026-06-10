"""
运行诊断 — 结构化事件采集（JSON Lines）

每行一条 JSON，便于规则分析或下游 LLM 润色；不包含论文正文与密钥。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


@dataclass
class RunRecorder:
    """
    将一次论文生成会话的事件追加写入 outputs/run_<session_id>.jsonl
    """

    session_id: str
    output_dir: Path
    enabled: bool = True
    keep_sessions: int = 20

    def __post_init__(self) -> None:
        self.output_dir = Path(self.output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._path = self.output_dir / f"run_{self.session_id}.jsonl"
        self._phase_started_at: float = time.monotonic()
        self._cleanup_old_logs()

    def _cleanup_old_logs(self) -> None:
        """删除旧 session 日志，保留最近 keep_sessions 个。"""
        logs = sorted(
            self.output_dir.glob("run_*.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for old in logs[self.keep_sessions :]:
            try:
                old.unlink()
                logger.debug("删除旧诊断日志: %s", old.name)
            except OSError:
                pass

    def append(self, event: dict[str, Any]) -> None:
        if not self.enabled:
            return
        row = {
            "ts": _now_iso(),
            "session_id": self.session_id,
            **event,
        }
        line = json.dumps(row, ensure_ascii=False)
        try:
            with self._path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError as e:
            logger.warning("诊断日志写入失败: %s", e)

    def begin_run(
        self,
        *,
        user_request_chars: int,
        n_doc_files: int,
        n_ref_files: int,
        quality_threshold: float,
        max_revision_rounds: int,
    ) -> None:
        self._phase_started_at = time.monotonic()
        self.append(
            {
                "type": "run_begin",
                "user_request_chars": user_request_chars,
                "n_doc_files": n_doc_files,
                "n_ref_files": n_ref_files,
                "quality_threshold": quality_threshold,
                "max_revision_rounds": max_revision_rounds,
                "log_path": str(self._path.resolve()),
            }
        )

    def phase_transition(self, from_phase: str, to_phase: str) -> None:
        now = time.monotonic()
        duration_ms = (now - self._phase_started_at) * 1000.0
        self._phase_started_at = now
        self.append(
            {
                "type": "phase_span",
                "from_phase": from_phase,
                "to_phase": to_phase,
                "duration_ms": round(duration_ms, 2),
            }
        )

    def parse_complete(self, *, block_count: int, truncation_applied: bool) -> None:
        self.append(
            {
                "type": "parse_complete",
                "block_count": block_count,
                "truncation_applied": truncation_applied,
            }
        )

    def plan_complete(self, *, outline_sections: int, n_keywords: int) -> None:
        self.append(
            {
                "type": "plan_complete",
                "outline_sections": outline_sections,
                "n_keywords": n_keywords,
            }
        )

    def search_complete(
        self, *, search_rounds: int, ref_pool_size: int, target_refs: int
    ) -> None:
        self.append(
            {
                "type": "search_complete",
                "search_rounds": search_rounds,
                "ref_pool_size": ref_pool_size,
                "target_refs": target_refs,
            }
        )

    def draft_complete(
        self, *, n_sections: int, version: int, approx_chars: int
    ) -> None:
        self.append(
            {
                "type": "draft_complete",
                "n_sections": n_sections,
                "version": version,
                "approx_chars": approx_chars,
            }
        )

    def evaluation(
        self,
        *,
        revision_round: int,
        score_total: float,
        n_actionable_items: int,
        n_static_rule_issues: int = 0,
        structure: float,
        logic: float,
        language: float,
        alignment: float,
        threshold: float,
        static_delta: dict | None = None,
        actionable_coarse_delta: dict | None = None,
        static_rule_breakdown: dict | None = None,
        stubborn_count: int | None = None,
    ) -> None:
        payload: dict[str, Any] = {
                "type": "evaluation",
                "revision_round": revision_round,
                "score_total": score_total,
                "n_actionable_items": n_actionable_items,
                "n_static_rule_issues": n_static_rule_issues,
                "dimensions": {
                    "structure": structure,
                    "logic": logic,
                    "language": language,
                    "alignment": alignment,
                },
                "threshold": threshold,
        }
        if static_delta is not None:
            payload["static_delta"] = {
                "prev_total": static_delta.get("prev_total"),
                "resolved": static_delta.get("resolved"),
                "new": static_delta.get("new"),
                "net": static_delta.get("net"),
                "resolve_rate_pct": static_delta.get("resolve_rate_pct"),
                "net_fix_rate_pct": static_delta.get("net_fix_rate_pct"),
                "resolved_rule_ids": static_delta.get("resolved_rule_ids"),
                "new_rule_ids": static_delta.get("new_rule_ids"),
            }
        if actionable_coarse_delta is not None:
            payload["actionable_coarse_delta"] = {
                "prev_fp_total": actionable_coarse_delta.get("prev_fp_total"),
                "resolved_fp": actionable_coarse_delta.get("resolved_fp"),
                "new_fp": actionable_coarse_delta.get("new_fp"),
                "net_fp": actionable_coarse_delta.get("net_fp"),
                "resolve_rate_pct": actionable_coarse_delta.get("resolve_rate_pct"),
                "net_fix_rate_pct": actionable_coarse_delta.get("net_fix_rate_pct"),
            }
        if static_rule_breakdown is not None:
            payload["static_rule_breakdown"] = static_rule_breakdown
        if stubborn_count is not None:
            payload["stubborn_count"] = stubborn_count
        self.append(payload)

    def revision_complete(
        self, *, revision_round: int, term_map_keys: list[str], new_version: int
    ) -> None:
        self.append(
            {
                "type": "revision_complete",
                "revision_round": revision_round,
                "term_map_keys": term_map_keys,
                "new_version": new_version,
            }
        )

    def run_end(
        self,
        *,
        status: str,
        paper_path: Optional[str] = None,
        error: Optional[str] = None,
        final_score: Optional[float] = None,
        total_wall_time_ms: Optional[float] = None,
    ) -> None:
        payload: dict[str, Any] = {
            "type": "run_end",
            "status": status,
        }
        if paper_path:
            payload["paper_path"] = paper_path
        if error:
            payload["error"] = error[:2000]
        if final_score is not None:
            payload["final_score"] = final_score
        if total_wall_time_ms is not None:
            payload["total_wall_time_ms"] = round(total_wall_time_ms, 2)
        self.append(payload)
        if self.enabled:
            logger.info("运行诊断日志：%s", self._path)

    @property
    def log_path(self) -> Path:
        return self._path
