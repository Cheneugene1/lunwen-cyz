# 毕业论文自动生成系统 — 模块测试方案

> **版本**: v1.1
> **编制日期**: 2026-05-22  
> **更新日期**: 2026-05-29 — 新增 `tests/` 测试基础设施
> **适用代码库**: `f:\code\lunwencyz`  
> **原则**: 不修改任何代码，仅通过导入调用和配置变更进行验证
> **快速入口**: `python tests/run_offline.py`（离线，~6s） / `python tests/run_online.py`（在线，需 API）

---

## 1. 测试目标与范围

### 1.1 测试目标

1. **功能完整性**：验证每个模块的核心功能在正常输入下产生符合预期的输出
2. **稳定性**：验证各模块在边界输入、异常输入和 API 失败时的降级行为
3. **兼容性**：验证配置变更后各模块行为一致，不同题型（嵌入式 / 雷达 / 纯软件）均能正常工作
4. **回归保护**：确认此前已修复的核心 Bug（跨章一致性、结论误报、引用跨行、检索全灭、MCU 评估误判等）不再复现

### 1.2 测试范围

| 模块 | 文件 | 测试范围 | 不测范围 |
|------|------|---------|---------|
| **配置加载** | `src/config.py` | 加载顺序、默认值、环境变量覆盖 | 文件权限异常 |
| **LLM 客户端** | `src/llm.py` | 流式/非流式、超时重试、429 降级 | 网络完全断连 |
| **数据模型** | `src/models.py` | Pydantic 校验、JSON 序列化/反序列化 | — |
| **解析器** | `src/parser.py` | 文本文件解析、关键词提取 | 二进制/加密文件 |
| **规划器** | `src/planner.py` | 大纲生成、LLM 解析用户大纲、大纲评估、修订 | 极端长文本(>100页) |
| **检索器** | `src/retriever.py` | OpenAlex/Crossref/S2/arXiv 四源、相关性过滤、泛化检索 | API 全宕 |
| **文献池** | `src/ref_store.py` | 增删去重、质量清洗、同义词映射、SQLite 持久化 | 并发写入 |
| **撰写器** | `src/writing/writer.py` | 初稿生成、修订、后处理（引用修正/术语统一/摘要截断）、term_map 构建 | — |
| **评估器** | `src/validation/evaluator.py` | 静态规则检查、LLM 评分、结论-引言对照、MCU 一致性 | — |
| **控制器** | `src/controller.py` | 状态机流转、检索循环、修订循环、Done 输出 | 完整 GUI 交互 |
| **封面** | `src/cover.py` | 封面渲染、目录生成 | — |
| **诊断** | `src/diagnosis/` | JSONL 记录、轮次追踪 | — |

---

## 2. 测试环境配置要求

### 2.1 硬件环境

| 项目 | 最低要求 | 推荐 |
|------|---------|------|
| CPU | 2 核 | 4 核+ |
| 内存 | 4 GB | 8 GB+ |
| 磁盘 | 1 GB 可用 | 5 GB 可用 |
| 网络 | 可访问 DeepSeek API + OpenAlex + Crossref + Semantic Scholar | 稳定宽带 |

### 2.2 软件环境

| 项目 | 版本 | 说明 |
|------|------|------|
| Python | 3.10+ | 不高于 3.12 |
| 操作系统 | Windows 10/11 | PowerShell 5 |
| pip 依赖 | 见 `requirements.txt` | 确保 `openai`, `httpx`, `pydantic`, `pyyaml`, `rich` 已安装 |

### 2.3 配置文件基线

测试前需确认以下配置项：

```yaml
# 必须检查的配置项及测试前基线值
min_ref_relevance_score: 0.01    # 不可 > 0.02
max_refs_total: 40
max_search_rounds: 3
min_references: 20
quality_threshold: 8.0
max_revision_rounds: 3
thesis_target_words_min: 25000
thesis_section_words.abstract_zh: 600
```

**验证命令**:
```python
from src.config import get
assert get("min_ref_relevance_score") <= 0.02, "阈值过高会导致检索全灭"
assert get("max_refs_total") >= 30, "文献池上限过低"
```

