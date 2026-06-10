# 论文 Agent 可解释指标设计方案（v2 修正版）

> 状态：待评审 | 日期：2026-06-03
> 优先级：P2（独立功能，不阻塞 Bug 修复和测试补全）

---

## 一、现状核实

以下声明均已在当前代码中逐条确认：

| 声明 | 位置 | 现状 |
|------|------|------|
| 阶段耗时采集 `phase_span` | [recorder.py:93-104](src/diagnosis/recorder.py#L93-L104) | 已实现：`from_phase→to_phase` + `duration_ms` |
| 评估事件 `evaluation` | [recorder.py:148-197](src/diagnosis/recorder.py#L148-L197) | 已实现：含 `score_total`、四维分、`n_static_rule_issues`、`n_actionable_items` |
| 评估事件中的 `static_delta` | [recorder.py:177-187](src/diagnosis/recorder.py#L177-L187) | 已实现：`resolve_rate_pct`、`net_fix_rate_pct`、`resolved_rule_ids`、`new_rule_ids` |
| 评估事件中的 `actionable_coarse_delta` | [recorder.py:188-196](src/diagnosis/recorder.py#L188-L196) | 已实现：同上维度的 actionable 指纹消解率 |
| JSONL 聚合 `summarize()` | [analyzer.py:30-56](src/diagnosis/analyzer.py#L30-L56) | 已实现：按 type 分组，取末次评估摘要 |
| 终端报告 `print_run_summary()` | [report.py:15-59](src/diagnosis/report.py#L15-L59) | 已实现：Rich 表格输出阶段耗时 + 末次评估 |
| 日志轮转 `_cleanup_old_logs()` | [recorder.py:42-52](src/diagnosis/recorder.py#L42-L52) | **已实现** — keep_sessions=20，自动清理旧日志 |
| Controller 中 `_diag` 调用点 | [controller.py:140-144](src/controller.py#L140-L144) 等 14 处 | 已实现：controller 在各阶段入口/出口调用 recorder |
| 初稿字数 `approx_chars` | [recorder.py:136-146](src/diagnosis/recorder.py#L136-L146) | **已实现** — `draft_complete` 事件记录 `approx_chars`，可直供用户时间估算 |
| 顽固问题已计算但未写 JSONL | [controller.py:637-647](src/controller.py#L637-L647) | `identify_stubborn_issues` 结果存于 `self._stubborn_items`，**未传入 `_diag.evaluation()`** |

### 确认缺口

1. **生成耗时**：阶段级 `phase_span` 记录的是粗粒度阶段转移，缺少 **DRAFT/REVISE 内部子阶段**（各章节生成/修订耗时）和 **总墙钟时间**
2. **规则命中**：有总数 `n_static_rule_issues`，缺少 **按 severity/category 分桶** 和 **高频规则 TOP-N**
3. **消解率**：差分数据已在 JSONL 中落地，但 **终端报告 `print_run_summary` 未展示**
4. **用户节省时间**：完全缺失

---

## 二、四项指标设计

### 2.1 生成耗时（Generation Time）

#### 2.1.1 采集策略：只用阶段级计时，不做逐次 LLM 计时

**不新增 `llm_call` 事件类型，不改 `llm.py`。**

理由：
- `llm.py` 目前完全不依赖项目内模块，注入 callback 会破坏底层工具层的独立性
- 阶段级计时已足够回答用户关心的「DRAFT 花了多久」
- 并行调用（`parallel_draft=true`）时逐次计时加总会超过墙钟，反而误导

**改动点**（仅 controller）：

```python
# controller.py __init__
self._started_at = time.monotonic()  # 新增：记录会话开始时间

# controller.py _do_draft / _do_revise 等阶段入口
t0 = time.monotonic()
# ... 阶段逻辑 ...
phase_ms = (time.monotonic() - t0) * 1000
# 可选：通过 _diag 记录子阶段（如 draft_s1、draft_s2 等）
```

**`run_end` 增加总墙钟**（[recorder.py:211-229](src/diagnosis/recorder.py#L211-L229)）：

```python
# controller.py _do_done → run_end 调用处
total_wall_time_ms = (time.monotonic() - self._started_at) * 1000
self._diag.run_end(status="success", ..., total_wall_time_ms=total_wall_time_ms)
```

#### 2.1.2 聚合计算（在 `metrics.py` 中）

| 指标 | 计算方式 | 数据来源 |
|------|----------|----------|
| 总墙钟时间 | `run_end.total_wall_time_ms` | `run_end` 事件 |
| 各阶段耗时 & 占比 | `phase_span` 按 `to_phase` 聚合 | `phase_span` 事件 |
| 修订循环总耗时 | Σ `phase_span` 中 to_phase 为 EVAL/REVISE 的 | `phase_span` 事件 |
| DRAFT 占比 | DRAFT 阶段耗时 / 总墙钟 | 计算 |

#### 2.1.3 终端展示（Rich 表格，`print_explainability_summary`）

```
🕐 生成耗时（总墙钟 9分47秒）
┌──────────┬──────────┬───────┐
│ 阶段     │ 耗时     │ 占比  │
├──────────┼──────────┼───────┤
│ PARSE    │    2.1s  │  0.4% │
│ PLAN     │   18.3s  │  3.1% │
│ SEARCH   │   45.2s  │  7.7% │
│ DRAFT    │  312.5s  │ 53.2% │
│ EVAL×3   │   72.8s  │ 12.4% │
│ REVISE×2 │  130.1s  │ 22.1% │
│ DONE     │    6.2s  │  1.1% │
├──────────┼──────────┼───────┤
│ 总计     │  587.2s  │ 100%  │
└──────────┴──────────┴───────┘
```

---

### 2.2 规则命中数（Rule Hit Count）

#### 2.2.1 采集策略：在 controller `_do_eval` 中 group by

不改 `_check_thesis_rules()` 也不改 evaluator — 最干净的做法是在 controller `_do_eval` 中对 `ev.static_rule_issues` 做一次 group by，然后传给 `_diag.evaluation()`。

```python
# controller.py _do_eval，传给 _diag.evaluation() 之前
breakdown: dict = {"by_severity": {}, "by_category": {}, "by_rule_id": {}}
for issue in (ev.static_rule_issues or []):
    sev = issue.severity
    cat = issue.rule_category
    rid = issue.rule_id
    breakdown["by_severity"][sev] = breakdown["by_severity"].get(sev, 0) + 1
    breakdown["by_category"][cat] = breakdown["by_category"].get(cat, 0) + 1
    breakdown["by_rule_id"][rid] = breakdown["by_rule_id"].get(rid, 0) + 1

self._diag.evaluation(
    ...,
    static_rule_breakdown=breakdown,                # 新增参数
    stubborn_count=len(self._stubborn_items),       # 新增参数：缓解 metrics.py 反推顽固问题的复杂度
)
```

**`recorder.evaluation()` 新增参数**：`static_rule_breakdown: dict | None = None`、`stubborn_count: int | None = None`，写入 JSONL。

**说明**：`by_rule_id` 是每轮完整命中映射（非差分），`metrics.py` 跨轮累加即可得到高频规则 TOP-N。这与已有的 `static_delta`（差分）互补——后者用于消解率计算，前者用于热点分析。

#### 2.2.2 聚合计算

| 指标 | 计算方式 |
|------|----------|
| 首轮命中数 | round=0 时 `n_static_rule_issues` |
| 末轮命中数 | 最后一轮 `n_static_rule_issues` |
| 命中趋势 | 各轮 error/warning 数量序列（从 `by_severity` 取） |
| 高频规则 TOP-N | 跨轮累加各轮 `by_rule_id`，按累计次数排序取 Top-N |
| error 清零时刻 | 遍历 evaluation 事件，找 `by_severity.error` 首次为 0（或不存在 key）的轮次 |

#### 2.2.3 终端展示

只展示**末次** evaluation 的快照 + 全流程汇总，不做逐轮明细表（减少终端噪音）：

```
📏 规则命中与消解（全流程）
  初稿 → 终稿：Error  5→0 ✅ | Warning 10→3
  全流程静态规则消解率 86.7%（13/15 项）
  error 清零：R2（第 2 轮修订）
  高频规则：citation_after_punct(3次) | mcu_abstract_body_mismatch(2次) | abstract_too_long(2次)
```

---

### 2.3 修订前后消解率（Revision Resolution Rate）

#### 2.3.1 采集策略：**数据已完整，无需新事件**

每轮 evaluation 事件的 `static_delta` 和 `actionable_coarse_delta` 已包含 `resolve_rate_pct`、`net_fix_rate_pct`。不需要新增采集，只做**聚合 + 展示**。

#### 2.3.2 聚合计算（从 JSONL 中提取）

| 指标 | 计算方式 |
|------|----------|
| R1 静态消解率 | round=1 的 `static_delta.resolve_rate_pct` |
| R2 静态消解率 | round=2 的 |
| 全流程消解率 | (首轮问题数 - 末轮问题数) / 首轮问题数 |
| Actionable 消解趋势 | 各轮 `actionable_coarse_delta.resolve_rate_pct` 序列 |
| 顽固问题数 | 直接读取各轮 `stubborn_count` 字段（controller 在 `_do_eval` 中传入） |
| 首次修订边际收益 | R1 消解率 |
| 修订收益递减情况 | R2 消解率 / R1 消解率（<1 递减，≥1 持续有效） |

#### 2.3.3 终端展示

和 2.2.3 合并为同一段（规则命中 + 消解率放一起，逻辑关联）：

```
📏 规则命中与消解（全流程）
  初稿 → 终稿：Error  5→0 ✅ | Warning 10→3
  R1 静态消解 60.0%（9/15）| R2 静态消解 80.0%（4/5）→ 收益递减（策略有效）
  R1 建议消解 55.6%（5/9）| R2 建议消解 75.0%（3/4）
  全流程消解率 86.7%（13/15 项）
  顽固问题：R1 后 4 项 → R2 后 1 项 → 终稿 0 项
```

---

### 2.4 真实用户节省时间（Real User Time Saved）

#### 2.4.1 估算模型

**参数**（按老师建议调整，本科学位论文场景）：

| 参数 | 值 | 说明 |
|------|-----|------|
| `manual_draft_speed_cph` | 500 | 开题报告/素材已有，主要是组织+扩写 |
| `manual_revise_speed_cph` | 800 | 修订比初稿快，但需读+判断 |
| `manual_format_speed_cph` | 2000 | 格式调整主要是替换，很快 |
| `hours_per_session` | 3 | 单次有效工作时长 |

**计算公式**：

```
draft_words        = 终稿总字数（从 draft_complete 或 run_end 取）
revise_words       = draft_words × revision_rounds × 0.3  # 假设每轮修订改动 30% 内容
total_manual_hours = draft_words / 500
                   + revise_words / 800
                   + draft_words / 2000

ai_hours           = total_wall_time_ms / 3_600_000

time_saved_hours   = total_manual_hours - ai_hours
time_saved_ratio   = time_saved_hours / total_manual_hours
work_sessions      = time_saved_hours / hours_per_session
```

**三档估计**：
- 乐观：草稿速度 400 cph（人工较慢场景）
- 中性：草稿速度 500 cph（默认）
- 保守：草稿速度 700 cph（人工较快场景）

#### 2.4.2 终端展示

```
⏱ 用户时间节省估算
  论文 25,000 字 | 人工估算 13.8h（中性）| AI 耗时 0.16h
  节省 ~13.6 小时（约 5 个半天）
  保守 10.0h | 中性 13.8h | 乐观 17.0h

⚠ 基于人工写作速度 500 字/小时的粗略估算，仅供参考
```

---

## 三、实现方案

### 3.1 文件变更清单

| 文件 | 改动类型 | 说明 |
|------|----------|------|
| `src/diagnosis/metrics.py` | **新建** | 四项可解释指标的计算函数（纯函数，零 LLM 依赖） |
| `src/diagnosis/recorder.py` | **小改** | `evaluation()` 新增 `static_rule_breakdown` + `stubborn_count` 参数；`run_end()` 新增 `total_wall_time_ms` 参数 |
| `src/diagnosis/report.py` | **修改** | 新增 `print_explainability_summary()` — Rich 渲染可解释指标 |
| `src/diagnosis/analyzer.py` | **小改** | `summarize()` 增加 `total_wall_time_ms`、`static_rule_breakdown`、`stubborn_count` 的透传 |
| `src/diagnosis/__init__.py` | **小改** | 导出 `compute_explainability_metrics` + `print_explainability_summary` |
| `src/controller.py` | **小改** | ① `__init__` 加 `self._started_at`；② `_do_done` 传 `total_wall_time_ms`；③ `_do_eval` 做 static_rule_issues 的 group by（含 `by_rule_id`）并传 `static_rule_breakdown` + `stubborn_count` 给 `_diag.evaluation` |
| `config/config.example.yml` | **小改** | 新增 `explainability` 配置段 |
| `src/diagnosis/README.md` | **修改** | 更新事件类型 + 新增 metrics 模块说明 |

**不改的文件**：`src/llm.py`、`src/validation/evaluator.py`、`src/writing/*.py`

### 3.2 新增模块 `src/diagnosis/metrics.py`

```python
"""
可解释指标计算（纯函数，零 LLM 依赖，不依赖项目内其他模块）

从 JSONL 事件列表计算四项指标：
  - compute_phase_timings(events) -> PhaseTimingReport
  - compute_rule_hits(events) -> RuleHitReport
  - compute_resolution_rates(events) -> ResolutionReport
  - estimate_user_time_saved(events, config?) -> TimeSavedEstimate

顶层聚合：
  - compute_explainability_metrics(events, config?) -> ExplainabilityReport
"""

@dataclass
class ExplainabilityReport:
    session_id: str
    # 1. 生成耗时
    total_wall_time_s: float
    phase_timings: list[PhaseTiming]
    # 2. 规则命中 + 消解率（合并）
    rule_and_resolution: RuleAndResolutionSummary
    # 3. 用户时间节省
    time_saved: TimeSavedEstimate


@dataclass
class RuleAndResolutionSummary:
    first_round_errors: int
    first_round_warnings: int
    last_round_errors: int
    last_round_warnings: int
    error_cleared_round: int | None       # error 首次归零的轮次
    total_static_resolution_pct: float      # 全流程静态消解率
    top_rules: list[tuple[str, int]]        # (rule_id, 累计命中次数) TOP-5
    resolution_by_round: list[RoundResolution]


@dataclass
class RoundResolution:
    round: int
    static_resolve_pct: float
    static_net_fix_pct: float
    actionable_resolve_pct: float
    stubborn_count: int


@dataclass
class TimeSavedEstimate:
    draft_words: int
    manual_hours_neutral: float
    ai_hours: float
    time_saved_hours: float
    time_saved_ratio: float
    work_sessions_saved: float
    conservative_hours: float
    optimistic_hours: float
```

### 3.3 终端展示与 JSONL 事件格式

#### run_end 事件增强

```json
{
  "type": "run_end",
  "status": "success",
  "paper_path": "outputs/paper_cb3a12f7_STM32温湿度_20260603_v3.md",
  "final_score": 8.5,
  "total_wall_time_ms": 587234.5
}
```

#### evaluation 事件增强

在现有字段基础上新增：

```json
{
  "type": "evaluation",
  "...": "... (现有字段不变)",
  "static_rule_breakdown": {
    "by_severity": {"error": 2, "warning": 5},
    "by_category": {
      "citation": 2,
      "mcu_consistency": 1,
      "abstract": 1,
      "section_overflow": 1,
      "punctuation": 1,
      "keyword": 1
    },
    "by_rule_id": {
      "citation_after_punct": 2,
      "mcu_abstract_body_mismatch": 1,
      "abstract_too_long": 1,
      "missing_punct": 1,
      "section_overflow_next_chapter": 1,
      "keyword_has_sep": 1
    }
  },
  "stubborn_count": 1
}
```

#### 终端汇总报告（仅 DONE 后输出一次）

```
╔══════════════════════════════════════════════════════════╗
║        📊 论文 Agent 可解释指标        session=cb3a12f7  ║
║        总分=8.5/10  修订 2 轮  终稿 v3                   ║
╚══════════════════════════════════════════════════════════╝

🕐 生成耗时  总墙钟 9分47秒
  PLAN     18.3s ( 3.1%)   SEARCH   45.2s ( 7.7%)
  DRAFT   312.5s (53.2%)   EVAL×3   72.8s (12.4%)
  REVISE×2 130.1s (22.1%)   DONE     6.2s ( 1.1%)

📏 规则命中与消解
  初稿 → 终稿：Error 5→0 ✅ | Warning 10→3
  R1 静态消解 60.0% → R2 80.0%  全流程 86.7%（13/15 项）
  顽固问题 4→1→0  error 清零于 R2
  高频规则：citation_after_punct(3) mcu_abstract_body_mismatch(2) ...

⏱ 用户时间节省估算
  论文 25,000 字 | 人工估算 13.8h | AI 耗时 0.16h
  节省 ~13.6 小时（约 5 个半天）  保守 10.0h | 乐观 17.0h
  ⚠ 基于 500 字/h 粗略估算，仅供参考
```

---

## 四、配置项

```yaml
# config/config.example.yml 新增

# ── 可解释指标 ──
explainability:
  enabled: true                         # DONE 后展示可解释指标报告（默认开启）
  user_time_saved:
    manual_draft_speed_cph: 500         # 人工初稿速度（字/小时）
    manual_revise_speed_cph: 800        # 人工修订速度（字/小时）
    manual_format_speed_cph: 2000       # 人工格式调整速度（字/小时）
    hours_per_session: 3                # 单次有效工作时长（小时）
  top_rules_n: 5                        # 高频规则展示数量
```

---

## 五、不做的

| 项目 | 原因 |
|------|------|
| 逐次 LLM 调用计时（`llm_call` 事件） | 过度设计；破坏 `llm.py` 独立性；阶段级计时已够用 |
| 独立 JSON 输出文件 | 冗余 —— JSONL 已含完整数据，`analyzer.py` 可随时重新生成 |
| 每轮 EVAL 后插入指标展示 | 信息过载 —— `presenter.py` 已有每轮快照；指标受众是「跑完后回顾效率」的人 |
| 改 `llm.py` | 保持底层工具层零依赖 |
| 改 `evaluator.py` | group by 逻辑放 controller，不改评估子系统 |

---

## 六、v1→v2 变更记录

| v1 内容 | 问题 | v2 修正 |
|---------|------|---------|
| 新增 `llm_call` 事件 + LLM 回调注入 | 过度设计，破坏 `llm.py` 独立性 | 删除，仅用阶段级 `phase_span` |
| 日志轮转列为新增功能 | `recorder.py` 已实现 `_cleanup_old_logs` | 删除，方案 1.1 节已核实 |
| 用户速度 200/500/1000 cph | 200 字/h 太保守（25000 字需 125h） | 改为 500/800/2000 cph |
| 独立 JSON 输出（方案 C） | 冗余 | 删除，仅保留 A+B |
| 每轮中插指标展示 | 信息过载，`presenter.py` 已有每轮快照 | 删除，仅 DONE 后输出一次 |
| `run_end.total_wall_time_ms` 实现方式模糊 | 未指定具体做法 | 明确：`self._started_at` + 两行计算 |
| `static_rule_breakdown` 放 evaluator | 不必要的跨层依赖 | 放 controller `_do_eval` 中 ~10 行 group by |

## 七、v2→v3 变更记录

> 2026-06-03：根据第三个评审轮次的反馈修正三个数据缺口（其中第 3 项经核实不存在）。

| 问题 | 严重度 | 修正 |
|------|--------|------|
| `top_rules` 缺少数据源 — `static_delta` 只记录差分（`resolved_rule_ids` / `new_rule_ids`），无法从中反推每轮完整命中列表 | 中 | `static_rule_breakdown` 新增 `by_rule_id` 子字段：controller group by 时对 `issue.rule_id` 做完整计数，`metrics.py` 跨轮累加即可得高频规则 TOP-N |
| `stubborn_count` 未写入 JSONL — controller 的 `identify_stubborn_issues` 结果存于 `self._stubborn_items`，但未传入 `_diag.evaluation()` | 低 | controller `_do_eval` 传 `stubborn_count=len(self._stubborn_items)`，recorder 的 `evaluation()` 新增 `stubborn_count` 参数写入 JSONL |
| 终稿字数数据源 — 方案 2.4.1 需要终稿字数 | 无 | [`draft_complete.approx_chars`](src/diagnosis/recorder.py#L136-L146) 已存在，无需改动；在 §1 表格中显式确认 |