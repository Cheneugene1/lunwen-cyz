# Publication Checklist

发布前逐项确认。每一项都标注：**P0 = 不修不能公开**；**P1 = 公开前建议修**；**P2 = 公开后优化**。

---

## P0 — 文件安全网（不修不能公开）

- [ ] `.gitignore` 已忽略：
  - `.claude/` — Claude Code IDE 项目级配置与 session 数据
  - `config/local.secrets.yml` — 真实 API Key
  - `resume_output/` — 个人简历运行产物
  - `RESUME_*.md` — 个人简历上下文与教师风格分析
  - `doc/*.doc` / `doc/*.docx` — 真实开题报告（含姓名、学号、导师）
  - `doc/*.pdf` / `doc/*.png` / `doc/*.jpg` — 论文截图/导出 PDF
  - `doc/PROJECT_DOCUMENTATION.md` — 含项目内部文档截图
  - `outputs/` / `cache/` / `uploads/` — 运行时产物（论文内容、检索记录、SQLite 会话库）

- [ ] 已从 Git 跟踪中移除：
  - `doc/信息学院赵江滔本科毕业设计...doc` — rm --cached 但文件保留本地

- [ ] 不存在的风险确认：
  - `config/local.secrets.yml` 从未被提交 ✓（已在 .gitignore）
  - `outputs/` `cache/` `uploads/` 从未被提交 ✓（已在 .gitignore）
  - `.claude/` 从未被提交 ✓（现已加入 .gitignore）

- [ ] 仓库策略选择（二选一，附理由）：
  - **方案 A：新建干净仓库**（推荐）
    - 从当前工作树 clone → 检查无敏感文件 → 推 GitHub
    - 优点：彻底无历史残留，操作简单
    - 缺点：丢失 commit 历史
  - **方案 B：BFG Repo-Cleaner 重写历史**
    - 优点：保留 commit 历史
    - 缺点：有残留风险，需用 `git reflog expire` + `git gc --aggressive` 验证
    - 见：https://rtyley.github.io/bfg-repo-cleaner/

---

## P1 — 公开前建议修

- [ ] CI 工作流优化（[`.github/workflows/ci.yml`](.github/workflows/ci.yml)）：
  - `run_offline.py` 已覆盖 fixture / outline fixture / 静态规则 / writer-evaluator 对齐等 ✅
  - `config_schema_check.py` 已运行 ✅
  - 最后一步 `pytest tests/unit/` 覆盖不全——`tests/unit/` 仅 4 个文件，但 `run_offline.py` 已做了大量检查。可保留不改，或改名 `Pytest unit tests` 避免误导
- [ ] `doc/reports/design_explainability_metrics.md` 已纳入跟踪 ✓
- [ ] `doc/reports/2026-06-02_*.md`（问题调查报告 + 修复方案）已核实无 PII/无密钥 ✓

---

## P2 — 公开后优化

- [ ] 更新根目录 `README.md`：
  - 项目定位改为 CLI Agent（而非单纯“论文写作助手”）
  - 标题建议：`LunWen CYZ: CLI Agent for Long-form Academic Writing`
  - 加一节「Agent-readable Knowledge Base」介绍模块 README
- [ ] 新增 `examples/` 目录，放脱敏示例：
  - `examples/sample_request.md` — 示例用户需求
  - `examples/sample_refs.csv` — 示例手动文献
  - `examples/locked_tech_spec.sample.json` — 锁定技术规格示例
  - `examples/sample_run_log.jsonl` — 脱敏运行日志
- [ ] 是否加 License（MIT / Apache 2.0）
- [ ] 是否加 `CONTRIBUTING.md` 或 `DEVELOPMENT.md`

---

## 已确认安全的公开文件

| 文件/目录 | 说明 |
|-----------|------|
| `main.py` `smoke_test.py` | CLI 入口与冒烟测试 |
| `src/` | 全部源码（agent / writing / validation / diagnosis） |
| `tests/` | 离线测试 / fixture / 单元测试 |
| `config/config.example.yml` | 配置模板，密钥为占位符 |
| `config/locked_tech_spec.example.json` | 示例数据（STC89C52RC/DHT11），无个人信息 |
| `src/writing/README.md` | Agent 入口文档 |
| `src/validation/README.md` | Agent 入口文档 |
| `src/diagnosis/README.md` | Agent 入口文档 + 可解释指标 |
| `tests/README.md` | 测试基础设施说明 |
| `PROGRESS.md` | 项目进度总览 |
| `requirements.txt` | Python 依赖 |
| `.github/workflows/ci.yml` | CI 工作流 |
| `doc/reports/2026-06-02_*.md` | 调试报告（无 PII/无密钥） |
| `doc/reports/design_explainability_metrics.md` | 可解释指标设计文档 |
