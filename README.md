# LunWen CYZ

> A CLI-first Agent for long-form academic writing: state-machine orchestration, multi-source literature retrieval, quality evaluation loops, traceable JSONL execution, and agent-readable module docs.

LunWen CYZ is not a "call an LLM and generate a paper" script. It is an experiment in turning a vague, high-friction writing task into an inspectable Agent workflow: parse materials, plan an outline, retrieve references, draft section by section, evaluate quality, revise with actionable feedback, and leave enough traces for a human or another Agent to debug the run.

The project was built around a practical question:

> Can a CLI Agent make long-form generation more controllable, observable, and maintainable than a one-shot prompt?

---

## Why This Project Matters

Long-form AI writing often fails in ways that are hard to see until the end: chapter drift, broken citations, hallucinated technical details, inconsistent terminology, missing sections, and vague "looks good" evaluations. This project treats those failures as engineering problems rather than prompt wording problems.

It introduces:

- **A real Agent control loop**: `PARSE -> PLAN -> SEARCH -> DRAFT -> EVAL -> REVISE -> DONE`
- **CLI-first operation**: interactive mode, batch arguments, phase-level debugging, session recovery, and independent paper evaluation
- **Quality gates**: static rules plus LLM scoring, with error/warning severity and actionable revision items
- **Traceability**: JSONL event logs, phase timing, rule hit counts, issue resolution metrics, and explainability summaries
- **Fact constraints**: generated TechSpec plus user-locked technical facts injected into writing and revision prompts
- **Agent-readable docs**: module README files with YAML front matter and `sync_rule` contracts for future contributors or coding Agents

---

## What Makes It an Agent

| Dimension | Simple LLM Script | LunWen CYZ |
| --- | --- | --- |
| Workflow | One linear generation call | State-machine orchestration across parsing, planning, search, drafting, evaluation, revision, and finalization |
| Control | Hidden prompt flow | CLI parameters, phase-level execution, resumable plans, and independent evaluation |
| Decision loop | Generate once | Evaluate, revise, re-evaluate, and stop by quality threshold or rule pass conditions |
| Grounding | Prompt-only context | Parsed user materials, manual references, public literature APIs, TechSpec, and locked technical facts |
| Observability | Console text at best | JSONL trace, diagnosis commands, explainability metrics, and cross-round issue deltas |
| Maintainability | Code is the only source of truth | Agent-readable module docs, sync rules, tests, fixtures, and progress notes |
| Privacy posture | Often unclear | Secrets, uploads, outputs, cache, and generated papers are excluded by default |

---

## Core Capabilities

### 1. CLI Agent Workflow

`main.py` exposes an argparse-based command-line interface:

```bash
python main.py
python main.py --files proposal.docx report.pdf --refs references.bib
python main.py --request "Write a thesis about V2X resource allocation" --files design.pdf
python main.py --locked-tech-spec config/locked_tech_spec.example.json
python main.py --session <session_id>
```

The CLI supports both interactive use and reproducible batch-style runs. It can also auto-detect files under `doc/` for local workflows.

### 2. Phase-Level Debugging

Long Agent runs need breakpoints. LunWen CYZ supports staged execution:

```bash
# Run only to outline planning
python main.py --phase plan --files proposal.docx

# Continue from an existing plan
python main.py --phase draft --plan outputs/plan_xxx.json

# Evaluate an existing Markdown paper without re-running generation
python main.py --phase eval --plan outputs/plan_xxx.json --paper outputs/paper_xxx.md
```

This makes the Agent easier to debug, replay, and demonstrate.

### 3. Multi-Source Retrieval and Reference Pool

The retrieval layer integrates public academic sources:

- OpenAlex
- Crossref
- arXiv
- Semantic Scholar
- Manual `.bib` / `.csv` reference imports

References are merged, deduplicated, scored, and stored in a local reference pool.

### 4. Quality Evaluation Loop

The project combines static checks and LLM-based evaluation. Static rules inspect issues such as:

- abstract length and citation markers
- citation position
- chapter overflow
- missing subsections
- placeholder residue
- model / hardware terminology consistency
- citation scope violations

The LLM evaluator produces structured scores and actionable revision items. The controller then decides whether to stop, revise, or run targeted fixes for stubborn issues.

### 5. Traceable Execution

Each run can emit structured JSONL events under `outputs/run_<session>.jsonl`. Diagnosis tools can summarize:

- phase transitions
- total wall time
- generation time
- rule hits
- issue resolution rate
- user time saved estimate
- stubborn issue trends

