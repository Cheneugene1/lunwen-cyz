---
agent_entry: true
slug: lunwencyz-progress
title: 项目进度总览
canonical_paths:
  - PROGRESS.md
sync_rule: >
  完成功能、切换当前任务、遇到阻塞或解除阻塞时，更新本文件三大区块（已完成 /
  进行中 / 阻塞）；重大架构变更时顺带检查 src/writing/README.md、
  src/validation/README.md 与 src/diagnosis/README.md 的 sync_rule 是否仍成立；
  凡变更已记入「已完成」的实现，应在同一迭代中核对对应子包 README 是否仍与代码一致（知识库不漂）。
related_docs:
  - README.md
  - src/writing/README.md
  - src/validation/README.md
  - src/diagnosis/README.md
---

# 项目进度（PROGRESS）

面向 Agent：把本文件当作**工程状态的真实来源**；描述应与仓库现状一致。

## 已完成

- **架构重构 — writer.py 拆分**：3461 行单体拆为 7 个模块：`helpers.py`（纯工具/常量/~560行）、`term_map.py`（术语映射/~230行）、`abstract.py`（摘要生成/~310行）、`postprocess.py`（后处理+引用重编号/~380行）、`draft_engine.py`（初稿引擎/~490行）、`revision_engine.py`（修订引擎/~580行）、`writer.py`（仅保留 `parse_manuscript_from_md`/~75行）。`__init__.py` 重导出保持 Public API 完全兼容。代码见 `src/writing/` 各模块。
- **消除 `_MCU_STC_DOMINANT` 模块级全局变量**：`build_global_term_map()` 返回 `{"term_map": {...}, "stc_dominant": str|None}`，并行 DRAFT/REVISE 下不再有互相覆盖隐患。controller 三处调用点+`postprocess_manuscript` 签名已适配。代码见 `src/writing/term_map.py`、`src/controller.py`。
- **抽离 presenter.py**：controller `_do_eval` 中 ~110 行 Panel 渲染逻辑迁入 `src/presenter.py`（`render_eval_panel` / `render_qa_panel` / `render_stubborn_panel`），controller 聚焦流程编排。代码见 `src/presenter.py`。
- **配置单例测试隔离**：`src/config.py` 新增 `reset_config()` 函数，测试中可重置缓存避免状态污染。代码见 `src/config.py`。
- **v4 全流程问题修复（2026-06-02）**：
  - **修订后参考文献顺序错乱**：`revise_manuscript` 三段式组装改为单次遍历，确保 refs 始终在致谢之后。同时补充 `reference_before_acknowledgment` 配置在修订路径中的支持。代码见 `src/writing/revision_engine.py`。
  - **顽固修复丢失关键词**：`stubborn_targeted_fix` 重建 Manuscript 时补齐 `keywords_zh_text`/`keywords_en_text`/`thesis_title` 字段。`_format_keywords_text` 增加空关键词防御日志。代码见 `src/writing/revision_engine.py`、`src/writing/helpers.py`。
  - **英文摘要非直译**：`_SYSTEM_EN_ABSTRACT_FROM_ZH` 从"学术编辑 + 自由润色"重写为"严格句对句翻译"，消除 compress/reorganize/embellish 等模糊授权。代码见 `src/writing/abstract.py`。
  - **小节-章节主题对齐强化**：`_SYSTEM_DRAFT_THESIS` 新增「小节归属规则」段（5 条）；`_build_draft_prompt` 对含 subsections 的章节自动生成小节主题对齐声明。代码见 `src/writing/draft_engine.py`。
  - 问题调查报告与修复方案存档：`doc/reports/2026-06-02_问题调查报告_v4全流程.md`、`doc/reports/2026-06-02_修复方案_v4全流程.md`。
