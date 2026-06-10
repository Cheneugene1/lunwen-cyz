# LunWen CYZ

> **CLI Agent for long-form academic writing with quality evaluation loops, traceable execution, and agent-readable knowledge base.**

一个面向长文本生成的 CLI Agent：状态机驱动的流程编排、多源文献检索、逐章生成与修订、LLM 评分 + 静态规则双重质量评估、JSONL 运行诊断与可解释指标。

---

## 为什么这是一个 Agent 项目

不是「调 API 生成一段文字」的脚本。区别在这里：

| 维度 | 普通脚本 | 这个项目 |
|------|----------|----------|
| **流程控制** | 线性顺序执行 | 状态机：`PARSE → PLAN → SEARCH → DRAFT ⇄ EVAL ⇄ REVISE → DONE` |
| **决策闭环** | 一次生成 | 评估→修订→再评估，error 清零 + 低分保护双门禁 |
| **可观测性** | 无 | JSONL 事件流 + 阶段耗时 + 规则命中/消解率 + 用户时间节省估算 |
| **模块文档** | 无或过时 | 4 个 Agent 入口 README（含 YAML front matter / `sync_rule` 维护契约） |
| **测试** | 无 | 54 项离线测试 + fixture 引擎 + 大纲规则验证 + 配置 schema 检查 |
| **隐私** | 密钥硬编码 | `local.secrets.yml` 不进 Git，运行时产物全部 `.gitignore` |

---

## 架构总览

```
main.py                     # CLI 入口（argparse）
  └── controller.py         # 状态机决策控制器
        ├── parser.py       # 多格式文件解析（docx/pdf/pptx/xlsx/csv/txt/图片）
        ├── planner.py      # 意图理解 + 大纲生成 + 大纲评分门禁（28 条硬规则 + LLM 五维语义评分）
        ├── retriever.py    # OpenAlex / Crossref / arXiv / Semantic Scholar 自适应检索
        ├── ref_store.py    # 文献池管理（去重、质量清洗、同义词映射）
        ├── writing/        # 撰写子包
        │   ├── draft_engine.py     # 逐章生成（并行/串行、多候选、scope 校验）
        │   ├── revision_engine.py  # 逐章修订（最小干预原则、顽固问题专项修复）
        │   ├── abstract.py         # 中英文摘要生成与校验
        │   ├── postprocess.py      # 后处理（引用位置、术语统一、越界清洗、标点）
        │   ├── term_map.py         # 全文术语映射（主控/传感器型号统一）
        │   ├── tech_spec.py        # LLM 技术规格生成
        │   └── locked_tech_spec.py # 用户锁定技术事实（L1）
        ├── validation/     # 评估子包
        │   └── evaluator.py        # LLM 四维评分 + 毕业论文静态规则（error/warning 分级）
        ├── diagnosis/      # 可观测性子包（零 LLM 依赖）
        │   ├── recorder.py         # JSONL 结构化事件采集
        │   ├── analyzer.py         # 规则聚合
        │   ├── metrics.py          # 可解释指标计算（生成耗时、规则命中/消解率、用户时间节省）
        │   └── report.py           # Rich 终端报告
        └── presenter.py    # 评估面板渲染（跨轮差分、顽固问题追踪）
```

---

## Agent-readable Knowledge Base

每个子系统都有带 YAML front matter 的入口 README，Agent 可以自动定位和使用：

| 模块 | 入口 | 内容 |
|------|------|------|
| 撰写 | [`src/writing/README.md`](src/writing/README.md) | 13 个模块职责、能力摘要、运行时序、公开 API |
| 评估 | [`src/validation/README.md`](src/validation/README.md) | 评分体系、静态规则、降级策略、配置依赖 |
| 诊断 | [`src/diagnosis/README.md`](src/diagnosis/README.md) | 事件类型表、JSONL 格式、可解释指标 API |
| 测试 | [`tests/README.md`](tests/README.md) | 离线/在线测试、fixture 规范 |
| 进度 | [`PROGRESS.md`](PROGRESS.md) | 已完成/进行中/阻塞，95+ 条功能点 |

