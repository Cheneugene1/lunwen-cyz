# LunWen CYZ

[English](README.md) | [中文](README.zh-CN.md)

> 一个 CLI-first 的长文本写作 Agent：状态机流程编排、多源文献检索、质量评估闭环、JSONL 可追踪运行日志，以及面向人类和 Agent 的模块知识库。

LunWen CYZ 不是“调一次大模型生成一篇论文”的脚本。它更像一次 Agent 工程化实验：把一个模糊、耗时、容易失控的长文本写作任务，拆成可观察、可调试、可恢复的命令行工作流：解析资料、规划大纲、检索文献、逐章生成、质量评估、自动修订，并留下足够的运行轨迹，方便人类或其他 Agent 复盘和接手。

这个项目围绕一个实际问题构建：

> CLI Agent 能不能让长文本生成比一次性 Prompt 更可控、更可观测、更易维护？

---

## 为什么这个项目值得看

长文本 AI 写作常见的问题不是“不会生成文字”，而是生成到最后才发现章节跑偏、引用混乱、技术事实漂移、术语不一致、小节缺失，或者只有一句模糊的“整体还可以”。这个项目把这些失败模式当成工程问题处理，而不是继续堆 Prompt。

它引入了：

- **真实的 Agent 控制循环**：`PARSE -> PLAN -> SEARCH -> DRAFT -> EVAL -> REVISE -> DONE`
- **CLI-first 运行方式**：交互模式、命令行参数、分阶段调试、会话恢复、已有论文独立评测
- **质量门禁**：静态规则 + LLM 评分，支持 error/warning 分级和可执行修订建议
- **可观测性**：JSONL 事件日志、阶段耗时、规则命中数、问题消解率、可解释指标摘要
- **事实约束**：LLM 生成的 TechSpec + 用户锁定技术事实，注入写作和修订 Prompt
- **Agent-readable 文档**：模块 README 带 YAML front matter 和 `sync_rule`，方便后续开发者或代码 Agent 快速上手

---

## 它和普通 LLM 脚本有什么不同

| 维度 | 普通 LLM 脚本 | LunWen CYZ |
| --- | --- | --- |
| 工作流 | 一次线性生成 | 状态机编排解析、规划、检索、生成、评估、修订和最终化 |
| 控制面 | Prompt 流程隐藏在代码里 | CLI 参数、分阶段执行、规划续跑、独立评测 |
| 决策闭环 | 生成一次就结束 | 评估、修订、再评估，并根据质量阈值或规则通过条件停止 |
| 事实来源 | 主要依赖 Prompt 上下文 | 用户资料、手动文献、公开文献 API、TechSpec、用户锁定事实 |
| 可观测性 | 最多输出控制台文本 | JSONL trace、诊断命令、可解释指标、跨轮问题差分 |
| 可维护性 | 代码是唯一事实源 | 模块文档、sync rule、测试、fixture、进度文档共同维护 |
| 隐私边界 | 往往不清楚 | 密钥、上传材料、输出论文、缓存和数据库默认不进入 Git |

---

## 核心能力

### 1. CLI Agent 工作流

`main.py` 提供基于 argparse 的命令行入口：

```bash
python main.py
python main.py --files proposal.docx report.pdf --refs references.bib
python main.py --request "写一篇关于 V2X 资源分配的论文" --files design.pdf
python main.py --locked-tech-spec config/locked_tech_spec.example.json
python main.py --session <session_id>
```

既可以交互式运行，也可以通过参数复现批处理式任务。项目还支持自动检测本地 `doc/` 目录中的素材文件。

### 2. 分阶段调试

长任务 Agent 需要断点。LunWen CYZ 支持按阶段执行：

```bash
# 只运行到大纲规划
python main.py --phase plan --files proposal.docx

# 从已有 plan 继续生成初稿
python main.py --phase draft --plan outputs/plan_xxx.json

# 对已有 Markdown 论文做独立评测，不重新生成
python main.py --phase eval --plan outputs/plan_xxx.json --paper outputs/paper_xxx.md
```

这让 Agent 更容易调试、复现和面试展示。

### 3. 多源文献检索与文献池

检索层集成多个公开学术数据源：

- OpenAlex
- Crossref
- arXiv
- Semantic Scholar
- 手动导入 `.bib` / `.csv` 文献

系统会对文献进行合并、去重、评分，并存入本地文献池。

### 4. 质量评估闭环

项目结合静态规则和 LLM 评估。静态规则会检查：

- 摘要长度和摘要引用标记
- 引用位置
- 章节越界
- 缺失小节
- 占位符残留
- 型号 / 硬件术语一致性
- 引用范围越界

LLM evaluator 输出结构化评分和 actionable revision items。控制器再决定停止、修订，或对顽固问题做专项修复。

### 5. 可追踪运行

