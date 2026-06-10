---
agent_entry: true
slug: lunwencyz-writing
title: 撰写子系统
canonical_paths:
  - src/writing/helpers.py
  - src/writing/term_map.py
  - src/writing/abstract.py
  - src/writing/postprocess.py
  - src/writing/draft_engine.py
  - src/writing/revision_engine.py
  - src/writing/writer.py
  - src/writing/tech_spec.py
  - src/writing/locked_tech_spec.py
  - src/writing/multi_candidate.py
  - src/writing/scope_enforce.py
  - src/writing/revision_helpers.py
  - src/writing/README.md
sync_rule: >
  更改章节生成规则、TechSpec、多候选初稿、scope_enforce（含摘要校验 validate_abstract_against_tech_spec / 章节 scope）、
  revision_helpers（修订建议分章拆分、静态问题 rule_id@rule_version delta、LLM 与静态去重 dedupe_llm_against_static）、子节串行/L3-A、
  `SectionNode` / 可执行大纲字段、`_fix_citation_position`（与评估器引用位置规则对齐）、
  公开函数（draft_manuscript / revise_manuscript / postprocess_manuscript / build_global_term_map）或毕业论文写作约束时，
  必须同步更新本文件中的「能力摘要」「模块职责」「公开 API」「维护清单」，**且必须与拆分后的模块文件一致**：
  `helpers.py`（工具函数）、`term_map.py`（术语映射）、`abstract.py`（摘要生成）、
  `postprocess.py`（后处理+引用重编号）、`draft_engine.py`（初稿引擎）、`revision_engine.py`（修订引擎）。
related_docs:
  - PROGRESS.md
  - src/validation/README.md
---

# 撰写子系统（Writing）

面向 Agent：本文件为**撰写能力**的入口说明；实现以 `canonical_paths` 中的代码为准。

## 模块职责（v2 — 2026-06 重构后）

| 模块 | 职责 |
|------|------|
| `helpers.py` | 纯工具函数与常量（章节规则 `_SECTION_RULES`、`_EXECUTION_PROTOCOL`、关键词格式化、小节对齐、修订产物清洗 `_strip_revision_artifacts`） |
| `term_map.py` | `build_global_term_map` — 返回 `{"term_map": {...}, "stc_dominant": str\|None}`，无全局变量 |
| `abstract.py` | 中英文摘要专用 Prompt、`_generate_abstract_from_body`、多候选择优、校验重试 |
| `postprocess.py` | `postprocess_manuscript`、`reorder_citations_by_first_appearance`、`_finalize_manuscript_postprocess`、引用/标点/越界/感悟清理 |
| `draft_engine.py` | `draft_manuscript`、`_build_draft_prompt`、`_SYSTEM_DRAFT_THESIS`、分块/多候选/子节串行/scope 重试 |
| `revision_engine.py` | `revise_manuscript`、`stubborn_targeted_fix`、`check_revision_compliance`、`_SYSTEM_REVISE_THESIS` |
| `writer.py` | 仅保留 `parse_manuscript_from_md`（从 paper_*.md 反解析） |
| `tech_spec.py` | LLM TechSpec 生成（已有，未动） |
| `locked_tech_spec.py` | 用户锁定层加载与合并（已有，未动） |
| `multi_candidate.py` | 初稿多候选择优（已有，未动） |
| `scope_enforce.py` | Scope 校验与摘要校验（已有，未动） |
| `revision_helpers.py` | 修订建议拆分/顽固问题追踪（已有，未动） |

## 能力摘要