每个 README 的 `sync_rule` 字段规定了「改哪段代码必须同步更新哪份文档」——这是给后续开发者和 Agent 看的维护契约。

---

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置密钥

```bash
cp config/config.example.yml config/local.secrets.yml
# 编辑 local.secrets.yml，填入 DeepSeek API Key
```

### 3. 运行

```bash
# 交互模式
python main.py

# 带文件和文献
python main.py --files report.docx proposal.pdf --refs my_refs.bib

# 分阶段调试
python main.py --phase plan          # 只跑到规划完成
python main.py --phase draft --plan outputs/plan_xxx.json     # 从已有规划续跑
python main.py --phase eval --plan outputs/plan_xxx.json --paper outputs/paper_xxx.md  # 独立评测
```

### 4. 运行诊断

```bash
# 查看阶段耗时与评估摘要
python -m src.diagnosis outputs/run_<session_id>.jsonl

# 查看可解释指标报告（生成耗时、规则命中/消解率、用户时间节省）
python -c "from src.diagnosis import print_explainability_summary; print_explainability_summary('outputs/run_xxx.jsonl')"
```

---

## 测试

```bash
# 离线测试（零 API 依赖，54 项）
python tests/run_offline.py

# 配置 schema 漂移检查
python tests/config_schema_check.py

# 单元测试
python -m pytest tests/unit/ -v
```

CI（[`.github/workflows/ci.yml`](.github/workflows/ci.yml)）：push/PR 自动跑离线测试 + 配置检查 + 单元测试。

---

## 配置说明

| 文件 | Git | 说明 |
|------|-----|------|
| `config/config.example.yml` | ✅ | 所有配置项模板（模型、检索、修订循环、字数体系、可解释指标） |
| `config/locked_tech_spec.example.json` | ✅ | 用户锁定技术事实示例 |
| `config/local.secrets.yml` | ❌ | 真实密钥，`.gitignore` 忽略 |

---

## 隐私

- `config/local.secrets.yml` 包含真实 API Key，**绝不提交**
- `outputs/` / `cache/` / `uploads/` 包含运行产物（论文内容、检索记录），**全部 `.gitignore`**
- 使用云端 API 时用户文档会经网络发送至服务商；敏感材料应脱敏后使用

详见 [`PUBLICATION_CHECKLIST.md`](PUBLICATION_CHECKLIST.md)。

---

## 目录结构

```
lunwencyz/
├── main.py                  # CLI 入口
├── PROGRESS.md              # 项目进度（Agent 入口）
├── PUBLICATION_CHECKLIST.md # 发布安全检查清单
├── requirements.txt
├── config/
│   ├── config.example.yml
│   └── locked_tech_spec.example.json
├── src/
│   ├── controller.py        # 状态机决策控制器
│   ├── planner.py           # 意图理解与大纲规划
│   ├── parser.py            # 多格式文件解析
│   ├── retriever.py         # 自适应文献检索
│   ├── ref_store.py         # 文献池管理
│   ├── llm.py               # LLM 客户端
│   ├── models.py            # Pydantic 数据模型
│   ├── config.py            # 配置加载
│   ├── writer.py            # Manuscript ↔ Markdown
│   ├── presenter.py         # 终端展示层
│   ├── writing/             # 撰写子包（13 模块）
│   │   └── README.md        # Agent 入口
│   ├── validation/          # 评估子包
│   │   └── README.md        # Agent 入口
│   └── diagnosis/           # 可观测性子包
│       ├── README.md        # Agent 入口
│       ├── metrics.py       # 可解释指标
│       ├── recorder.py      # 事件采集
│       └── report.py        # 终端报告
├── tests/                   # 测试（离线/在线/fixture/单元）
│   └── README.md            # Agent 入口
└── .github/workflows/ci.yml # CI
```

---

## License

MIT
