"""运行诊断：结构化 JSONL 采集 + 规则摘要 + 可解释指标（无 LLM）。"""
from .analyzer import load_events, summarize
from .recorder import RunRecorder
from .report import print_run_summary, print_explainability_summary
from .metrics import (
    ExplainabilityReport,
    PhaseTiming,
    RoundResolution,
    RuleAndResolutionSummary,
    TimeSavedEstimate,
    compute_explainability_metrics,
    compute_phase_timings,
    compute_rule_and_resolution_summary,
    estimate_user_time_saved,
)

__all__ = [
    "RunRecorder",
    "load_events",
    "summarize",
    "print_run_summary",
    "print_explainability_summary",
    "ExplainabilityReport",
    "PhaseTiming",
    "RoundResolution",
    "RuleAndResolutionSummary",
    "TimeSavedEstimate",
    "compute_explainability_metrics",
    "compute_phase_timings",
    "compute_rule_and_resolution_summary",
    "estimate_user_time_saved",
]