### 2.4 API 密钥检查

```python
from src.config import get
assert get("deepseek_api_key"), "缺少 DEEPSEEK_API_KEY"
```

---

## 3. 测试资源准备清单

### 3.1 测试文档

| 文档 | 路径 | 用途 |
|------|------|------|
| 嵌入式论文文档 | `doc/` 下 .docx/.pdf | 解析器测试、规划器测试 |
| 雷达论文文档 | 无文档模式 | 检索器兼容性测试 |
| 空文档 | 不存在路径 | 解析器边界测试 |

### 3.2 测试数据

| 数据 | 说明 |
|------|------|
| `test_keywords_embedded` | `["温湿度检测", "土壤湿度检测", "STM32", "DHT11", "PID控制"]` |
| `test_keywords_radar` | `["微多普勒特征", "动目标检测", "时频分析", "MATLAB仿真", "目标识别"]` |
| `test_outline_mixed` | 中英混排大纲文本（用户手动粘贴格式） |
| `test_citation_text` | 含 `。\n[1]` 跨行引用、含对比语境 MCU 型号的正文 |
| `test_term_conflict` | 同时含 OLED 和 LCD1602 的正文 |

### 3.3 预期输出模板

| 模块 | 检查项 |
|------|--------|
| 检索器 | `相关性过滤: N 原始 → M 通过 (M > 0)` |
| 撰写器 | 摘要 500-800 字、引用在标点前、型号一致 |
| 评估器 | 无 MCU 对比误报、无 conclusion_intro 误报 |

---

## 4. 测试用例设计原则

### 4.1 分层原则

| 层 | 说明 | 依赖 |
|----|------|------|
| **L0 单元** | 单函数、无外部依赖 | 无 |
| **L1 模块** | 单模块、可 Mock API | L0 |
| **L2 集成** | 跨模块链路、真实 API | L1 |
| **L3 端到端** | 完整控制器流程 | L2 |

### 4.2 用例命名规范

```
[MODULE]_[FUNCTION]_[SCENARIO]_[EXPECTED]
```

示例：
```
RETRIEVER_relevance_score_chinese_keywords_synonym_map_存活5篇
EVALUATOR_mcu_mismatch_comparison_context_not_reported
WRITER_fix_citation_crossline_merged_correctly
```

### 4.3 数据构造原则

- 每条测试独立，不依赖执行顺序
- 输入数据写在用例内（不依赖外部文件，测试脚本可独立运行）
- 边界值至少覆盖：空输入、单元素、超长输入、特殊字符

---

## 5. 详细测试流程

### 5.1 测试执行步骤

```
1. 环境检查  →  验证 Python 版本、依赖、API Key、配置基线
2. L0 快速冒烟 →  模型校验、配置加载、工具函数（5 分钟）
3. L1 模块测试 →  按模块逐一执行（30 分钟）
4. L2 集成测试 →  检索→清洗、撰写→后处理、评估→修订（30 分钟）
5. L3 端到端    →  完整论文生成（2 小时，可选）
6. 结果汇总    →  输出测试报告
```

### 5.2 缺陷管理

| 严重级别 | 定义 | 响应 |
|---------|------|------|
| **Blocker** | 系统无法启动、API 全灭、核心功能完全不工作 | 立即修复 |
| **Critical** | 核心功能有严重 Bug（检索全灭、后处理破坏正文） | 优先修复 |
| **Major** | 功能部分失效（某类论文无法检索、某规则误报） | 本迭代修复 |
| **Minor** | 日志缺失、提示不友好、性能可优化 | 排入后续迭代 |

---

## 6. L0 单元测试用例

### 6.1 配置模块 (`src/config.py`)

| ID | 用例 | 输入 | 预期 |
|----|------|------|------|
| CONFIG-01 | 默认值读取 | `get("max_refs_total")` | 返回 `40` |
| CONFIG-02 | local.secrets 覆盖 | `get("min_ref_relevance_score")` | 返回 `0.01` |
| CONFIG-03 | 缺失键返回 None | `get("nonexistent_key")` | 返回 `None` |
| CONFIG-04 | 环境变量覆盖 | 设置 `DEEPSEEK_MODEL=test-model` | `get("deepseek_model")` 返回 `test-model` |

