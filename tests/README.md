---
agent_entry: true
slug: lunwencyz-tests
title: 测试基础设施
canonical_paths:
  - tests/run_offline.py
  - tests/run_online.py
  - tests/fixture_runner.py
  - tests/outline_fixture_runner.py
  - tests/config_schema_check.py
sync_rule: >
  新增/修改测试用例、fixture 文件或测试工具函数时，同步更新本文件；
  静态规则有增删改时必须同步更新对应 fixture。
related_docs:
  - PROGRESS.md
  - TEST_PLAN.md
---

# 测试基础设施

## 快速命令

```bash
# 离线测试（零依赖，~1s）
python tests/run_offline.py

# 在线测试（需 API Key）
python tests/run_online.py

# Config schema 检查
python tests/config_schema_check.py

# 单独跑 fixtures
python -c "from tests.fixture_runner import run_all_fixtures; run_all_fixtures()"
```

## 目录结构

```
tests/
  run_offline.py          # 离线测试入口 (CONFIG / MODELS / WRITER / STATIC_RULES / PLANNER / FIXTURES / ALIGNMENT)
  run_online.py           # 在线测试入口 (API Health / Retrieval / Eval)
  fixture_runner.py       # Markdown fixture 引擎 (YAML front matter → _check_thesis_rules)
  outline_fixture_runner.py  # YAML outline fixture 引擎 (_check_outline_hard_rules)
  config_schema_check.py  # 配置键结构对比 (local.secrets.yml vs config.example.yml)
  fixtures/
    mcu_consistency/      # MCU 型号一致性规则
    citation_position/    # 引用位置规则
    section_overflow/     # 章节越界规则
    abstract_rules/       # 摘要规则 (引用/长度)
    placeholder/          # 占位符残留规则
    outline_rules/        # 大纲硬规则 (YAML)
```

## Fixture 规范

### Markdown fixture

```markdown
---
case_id: my_test_case
expected_rules:
  - rule_id_to_expect
forbidden_rules:
  - rule_id_that_must_not_fire
exact_rules: false       # 可选，true 时实际规则必须完全等于 expected_rules
---

# 第1章 标题
...
```

### YAML outline fixture

```yaml
case_id: outline_test
thesis_mode: true
expected_rules:
  - THESIS_CHAPTER_PREFIX
forbidden_rules:
  - THESIS_EXTRA_SECTIONS
keywords:
  - keyword1
outline:
  - section_id: s1
    title: 引言
    bullets:
      - bullet1
```

## 验收标准

- `run_offline.py` 全部通过，耗时 < 5 秒
- 每个高风险规则至少 1 个 good + 1 个 bad fixture
- 修改 evaluator.py 后 fixture 能立刻反映规则行为变化