```bash
python -m src.diagnosis outputs/run_<session_id>.jsonl

python -c "from src.diagnosis import print_explainability_summary; print_explainability_summary('outputs/run_xxx.jsonl')"
```

### 6. Agent-Readable Knowledge Base

This repository intentionally keeps module docs close to code. These files are designed for humans and coding Agents:

| Module | Entry doc | Purpose |
| --- | --- | --- |
| Writing | [`src/writing/README.md`](src/writing/README.md) | Drafting, revision, TechSpec, term mapping, post-processing, public APIs |
| Validation | [`src/validation/README.md`](src/validation/README.md) | Scoring system, static rules, fallback behavior, evaluator contract |
| Diagnosis | [`src/diagnosis/README.md`](src/diagnosis/README.md) | JSONL event schema, diagnosis commands, explainability metrics |
| Tests | [`tests/README.md`](tests/README.md) | Offline tests, fixtures, outline rules, config schema checks |
| Progress | [`PROGRESS.md`](PROGRESS.md) | Implementation history, completed work, current constraints |

Each entry doc includes YAML front matter and a `sync_rule`, so later contributors know which docs must be updated when code changes. This is a lightweight knowledge-base pattern for Agent-assisted development.

---

## Architecture Overview

```text
main.py
  └── src/controller.py              # State-machine controller
        ├── parser.py                # Multi-format document parsing
        ├── planner.py               # Intent understanding, outline generation, outline evaluation
        ├── retriever.py             # OpenAlex / Crossref / arXiv / Semantic Scholar retrieval
        ├── ref_store.py             # Reference pool, deduplication, scoring, synonym mapping
        ├── writing/
        │   ├── draft_engine.py      # Section-by-section drafting
        │   ├── revision_engine.py   # Revision and stubborn issue repair
        │   ├── abstract.py          # Chinese / English abstract generation
        │   ├── postprocess.py       # Citation, punctuation, overflow, and terminology cleanup
        │   ├── term_map.py          # Global terminology normalization
        │   ├── tech_spec.py         # LLM-generated technical specification
        │   └── locked_tech_spec.py  # User-locked technical facts
        ├── validation/
        │   └── evaluator.py         # Static rules + LLM scoring
        ├── diagnosis/
        │   ├── recorder.py          # JSONL event recording
        │   ├── analyzer.py          # Rule aggregation
        │   ├── metrics.py           # Explainability metrics
        │   └── report.py            # Rich terminal reports
        └── presenter.py             # Evaluation panel rendering
```

---

## Quick Start

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure secrets

```bash
cp config/config.example.yml config/local.secrets.yml
# Edit config/local.secrets.yml and set your DeepSeek API key.
```

`config/local.secrets.yml` is ignored by Git.

### 3. Run

```bash
python main.py
```

Example with documents and references:

```bash
python main.py --files report.docx proposal.pdf --refs my_refs.bib
```

Example with locked technical facts:

```bash
python main.py --locked-tech-spec config/locked_tech_spec.example.json
```

---

## Tests

```bash
# Offline checks: no API calls
PYTHONIOENCODING=utf-8 python tests/run_offline.py

# Config schema check
PYTHONIOENCODING=utf-8 python tests/config_schema_check.py

# Pytest unit tests
PYTHONIOENCODING=utf-8 python -m pytest tests/unit/ -v
```

The offline test entry covers model behavior, writer helpers, static rules, planner normalization, Markdown fixtures, outline fixtures, and writer/evaluator alignment. CI runs the same offline checks on push and pull request.

---

## Repository Safety

This public snapshot was prepared from a clean repository. Sensitive files are intentionally excluded:

- `config/local.secrets.yml`
- `outputs/`
- `cache/`
- `uploads/`
- generated papers
- local databases
- personal resume artifacts
- real user documents

See [`PUBLICATION_CHECKLIST.md`](PUBLICATION_CHECKLIST.md) for the publication safety checklist.

---

## Suggested Reading Path

If you are reviewing the project quickly:

1. Start with [`main.py`](main.py) to see the CLI surface.
2. Read [`src/controller.py`](src/controller.py) for the state-machine orchestration.
3. Read [`src/validation/README.md`](src/validation/README.md) to understand the quality gate.
4. Read [`src/diagnosis/README.md`](src/diagnosis/README.md) to understand traceability.
5. Run `PYTHONIOENCODING=utf-8 python tests/run_offline.py`.

---

## License

MIT