**执行脚本**:
```python
from src.config import get, load_config
cfg = load_config()
assert "deepseek_api_key" in cfg
assert get("max_refs_total") == 40
assert get("min_ref_relevance_score") <= 0.02, "阈值过高"
assert get("nonexistent_key_xyz") is None
print("✅ CONFIG 全部通过")
```

### 6.2 数据模型 (`src/models.py`)

| ID | 用例 | 预期 |
|----|------|------|
| MODEL-01 | `SectionNode` 创建+序列化 | JSON 包含 section_id/title/bullets |
| MODEL-02 | `WritingPlan` 创建+序列化 | JSON 包含 outline/keywords/search_queries |
| MODEL-03 | `Manuscript` to_markdown | 输出包含所有章节标题 |
| MODEL-04 | `Evaluation` 模型校验 | score_total 为 float，dimensions 有 4 个 |
| MODEL-05 | `Reference` 去重 ID | 相同 title 的 ref ID 不同 |

**执行脚本**:
```python
import json
from src.models import SectionNode, WritingPlan, Manuscript, ManuscriptSection

# MODEL-01
sn = SectionNode(section_id="s1", title="测试标题", bullets=["要点1", "要点2"])
d = sn.model_dump()
assert d["section_id"] == "s1"
assert len(d["bullets"]) == 2

# MODEL-02
wp = WritingPlan(outline=[sn], keywords=["测试"], search_queries=["test query"])
assert len(wp.outline) == 1
assert wp.keywords == ["测试"]

# MODEL-03
ms = Manuscript(sections=[ManuscriptSection(section_id="s1", title="测试", markdown_body="正文内容")], version=1)
md = ms.to_markdown()
assert "正文内容" in md

print("✅ MODEL 全部通过")
```

---

## 7. L1 模块测试用例

### 7.1 解析器 (`src/parser.py`)

| ID | 用例 | 预期 |
|----|------|------|
| PARSER-01 | 解析 .docx 文档 | 返回非空 DocumentBundle |
| PARSER-02 | 解析 .pdf 文档 | 返回非空 DocumentBundle |
| PARSER-03 | 不存在的文件路径 | 抛出或返回空 |
| PARSER-04 | 空目录 | 返回空列表 |

### 7.2 规划器 (`src/planner.py`) — 核心

| ID | 用例 | 预期 |
|----|------|------|
| PLAN-01 | 毕业论文大纲生成 | 返回含 abstract_zh/s1-s6/acknowledgment 的 WritingPlan |
| PLAN-02 | 关键词不为用户原文抄送 | `plan.keywords` 为独立学术名词，不含 "不要"、"本科毕业设计" |
| PLAN-03 | `update_plan_from_user` 中英混排解析 | `1 Introduction\n1.1 背景` → 正确映射到 s1 等 section_id |
| PLAN-04 | `update_plan_from_user` LLM 失败回退 | 制造无效输入 → 回退到字符串解析器（不崩溃） |
| PLAN-05 | 大纲评估通过 | 标准结构 → passed=True |
| PLAN-06 | `revise_outline` 基本修订 | 输入一条建议 → 返回修改后 plan |

**PLAN-03 关键测试**（验证上次修复）:
```python
from src.models import WritingPlan, SectionNode
from src.planner import update_plan_from_user

plan = WritingPlan(
    outline=[SectionNode(section_id=f"s{i}", title=f"旧标题{i}") for i in range(1,10)],
    keywords=["test"], search_queries=["test"],
)
user_input = """1 Introduction 
1.1 课题背景与意义
2 Literature Review
3 Research Methodology
4 系统硬件与软件设计
5 系统实现与调试
6 Conclusions"""
result = update_plan_from_user(plan, user_input)
assert len(result.outline) >= 6, f"解析失败: 仅 {len(result.outline)} 章节"
titles = [s.title for s in result.outline]
assert any("引言" in t or "Introduction" in t for t in titles)
print("✅ PLAN-03 LLM 解析通过")
```

### 7.3 检索器 (`src/retriever.py`) — 核心