- 撰写与验证代码已分包：`src/writing/`（`writer.py`、`tech_spec.py`、`locked_tech_spec.py`、`multi_candidate.py`、`scope_enforce.py`）、`src/validation/`（`evaluator.py`）；对外通过 `src.writing`、`src.validation` 导入。
- 建立三份 Agent 入口文档：本文件、`src/writing/README.md`、`src/validation/README.md`（均含 `agent_entry` 与 `sync_rule`  front matter）。
- **撰写模块优化**：`revise_manuscript` 返回 `(Manuscript, term_map)` 且在内部执行 `_finalize_manuscript_postprocess`；摘要用章节首尾节选、分块统一低温+截断续写、越界截断启发式收紧、摘要图表替换为中性短语、非 thesis 参考文献 `et al.`、传感器术语频次并列时跳过统一、引用支持 `. [n]`、扩展个人感悟句型（代码见 `src/writing/writer.py`，说明见 `src/writing/README.md`）。
- **跨轮提分相关**：主控术语分组合并（STM32/51 与 ESP）、STC 主导时 `STM32*` 正则后处理；`revise` 重试+截断续写；`_do_eval` 前强制全文规整；评估静态规则增加摘要/关键词 vs 正文 MCU 一致性与占位串检测（见 `src/validation/evaluator.py`）。
- **L1 用户锁定 TechSpec**：`locked_tech_spec.py` + 配置/CLI；与 LLM TechSpec 深合并（锁定覆盖同键），`Manuscript.tech_spec` 贯穿撰写与修订 prompt；`postprocess_manuscript` 文档中标注 **L3-C** 成稿后道。
- **可执行大纲（SectionNode）**：`outline_detail` / `scope_*` / `subsections`；撰写与修订 prompt 注入边界 + 上一章摘录 + 后章边界；`outline_to_markdown` 可审阅。
- **初稿多候选**：`multi_candidate.py`（配置 `multi_candidate`）。
- **Scope 校验重试 + 子节串行 + outline 覆盖 + L3-A 型号**：`scope_enforce.py`；配置 `scope_validation`、`subsections_sequential_draft`、`outline_scope_overrides`、`l3a_*`；`postprocess_manuscript` 合并 L3-A 与 `term_map` 并传入 `tech_spec`。
- **运行诊断基础设施**：`src/diagnosis/`（`RunRecorder` → `outputs/run_<session>.jsonl`；`analyzer`/`report` 规则摘要；`python -m src.diagnosis <path>`）。配置项 `diagnostics_enabled`（`config.example.yml`）。
- **Phase 2.5 修订稳态**：默认 `max_revision_rounds: 2`；`stop_on_rule_pass`（`thesis_mode` 下 **error 级别**静态项清空则停，warning 不阻停）；`Evaluation.static_rule_issues`；`_clean_section_overflow` 扩展 `### 第N章` 与末尾窗口裸标题（`section_overflow_tail_scan_fraction`）。
- **Phase 3 写端与评估对齐 + 文档**：中文摘要 `validate_abstract_against_tech_spec`（TechSpec/L3-A、禁 `[n]`、图表层）+ 配置允许时一次整段重生成；`revise_manuscript` 单章修订后同样跑 scope 校验并重试一轮；`_fix_citation_position` 按评估侧句末标点 `。；！？` 与 `[n]` 位置规则对齐。根目录 `README.md` 增加「知识库维护约定」；`src/writing` / `src/validation` README 的 `sync_rule`、能力摘要与维护清单已同步。
- **Phase 4 评估契约与修订可观测性**：`Evaluation.static_rule_issues` 为 `StaticRuleIssue`（**rule_id** + **rule_version** + **rule_category** + **severity** + message）；控制器展示硬规则快照、按 **rule_id@rule_version** 跨轮差分及 `actionable_items` **【标签】|章节** 粗指纹差分（可选 `actionable_fingerprint_include_keywords`）；面板快照条数 `panel_static_summary_max_items`；可选 **`mcu_platform_normalize`**（`only_section_ids` / `skip_section_ids` / `protect_line_substrings`，按行保护）在 postprocess 中按 TechSpec 统一 ESP 词为 MCU 型号。
- **Phase 4.5 静态–LLM 建议对齐**：`evaluate` 合并 LLM `actionable_items` 前经 `dedupe_llm_against_static` 去掉与静态问题重复的条目；静态 **error 数量 ≥3** 时对 `score_total` 加罚（每 error 0.3，下限 3.0）；静态检查日志区分 error / warning 计数。
- **LLM 客户端超时**：`src/llm.py` 非流式请求默认 **read 300s**（`deepseek_timeout_read_blocking`），避免 DeepSeek Pro 等模型在规划 / TechSpec / **评估 `chat_json`** 场景因旧 90s 限制触发 `Request timed out`；流式为 `deepseek_timeout_read_stream`（chunk 间隔）。
- **评估 JSON 解析修复**：`chat_json` 增强：P0 强制 `response_format={"type": "json_object"}` 约束 LLM 输出；P1 `_try_parse` 三层兜底（直接解析 → 代码块提取 → `{...}` 子串提取）；P2 `response_format` 偶发空响应→自动降级为无约束调用；P3 `max_tokens` 4096→8000 防止评估响应截断；P4 `_salvage_truncated_json()` 截断 JSON 字符级扫描补全 `}` `]`。解决评估阶段 JSON 解析失败导致降级 5.0 分和废话建议的根因。
- **后处理特殊章节修复**：`postprocess_manuscript` 对 `refs` / `keywords` 跳过 `_fix_citation_position` 等正文规则，避免参考文献序号被正则误搬上行末尾。新增 `_truncate_abstract` 摘要规则截断（>850 字按末句截断），摘要 `max_tokens` 从 1500 降至 1000。
- **论文框架对齐学校规范**：s2～s5 标题由 LLM 自拟（须以「第X章」开头），s1 区分饱满型/简要型引言；按章节控制参考文献注入量（`_ref_limit_for_section`），s2 全量 50 条，s4/s6 不注入，大幅减少 token 浪费。
- **分阶段调试与独立评测**：`main.py` 新增 `--phase`/`--plan`/`--paper` 参数，支持跑到指定阶段停止或对已有论文独立评测；`controller.run_to_phase()` 支持中途续跑；`writer.parse_manuscript_from_md()` 从 paper_*.md 反解析 Manuscript。
- **跨轮次顽固问题追踪**：`revision_helpers.identify_stubborn_issues()` 对比静态 rule_id 集合与 actionable 粗指纹，识别跨轮未消解项；控制器每轮评估后计算顽固问题并以红色 Panel 展示，下轮修订 prompt 顶部注入 `【顽固问题 - 必须优先处理】`。`revise_manuscript` 新增 `stubborn_issues_md` 参数。
- **同轮修订自检**：`check_revision_compliance()` 在修订后逐条检查 actionable_items 是否已修正（temperature=0.1, max_tokens=500）。未修正项在同轮内再调 `revise_manuscript` 重修一次，不消耗修订轮次配额。控制器 `_do_revise` 集成了自检→重修订闭环。
- **论文框架参照例文完善**：饱满型引言→s2 改为「系统总体设计」而非技术综述；嵌入式项目 s3/s4 可按硬件/软件拆分，s5 建议「系统实现及调试」。
- **大纲评分门禁**：PLAN 阶段后自动执行硬规则检查（28 条规则，不依赖 LLM）+ LLM 语义**五维评分**（logic 0.30 / content_depth 0.25 / feasibility 0.25 / format_compliance 0.15 / novelty 0.05），不达标时自动修订大纲最多 2 轮。**每次评分后支持人工交互修订**。配置项 `outline_evaluation_enabled`、`outline_evaluation_threshold`、`outline_evaluation_max_revision_rounds`。核心代码见 `src/planner.py`（`_check_outline_hard_rules`、`evaluate_outline`、`revise_outline`），控制器 `_do_outline_check` + `_ask_outline_feedback`。
- **stop_on_rule_pass 低分保护**：`stop_on_rule_pass_min_score`（默认 8.0）阻止静态规则全过但 LLM 评分极低时提前退出修订，要求同时满足 error=0 且 score ≥ min_score 才可按 `stop_on_rule_pass` 停止。
- **字数体系校准**：目标总量上调至 25000-30000 字，各章节目标调整为 s1:2500 / s2:4000 / s3:5500 / s4:5500 / s5:4000 / s6:1500 / abstract_zh:800；`max_tokens` 生成系数从 ×2.5 降至 ×1.8（4 处）；`_CHUNK_TARGET_WORDS` 1200→1500；评估侧 `section_min_map` 同步上调。涉及 config.example.yml / local.secrets.yml / writer.py / evaluator.py。config.example.yml 中 thesis_section_words 键名修正（introduction→s1 等），修复键名不匹配导致 fallback 问题。
- **独立评测文献库自动加载**：`_eval_paper_direct` 从 `paper_{sid}_vN.md` 文件名提取 session_id，自动加载 `session_{sid}.db`；文献池持久化默认启用（`db_path` 为目录时 controller 自动拼 `session_{id}.db`）。`--no-db` 可禁用。
- **顽固问题专项修复**：`stubborn_targeted_fix()` 三层分流：**删除类**（`_stubborn_hard_delete` 代码直接删段落，零 LLM）+ **重写类**（段落级→失败升级节级，`_REWRITE_SYSTEM_SECTION` 输出整章 JSON，max_tokens=8000）+ **跳过类**（仅参考文献格式/个人感悟/心得体会已由 postprocess 处理；`引用位置` 不再跳过以免漏修标点问题）。P0 修复：anchor 匹配失败不追加到章末（破坏结构）；顽固修复后跑越界清洗+scope 校验，失败则回滚。P1 修复：简短章节跳过（避免被清空后误操作）；节级重写补全段落级无法处理的跨段问题。代码见 `src/writing/writer.py`。
- **质量阈值提升**：`quality_threshold` 7.5→9.0（config + controller 默认值），推动论文向高分迭代。
- **DRAFT / REVISE 并行化**：`parallel_draft` / `parallel_revise`（默认 false，配置开启）。初稿正文各章和修订各章用 `ThreadPoolExecutor` 并行调用 LLM（IO bound），加速比约 4×。并行模式下 `prev_chapter_excerpt` 为空。`_draft_section_body_inner` 为串行/并行共用核心。
- **TechSpec token 上限**：`generate_tech_spec` 的 `max_tokens` 3000→6000，防止大 JSON 响应截断导致 TechSpec 生成失败（与评估阶段 4096→8000 同理）。
- **大纲修订 token 上限**：`revise_outline` / `revise_outline_fix_errors` 的 `max_tokens` 4000→8000，防止完整大纲 JSON 响应截断导致的静默修复失败（9 章节 × bullets/outline_detail/scope 轻松超 4000 token）。
- **J 类文献无期刊名过滤**：`cull_poor_quality` 新增规则：J 类文献无 venue 且非 pinned → 直接过滤，杜绝参考文献列表中出现 `（期刊未知）`。
- **无作者文献过滤 + 格式化去佚名**：`_is_poor` 新增规则：无作者文献一律过滤（合并原"佚名作者"+"无作者无DOI"规则）。`_format_authors_thesis` 空列表不再输出"佚名"。
- **冗余标点清理**：`postprocess_manuscript` 新增 `_clean_double_punctuation`（同类+异类双标点→单标点，如 `，。→。`），在引用修正之后执行；`_fix_citation_position` swap 后也执行一道。`to_markdown` 去掉了 `---` 分隔线。
- **作者名合法性检查**：`_is_poor` 新增规则：所有作者名长度<2 或纯数字 → 视为无效文献过滤（杜绝 `"2[J]."` 假作者条目）。
- **相关性过滤收紧**：`_relevance_score` 保底分 0.1→0.02；`min_ref_relevance_score` 0.05/0.08→0.15；`cull_poor_quality` 新增关键词命中数检查（标题/摘要至少命中 2 个 plan.keywords 才保留）。
- **关键词容错拆分**：`generate_plan` 中 LLM 偶将多个关键词写入单字符串（如 `"STM32；温湿度；DHT11"`），新增加按 `；;，,` 自动拆分逻辑（中/英文关键词均适用），避免 `KEYWORDS_TOO_FEW` / `KEYWORD_HAS_SEP` / `KEYWORDS_EN_MISMATCH` 误报。
- **论文文件名加关键词+日期**：`_do_done` 输出文件名从 `paper_{id}_v{N}.md` → `paper_{id}_{取前2关键词}_{日期}_v{N}.md`（如 `paper_cb3a12f7_STM32温湿度监控_20260517_v3.md`），session_id 保持前缀不影响 `_eval_paper_direct` 正则匹配。
- **文献池不足补救（B+C 组合）**：检索循环结束后若 pool < `min_references`，先走 **C 泛化检索**（`run_expanded_search`：从 `_KEYWORD_EXPANSION` 映射表生成泛化英文 query，打 OpenAlex + Crossref + Semantic Scholar，per_page=5）；再走 **B 兜底放宽**（`cull_poor_quality` 新增 `min_refs_to_keep` 参数：关键词命中≥1 替代≥2，救回的文献标记 `low_confidence=True`，在 `as_context_text` 中显示 `[低信]` 标记）。`_KEYWORD_EXPANSION` 硬编码映射表覆盖 STM32/DHT11/ESP32/Arduino/传感器/通信协议/农业灌溉等常见嵌入式选题 30+ 键。代码见 `src/retriever.py`（`_KEYWORD_EXPANSION`、`_generate_expanded_queries`、`run_expanded_search`）、`src/ref_store.py`（`cull_poor_quality.min_refs_to_keep`、`as_context_text` 低信标记）、`src/controller.py`（`_do_search` 集成）。
- **Semantic Scholar API 检索源**：新增 `_search_semantic_scholar()`：调用 S2 语义搜索 API（SPECTER embedding 模型），`run_search()` / `run_expanded_search()` 均已集成 per_page=8/5，与 OpenAlex/Crossref 并列请求。S2 的 CS/EE 论文覆盖率（IEEE/ACM 顶会顶刊）和 venue 元数据完整度均优于 OpenAlex，语义搜索可命中关键词变体（如 `"microcontroller"` 匹配 `"ARM Cortex-M"`）。配置项 `semantic_scholar_api_key`（`config.example.yml`），无 key 仍可用（rate limit 低但够用）。代码见 `src/retriever.py`。
- **中→英关键词命中映射 + venue 放宽**：`cull_poor_quality._is_poor` 移除 venue 缺失硬过滤（无DOI+无venue / J类无venue → 保留但标记 `low_confidence`），避免嵌入式/交叉学科 Scopus 覆盖率低误杀正常论文。`_count_hits` 新增 `_KW_EN_MAP` 中文→英文同义子串映射（50 键），英文标题/摘要中的 `"microcontroller"` 可命中中文关键词 `"单片机"`。
- **运行时关键词同义词 LLM 生成**：新增 `_build_synonym_map(keywords)` ——检索阶段用一次极轻量 LLM 调用（temperature=0.1, max_tokens=1024）将中文关键词翻译为英文学术同义词子串列表，与硬编码 `_KW_EN_MAP` 合并后注入 `cull_poor_quality` 的 `_count_hits`。LLM 失败自动回退硬编码映射。解决硬编码映射无法覆盖非嵌入式选题（化学/文学/经济等）的通用性问题。`cull_poor_quality` 新增 `synonym_map` 参数，controller `_do_search` 在清洗前调用。代码见 `src/ref_store.py`、`src/controller.py`。
- **s5 测试章节传感器覆盖清单**：`_build_draft_prompt` 和 `_build_revise_prompt` 新增 `_build_sensor_checklist()`：从 TechSpec 文本中解析传感器列表（型号/类型/接口），对 s5 自动注入「传感器测试覆盖清单」要求每个传感器有独立测试子节，解决实验验证遗漏硬件的问题。代码见 `src/writing/writer.py`。
- **修订后新生问题检测**：`_do_revise` 结束后跑 `_check_thesis_rules` 对比修订前静态规则数，若新增则告警；另做轻量引用一致性检查（正文 `[N]` 序号 vs 文献池上限），发现不存在的引用序号即时提示。代码见 `src/controller.py`（`_do_revise` 尾部两段检测）。
- **引用序号 vs 文献池静态规则**：`_check_thesis_rules` 新增 `citation_missing_ref`（error）：扫描全文 `[N]` 序号，超出文献池范围的直接报错（如文献池仅 5 条但正文引用了 `[7]`、`[12]`）。`_RULE_META` 新增条目，`src/validation/__init__.py` 导出 `_check_thesis_rules` 供 controller 复用。代码见 `src/validation/evaluator.py`。
- **大纲评估流程优化**：`evaluate_outline` 内容级硬 error（S1 forbidden/S6 forbidden/标题过长等）不再阻断 LLM 语义评分——仅结构级 error（缺失章节/ID重复）仍阻断；`_do_outline_check` 合并「硬修复+语义修复」为单次 `revise_outline` 调用（`actionable_items` 同时含硬规则提示和 LLM 语义建议），移除了 `revise_outline_fix_errors` 单独路径。代码见 `src/planner.py`、`src/controller.py`。
- **摘要字数阈值下调**：`abstract_too_short` 400→200 字符（约 100 字），`abstract_too_long` 1200→800；配置 `thesis_section_words.abstract_zh` 800→400。代码见 `src/validation/evaluator.py`、`config.example.yml`、`local.secrets.yml`。
- **标点处理全面升级**：`_clean_double_punctuation` 从 16 组硬编码替换升级为正则引擎（`\1+` 压缩同类 + 异类保留末标点）。新增 `_check_missing_punct`：启发式补全缺失句末标点（中文>20字、非列表、无公式表格的行末尾自动加句号），集成进 `postprocess_manuscript`。evaluator 新增 3 条静态规则：`missing_punct`（warning）、`mixed_punctuation`（warning，中英文标点混用）、`double_punctuation`（warning）。撰写+修订系统 prompt 新增「标点规范」段落（禁止英文句点、禁止漏写标点、禁止重复标点）。代码见 `src/writing/writer.py`、`src/validation/evaluator.py`。
- **生成器字数一致性+容量优化**：六处摘要字数统一为 300-500 字（`_SECTION_RULES`/`_generate_abstract_from_body`/`_DEFAULT_SECTION_WORDS`/`section_min_map`）；初稿/修订/multi-candidate `max_tokens` 天花板 6000→8000，子节串行 3500→5000；摘要生成 `max_tokens` 1000→1200；`_build_revise_prompt` 新增字数提示；s2 `special_rule` 加 3500-4500 字压缩指令；`_clean_section_overflow` 截断时回溯到最后一个完整句号结尾。代码见 `src/writing/writer.py`、`src/validation/evaluator.py`。
- **评估器 prompt 重构（数据占位豁免+清单瘦身+禁实验建议+显式加权）**：`_SYSTEM_EVAL_THESIS` 新增「数据占位符豁免」段落（`[实测数据]`/`[待测]`/`TBD` 等不扣分）、hard logic/alignment 维度数据豁免说明、总分显式加权公式 `structure×0.30+logic×0.30+language×0.25+alignment×0.15`、强制检查清单从 10 项瘦身为 3 项（删静态已覆盖的7项）、禁止输出需人工实验的修订建议。代码见 `src/validation/evaluator.py`。
- **7.5→8.5 冲刺优化**：`postprocess_manuscript` 对 `abstract_zh`/`abstract_en` 强制删除引用标记；`_format_keywords_section` 英文关键词改为全大写保留+其余小写；英文摘要 prompt 改为 MUST 200-300 words + `max_tokens` 1000→1500；`_SYSTEM_DRAFT_THESIS` 加「自身目标不引用」规则；postprocess 扫描删除 `本文/本系统.{0,15}[n]` 误加引用；`_build_draft_prompt` 对 s6 自动注入 s1 bullets 中的研究问题清单；`_SECTION_RULES` 新增 s3/s4 章节规则（选型论证+流程图）；`_is_poor` 加标题异常检测（`CoreID`/`undefined`/全大写无空格等）。`_check_thesis_rules` 新增 `conclusion_intro_gap` 静态规则。
- **修订轮次提升 + s1 贡献总结 + s2 硬件越界检测**：`max_revision_rounds` 2→3（`local.secrets.yml` + `config.example.yml`）；`_SECTION_RULES` 新增 s1 规则（末尾贡献总结+禁展开后续章内容）；`_build_revise_prompt` 对 s2 增加段落级硬件特征词密度检测（`PCB/引脚/消抖电路`等关键词≥3次时在修订 prompt 顶部注入越界警告）。代码见 `src/writing/writer.py`、`config/local.secrets.yml`、`config/config.example.yml`。
- **Prompt 全局思维注入（全文主线+跨章上下文+s5规则+术语统一）**：`_build_draft_prompt` 顶部注入全文主线（50 token）；新增 `_build_chapter_chain_context()` 为每章注入前章摘要+后章要点+研究问题牵引；新增 `_filter_tech_spec_for_section()` 按章节裁剪 TechSpec；`_SECTION_RULES` 新增 s5 规则；`_SYSTEM_DRAFT_THESIS` 术语以 TechSpec 为准。代码见 `src/writing/writer.py`。
- **检索模块全面优化（S2降级+筛选合并+容量翻倍）**：`_search_semantic_scholar` 403 时自动降级为无 Key 模式重试（恢复 S2 最佳检索源）；`min_ref_relevance_score` 0.15→0.03（消除 `_filter_and_score` 与 `cull_poor_quality` 的双重截杀，让更智能的关键词+同义词映射承担实质筛选）；`_search_openalex` per_page 8→15、`_search_crossref` rows 8→15、`_search_semantic_scholar` per_page 8→25（主检索容量翻倍）；泛化检索 per_page/rows 5→10/10/12。代码见 `src/retriever.py`、`config/config.example.yml`。
- **修订跨章一致性 + 结论误报 + 引用跨行 + 检索首轮 + 关键词格式 五连修复**：修订时注入术语锁定快照（`_build_term_lockdown_snapshot`，从 TechSpec 提取型号清单并注入每章修订 prompt）；`build_global_term_map` 新增显示模块冲突组（OLED/LCD1602/LCD12864 等）+ TechSpec 动态 `candidate_models` 校验；`conclusion_intro_gap` fallback 加元信息 bullet 白名单（排除"本文工作""论文结构"等无关项避免虚假告警）；`_fix_citation_position` 新增跨行中文标点预处理（`。\\n[1]` 模式）；`_do_search` while 条件 `search_round==0` 时无条件进入首轮检索（避免旧 session DB 锁死文献池）；planner `_SYSTEM_THESIS` 英文关键词格式规范（除专有名词外首字母大写其余小写）。代码见 `src/writing/writer.py`、`src/validation/evaluator.py`、`src/controller.py`、`src/planner.py`。
- **大纲用户输入 LLM 解析 + 关键词/检索词反抄送约束**：`update_plan_from_user` 集成 LLM 语义解析（`_OUTLINE_PARSE_USER_SYSTEM_PROMPT`），支持用户粘贴任意格式大纲（数字编号/中英混排/内联注释），LLM 自动映射到标准 section_id 并提取 bullets/outline_detail；LLM 失败时回退旧版字符串解析器。`_SYSTEM_THESIS` 新增关键词提炼约束（严禁抄送原始输入全文；必须拆分为独立学术名词）+ search_queries 约束（严禁中文原始输入拼接 "survey"；必须提炼独立英文检索短语）。代码见 `src/planner.py`。
- **中文摘要字数全面调整 300-500→500-800 字**：`_SYSTEM_ABSTRACT_FROM_BODY` / `_SECTION_RULES` / `_SYSTEM_DRAFT_NORMAL` / `zh_prompt` 等 9 处提示/模板统一为 500-800 字；生成 `max_tokens` 1200→1500、校验重试 `max_tokens` 1000→1200；`_DEFAULT_SECTION_WORDS` abstract_zh 400→600；`_truncate_abstract` 截断阈值 850→800；评估侧 `abstract_too_short` 200→480、`abstract_too_long` 800→850、`section_min_map.abstract_zh` 300→500；配置 `thesis_section_words.abstract_zh` 400→600。代码见 `src/writing/writer.py`、`src/validation/evaluator.py`、`config/config.example.yml`、`config/local.secrets.yml`。
- **Prompt 工程化重构（Roles + Constraints + Execution Protocol）**：参照 doc/README.md 的审稿人双重身份模式，`_SYSTEM_DRAFT_THESIS` 加去 AI 味规则块 + 复合 Role（评委+写作专家）+ Protocol；`_SYSTEM_REVISE_THESIS` 从 6 行扁平规则重构为 #Role/#Task/#Constraints(5分层)/#Output(Part1/Part2) 四层结构，引入「修订阈值/最小干预原则」；`_SYSTEM_ABSTRACT_FROM_BODY` 充实为 Role-Task-Constraints-WritingTips-Protocol 五层；`_SYSTEM_EN_ABSTRACT_FROM_ZH` 从 7 行扩展到 Role-Constraints-Output-Protocol 四层（含去中式英语规则）；`_SYSTEM_EVAL_THESIS` 末尾加评估者 Protocol。提取 `_EXECUTION_PROTOCOL` 为模块常量（"仅内部执行，严禁输出"），五处 prompt 统一引用。代码见 `src/writing/writer.py`、`src/validation/evaluator.py`。
- **MCU 评估误判四连修复（D1-D4）**：D1 `mcu_abstract_body_mismatch` 从双向不等改为单向子集检查（`front_mcu - body_mcu` 非空才报错，正文中的对比/竞品型号不再触发误判）；D2 `_MCU_STC_DOMINANT` 正则替换加段落级对比检测（`_COMPARE_SCOPE`，含对比语境的段落整段跳过 STM32*→8051 替换，防止"与 STM32F103 方案相比"被破坏为"与 STC89C52 方案相比"）；D3 LLM 评估 prompt 加对比/竞品排除指引（正文仅作为方案对比出现的其他型号不视为矛盾）；D4 `build_global_term_map` 组3a 主导型号判定从纯频次改为 TechSpec MCU 优先（`hardware.mcu.model` 强制为主导，频次仅在无 TechSpec 时 fallback，杜绝对比段落高频竞品名导致主导方向错误）。代码见 `src/writing/writer.py`、`src/validation/evaluator.py`。
- **中文关键词检索全灭修复 + 同义词映射前置**：P0 统一 `min_ref_relevance_score` 为 0.01（`config.example.yml` 0.03→0.01，`local.secrets.yml` 0.15→0.01），使 `_relevance_score` 保底分 0.02 > 阈值 0.01，非嵌入式选题不再 281→0；controller 入口加配置一致性 warning（阈值 > 保底分时预警）。P1 同义词映射 `_build_synonym_map` 从检索后移到检索前，传入 `_relevance_score` → `_filter_and_score` → `run_search`/`run_expanded_search` 整条链路（5 处签名改动）。同义词命中时得分 ×0.6 权重，解决中文关键词（如"微多普勒特征"）无法直接匹配英文标题的问题。`run_search` 零结果时新增诊断日志（阈值/原始数量/排查提示）。代码见 `src/controller.py`、`src/retriever.py`、`config/config.example.yml`、`config/local.secrets.yml`。
- **文献池裁剪时序修复（先洗后裁）**：移除 `run_search` 中每轮结束后的 `_trim_store_to_limit(store, ..., max_total)`——该调用在 `cull_poor_quality` 之前用简单 relevance 排序一刀切砍掉文献（如 336→40），导致被裁文献无缘后续同义词映射驱动的智能质量筛选。现改为 `cull_poor_quality` 为唯一裁剪点：先全量做关键词命中数 + 同义词映射 + 兜底救回，再裁剪到 `max_total`。旧行为：336→40（一刀切）→9（清洗），新行为：336→N（清洗+裁剪，N 显著 > 9）。代码见 `src/retriever.py`。
- **章节规则配置覆盖（`override_section_rules`）**：新增 `_get_section_rule(section_id)` 函数——优先读取 `config.override_section_rules` 中用户自定义的章节特殊规则，未配置时回退硬编码 `_SECTION_RULES`。3 处撰写/修订调用点统一替换。用户可在 `local.secrets.yml` 中按 section_id 编写老师/学校要求的特殊规则（如"第三章只讲总体设计，禁止给出电路原理图、PCB布局、引脚连接表"），彻底消除默认规则与导师框架冲突导致的生成内容偏离。配置示例已写入 `config/config.example.yml`。代码见 `src/writing/writer.py`、`config/config.example.yml`。
- **引用范围配置化 + 越界检测四层防护**：新增 `citation_enabled_sections` 配置（默认 `["s1", "s2"]`）。①撰写端 `_ref_limit_for_section` 读配置决定注入参考文献上下文数量；② `_SYSTEM_DRAFT_THESIS` 加「引用范围规则」段（s3-s6 绝对禁止 [n]）；③ `_build_draft_prompt` 非引用章节加 ⚠ **本章禁止引用** 提示；④ `_build_revise_prompt` 修订时注入引用范围规则。后处理端 `postprocess_manuscript` 对非引用章节自动强制删除所有 `[n]` 标记。评估端新增 `citation_out_of_scope` error 级静态规则——遍历章节检测越界引用并报告具体章节名和允许列表。代码见 `src/writing/writer.py`、`src/validation/evaluator.py`、`config/config.example.yml`、`config/local.secrets.yml`。
- **引用按出现顺序重编号 + 文献池上限 30 篇**：`_do_done` 最终阶段调用 `reorder_citations_by_first_appearance()`——四步算法（扫描→去重→建映射→re.sub 一次性替换）将正文中 `[n]` 按首次出现顺序重排为 `[1][2][3]...`，未提及文献排在末尾。文献列表 `format_thesis_ref_list(order_map=...)` 按新顺序输出且仅列出被引用文献。上限 `max_refs_total` 从 40 调为 30。仅在最终 DONE 阶段执行一次，修订循环中保持原始编号稳定。代码见 `src/controller.py`、`src/writing/writer.py`、`src/ref_store.py`、`config/local.secrets.yml`。
- **主线行动态化 + 硬件越界/传感器清单配置化**：P0 撰写 prompt 主线行从硬编码 `第1章→第2章总体设计→第3章硬件→第4章软件→第5章测试→第6章总结` 改为从 `plan.outline` 动态读取章节标题拼接（如你的框架→ `第1章引言→第2章文献综述→第3章总体设计→第4章硬件与软件设计→第5章系统实现与调试→第6章结论`）。P1 `hardware_overflow_check_sections` 配置项替代硬编码 `if section_id=="s2"`——你的 s2 是文献综述设 `[]` 不再误报。P2 `sensor_checklist_sections` 配置项替代硬编码 `if section_id=="s5"`。三处改动消除章节编号语义假设的 3 个核心硬编码点。代码见 `src/writing/writer.py`、`config/config.example.yml`、`config/local.secrets.yml`。
- **强制小节结构**：`_SYSTEM_DRAFT_THESIS`「标题格式」从建议升级为强制（正文必须包含 ###/#### 小节）；`_executable_outline_prompt_section` 措辞从「建议小节结构」→「强制小节结构」；`_build_draft_prompt` 对含 `subsections` 的章节追加强制小节清单。新增 `_ensure_subsections_present()` 后处理兜底——检测缺失小节自动追加 `<!-- TODO: 待撰写 -->` 占位。代码见 `src/writing/writer.py`。
- **修订产物清洗**：新增 `_strip_revision_artifacts()`——清除 LLM 修订输出中的 Part 1/Part 2 标签、修改日志、修订说明等 Markdown 垃圾。位于 `_revise_one_section_body` return 前，串行/并行修订路径全覆盖。`_SYSTEM_REVISE_THESIS` 输出格式指令从 Part 1/Part 2 改为直接输出正文、禁止任何标记文字。代码见 `src/writing/writer.py`。
- **关键词嵌入摘要**：`Manuscript` 新增 `keywords_zh_text`/`keywords_en_text` 字段；`to_markdown` 将关键词拼接到对应摘要末尾，不再生成独立 `## 关键词` 章节；`_format_keywords_text()` 返回纯文本对。评估器 `abstract_too_short` 等规则不受影响（摘要正文不含关键词）。代码见 `src/models.py`、`src/writing/writer.py`。
- **占位符评估联动**：`_check_thesis_rules` BAD_MARKS 新增 `<!-- TODO:` 检测；`_SYSTEM_REVISE_THESIS` 新增「展开 TODO 占位符」指令。代码见 `src/validation/evaluator.py`、`src/writing/writer.py`。
- **参考文献顺序配置**：新增 `reference_before_acknowledgment` 配置项（默认 false=致谢→参考文献；true=参考文献→致谢）；`draft_manuscript` 组装阶段按配置调整顺序；`_SYSTEM_DRAFT_THESIS` 一致性规则加「致谢不引用」约束。仅影响最终输出顺序，不影响 LLM 各章引用权限。代码见 `src/writing/writer.py`、`config/config.example.yml`。
- **章标题规范化**：`to_markdown` 出口层自动补齐 s1-s6 缺失的「第X章」前缀（白名单 + `^(?:第\d+章\s*)+` 去重正则）；DEBUG 日志记录每笔规范化。代码见 `src/models.py`。
- **规划层标题规范化 + 子节清理**：`_normalize_outline_titles` 在 `generate_plan` 中前置规范化 `plan.outline` 中所有 s1-s6 标题（缺前缀补齐、重复去重），解决 `THESIS_CHAPTER_PREFIX` 硬规则误报。`_clean_top_level_subsections` 自动移除 LLM 错误平铺到顶层的 `s3_1` 等子节节点，解决 `THESIS_EXTRA_SECTIONS` 假警。`_SYSTEM_THESIS` prompt 追加「禁止顶层列出子节 ID」约束。代码见 `src/planner.py`。
- **H1 文档锚点 + Body 污染清理**：`Manuscript` 新增 `thesis_title` 字段，`to_markdown` 首行输出 `# {标题}` 建立文档 H1 根节点，解决下游工具（Pandoc）层级解析混乱。`postprocess_manuscript` 新增 `_clean_horizontal_rules`（删 `^---+$` 行，不伤表格）和 `_downgrade_body_h1`（`# `→`## `，排除代码块），消除 body 内碎片对工具结构的干扰。`draft_manuscript`/`revise_manuscript`/`postprocess`/`parse` 全链路透传 `thesis_title`。代码见 `src/models.py`、`src/writing/writer.py`。
- **TODO 自锁打破 + 小节匹配升级**：`_ensure_subsections_present` 从修订路径移除（仅 DRAFT 跑一次）；标题匹配从精确改为字符级模糊（去标点后包含关系）；TODO 占位符从 `<!-- TODO: 待撰写 -->` 改为可见文本 `> **待撰写：{sub.title}**`。新增 `_align_subsections_titles`：修订后自动将 LLM 使用的小节标题同步到 plan（1.5 倍长度限制），减少下轮评估误判。代码见 `src/writing/writer.py`。
- **评估器小节缺失检测**：新增 `missing_subsections`（warning 级）——对比 `plan.subsections` 与正文 `###` 标题，直接检测真正缺失的小节；移除 `placeholder_residual` 中的 `<!-- TODO:` 检测，避免初稿 TODO 占位符误触发。代码见 `src/validation/evaluator.py`。
- **冒烟测试扩展**：共计 64 项，新增 ENSURE-05 + ALIGN-01~02。代码见 `smoke_test.py`。
- **离线测试基础设施**：新建 `tests/` 目录，包含 `run_offline.py`（零依赖，~1s 完成 50+ 项检测）、`run_online.py`（需 API Key 选跑）、`fixture_runner.py`（Markdown YAML front matter → 静态规则验证）、`outline_fixture_runner.py`（YAML → 大纲硬规则验证）、`config_schema_check.py`（local.secrets.yml vs config.example.yml 键结构对比）。`_check_thesis_rules` 签名改为 `store: ReferenceStore | None = None` 支持离线无参调用。新建 13 个 fixture 文件覆盖 MCU 一致性、引用位置、章节越界、摘要规则、占位符残留、大纲硬规则 6 类。代码见 `tests/`、`src/validation/evaluator.py`。

## 进行中

- （当前无进行中任务）

## 阻塞 / 风险

- （无）

## Agent 快速跳转

| 领域 | 入口文档 | 核心代码 |
|------|----------|----------|
| 撰写 | `src/writing/README.md` | `writer.py`, `tech_spec.py`, `locked_tech_spec.py`, `multi_candidate.py`, `scope_enforce.py` |
| 验证 | `src/validation/README.md` | `src/validation/evaluator.py` |
| 运行诊断 | `src/diagnosis/README.md` | `src/diagnosis/recorder.py` 等 |
| 全流程状态 | `PROGRESS.md`（本文件） | `src/controller.py` |
| 规划 | - | `src/planner.py` |