- 按 `WritingPlan` 分章节调用 LLM 生成 Markdown 正文（普通模式与 `thesis_mode` 毕业论文模式分支）。
- **并行 DRAFT / REVISE**：配置 `parallel_draft` / `parallel_revise`（默认 false）。启用后初稿正文各章和修订各章用 `ThreadPoolExecutor` 并行调用 LLM（IO bound，加速比约 4×）。并行模式下 `prev_chapter_excerpt` 为空；串行/并行共用 `_draft_section_body_inner` 核心逻辑。
- 在撰写前可生成 **TechSpec**（`tech_spec.py`），可与用户提供的 **锁定 JSON**（`locked_tech_spec.py`，配置 `locked_tech_spec_path` 或 CLI `--locked-tech-spec`）深合并后作为单一事实源注入各章；合并结果存于 `Manuscript.tech_spec`，**修订**时同样注入 prompt，避免 REVISE 阶段偏离锁定事实。
- **可执行大纲**：`SectionNode` 含 `outline_detail`、`scope_must_include`、`scope_forbidden`、可选树状 `subsections`（规划阶段由 `planner` JSON 填充），`draft_manuscript` / `revise_manuscript` 的 prompt 注入边界、**上一章摘录**与下一章不前置提示；默认仍**整章一次生成**；若开启 **子节串行**则按 `subsections` 多次调用再拼接。
- **初稿多候选**（`multi_candidate.py`，配置 `multi_candidate`）：未分块、未走子节串行的章节可 N 路择优；LLM 评分 prompt 含**全文大纲**、**章节结构边界**（与 `_clean_section_overflow` 语义一致）、**scope**；并对命中越界模式的候选做**确定性分数封顶（≤3）**。建议勿对 s1/s6 启用。
- **Scope 校验与重试**：初稿每章 `clean` 后检查 `scope_must_include` / `scope_forbidden`（`scope_enforce.py`，配置 `scope_validation`）。**修订**路径（`revise_manuscript`）在单章修订产出后、`_fix_citation_position` 前对仍含 scope 约束的章节做同样校验；未通过且 `retry_once` 为真时，将 issues 追加进 prompt **再修订一轮**（与初稿行为对称）。
- **中文摘要硬校验**：`draft_manuscript` 在生成中文摘要正文后调用 `validate_abstract_against_tech_spec`（`scope_enforce.py`）：禁止文内引用编号 `[n]`、与评估器类似的图/表引用类措辞、以及与当前 `Manuscript.tech_spec` / L3-A 术语集明显冲突的摘要表述（如 TechSpec 定 STM32 而摘要写 ESP 等）。未通过且 `scope_validation` 允许重试时，**整段中文摘要再生成一次**（issues 注入修复提示），再生成英文摘要。
- **大纲覆盖**：`outline_scope_overrides` 在撰写前合并进 `SectionNode`。
- **L3-A**：`postprocess_manuscript` 合并 TechSpec 驱动的型号替换与 `term_map`（`l3a_tech_spec_enforce` / `l3a_manual_replacements`）。
- **小节-章主题对齐**（v2 新增）：`_SYSTEM_DRAFT_THESIS` 含「小节归属规则」（5 条强制约束）；`_build_draft_prompt` 对含 `subsections` 的章节自动注入小节主题对齐声明。代码见 `src/writing/draft_engine.py`。
- **英文摘要严格直译**（v2 修改）：`_SYSTEM_EN_ABSTRACT_FROM_ZH` 不再允许压缩/重组/润色，要求逐句一一对应翻译。代码见 `src/writing/abstract.py`。
- **修订后章节顺序保持**（v2 修复）：`revise_manuscript` 单次遍历组装，refs 始终在致谢之后；支持 `reference_before_acknowledgment` 配置。代码见 `src/writing/revision_engine.py`。
- **章节越界清洗**：`postprocess_manuscript` 与初稿/修订路径中的 `_clean_section_overflow` 会删去正文中误写入的**下一章**标题起至章末的内容；支持 `### 第N章`、**末节窗口内**裸「第N.M」行首编号（如 `2.1 标题`）等（末尾比例 `section_overflow_tail_scan_fraction`，默认 0.3）。
- 支持根据评估产生的 `actionable_items` 做 **修订**（`revise_manuscript`）：逐章修订后**统一**执行 `build_global_term_map` + `postprocess_manuscript`（与定稿路径一致，避免「只修局部、全文引用仍乱」）。
- **摘要素材**：正文章节按 `s1、s2…` 动态收集，长章用「首段+尾段」节选，减轻「只取前 500 字漏掉实验/结论」的问题。
- **分块生成**：统一较低温度；最后一块若疑似句末截断，自动尝试一次「仅续写收尾」请求。
- **术语合并**：除 DHT11/DS18B20、YL-69 电容/电阻外，分两组自动统一常见主控：**STM32 家族 vs STC/AT89**、**ESP32 vs ESP8266**（不混并这两组）；若以 STC 为主且文中仍有 `STM32F103` 等，在 `postprocess_manuscript` 内用正则将 `STM32*` 整体替换为主型号。
- **修订**：`revise_manuscript` 中单章 LLM 调用支持**多次重试**与**疑似截断续写**；`_build_revise_prompt` 使用 `revision_helpers.partition_actionable_items` 将 `actionable_items` 分为 **本章修订重点** 与 **其他章/全文性问题**；`revision_helpers` 还提供静态问题差分、可操作建议粗指纹、**`identify_stubborn_issues`**（跨轮次顽固问题识别）、**`dedupe_llm_against_static`**。控制器在每轮 REVISE 开始前执行 `build_global_term_map` + `postprocess_manuscript`。**跨轮次顽固问题**以红色 Panel 展示并注入每章修订 prompt 顶部。

  修订后执行 **`check_revision_compliance`** 自检（≥3 条建议时触发，temperature=0.1，max_tokens=500）：逐条检查 actionable_items 是否已被实际修正。未修正项在同轮内再调一次 `revise_manuscript`（仅传入未修项），**不消耗修订轮次配额**。

   跨轮次顽固问题（`identify_stubborn_issues` 识别）除注入修订 prompt 顶部外，还经过 **`stubborn_targeted_fix`** 专项修复，**三层分流**：**删除类**（代码直接删段落，零 LLM）+ **重写类**（段落级→失败升级节级重写整章 JSON）+ **跳过类**（仅参考文献格式/心得/体会由 postprocess 处理）。P0 防护：anchor 匹配失败不追加到章末、修复后越界清洗+scope 校验（失败回滚）。P1 优化：简短章节跳过（防误清空）、节级升级覆盖跨段问题。