| ID | 用例 | 预期 |
|----|------|------|
| RET-01 | `_relevance_score` 直接匹配 | 关键词 "micro-Doppler" 在标题中 → 得分 > 0 |
| RET-02 | `_relevance_score` 同义词匹配 | 中文 "微多普勒特征" + synonym_map → 得分 > 0 |
| RET-03 | `_relevance_score` 保底分 | 量子计算论文 + 雷达关键词 → 0.02 |
| RET-04 | `_filter_and_score` 阈值 0.01 放行 | 5 篇雷达论文 → 5 篇通过 |
| RET-05 | `run_search` 真实检索 | 返回 True，store 非空 |
| RET-06 | `run_expanded_search` 泛化检索 | 主检索不足时 → 泛化词有增量 |

**RET-02 关键测试**（验证修复后同义词映射生效）:
```python
from src.retriever import _relevance_score, _filter_and_score
from src.models import Reference

ref = Reference(
    id="test-1", title="Micro-Doppler Signature Analysis for Moving Target Detection in Radar Systems",
    abstract="Time-frequency analysis method for micro-Doppler signatures.",
    venue="IEEE Trans", year=2023, authors=["Z"], ref_type="J", source="semantic_scholar",
)
keywords = ["微多普勒特征", "动目标检测", "时频分析"]
synonym_map = {"微多普勒特征": ["micro-doppler"], "动目标检测": ["moving target detection"], "时频分析": ["time-frequency"]}

score_no_syn = _relevance_score(ref, keywords)
score_syn = _relevance_score(ref, keywords, synonym_map=synonym_map)
assert score_syn > 0.1, f"同义词映射未生效: {score_syn}"
assert score_syn > score_no_syn, f"同义词得分应高于无同义词: {score_syn} <= {score_no_syn}"
print(f"✅ RET-02 通过: 无同义词={score_no_syn:.3f}, 有同义词={score_syn:.3f}")
```

### 7.4 文献池 (`src/ref_store.py`)

| ID | 用例 | 预期 |
|----|------|------|
| REFS-01 | 添加+去重 | 相同 title 的文献合并 |
| REFS-02 | `cull_poor_quality` 质量过滤 | 无作者/标题异常的文献被移除 |
| REFS-03 | 同义词映射注入清洗 | 中文关键词+英文学术文献仍命中 |
| REFS-04 | SQLite 持久化 | 关闭后重新打开仍可读取 |
| REFS-05 | `_build_synonym_map` LLM 生成 | 5 个中文关键词 → 返回 5 个映射 |
| REFS-06 | `_build_synonym_map` LLM 失败回退 | DeepSeek 不可用时返回 {} |

### 7.5 撰写器 (`src/writing/writer.py`) — 核心

| ID | 用例 | 预期 |
|----|------|------|
| WRIT-01 | `_fix_citation_position` 跨行引用 | `。\n[1]` → `[1]。\n` |
| WRIT-02 | `_fix_citation_position` 普通引用 | `应用。[1]` → `应用[1]。` |
| WRIT-03 | `_fix_citation_position` 对比语境不破坏 | 含 "对比" 的行中引用仍正确修正 |
| WRIT-04 | `build_global_term_map` 显示模块冲突 | OLED vs LCD1602 同时出现 → 取高频统一 |
| WRIT-05 | `build_global_term_map` TechSpec 优先 | spec.mcu.model 存在时 → 以其为主导 |
| WRIT-06 | `_truncate_abstract` 截断 | 900 字摘要 → 截断在 ≤800 字处 |
| WRIT-07 | `_truncate_abstract` 不截断 | 600 字摘要 → 原样返回 |
| WRIT-08 | `_build_term_lockdown_snapshot` | TechSpec 有传感器 → 输出型号清单 |
| WRIT-09 | `postprocess_manuscript` MCU 对比保护 | 正文含对比语境时不替换 STM32* |

**WRIT-01 关键测试**:
```python
from src.writing.writer import _fix_citation_position

text = "实验验证了可行性。\n[1]\n在此基础上"
result = _fix_citation_position(text)
assert "。[1]\n" not in result
assert "[1]。\n" in result or "[1]\n。" in result
print("✅ WRIT-01 通过")
```

