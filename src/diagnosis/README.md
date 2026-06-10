---
agent_entry: true
slug: lunwencyz-diagnosis
title: 运行诊断（基础设施 + 可解释指标）
canonical_paths:
  - src/diagnosis/recorder.py
  - src/diagnosis/analyzer.py
  - src/diagnosis/report.py
  - src/diagnosis/metrics.py
  - src/controller.py
sync_rule: >
  更改事件类型字段、写入路径或与控制器挂钩位置时，同步更新本文件及 analyzer.summarize 的约定；
  若新增 config 开关，更新 config/config.example.yml。
related_docs:
  - PROGRESS.md
---

# 运行诊断子系统

## 能力

- **采集**：会话期间向 `outputs/run_<session_id>.jsonl` 追加一行一事件的 JSON（UTC 时间戳 + `session_id`）。
- **分析**：`load_events` / `summarize` 只做规则聚合，**不调用 LLM**。
- **展示**：`python -m src.diagnosis outputs/run_xxx.jsonl` 用 Rich 打印阶段耗时表与末次评估摘要。
- **可解释指标**：`metrics.py` 从 JSONL 计算四项可解释指标（生成耗时、规则命中/消解率、用户时间节省），`print_explainability_summary()` 渲染 Rich 面板。

## 事件类型（`type`）

| type | 说明 |
|------|------|
| `run_begin` | 会话开始：文档数、文献文件数、需求长度、阈值等（不含正文） |
| `phase_span` | 控制器 `_set_phase`：上一阶段 → 下一阶段，`duration_ms` |
| `parse_complete` | 解析块数、是否截断 |
| `plan_complete` | 大纲节数、关键词数 |
| `search_complete` | 检索轮次、文献池大小 |
| `draft_complete` | 章节数、版本、近似字数（`approx_chars`） |
| `evaluation` | 评分、四维、建议条数、`revision_round`、可选 `static_delta`（按 **rule_id@rule_version** 键集合差分，`resolved_rule_ids` / `new_rule_ids` 为复合键字符串）、可选 `actionable_coarse_delta`（【标签】\|章节 指纹；可选含关键词子串）、可选 `static_rule_breakdown`（`by_severity` / `by_category` / `by_rule_id` 分桶）、可选 `stubborn_count`（跨轮顽固问题数） |
| `revision_complete` | 修订轮次、`term_map` 键、新版本 |
| `run_end` | `success` / `interrupted` / `error` + 输出路径或错误摘要 + `total_wall_time_ms`（会话总墙钟） |

## 可解释指标模块

`src/diagnosis/metrics.py` — 纯函数（零 LLM 依赖），从 JSONL 事件列表计算四项指标：

| 函数 | 说明 |
|------|------|
| `compute_phase_timings(events)` | 从 `phase_span` 提取各阶段耗时列表 |
| `compute_rule_and_resolution_summary(events, top_n)` | 规则命中趋势 + 消解率趋势 + 高频规则 TOP-N |
| `estimate_user_time_saved(events, ...)` | 基于字数+人工速度估算用户节省时间 |
| `compute_explainability_metrics(events, ...)` | 顶层聚合，返回 `ExplainabilityReport` |

数据结构（dataclass）：`ExplainabilityReport`、`PhaseTiming`、`RuleAndResolutionSummary`、`RoundResolution`、`TimeSavedEstimate`。

终端展示入口：`print_explainability_summary(path)` — 读取 JSONL 并渲染 Rich 面板（生成耗时表 + 规则命中与消解 + 用户时间节省）。

## 配置

- `diagnostics_enabled`（`config/config.example.yml`）：`false` 时仍创建 `RunRecorder` 但 `append` 不写文件。
- `explainability`（`config/config.example.yml`）：可解释指标开关及用户速度参数。
- `keep_sessions: 20`：`RunRecorder` 初始化时自动清理旧 JSONL 日志（`_cleanup_old_logs`）。

## 维护

- 后续可加 `reporter_llm.py` 仅消费 `summarize` 结果做润色；**勿**把全文论文写入 JSONL。
- 可解释指标的计算逻辑在 `metrics.py` 中，保持纯函数、零 LLM 依赖。