- **引用**：`_fix_citation_position` 先将每行按评估器使用的**句末标点** `。；！？` 与后续至 `[n]` 的模式做对齐调整，再处理紧邻的 `标点 + [n]` 交换，最后处理英文 `.\s*[n]`。`_clean_double_punctuation` 为正则引擎（`\1+` 压缩同类 + 异类保留末标点）。`_check_missing_punct` 启发式补全缺失句末标点（中文>20字非列表行自动加句号）。撰写+修订 system prompt 含「标点规范」段落（禁英文句点/禁漏标点/禁重复标点）。`refs` 章节跳过修正。
- **主控平台（可选）**：配置项 **`mcu_platform_normalize`**（见 `config.example.yml`）且 TechSpec `hardware.mcu.model` 为 STM32* 时，`postprocess_manuscript` 可对**除参考文献外**各节将独立词 ESP8266/ESP32 **替换为该 MCU 型号**（默认 `enabled: false`，避免相关工作误伤）。支持 **`only_section_ids`**（非空则仅处理所列节）、**`skip_section_ids`**（整节跳过）、**`protect_line_substrings`**（行内含任一则整行不替换）；实现为 **按行**扫描替换。
- **摘要字数控制**：中文摘要目标 500-800 字（`_SYSTEM_ABSTRACT_FROM_BODY` / `_SECTION_RULES` / `_DEFAULT_SECTION_WORDS` / `zh_prompt` / 生成 `max_tokens` 1200→1500 / 校验重试 `max_tokens` 1000→1200 统一调整）；`_truncate_abstract` 截断阈值 850→800；评估侧 `abstract_too_short`/`abstract_too_long` 阈值从 200/800 调整为 480/850，`section_min_map` 中 `abstract_zh` 300→500；配置 `thesis_section_words.abstract_zh` 400→600。代码见 `src/writing/` 各模块、`src/validation/evaluator.py`、`config/config.example.yml`、`config/local.secrets.yml`。
- **后处理特殊章节**：`postprocess_manuscript` 对 `refs` / `keywords` 跳过 `_fix_citation_position`、`_clean_section_overflow`、`_remove_personal_remarks` 和 `_truncate_abstract`，仅做术语统一替换；避免参考文献序号被误搬到上一行末尾。
- **按章节控制参考文献注入**：`_ref_limit_for_section` 现在读取 `citation_enabled_sections` 配置（默认 `["s1","s2"]`），仅配置内的章节注入参考文献上下文（s2 50 条全量，其余 10 条）。非引用章节注入 0 条。撰写/修订 prompt 中非引用章节收到 ⚠ **本章禁止引用** 警告；后处理自动删除越界引用；评估端 `citation_out_of_scope` 静态规则兜底检测。用户可按老师要求通过配置灵活调整引用范围。
- **字数控制体系**：目标总量 25000-30000 字（`thesis_target_words_min/max`）；各章目标由 `thesis_section_words` 配置管理（s1:2500 / s2:4000 / s3:5500 / s4:5500 / s5:4000 / s6:1500 / abstract_zh:400）；`max_tokens` 系数 ×1.8 控制生成篇幅；评估侧 `section_min_map` 做下限检查。分块生成 `_CHUNK_TARGET_WORDS=1500` 配合 ×1.8 系数。
- **s5 测试覆盖清单自动注入**：`_build_draft_prompt` / `_build_revise_prompt` 现在通过 `sensor_checklist_sections` 配置项决定在哪些章节注入「传感器测试覆盖清单」（默认 `["s5"]`）。用户可按框架结构调整。硬件越界检测也改为读 `hardware_overflow_check_sections` 配置（默认 `["s2"]`，留空不检查）。撰写 prompt 主线行从硬编码改为从 `plan.outline` 动态拼接实际章节标题。
- **术语锁定快照（跨章一致性预防）**：`_build_term_lockdown_snapshot(tech_spec)` 从 TechSpec 抽取主控/传感器/执行器/通信模块型号，生成「术语一致性锁定清单」注入每章修订 prompt，告知 LLM 哪些型号不可更改——从源头预防逐章独立修订导致的前后端型号矛盾（如 s3 改 OLED → s5 仍写 LCD1602）。
- **term_map 动态扩展**：`build_global_term_map` 新增矛盾组4（显示模块：OLED/LCD1602/LCD2004/12864 等互斥型号自动统频）+ 矛盾组5（TechSpec `candidate_models` 字段驱动，用户可在锁定 JSON 中为每个传感器/执行器/通信模块指定备选名称，自动纳入替换字典）。
- **引用跨行预处理**：`_fix_citation_position` 在单行扫描前新增跨行预处理（`。\\n[1]` → `[1]。\\n`），覆盖 LLM 生成时因换行导致引用与句末标点分离的漏网模式。
- **Prompt 工程化架构（Role-Constraints-Protocol）**：参照 `doc/README.md` 审稿人双重身份模式重构五个系统 prompt。`_SYSTEM_REVISE_THESIS` 从 6 行扩展为 #Role/#Task/#Constraints(修订阈值/引用格式/语言风格/术语规则/输出格式)/#Protocol 四层结构，引入「最小干预原则」和 Part1/Part2 输出格式；`_SYSTEM_DRAFT_THESIS` 加去 AI 味规则块（禁"首先/其次/然后/最后"流水账）；`_SYSTEM_ABSTRACT_FROM_BODY` 升级为五层结构；`_SYSTEM_EN_ABSTRACT_FROM_ZH` 从 7 行扩展到四层（含去中式英语）。提取共享 `_EXECUTION_PROTOCOL` 常量（"仅内部执行，严禁输出"）。代码见 `src/writing/` 各模块、`src/validation/evaluator.py`。
- **章节规则配置覆盖**：新增 `_get_section_rule(section_id)` → 优先读 `config.override_section_rules`（按 section_id 映射的用户自定义规则），未配置回退 `_SECTION_RULES`。3 处调用点（初稿构建 / 修订 prompt / 顽固修复）已统一替换。用户可在 `local.secrets.yml` 中按导师/学校框架编写 s3/s4/s5 等章节的特殊规则，彻底消除默认规则与自定义框架的指令冲突。配置示例见 `config/config.example.yml`。
- **章节标题自拟**：规划阶段 `_SYSTEM_THESIS` 要求 s2～s5 标题由 LLM 根据课题自拟（须以「第X章」开头），不再使用泛称；s1 可选择饱满型/简要型引言，决定 s2 的展开深度。**饱满型**：s1 含研究现状，s2 变为「系统总体设计」（需求+方案+选型）；**简要型**：s1 仅目的意义，s2 全面展开现存工作分析。嵌入式项目 s3/s4 可按硬件/软件拆分。
- **强制小节结构**：`_SYSTEM_DRAFT_THESIS` 标题格式从建议升级为强制（正文必须含 ###/#### 小节）；`_executable_outline_prompt_section` 措辞「建议」→「强制」；`_build_draft_prompt` 对含 subsections 的章追加小节清单。`_ensure_subsections_present()` 后处理兜底——检测缺失小节自动追加 `> **待撰写：xxx**` 可见占位符（仅 DRAFT 阶段运行，修订路径跳过）。代码见 `src/writing/` 各模块。
- **修订产物清洗**：`_strip_revision_artifacts()` 清除 LLM 修订输出中的 Part 1/Part 2/修改日志/修订说明标签，位于 `_revise_one_section_body` return 前，串行/并行路径全覆盖。`_SYSTEM_REVISE_THESIS` 输出格式改为直接输出正文、禁止任何标记文字、自动展开 `> **待撰写：xxx**` 可见占位符。代码见 `src/writing/` 各模块。
- **关键词嵌入摘要**：`Manuscript` 新增 `keywords_zh_text`/`keywords_en_text` 字段（独立存储）；`to_markdown` 将关键词拼接到对应摘要末尾，不再生成独立 `## 关键词` 节；`_format_keywords_text()` 返回纯文本对。修订循环中关键词同步保留不变。代码见 `src/models.py`、`src/writing/writer.py`。
- **参考文献顺序可配**：`reference_before_acknowledgment` 配置（默认 false=致谢→参考文献；true=参考文献→致谢），仅影响输出顺序，不影响 LLM 引用权限。`_SYSTEM_DRAFT_THESIS` 一致性规则加「致谢不得有新引用」约束。代码见 `src/writing/` 各模块、`config/config.example.yml`。