### 7.6 评估器 (`src/validation/evaluator.py`) — 核心

| ID | 用例 | 预期 |
|----|------|------|
| EVAL-01 | `_check_thesis_rules` MCU 对比不误报 | 摘要=STC89C51, 正文=STC89C51+ESP32(对比) → 不触发 `mcu_abstract_body_mismatch` |
| EVAL-02 | `_check_thesis_rules` MCU 真正矛盾报 | 摘要=STM32, 正文=STC89C51 → 触发 error |
| EVAL-03 | `_check_thesis_rules` 结论-引言对照 | s1 bullets 为 "本文工作"+"论文结构" → fallback 不抓取（白名单过滤） |
| EVAL-04 | `_check_thesis_rules` 摘要字数 | <480 字 → abstract_too_short; >850 字 → abstract_too_long |
| EVAL-05 | `_check_thesis_rules` 摘要禁引用 | 摘要含 `[1]` → abstract_citation_markers |
| EVAL-06 | LLM 评估 JSON 有效 | evaluate 返回合法 Evaluation |
| EVAL-07 | 数据占位符豁免 | 正文含 `[实测数据]` 不扣分 |

**EVAL-01 关键测试**:
```python
from src.validation.evaluator import _check_thesis_rules
from src.models import Manuscript, ManuscriptSection, WritingPlan, SectionNode

# 构造对比场景：摘要只有主控，正文有对比型号
ms = Manuscript(sections=[
    ManuscriptSection(section_id="abstract_zh", title="摘要", markdown_body="基于STC89C51单片机的温湿度系统"),
    ManuscriptSection(section_id="keywords", title="关键词", markdown_body="STC89C51；温湿度"),
    ManuscriptSection(section_id="s1", title="第1章 引言", markdown_body="..."),
    ManuscriptSection(section_id="s2", title="第2章 相关工作", markdown_body="本文对比了ESP32方案与Arduino方案"),
    ManuscriptSection(section_id="s6", title="第6章 结论", markdown_body="总结"),
], version=1)
plan = WritingPlan(
    outline=[SectionNode(section_id="s1", title="引言", bullets=["研究背景", "问题", "贡献", "结构"])],
    keywords=["STC89C51", "温湿度"], search_queries=["test"],
)
issues = _check_thesis_rules(ms, plan, None)
mcu_issues = [i for i in issues if i.rule_id == "mcu_abstract_body_mismatch"]
assert len(mcu_issues) == 0, f"对比场景误报: {mcu_issues}"
print("✅ EVAL-01 MCU 对比不误报")
```

### 7.7 封面 (`src/cover.py`)

| ID | 用例 | 预期 |
|----|------|------|
| COV-01 | `render_cover` 输出 | 返回含学校名称、题目的字符串 |
| COV-02 | `render_toc` 输出 | 返回 "目录" + 章节列表 |

---

## 8. L2 集成测试用例

### 8.1 检索 → 清洗 链路

| ID | 用例 | 预期 |
|----|------|------|
| INTEG-01 | 嵌入式论文完整检索 | run_search → store 非空 → cull_poor_quality 后 ≥15 条 |
| INTEG-02 | 雷达论文完整检索 | run_search → store 非空 → 同义词映射生效 |
| INTEG-03 | 泛化检索补充 | 主检索不足 → run_expanded_search 有增量 |

### 8.2 撰写 → 后处理 链路

| ID | 用例 | 预期 |
|----|------|------|
| INTEG-04 | postprocess 引用修正 + 术语统一 | 输入含 `。[1]` 和 OLED/LCD1602 矛盾 → 输出引用前移 + 术语统一 |
| INTEG-05 | postprocess MCU 对比保护 | 正文含"对比了 STM32F103" → 不被替换为其他型号 |

### 8.3 评估 → 修订 链路

| ID | 用例 | 预期 |
|----|------|------|
| INTEG-06 | 评估-修订单轮 | evaluate 返回 actionable_items → revise_manuscript 不崩溃 |
| INTEG-07 | 顽固问题跨轮追踪 | 首轮 evaluate → revise → 再 evaluate → stubborn 标记 |