每次运行可以在 `outputs/run_<session>.jsonl` 下输出结构化事件。诊断工具可以汇总：

- 阶段流转
- 总耗时
- 生成耗时
- 规则命中
- 问题消解率
- 用户时间节省估算
- 顽固问题趋势

```bash
python -m src.diagnosis outputs/run_<session_id>.jsonl

python -c "from src.diagnosis import print_explainability_summary; print_explainability_summary('outputs/run_xxx.jsonl')"
```

### 6. Agent-readable Knowledge Base

这个仓库有意把模块文档和代码放在一起，供人类和代码 Agent 阅读：

| 模块 | 入口文档 | 作用 |
| --- | --- | --- |
| Writing | [`src/writing/README.md`](src/writing/README.md) | 初稿、修订、TechSpec、术语映射、后处理、公开 API |
| Validation | [`src/validation/README.md`](src/validation/README.md) | 评分体系、静态规则、降级策略、评估契约 |
| Diagnosis | [`src/diagnosis/README.md`](src/diagnosis/README.md) | JSONL 事件格式、诊断命令、可解释指标 |
| Tests | [`tests/README.md`](tests/README.md) | 离线测试、fixture、大纲规则、配置 schema 检查 |
| Progress | [`PROGRESS.md`](PROGRESS.md) | 实现历史、已完成能力、当前约束 |

每个入口文档都有 YAML front matter 和 `sync_rule`。这意味着后续改代码的人或 Agent 可以知道：改哪块实现时，必须同步更新哪份文档。这是一种轻量的 Agent 协作知识库模式。

---

## 架构总览

```text
main.py
  `-- src/controller.py              # 状态机控制器
        |-- parser.py                # 多格式文档解析
        |-- planner.py               # 意图理解、大纲生成、大纲评估
        |-- retriever.py             # OpenAlex / Crossref / arXiv / Semantic Scholar 检索
        |-- ref_store.py             # 文献池、去重、评分、同义词映射
        |-- writing/
        |   |-- draft_engine.py      # 逐章初稿生成
        |   |-- revision_engine.py   # 修订与顽固问题修复
        |   |-- abstract.py          # 中英文摘要生成
        |   |-- postprocess.py       # 引用、标点、越界、术语后处理
        |   |-- term_map.py          # 全文术语统一
        |   |-- tech_spec.py         # LLM 技术规格生成
        |   `-- locked_tech_spec.py  # 用户锁定技术事实
        |-- validation/
        |   `-- evaluator.py         # 静态规则 + LLM 评分
        |-- diagnosis/
        |   |-- recorder.py          # JSONL 事件记录
        |   |-- analyzer.py          # 规则聚合
        |   |-- metrics.py           # 可解释指标
        |   `-- report.py            # Rich 终端报告
        `-- presenter.py             # 评估面板渲染
```

---

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置密钥

```bash
cp config/config.example.yml config/local.secrets.yml
# 编辑 config/local.secrets.yml，填入你的 DeepSeek API Key
```

`config/local.secrets.yml` 已被 Git 忽略。

### 3. 运行

```bash
python main.py
```

带文档和文献运行：

```bash
python main.py --files report.docx proposal.pdf --refs my_refs.bib
```

带用户锁定技术事实运行：

```bash
python main.py --locked-tech-spec config/locked_tech_spec.example.json
```

---

## 测试

```bash
# 离线测试：不调用 API
PYTHONIOENCODING=utf-8 python tests/run_offline.py

# 配置 schema 检查
PYTHONIOENCODING=utf-8 python tests/config_schema_check.py

# Pytest 单元测试
PYTHONIOENCODING=utf-8 python -m pytest tests/unit/ -v
```

离线测试入口覆盖模型行为、写作辅助函数、静态规则、大纲规范化、Markdown fixture、outline fixture，以及 writer/evaluator 对齐。CI 会在 push 和 pull request 时运行这些检查。

---

## 仓库安全边界

这个公开快照来自干净仓库。以下敏感内容不会进入 Git：

- `config/local.secrets.yml`
- `outputs/`
- `cache/`
- `uploads/`
- 生成论文
- 本地数据库
- 个人简历产物
- 真实用户文档

详见 [`PUBLICATION_CHECKLIST.md`](PUBLICATION_CHECKLIST.md)。

---

## 建议阅读路径

如果你想快速了解这个项目：

1. 先看 [`main.py`](main.py)，了解 CLI 使用面。
2. 再看 [`src/controller.py`](src/controller.py)，了解状态机编排。
3. 阅读 [`src/validation/README.md`](src/validation/README.md)，了解质量门禁。
4. 阅读 [`src/diagnosis/README.md`](src/diagnosis/README.md)，了解可观测性设计。
5. 运行 `PYTHONIOENCODING=utf-8 python tests/run_offline.py`。

---

## License

MIT