## 运行时序（与控制器对齐）

入口在 `src/controller.py`：

1. **`_do_draft`**：调用 `draft_manuscript(...)` → LLM TechSpec + 可选用户锁定合并；按章撰写（可选多候选择优）→ 毕业论文模式下由 controller 附加封面/目录文本。
2. **`_do_revise`**：调用 `revise_manuscript(...)`，其在内部通过 `_finalize_manuscript_postprocess` 完成 **术语映射构建 + `postprocess_manuscript`**；controller 仅根据返回的 `term_map` 提示用户（不再重复跑一遍后处理）。
3. **`_do_eval`**：**先** `build_global_term_map` + `postprocess_manuscript` 规整当前稿，再调用验证包 `evaluate`（见 `src/validation/README.md`）。
4. **`_do_done`**：再次 `build_global_term_map` + `postprocess_manuscript`，写出最终 `paper_*.md`。

评估与打分的闭环见 `src/validation/README.md`，本包不负责评分。

## 公开 API（import 约定）

应用代码应通过包入口导入，勿依赖模块路径细节：

```python
from src.writing import (
    draft_manuscript,
    revise_manuscript,
    postprocess_manuscript,
    build_global_term_map,
    parse_manuscript_from_md,      # 从 paper_*.md 反解析 Manuscript
    check_revision_compliance,     # 修订后逐条自检
    stubborn_targeted_fix,         # 顽固问题针对性修复
)

# 修订返回 (Manuscript, term_map)，便于日志或 UI；无建议时第二项为 {}
manuscript, term_map = revise_manuscript(manuscript, plan, store, items)

# 反解析：用于调试/单步评测
manuscript = parse_manuscript_from_md("outputs/paper_xxx.md", plan)

# 修订自检：检查 actionable_items 是否已修正，返回 (fixed, unfixed)
fixed, unfixed = check_revision_compliance(manuscript, plan, actionable_items)
```