---

## 9. L3 端到端测试

### 9.1 完整流程 — 嵌入式论文

```
INIT → PARSE → PLAN → OUTLINE_REVIEW → SEARCH → DRAFT → EVAL → (REVISE→EVAL)×N → DONE
```

**预期**:
- 至少通过 1 轮 EVAL
- 最终 `paper_*.md` 文件存在
- 摘要 500-800 字
- 文献池 ≥ 15 条
- 评估分 ≥ 6.0

### 9.2 完整流程 — 雷达论文（无文档模式）

```
INIT → PLAN → SEARCH → DRAFT → EVAL → DONE
```

**预期**:
- 检索不出现 "0 条通过相关性过滤"
- 文献池 ≥ 10 条
- 论文正常生成（基于仅检索模式）

---

## 10. 测试通过标准

| 层级 | 通过标准 |
|------|---------|
| **L0** | 100% 用例通过（6/6） |
| **L1** | ≥ 95% 用例通过（≥ 33/35） |
| **L2** | 100% 用例通过（7/7） |
| **L3** | 论文正常生成，无 Blocker/Critical 缺陷 |

### 不通过条件

| 层级 | Block | 处理 |
|------|-------|------|
| L0 | 任何一条失败 | **阻塞**：禁止进入 L1 |
| L1 | > 2 条失败 | **暂缓**：修复后重新进入 |
| L2 | 任何一条失败 | **阻塞**：核心链路断裂 |
| L3 | 论文无法生成或文献池为空 | **阻塞**：禁止发布 |

---

## 11. 风险评估与应对

| 风险 | 概率 | 影响 | 应对措施 |
|------|------|------|---------|
| DeepSeek API 不可用 | 中 | 阻断 | 检查 API Key 有效期；L1 可 Mock 部分用例 |
| Semantic Scholar 429 限流 | 高 | 文献略少 | 已内置降级重试；429 时自动无 Key 模式 |
| 本地配置未同步 | **高** | 关键 | **测试前必须先执行配置基线检查（§2.3）** |
| `local.secrets.yml` 阈值未更新 | 中 | 检索全灭 | L0 CONFIG-02 会捕获此问题 |
| 网络不稳定 | 低 | 部分用例超时 | 设置 httpx timeout=30s；单用例超 >60s 标记跳过 |
| API 配额耗尽 | 低 | 阻断 | 避免重复全量 L3；L1/L2 可复用缓存 |

---

## 12. 测试执行脚本

### 一键 L0+L1 冒烟（不含 LLM API 调用）

