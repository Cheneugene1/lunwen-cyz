---
agent_entry: true
slug: lunwencyz-validation
title: 验证与评估子系统
canonical_paths:
  - src/validation/evaluator.py
  - src/validation/README.md
sync_rule: >
  更改评估维度、毕业论文静态规则（含 rule_id 命名、rule_version、severity 或静态项增减）、LLM 评分 prompt、
  dedupe_llm_against_static 或 error≥3 加罚逻辑、降级策略或 evaluate() 的
  入参与返回值约定时，必须同步更新本文件中的「能力摘要」「运行时序」「公开 API」；
  若修改 Evaluation 模型字段，需同时更新 src/models.py 与本文件及控制器展示逻辑说明；
  若静态规则中的引用位置与撰写侧 `_fix_citation_position` 语义分叉，须同步更新 src/writing/README.md「引用」小节。
related_docs:
  - PROGRESS.md
  - src/writing/README.md
related_code:
  - src/models.py  # Evaluation, EvaluationDimensions
  - src/controller.py  # _do_eval, 修订循环退出条件
  - src/llm.py  # chat_json 超时（deepseek_timeout_read_blocking）
---

# 验证与评估子系统（Validation）

面向 Agent：本文件为**质量验证与评估**的入口说明；实现以 `canonical_paths` 中的代码为准。

## 能力摘要

- 对整体稿 `Manuscript` 做 **LLM 多维度评分**（结构 / 逻辑 / 语言 / 匹配），产出经 Pydantic 校验的 `Evaluation`。`_SYSTEM_EVAL_THESIS` 含数据占位符豁免（`[实测数据]`/`[待测]`/`TBD`等不扣分）、显式加权公式（`structure×0.30+logic×0.30+language×0.25+alignment×0.15`）、强制检查清单瘦身（10→3项，静态已覆盖的跳过）、禁止输出需人工实验的修订建议。
- **大纲评分门禁**（`src/planner.py`）：PLAN 阶段后对 `WritingPlan` 执行 **硬规则检查（28条）+ LLM 五维语义评分**（logic 0.30 / content_depth 0.25 / feasibility 0.25 / format_compliance 0.15 / novelty 0.05）；仅结构级 error（缺失章节/ID重复）阻断 LLM 调用，内容级 error 与 LLM 评分并行；warning ≥5 条时总分扣 0.1×warning（上限 -1.0）。详见 `PROGRESS.md`「大纲评分门禁」条目。
- **毕业论文模式**（`thesis_mode=true`）：在 LLM 评分前执行 **`_check_thesis_rules`** 静态检查（字数、参考文献数量、摘要/结论/关键词与引用格式、**引用序号 vs 文献池一致性**（`citation_missing_ref`）、**标点缺失/混用/重复**（`missing_punct`/`mixed_punctuation`/`double_punctuation`）、**主控型号与摘要/关键词一致性**（`mcu_abstract_body_mismatch`：单向子集检查，正文中的对比/竞品型号不触发误报）、**生成失败占位串**（含 `<!-- TODO:` 小节占位符）、**章节越界标题**等），结果写入 `Evaluation.static_rule_issues`；severity 为 error 时计入修订提前结束条件与总分加罚。LLM `actionable_items` 合并前经 `dedupe_llm_against_static` 去重。`conclusion_intro_gap` 规则 fallback 加元信息 bullet 白名单过滤（排除"本文工作""论文结构"等无关项），避免将章节结构描述误判为研究问题。LLM 评估 prompt 型号不一致检查加对比/竞品排除指引。
- **降级**：LLM 失败、JSON 无效或 schema 校验失败时，返回保守默认分并保留静态问题列表（若有）。

## 运行时序（与控制器对齐）

入口在 `src/controller.py` 的 **`_do_eval`**：

1. **先**对当前 `draft` 执行 `build_global_term_map` + `postprocess_manuscript`（与撰写包一致的全文规整），再调用 `evaluate(...)`，减少仅凭 LLM 肉眼可回收的低级格式/术语问题。
2. 将返回的 `Evaluation` 展示给用户（总分、四维分、反馈与建议列表）。
3. **修订循环**（同级逻辑在 controller，不在本包）：若 `score_total ≥ quality_threshold` → 结束；否则若 **`thesis_mode` 且 `stop_on_rule_pass=true` 且 `static_rule_issues` 中无 `severity == "error"` 项且 `score_total ≥ stop_on_rule_pass_min_score`（默认 8.0）** → 结束（低分保护：不满足最低分则继续修订）；否则若未达 `max_revision_rounds` → `_do_revise` 后再回到 `_do_eval`。

阈值与轮次来自配置：`quality_threshold`、`max_revision_rounds`、`stop_on_rule_pass`、`stop_on_rule_pass_min_score`（毕业论文模式下：静态规则 **error 级别**全部消除 且 LLM 评分达到最低分 时可提前结束修订，warning 不阻止结束）。

## 公开 API（import 约定）

```python
from src.validation import evaluate
from src.validation import _check_thesis_rules  # 供 controller 复用修订后检测
```

## 配置依赖

- `thesis_mode`：见 `config/config.example.yml`。
- **`deepseek_timeout_read_blocking`**（及 `deepseek_timeout_connect` / `deepseek_timeout_read_stream`）：由 `src/llm.py` 读取；**评估**走非流式 `chat_json`，若仍报 `Request timed out` 可调大 **`deepseek_timeout_read_blocking`**（默认 300s）。
- `chat_json` 默认 `max_tokens=8000`（防止评估响应被 API 截断），含 `response_format` 空响应降级与 `_salvage_truncated_json` 截断恢复。详见 `src/llm.py`。
- **`panel_static_summary_max_items`**、**`actionable_fingerprint_include_keywords`**、`stop_on_rule_pass`：由 `src/controller.py` 读取，影响评估后面板展示与 actionable 指纹粒度（本包 `evaluate` 不直接依赖后两项）。
- `Evaluation` 结构：见 `src/models.py`。

## 维护清单（人类 / Agent）

- [ ] 修改 `evaluate()` 签名或语义时：更新本文件「运行时序」「公开 API」，并检查 `src/controller.py` 调用处。
- [ ] 修改静态规则或 prompt 时：更新本文件「能力摘要」，并视情况更新 `src/writing/README.md`（若影响_REVISION_所需信息）。
- [ ] 修改毕业论文**引用位置/摘要**静态检测逻辑时：核对撰写侧 `_fix_citation_position` 与摘要校验是否仍与其一致，必要时双端 README 同更。