## 配置依赖

- `thesis_mode`、`citation_style`、`locked_tech_spec_path`、`multi_candidate`（及环境变量 `LUNWENCYZ_LOCKED_TECH_SPEC`）等：见 `config/config.example.yml` 与 `config/locked_tech_spec.example.json`。**Planning / TechSpec / 多候选评分**等非流式 JSON 走 `src/llm.py` 的 **`deepseek_timeout_read_blocking`**（默认 300s）；长文撰写为流式，使用 **`deepseek_timeout_read_stream`**（详见配置中 `deepseek_timeout_*`）。
- 数据模型：`Manuscript`、`WritingPlan` 等在 `src/models.py`。

## 维护清单（人类 / Agent）

- [ ] 改名或增减公开函数时：改 `src/writing/__init__.py` 与本节「公开 API」。
- [ ] 调整 `_do_draft` / `_do_revise` 行为时：改 `src/controller.py` 与本节「运行时序」。
- [ ] 改 `scope_enforce.py`（章节 scope、摘要校验、配置项语义）时：同步本节「能力摘要」与 `config/config.example.yml` 中相关注释（若有）。
- [ ] 改 `postprocess.py` 中 `_fix_citation_position`、`_clean_*` 系列时：同步本节「能力摘要」；若评估侧引用/摘要规则随之变化，同步 `src/validation/README.md`。
- [ ] 改 `draft_engine.py`（初稿 prompt/分块/多候选/子节串行/小节对齐声明）或 `revision_engine.py`（修订 prompt/顽固修复/自检/章节组装顺序）时：同步本节「能力摘要」与对应模块职责表。
- [ ] 改 `abstract.py`（中英文摘要 prompt / `_generate_abstract_from_body` / 校验重试）时：同步本节「能力摘要」；英文摘要 prompt 改后需确认「严格直译」约束未被弱化。
- [ ] 改 `term_map.py`（`build_global_term_map` 返回格式）时：同步本文件「模块职责」表及 controller 调用方。
- [ ] **知识库**：凡触及本文件 `sync_rule` 所述代码路径，须在**同一变更**内更新本 README（及根目录 `README.md` 中若有交叉描述），避免实现与文档长期漂移；重大行为变更可同时在 `PROGRESS.md` 记一笔。