```python
# smoke_test.py — 保存后执行 python smoke_test.py
import sys; sys.path.insert(0, ".")

# === CONFIG ===
from src.config import get
assert get("min_ref_relevance_score") <= 0.02, "❌ 阈值过高: 检索将全灭"
assert get("max_refs_total") >= 30, "❌ 文献池上限过低"
print("✅ CONFIG")

# === MODEL ===
from src.models import SectionNode, WritingPlan
sn = SectionNode(section_id="s1", title="测试", bullets=["a", "b"])
wp = WritingPlan(outline=[sn], keywords=["k"], search_queries=["q"])
assert len(wp.outline) == 1
print("✅ MODEL")

# === RETRIEVER (无 API) ===
from src.retriever import _relevance_score
from src.models import Reference
ref = Reference(id="t1", title="Micro-Doppler Signature Analysis",
    abstract="time-frequency analysis of micro-Doppler signatures",
    venue="IEEE", year=2023, authors=["Z"], ref_type="J", source="s2")
kws = ["微多普勒特征", "时频分析"]
syn = {"微多普勒特征": ["micro-doppler"], "时频分析": ["time-frequency"]}
s0 = _relevance_score(ref, kws)
s1 = _relevance_score(ref, kws, syn)
assert s1 > s0, f"同义词未生效: {s1} <= {s0}"
print(f"✅ RETRIEVER (无同义词={s0:.3f}, 有同义词={s1:.3f})")

# === WRITER ===
from src.writing.writer import _fix_citation_position
r = _fix_citation_position("通过实验验证。\n[1]\n进一步")
assert "[1]。" in r, f"跨行引用未修正: {repr(r[:60])}"
print("✅ WRITER citation cross-line")

# === WRITER term_map ===
from src.writing.writer import _build_term_lockdown_snapshot
spec = {"hardware": {"mcu": {"model": "STC89C52"}, "sensors": [{"name": "温湿度", "model": "DHT11"}]}}
snap = _build_term_lockdown_snapshot(spec)
assert "STC89C52" in snap and "DHT11" in snap
print("✅ WRITER term lockdown")

# === EVALUATOR (无 API, 静态规则) ===
from src.validation.evaluator import _check_thesis_rules
from src.models import Manuscript, ManuscriptSection
ms = Manuscript(sections=[
    ManuscriptSection(section_id="abstract_zh", title="摘要", markdown_body="基于STC89C51的温湿度系统"),
    ManuscriptSection(section_id="keywords", title="关键词", markdown_body="STC89C51；温湿度"),
    ManuscriptSection(section_id="s1", title="引言", markdown_body="..."),
    ManuscriptSection(section_id="s2", title="相关工作", markdown_body="本文对比了ESP32方案"),
    ManuscriptSection(section_id="s6", title="结论", markdown_body="总结"),
], version=1)
p2 = WritingPlan(outline=[SectionNode(section_id="s1", title="引言", bullets=["背景", "问题", "贡献", "结构"])], keywords=["k"], search_queries=["q"])
issues = _check_thesis_rules(ms, p2, None)
mcu_bug = [i for i in issues if i.rule_id == "mcu_abstract_body_mismatch"]
assert len(mcu_bug) == 0, f"MCU对比误报: {[i.message for i in mcu_bug]}"
print("✅ EVALUATOR MCU comparison guard")

print("\n🎉 全部冒烟测试通过")
```

### L1 完整测试

运行上述冒烟脚本后，分模块执行第 7 节中各 ID 的独立测试脚本。

### L2 集成测试

```python
# integration_test.py
# 依赖：有效的 DeepSeek API Key + 网络
# 警告：会消耗 API 配额，建议仅在完整验证时运行
```

---

## 13. 测试报告模板

```
========================================
测试报告
========================================
执行时间: YYYY-MM-DD HH:MM
执行人:   [姓名]
代码版本: [git commit hash]

[L0 结果]
  CONFIG:  PASS  (4/4)
  MODEL:   PASS  (5/5)

[L1 结果]
  PARSER:  PASS/FAIL  (X/Y)
  PLANNER: PASS/FAIL  (X/Y)
  RETRIEVER: PASS/FAIL  (X/Y)
  REF_STORE: PASS/FAIL  (X/Y)
  WRITER:  PASS/FAIL  (X/Y)
  EVALUATOR: PASS/FAIL  (X/Y)
  COVER:   PASS/FAIL  (X/Y)

[L2 结果]
  INTEG-01~07: PASS/FAIL  (X/7)

[L3 结果]
  嵌入式论文:  PASS/FAIL  (score=X.X, refs=N)
  雷达论文:    PASS/FAIL  (score=X.X, refs=N)

[缺陷清单]
  - [级别] [模块] 描述

[结论]
  ☐ 通过，可发布
  ☐ 有条件通过（缺陷列表非阻塞）
  ☐ 不通过
========================================
```

---

## 14. 附录：已知需关闭的旧 Bug 清单

| Bug # | 描述 | 验证用例 |
|-------|------|---------|
| Bug 1 | 修订无跨章一致性保护 | WRIT-09 |
| Bug 2 | conclusion_intro_gap 误报 | EVAL-03 |
| Bug 3 | citation cross-line 漏网 | WRIT-01 |
| Bug 4 | keywords 大小写不规范 | PLAN-02 |
| Bug 5 | 文献池旧 session 锁死 | RET-05 |
| D1 | MCU 对比误判 | EVAL-01 |
| D2 | _MCU_STC_DOMINANT 破坏对比内容 | INTEG-05 |
| 检索全灭 | 281→0 相关性过滤 | RET-04 |
