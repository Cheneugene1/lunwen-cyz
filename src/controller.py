"""
决策控制器 — 状态机
状态流转：
  INIT → PARSE → INGEST_MANUAL_REFS → PLAN →
  [OUTLINE_REVIEW（若 ask_confirm_outline=true）] →
  SEARCH（循环至 max_search_rounds 或 |refs| >= min_references）→
  DRAFT → EVAL →
  若 score < threshold 且修订轮数未达上限 → REVISE → EVAL（循环）→
  DONE

每个状态都有明确的进入/执行/退出动作和向用户的输出。
撰写与评估子系统的说明见 src/writing/README.md、src/validation/README.md；
运行诊断（JSONL）见 src/diagnosis/README.md；总进度见 PROGRESS.md。
"""

import json
import logging
import time
import uuid
from enum import Enum
from pathlib import Path
from typing import Callable, List, Optional

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn

from .config import get
from .cover import CoverInfo, collect_cover_info_interactive, render_cover, render_toc
from .validation import _check_thesis_rules, evaluate
from .manual_refs import ingest_manual_refs
from .memory import ConversationMemory
from .models import Evaluation, Manuscript, WritingPlan, StaticRuleIssue
from .parser import parse_files
from .planner import evaluate_outline, generate_plan, outline_to_markdown, revise_outline, revise_outline_fix_errors, update_plan_from_user
from .ref_store import _build_synonym_map, ReferenceStore
from .retriever import run_expanded_search, run_search
from .diagnosis import RunRecorder
from .presenter import render_eval_panel, render_qa_panel, render_stubborn_panel
from .writing import (
    check_revision_compliance,
    draft_manuscript,
    revise_manuscript,
    postprocess_manuscript,
    build_global_term_map,
    stubborn_targeted_fix,
    reorder_citations_by_first_appearance,
    _build_ref_list_section,
)
from .writing.revision_helpers import (
    actionable_coarse_delta,
    extract_stubborn_actionable_items,
    identify_stubborn_issues,
    static_issue_delta_by_rule_id,
)

logger = logging.getLogger(__name__)
console = Console()


class Phase(str, Enum):
    INIT            = "INIT"
    PARSE           = "PARSE"
    INGEST_MANUAL   = "INGEST_MANUAL_REFS"
    PLAN            = "PLAN"
    OUTLINE_EVAL    = "OUTLINE_EVAL"
    OUTLINE_REVIEW  = "OUTLINE_REVIEW"
    SEARCH          = "SEARCH"
    DRAFT           = "DRAFT"
    EVAL            = "EVAL"
    REVISE          = "REVISE"
    DONE            = "DONE"
    ERROR           = "ERROR"


class AgentController:
    """
    论文写作智能体核心控制器。
    外部通过 run_interactive() 或 run_batch() 驱动。
    """

    def __init__(
        self,
        doc_files: Optional[List[str]] = None,
        ref_files: Optional[List[str]] = None,
        user_request: str = "",
        session_id: Optional[str] = None,
        db_path: Optional[Path] = None,
        output_dir: Optional[Path] = None,
        cover_info: Optional[CoverInfo] = None,
        locked_tech_spec_path: str | None = None,
        # 交互回调（CLI 中为 input()，测试可注入 mock）
        ask_user: Optional[Callable[[str], str]] = None,
    ):
        self.doc_files  = doc_files or []
        self.ref_files  = ref_files or []
        self.user_request = user_request
        self.session_id = session_id or str(uuid.uuid4())[:8]
        self.output_dir = output_dir or Path("outputs")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.ask_user   = ask_user or input
        # None：撰写时仅由 load_locked_tech_spec 从配置读取；传 "" 可强制不读锁定文件
        self.locked_tech_spec_path = locked_tech_spec_path

        # 子模块
        if db_path is not None and db_path.is_dir():
            db_path = db_path / f"session_{self.session_id}.db"
        self.store  = ReferenceStore(db_path=db_path)
        self.memory = ConversationMemory(self.session_id, db_path=db_path)

        # 封面信息（毕业论文模式使用）
        self.cover_info: Optional[CoverInfo] = cover_info

        # 运行时状态
        self.phase: Phase = Phase.INIT
        self.plan:  Optional[WritingPlan]  = None
        self.draft: Optional[Manuscript]   = None
        self.last_eval: Optional[Evaluation] = None
        self.revision_round = 0
        self.search_round   = 0
        # 上一轮评估的静态规则问题（用于计算修订消解率）
        self._last_static_rule_issues: Optional[list[StaticRuleIssue]] = None
        self._last_actionable_items: Optional[list[str]] = None
        # 跨轮次顽固问题追踪
        self._stubborn_md: str = ""
        self._stubborn_items: list[str] = []
        self._prev_static_ids: set[str] = set()
        self._prev_actionable_items: list[str] = []

        # 从配置读循环参数
        self.max_search_rounds  = int(get("max_search_rounds",  3))
        self.min_references     = int(get("min_references",    10))
        self.max_revision_rounds= int(get("max_revision_rounds", 2))
        self.quality_threshold  = float(get("quality_threshold", 9.0))
        self.stop_on_rule_pass  = bool(get("stop_on_rule_pass", True))
        self.stop_on_rule_pass_min_score = float(get("stop_on_rule_pass_min_score", 8.0))
        self.ask_confirm_outline= bool(get("ask_confirm_outline", False))

        self._started_at = time.monotonic()  # 会话启动时刻（用于 total_wall_time_ms）

        self._diag = RunRecorder(
            self.session_id,
            self.output_dir,
            enabled=bool(get("diagnostics_enabled", True)),
        )

    # ── 状态转换 ───────────────────────────────────────────────

    def _set_phase(self, phase: Phase):
        self._diag.phase_transition(self.phase.value, phase.value)
        self.phase = phase
        self.memory.update_phase(phase.value)
        logger.info("→ 进入阶段：%s", phase.value)

    # ── 各阶段执行 ─────────────────────────────────────────────

    # 示例论文/参考格式文件的特征词（文件名中含有这些词时视为格式示例，不作事实来源）
    _EXAMPLE_PAPER_KEYWORDS = ("示例", "example", "样例", "参考论文", "格式", "模板")

    def _do_parse(self):
        """PARSE：解析上传的文档文件，自动区分'项目文档'与'示例论文'"""
        self._set_phase(Phase.PARSE)
        if not self.doc_files:
            console.print("[yellow]未提供文档文件，跳过解析阶段[/yellow]")
            self._bundle = None
            self._diag.parse_complete(block_count=0, truncation_applied=False)
            return

        # 将文件分为「项目文档」（开题/中期报告等）和「示例论文」（格式参考）
        project_files = []
        example_files = []
        for f in self.doc_files:
            fname = f.lower()
            is_example = (
                any(kw in fname for kw in self._EXAMPLE_PAPER_KEYWORDS)
                or (
                    # 不含学生姓名/学号的文件，且文件名含"论文"但不含常见报告词
                    "论文" in fname
                    and not any(w in fname for w in ("开题", "中期", "报告", "设计", "陈与争"))
                )
            )
            if is_example:
                example_files.append(f)
            else:
                project_files.append(f)

        if example_files:
            console.print(
                f"[dim]检测到 {len(example_files)} 个示例/格式参考文件（仅用于格式学习，不作技术事实）：[/dim]"
            )
            for ef in example_files:
                console.print(f"  [dim]{ef}[/dim]")

        # 优先解析项目文档（作为技术事实来源）
        files_to_parse = project_files + example_files  # 示例放后面，权重自然较低
        console.print(f"[cyan]正在解析 {len(files_to_parse)} 个文件"
                      f"（项目文档 {len(project_files)} 个，示例 {len(example_files)} 个）...[/cyan]")

        with _spinner("解析文档"):
            self._bundle = parse_files(files_to_parse)

        block_count = len(self._bundle.blocks)
        console.print(f"[green]✓ 解析完成：{block_count} 个文本块[/green]")

        if self._bundle.flags.get("truncation_applied"):
            console.print(Panel(
                self._bundle.truncation_message,
                title="⚠ 文档超长提示",
                border_style="yellow",
            ))

        self._diag.parse_complete(
            block_count=len(self._bundle.blocks),
            truncation_applied=bool(self._bundle.flags.get("truncation_applied")),
        )

    def _do_ingest_manual(self):
        """INGEST_MANUAL_REFS：导入手动文献"""
        self._set_phase(Phase.INGEST_MANUAL)
        if not self.ref_files:
            return

        console.print(f"[cyan]正在导入手动文献（{len(self.ref_files)} 个文件）...[/cyan]")
        with _spinner("导入文献"):
            added = ingest_manual_refs(self.ref_files, self.store)
        console.print(f"[green]✓ 手动文献导入：新增 {added} 条，{self.store.summary()}[/green]")
        self.memory.update_ref_pool_hash(len(self.store))

    def _do_plan(self):
        """PLAN：生成写作规划"""
        self._set_phase(Phase.PLAN)
        console.print("[cyan]正在分析需求并生成写作规划...[/cyan]")

        bundle = getattr(self, "_bundle", None)
        from .models import DocumentBundle
        if bundle is None:
            bundle = DocumentBundle()

        # 提取项目文档摘要（开题/中期报告中的技术事实，供 TechSpec 优先使用）
        doc_summary = ""
        if bundle and bundle.blocks:
            project_blocks = [
                b for b in bundle.blocks
                if any(kw in (b.source_file or "").lower()
                       for kw in ("开题", "中期", "报告", "陈与争"))
            ]
            # 取前 2000 字作为项目文档摘要
            doc_summary = "\n".join(b.text for b in project_blocks)[:2000]

        with _spinner("规划中"):
            self.plan = generate_plan(
                bundle=bundle,
                user_request=self.user_request,
                store=self.store,
                conversation_summary=self.memory.get_summary(),
            )

        # 把项目文档摘要挂到 plan 上，供 TechSpec 生成时使用
        if doc_summary and self.plan:
            self.plan._doc_summary = doc_summary

        outline_md = outline_to_markdown(self.plan)
        console.print(Markdown(outline_md))
        self.memory.update_outline_snapshot(outline_md)
        if self.plan:
            self._diag.plan_complete(
                outline_sections=len(self.plan.outline),
                n_keywords=len(self.plan.keywords),
            )

    def _do_outline_check(self):
        """OUTLINE_CHECK：大纲硬规则检查 + LLM 语义评分 + 自动修订循环 + 人工交互修订"""
        enabled = bool(get("outline_evaluation_enabled", False))
        if not enabled:
            return

        self._set_phase(Phase.OUTLINE_EVAL)
        threshold = float(get("outline_evaluation_threshold", 7.5))
        max_rounds = int(get("outline_evaluation_max_revision_rounds", 2))
        max_user_revisions = 3

        auto_round = 0
        user_revision_count = 0
        while auto_round <= max_rounds:
            result = evaluate_outline(self.plan, threshold=threshold)

            # ── 展示硬规则检查结果 ──
            hard_issues = result.get("hard_rule_issues", [])
            errors = [i for i in hard_issues if i["severity"] == "error"]
            warnings = [i for i in hard_issues if i["severity"] == "warning"]

            if result["total_score"] == 0.0 and errors:
                console.print(
                    "[yellow]⚠ 存在结构级硬规则错误（缺失章节/ID重复），已跳过语义评分，请优先修正[/yellow]"
                )

            if errors or warnings:
                lines = []
                if errors:
                    lines.append(f"**硬规则错误（{len(errors)} 条）**：")
                    for e in errors:
                        lines.append(f"- ❌ `{e['rule_id']}` {e['message']}")
                if warnings:
                    lines.append(f"**硬规则警告（{len(warnings)} 条）**：")
                    for w in warnings:
                        lines.append(f"- ⚠ `{w['rule_id']}` {w['message']}")
                console.print(Panel(
                    "\n".join(lines),
                    title="📏 大纲硬规则检查",
                    border_style="red" if errors else "yellow",
                ))

            # ── 展示语义评分 ──
            dims = result.get("dimension_scores", {})
            score_lines = [
                f"**总分：{result['total_score']:.1f} / 10**（阈值 {threshold}）",
                f"- 逻辑结构：{dims.get('logic', 0):.1f}　"
                f"内容深度：{dims.get('content_depth', 0):.1f}",
                f"- 可行性：{dims.get('feasibility', 0):.1f}　"
                f"规范符合：{dims.get('format_compliance', 0):.1f}",
                f"- 创新性：{dims.get('novelty', 0):.1f}",
            ]
            penalty = result.get("warning_penalty", 0.0)
            raw = result.get("raw_llm_score", 0.0)
            if penalty > 0:
                score_lines.append(
                    f"\n⚠ LLM 原始评分 {raw:.1f}，硬规则 warning 惩罚 -{penalty:.1f}"
                    f"（warning 共 {result['hard_warnings']} 条）"
                )
            console.print(Panel(
                "\n".join(score_lines) + "\n\n"
                + ("\n".join(f"• {item}" for item in result.get("actionable_items", []))
                   if result.get("actionable_items") else ""),
                title="📊 大纲语义评分",
                border_style="green" if result["passed"] else "yellow",
            ))

            # ── 人工交互修订（每次评分后都允许，但有上限）──
            if user_revision_count >= max_user_revisions:
                console.print(
                    f"[yellow]⚠ 已达手动修订上限（{max_user_revisions} 次），"
                    f"使用当前大纲继续后续流程[/yellow]"
                )
                break

            user_input = self._ask_outline_feedback()
            if user_input:
                user_revision_count += 1
                console.print("[cyan]正在根据你的意见修订大纲...[/cyan]")
                self.plan = revise_outline(self.plan, [user_input])
                if self.plan:
                    outline_md = outline_to_markdown(self.plan)
                    console.print(Markdown(outline_md))
                    console.print("[green]✓ 大纲已按你的意见修订，重新评估...[/green]")
                    auto_round = max(0, auto_round - 1)
                    continue
                else:
                    console.print("[red]大纲修订失败，保留原大纲[/red]")

            # ── 通过 → 继续 ──
            if result["passed"]:
                console.print("[green]✓ 大纲质量达标，继续后续流程[/green]")
                break

            # ── 自动修订轮次耗尽 → 继续（不阻断流程）──
            if auto_round >= max_rounds:
                console.print(
                    f"[yellow]大纲经过 {max_rounds} 轮自动修订仍未达到阈值 {threshold}，"
                    f"以当前大纲继续后续流程[/yellow]"
                )
                break

            # ── 自动修订（合并硬规则修复 + 语义优化，单次 LLM 调用）──
            items = result.get("actionable_items", [])
            if not items:
                console.print("[yellow]无修订建议，跳过本轮[/yellow]")
                break
            if errors:
                severity_parts = "、" .join(
                    f"{len(errors)} 条 error" if errors else "",
                )
                console.print(
                    f"[cyan]存在硬规则问题（{severity_parts}），"
                    f"与语义建议合并修订（第 {auto_round + 1}/{max_rounds} 轮）...[/cyan]"
                )
            else:
                console.print(
                    f"[cyan]正在进行第 {auto_round + 1}/{max_rounds} 轮自动大纲修订...[/cyan]"
                )
            self.plan = revise_outline(self.plan, items)
            if self.plan:
                console.print("[green]✓ 大纲已自动修订，重新评估...[/green]")
            else:
                console.print("[red]大纲修订失败，保留原大纲[/red]")
                break
            auto_round += 1

    def _ask_outline_feedback(self) -> str:
        """
        询问用户是否要对大纲提出修改意见。
        返回用户输入的文字（可为空），空字符串表示跳过。
        """
        try:
            console.print()
            response = self.ask_user(
                "✏️  输入修改意见让 LLM 修订大纲（如\"去掉光照采集，我的文档里没有\"），"
                "直接按 Enter 跳过："
            ).strip()
            return response
        except (EOFError, KeyboardInterrupt):
            return ""

    def _do_outline_review(self):
        """OUTLINE_REVIEW：暂停等待用户确认或修改大纲"""
        self._set_phase(Phase.OUTLINE_REVIEW)
        console.print(Panel(
            "当前大纲已展示在上方。\n"
            "• 直接按 Enter 确认并继续\n"
            "• 或粘贴修改后的大纲（以空行结束输入，输入 END 完成）",
            title="📋 大纲审阅",
            border_style="blue",
        ))

        lines = []
        while True:
            try:
                line = self.ask_user("")
            except EOFError:
                break
            if line.strip().upper() == "END" or (not line.strip() and not lines):
                break
            if line.strip().upper() == "END":
                break
            lines.append(line)

        if lines:
            user_outline = "\n".join(lines)
            self.plan = update_plan_from_user(self.plan, user_outline)
            console.print("[green]✓ 大纲已更新[/green]")
        else:
            console.print("[green]✓ 大纲已确认，继续流程[/green]")

    def _do_search(self):
        """SEARCH：多轮自适应检索 → 泛化补充 → 深度清洗（含兜底放宽）"""
        self._set_phase(Phase.SEARCH)

        # 配置一致性检查：阈值高于 _relevance_score 保底分将导致非嵌入式中文学术论文全灭
        min_score = float(get("min_ref_relevance_score", 0.03))
        _RELEVANCE_FLOOR = 0.02
        if min_score > _RELEVANCE_FLOOR:
            logger.warning(
                "min_ref_relevance_score=%s > _relevance_score 保底分=%s，"
                "非嵌入式中文学术论文将被全部丢弃！建议设为 0.01。",
                min_score, _RELEVANCE_FLOOR,
            )

        # 运行时同义词映射：提前生成，注入检索阶段相关性打分
        keywords = self.plan.keywords if self.plan else []
        synonym_map: dict = {}
        if keywords:
            console.print("[dim]正在生成关键词同义词映射…[/dim]")
            synonym_map = _build_synonym_map(keywords)

        # 新 session（search_round==0）至少跑一轮检索以刷新文献池
        while (
            self.search_round < self.max_search_rounds
            and (self.search_round == 0 or len(self.store) < self.min_references)
        ):
            self.search_round += 1
            console.print(
                f"[cyan]第 {self.search_round}/{self.max_search_rounds} 轮检索，"
                f"当前文献池：{len(self.store)} 条（目标 {self.min_references}）...[/cyan]"
            )
            with _spinner(f"检索中（第 {self.search_round} 轮）"):
                success = run_search(self.plan, self.store, synonym_map=synonym_map)

            if not success:
                console.print("[yellow]⚠ 所有检索源均失败，将使用手动文献继续[/yellow]")
                break

            console.print(f"[green]✓ {self.store.summary()}[/green]")
            self.memory.update_ref_pool_hash(len(self.store))

        # ── 泛化检索补充（C 方案）：主检索不够 → 用泛化词再搜一轮 ──
        if len(self.store) < self.min_references and keywords:
            console.print(
                f"[cyan]文献池 {len(self.store)} 条不足 {self.min_references}，"
                f"尝试泛化词补充检索...[/cyan]"
            )
            with _spinner("泛化检索中"):
                expanded_added = run_expanded_search(self.store, keywords, synonym_map=synonym_map)
            if expanded_added > 0:
                console.print(
                    f"[green]✓ 泛化检索新增 {expanded_added} 条，"
                    f"文献池共 {len(self.store)} 条[/green]"
                )
                self.memory.update_ref_pool_hash(len(self.store))
            else:
                console.print("[dim]泛化检索无新增文献[/dim]")

        if len(self.store) == 0:
            console.print("[yellow]⚠ 文献池为空，将生成基于文档内容的初稿[/yellow]")
        elif len(self.store) < self.min_references:
            console.print(
                f"[yellow]文献池仅 {len(self.store)} 条，未达目标 {self.min_references}，继续撰写[/yellow]"
            )

        # ── 深度清洗（含 B 方案兜底放宽 + 运行时同义词映射）──
        max_total = int(get("max_refs_total", 40))
        removed = self.store.cull_poor_quality(
            keywords=keywords,
            max_total=max_total,
            min_refs_to_keep=self.min_references,
            synonym_map=synonym_map,
        )
        if removed > 0:
            low_conf = sum(1 for r in self.store.all_refs() if r.low_confidence)
            msg = f"文献池清洗完成：移除 {removed} 条，剩余 {len(self.store)} 条"
            if low_conf > 0:
                msg += f"（含 {low_conf} 条低置信兜底文献）"
            console.print(f"[cyan]{msg}[/cyan]")
        if len(self.store) < self.min_references:
            console.print(
                f"[yellow]清洗后文献仅 {len(self.store)} 条，"
                f"部分引用可能不足，建议通过 --refs 补充手动文献[/yellow]"
            )
        self._diag.search_complete(
            search_rounds=self.search_round,
            ref_pool_size=len(self.store),
            target_refs=self.min_references,
        )

    def _do_collect_cover(self):
        """
        在 PLAN 完成后、DRAFT 开始前，收集封面信息。
        毕业论文模式（thesis_mode=true）且未通过参数传入时才交互询问。
        """
        thesis_mode = bool(get("thesis_mode", False))
        if not thesis_mode:
            return
        if self.cover_info:
            return  # 已由命令行参数传入，无需询问

        try:
            console.print()
            self.cover_info = collect_cover_info_interactive()
            console.print("[green]✓ 封面信息已收集[/green]")
        except (EOFError, KeyboardInterrupt):
            console.print("[yellow]⚠ 封面信息收集已跳过，请在输出文件中手动填写[/yellow]")
            self.cover_info = CoverInfo()

    def _do_draft(self):
        """DRAFT：生成初稿（先收集封面信息，然后撰写章节，最后附加封面+目录）"""
        self._set_phase(Phase.DRAFT)

        # 封面信息收集（仅毕业论文模式）
        self._do_collect_cover()

        console.print("[cyan]正在撰写论文初稿...[/cyan]")
        with _spinner("撰写中"):
            self.draft = draft_manuscript(
                self.plan,
                self.store,
                user_request=self.user_request,
                locked_tech_spec_path=self.locked_tech_spec_path,
            )

        # 附加封面与目录
        thesis_mode = bool(get("thesis_mode", False))
        if thesis_mode and self.plan:
            cover_info = self.cover_info or CoverInfo(
                title_zh=self.plan.title or "", title_en=self.plan.title_en or ""
            )
            self.draft.cover_text = render_cover(cover_info)
            self.draft.toc_text = render_toc(self.plan)

        self.memory.update_draft_version(self.draft.version)
        word_count = sum(len(s.markdown_body) for s in self.draft.sections)
        console.print(f"[green]✓ 初稿完成：{len(self.draft.sections)} 章节，约 {word_count} 字[/green]")
        self._diag.draft_complete(
            n_sections=len(self.draft.sections),
            version=self.draft.version,
            approx_chars=word_count,
        )

    def _do_eval(self) -> Evaluation:
        """EVAL：质量评估（先对当前稿做一次全文规整，再交给评估，减少「可自动修却因未修而扣分」）。"""
        self._set_phase(Phase.EVAL)
        console.print("[cyan]正在评估论文质量...[/cyan]")

        console.print(
            "[dim]评估前全文规整（引用位置、章节越界、术语合并、个人感悟句）…[/dim]"
        )
        tm_result = build_global_term_map(self.draft)
        self.draft = postprocess_manuscript(
            self.draft, plan=self.plan, term_map=tm_result["term_map"],
            tech_spec=self.draft.tech_spec, stc_dominant=tm_result.get("stc_dominant"),
        )

        with _spinner("评估中"):
            self.last_eval = evaluate(
                manuscript=self.draft,
                plan=self.plan,
                user_requirement=self.user_request,
                store=self.store,
            )

        ev = self.last_eval
        thesis_mode_ev = bool(get("thesis_mode", False))

        static_delta: dict | None = None
        coarse_delta: dict | None = None
        if self._last_static_rule_issues is not None:
            static_delta = static_issue_delta_by_rule_id(
                self._last_static_rule_issues,
                ev.static_rule_issues or [],
            )
        if self._last_actionable_items is not None:
            coarse_delta = actionable_coarse_delta(
                self._last_actionable_items,
                ev.actionable_items or [],
                include_keyword_hints=bool(
                    get("actionable_fingerprint_include_keywords", False)
                ),
            )

        # ── 终端展示：委托 presenter ──
        render_eval_panel(ev, self.quality_threshold)
        render_qa_panel(
            ev, static_delta, coarse_delta,
            thesis_mode=thesis_mode_ev,
        )

        self._last_static_rule_issues = list(ev.static_rule_issues or [])
        self._last_actionable_items = list(ev.actionable_items or [])

        # 跨轮次顽固问题追踪（从第2轮开始生效）
        if self.revision_round >= 1 and self._prev_actionable_items:
            cur_static = {s.rule_id for s in (ev.static_rule_issues or [])}
            self._stubborn_md = "\n".join(
                identify_stubborn_issues(
                    self._prev_actionable_items,
                    ev.actionable_items or [],
                    self._prev_static_ids,
                    cur_static,
                )
            )
            self._stubborn_items = extract_stubborn_actionable_items(
                self._prev_actionable_items,
                ev.actionable_items or [],
            )
            if self._stubborn_md:
                render_stubborn_panel(self._stubborn_md)
        self._prev_static_ids = {s.rule_id for s in (ev.static_rule_issues or [])}
        self._prev_actionable_items = list(ev.actionable_items or [])

        # ── 静态规则分桶（供可解释指标）──
        breakdown: dict = {"by_severity": {}, "by_category": {}, "by_rule_id": {}}
        for issue in (ev.static_rule_issues or []):
            sev = issue.severity
            cat = issue.rule_category
            rid = issue.rule_id
            breakdown["by_severity"][sev] = breakdown["by_severity"].get(sev, 0) + 1
            breakdown["by_category"][cat] = breakdown["by_category"].get(cat, 0) + 1
            breakdown["by_rule_id"][rid] = breakdown["by_rule_id"].get(rid, 0) + 1

        self._diag.evaluation(
            revision_round=self.revision_round,
            score_total=ev.score_total,
            n_actionable_items=len(ev.actionable_items or []),
            n_static_rule_issues=len(ev.static_rule_issues or []),
            structure=ev.dimensions.structure,
            logic=ev.dimensions.logic,
            language=ev.dimensions.language,
            alignment=ev.dimensions.alignment,
            threshold=self.quality_threshold,
            static_delta=static_delta,
            actionable_coarse_delta=coarse_delta,
            static_rule_breakdown=breakdown,
            stubborn_count=len(self._stubborn_items),
        )
        return ev

    def _do_revise(self):
        """REVISE：根据评估建议修订；修订后自检未修正项并重修订一次（同轮内）。"""
        self._set_phase(Phase.REVISE)
        self.revision_round += 1
        console.print(
            f"[cyan]第 {self.revision_round}/{self.max_revision_rounds} 轮修订...[/cyan]"
        )
        console.print(
            "[dim]修订前全文规整（术语合并、引用位置、越界与个人感悟句）…[/dim]"
        )
        tm_pre = build_global_term_map(self.draft)
        self.draft = postprocess_manuscript(
            self.draft,
            plan=self.plan,
            term_map=tm_pre["term_map"],
            tech_spec=self.draft.tech_spec,
            stc_dominant=tm_pre.get("stc_dominant"),
        )
        with _spinner("修订中"):
            self.draft, term_map = revise_manuscript(
                manuscript=self.draft,
                plan=self.plan,
                store=self.store,
                actionable_items=self.last_eval.actionable_items,
                stubborn_issues_md=self._stubborn_md,
            )

        if term_map:
            console.print(
                f"[cyan]全文术语统一：{', '.join(f'{k}→{v}' for k,v in term_map.items())}[/cyan]"
            )

        # ── 修订自检：未修正项在同轮内重修订一次 ──
        items = self.last_eval.actionable_items or []
        if len(items) >= 3:
            console.print("[dim]修订自检中…[/dim]")
            _, unfixed = check_revision_compliance(
                self.draft, self.plan, items,
            )
            if unfixed and len(unfixed) < len(items):
                console.print(
                    f"[yellow]⚠ 自检发现 {len(unfixed)}/{len(items)} 条仍未修正，"
                    "同轮内重修订一次[/yellow]"
                )
                with _spinner("自检后重修订"):
                    self.draft, term_map2 = revise_manuscript(
                        manuscript=self.draft,
                        plan=self.plan,
                        store=self.store,
                        actionable_items=unfixed,
                        stubborn_issues_md="",
                    )
                if term_map2:
                    console.print(
                        f"[cyan]重修订术语统一：{', '.join(f'{k}→{v}' for k,v in term_map2.items())}[/cyan]"
                    )
            elif unfixed:
                console.print(
                    f"[yellow]⚠ 自检发现 {len(unfixed)}/{len(items)} 条仍未修正"
                    "（但已无缩小空间，跳过重修订）[/yellow]"
                )
            else:
                console.print("[green]✓ 修订自检通过，全部建议已修正[/green]")

        # ── 顽固问题专项修复：跨轮次未消解项做针对性 LLM 调用 ──
        if self._stubborn_items:
            console.print("[cyan]顽固问题专项修复中…[/cyan]")
            with _spinner("专项修复"):
                self.draft, tmap3 = stubborn_targeted_fix(
                    manuscript=self.draft,
                    plan=self.plan,
                    store=self.store,
                    stubborn_items=self._stubborn_items,
                )
            if tmap3:
                console.print(
                    f"[cyan]专项修复术语统一：{', '.join(f'{k}→{v}' for k,v in tmap3.items())}[/cyan]"
                )
            console.print(
                f"[green]✓ 顽固专项修复完成（{len(self._stubborn_items)} 个顽固项）[/green]"
            )
            self._stubborn_items = []  # 本轮已处理

        # ── 修订后新生问题检测：跑一遍静态规则，对比修订前是否有新增 ──
        pre_count = len(self.last_eval.static_rule_issues) if self.last_eval else 0
        post_issues = _check_thesis_rules(self.draft, self.plan, self.store)
        post_count = len(post_issues)
        if post_count > pre_count:
            new_ids = {i.rule_id for i in post_issues} - {
                i.rule_id for i in (self.last_eval.static_rule_issues or [])
            }
            console.print(
                f"[yellow]⚠ 修订后静态规则数 {pre_count}→{post_count}"
                f"（新增 {post_count - pre_count} 条：{', '.join(sorted(new_ids))}），"
                f"修订可能引入了新问题[/yellow]"
            )

        # ── 引用一致性轻量检查：正文中 [N] 序号是否超出文献池范围 ──
        import re as _re
        full_text = self.draft.to_markdown()
        cited_nums = {int(m) for m in _re.findall(r"\[(\d+)\]", full_text) if m.isdigit()}
        ref_count = len(self.store)
        missing_refs = [n for n in cited_nums if n > ref_count]
        if missing_refs:
            console.print(
                f"[yellow]⚠ 正文引用了 {len(missing_refs)} 个不存在的文献序号"
                f"：{', '.join(f'[{n}]' for n in sorted(missing_refs)[:8])}"
                f"（文献池共 {ref_count} 条），请在下轮修订中修正或补全文献[/yellow]"
            )

        self.memory.update_draft_version(self.draft.version)
        console.print(f"[green]✓ 修订完成（版本 {self.draft.version}）[/green]")
        self._diag.revision_complete(
            revision_round=self.revision_round,
            term_map_keys=list(term_map.keys()),
            new_version=self.draft.version,
        )

    def _do_done(self):
        """DONE：最终全文后处理 + 引用重编号 + 保存"""
        self._set_phase(Phase.DONE)

        # 最终全文后处理：修引用位置 + 术语统一 + 删除个人感悟
        tm_result = build_global_term_map(self.draft)
        self.draft = postprocess_manuscript(
            self.draft, plan=self.plan, term_map=tm_result["term_map"],
            tech_spec=self.draft.tech_spec, stc_dominant=tm_result.get("stc_dominant"),
        )

        # 引用重编号：按正文首次出现顺序重排 [n]，仅列出被引用文献
        order_map = reorder_citations_by_first_appearance(self.draft, self.store)
        if order_map:
            # 重建 refs 章节：按新顺序生成文献列表
            refs_section = _build_ref_list_section(self.store, thesis_mode=True, order_map=order_map)
            new_sections = [refs_section if s.section_id == "refs" else s for s in self.draft.sections]
            self.draft.sections = new_sections

        keywords_part = "".join(self.plan.keywords[:2]) if self.plan and self.plan.keywords else ""
        keywords_part = "".join(c for c in keywords_part if c not in '/\\:*?"<>|')[:30]
        datestr = time.strftime("%Y%m%d")
        fname = f"paper_{self.session_id}_{keywords_part}_{datestr}_v{self.draft.version}.md" if keywords_part else f"paper_{self.session_id}_{datestr}_v{self.draft.version}.md"
        out_path = self.output_dir / fname
        out_path.write_text(self.draft.to_markdown(), encoding="utf-8")

        console.print(Panel(
            f"论文已生成！\n\n"
            f"📄 文件：{out_path}\n"
            f"📊 最终评分：{self.last_eval.score_total:.1f} / 10\n"
            f"📚 文献池：{self.store.summary()}",
            title="✅ 完成",
            border_style="bright_green",
        ))
        return str(out_path)

    # ── 主流程 ────────────────────────────────────────────────

    def run(self) -> str:
        """
        运行完整状态机。
        返回最终输出文件路径（或空字符串）。
        """
        try:
            self._diag.begin_run(
                user_request_chars=len(self.user_request or ""),
                n_doc_files=len(self.doc_files or []),
                n_ref_files=len(self.ref_files or []),
                quality_threshold=self.quality_threshold,
                max_revision_rounds=self.max_revision_rounds,
            )

            self._last_static_rule_issues = None
            self._last_actionable_items = None
            self._stubborn_md = ""
            self._stubborn_items = []
            self._prev_static_ids = set()
            self._prev_actionable_items = []

            self._do_parse()
            self._do_ingest_manual()
            self._do_plan()

            self._do_outline_check()

            if self.ask_confirm_outline:
                self._do_outline_review()

            self._do_search()
            self._do_draft()

            while True:
                ev = self._do_eval()

                # 达标或修订轮次耗尽 → 结束
                if ev.score_total >= self.quality_threshold:
                    console.print(
                        f"[green]✓ 论文达到质量阈值 {self.quality_threshold}，停止修订[/green]"
                    )
                    break
                thesis_mode = bool(get("thesis_mode", False))
                if (
                    self.stop_on_rule_pass
                    and thesis_mode
                    and not any(
                        getattr(x, "severity", "error") == "error"
                        for x in (ev.static_rule_issues or [])
                    )
                    and ev.score_total >= self.stop_on_rule_pass_min_score
                ):
                    console.print(
                        "[green]✓ 毕业论文 **error** 级别静态规则已全部通过，"
                        f"且 LLM 评分 {ev.score_total:.1f} ≥ {self.stop_on_rule_pass_min_score}，"
                        "按 stop_on_rule_pass 停止修订"
                        "（warning 仍可能存在；未达 quality_threshold 时可人工审阅）[/green]"
                    )
                    break
                if self.revision_round >= self.max_revision_rounds:
                    console.print(
                        f"[yellow]已达最大修订轮次 {self.max_revision_rounds}，停止[/yellow]"
                    )
                    break

                self._do_revise()

            out_path = self._do_done()
            self._diag.run_end(
                status="success",
                paper_path=out_path,
                final_score=self.last_eval.score_total if self.last_eval else None,
                total_wall_time_ms=(time.monotonic() - self._started_at) * 1000,
            )
            # ── 可解释指标报告 ──
            if get("explainability", {}).get("enabled", True):
                from .diagnosis import print_explainability_summary
                print_explainability_summary(str(self._diag.log_path))
            return out_path

        except KeyboardInterrupt:
            console.print("\n[red]用户中断[/red]")
            self._diag.run_end(
                status="interrupted",
                total_wall_time_ms=(time.monotonic() - self._started_at) * 1000,
            )
            return ""
        except Exception as e:
            logger.exception("控制器异常: %s", e)
            console.print(f"[red]❌ 发生错误：{e}[/red]")
            self._set_phase(Phase.ERROR)
            self._diag.run_end(
                status="error",
                error=str(e),
                total_wall_time_ms=(time.monotonic() - self._started_at) * 1000,
            )
            return ""

    # ── 分阶段运行与调试 ──────────────────────────────────────

    _PHASE_ORDER = [
        Phase.INIT, Phase.PARSE, Phase.INGEST_MANUAL, Phase.PLAN,
        Phase.SEARCH, Phase.DRAFT, Phase.EVAL, Phase.REVISE, Phase.DONE,
    ]

    def run_to_phase(self, target_phase: str, *, plan_path: str = "", paper_path: str = "") -> str:
        """
        从指定起点跑到 target_phase 停止。支持中途续跑和独立评测。

        用法：
          --phase plan          → 从头跑到规划完成，输出 plan.json
          --phase draft --plan plan.json  → 从已有规划续跑初稿
          --phase eval --plan plan.json --paper paper_v1.md  → 对已有论文独立评测
        """
        target_idx = next(
            (i for i, p in enumerate(self._PHASE_ORDER) if p.value == target_phase.upper()), -1
        )
        if target_idx < 0:
            console.print(f"[red]无效阶段: {target_phase}[/red]")
            return ""

        self._last_static_rule_issues = None
        self._last_actionable_items = None
        self._stubborn_md = ""
        self._stubborn_items = []
        self._prev_static_ids = set()
        self._prev_actionable_items = []

        # 判断起点：若提供了 plan_path 则跳过前面的阶段
        if plan_path:
            self.plan = self.load_plan(plan_path)
            self._diag.plan_complete(
                outline_sections=len(self.plan.outline),
                n_keywords=len(self.plan.keywords),
            )
            start_idx = self._PHASE_ORDER.index(Phase.SEARCH)
            if target_phase.upper() == "EVAL" and paper_path:
                return self._eval_paper_direct(paper_path)
        else:
            self._diag.begin_run(
                user_request_chars=len(self.user_request or ""),
                n_doc_files=len(self.doc_files or []),
                n_ref_files=len(self.ref_files or []),
                quality_threshold=self.quality_threshold,
                max_revision_rounds=self.max_revision_rounds,
            )
            start_idx = 0

        try:
            for phase in self._PHASE_ORDER[start_idx:]:
                if phase == Phase.INIT:
                    continue
                if phase == Phase.PARSE and Phase.PARSE.value in [p.value for p in self._PHASE_ORDER[start_idx:target_idx + 1]]:
                    self._do_parse()
                elif phase == Phase.INGEST_MANUAL and Phase.INGEST_MANUAL.value in [p.value for p in self._PHASE_ORDER[start_idx:target_idx + 1]]:
                    self._do_ingest_manual()
                elif phase == Phase.PLAN and Phase.PLAN.value in [p.value for p in self._PHASE_ORDER[start_idx:target_idx + 1]]:
                    self._do_plan()
                    if target_phase.upper() == "PLAN":
                        saved = self.save_plan()
                        console.print(f"[green]✓ 规划已保存: {saved}[/green]")
                elif phase == Phase.SEARCH and Phase.SEARCH.value in [p.value for p in self._PHASE_ORDER[start_idx:target_idx + 1]]:
                    self._do_search()
                elif phase == Phase.DRAFT and Phase.DRAFT.value in [p.value for p in self._PHASE_ORDER[start_idx:target_idx + 1]]:
                    self._do_draft()
                elif phase == Phase.EVAL and Phase.EVAL.value in [p.value for p in self._PHASE_ORDER[start_idx:target_idx + 1]]:
                    self._do_eval()
                elif phase == Phase.REVISE and Phase.REVISE.value in [p.value for p in self._PHASE_ORDER[start_idx:target_idx + 1]]:
                    while True:
                        ev = self._do_eval()
                        if ev.score_total >= self.quality_threshold:
                            break
                        if self.revision_round >= self.max_revision_rounds:
                            break
                        self._do_revise()
                elif phase == Phase.DONE and Phase.DONE.value in [p.value for p in self._PHASE_ORDER[start_idx:target_idx + 1]]:
                    return self._do_done()

                if phase.value == target_phase.upper():
                    break

            self._diag.run_end(
                status="success",
                total_wall_time_ms=(time.monotonic() - self._started_at) * 1000,
            )
            return ""

        except KeyboardInterrupt:
            console.print("\n[red]用户中断[/red]")
            return ""
        except Exception as e:
            logger.exception("控制器异常: %s", e)
            console.print(f"[red]❌ {e}[/red]")
            return ""

    def _eval_paper_direct(self, paper_path: str) -> str:
        """对已有 paper_*.md 独立评测（不触发修订循环）"""
        import re
        from .writing import parse_manuscript_from_md
        from .ref_store import ReferenceStore

        if not self.plan:
            console.print("[red]需要 --plan 参数提供规划文件[/red]")
            return ""

        self.draft = parse_manuscript_from_md(paper_path, self.plan)
        # 从 paper 文件名提取 session_id，自动加载同 session 的文献库
        if len(self.store) == 0:
            stem = Path(paper_path).stem
            m = re.match(r"paper_([a-f0-9]{8})", stem)
            if m:
                sid = m.group(1)
                db = self.output_dir / f"session_{sid}.db"
                if db.exists():
                    self.store = ReferenceStore(db_path=db)
                    console.print(f"[dim]已加载文献库: {db} ({len(self.store)} 条)[/dim]")
            if len(self.store) == 0:
                self.store = ReferenceStore()
                console.print("[dim]⚠ 未找到对应文献库，参考文献检查将不准确[/dim]")

        ev = self._do_eval()
        console.print(f"\n[bold]独立评测完成。总分: {ev.score_total:.1f} / 10[/bold]")
        return ""

    def save_plan(self) -> str:
        """保存规划到 outputs/plan_{session_id}.json"""
        if not self.plan:
            return ""
        path = self.output_dir / f"plan_{self.session_id}.json"
        path.write_text(self.plan.model_dump_json(indent=2, ensure_ascii=False), encoding="utf-8")
        return str(path)

    @staticmethod
    def load_plan(path: str) -> "WritingPlan":
        from .models import WritingPlan

        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return WritingPlan.model_validate(data)

    # ── 追加用户需求（多轮对话支持）─────────────────────────────

    def append_user_request(self, text: str):
        """用户在任意阶段补充需求，更新内部状态"""
        self.user_request = text
        self.memory.add_user(text)
        # 若已有规划，可重新规划（简化：直接返回，由外部决定是否重跑）


# ── 工具：Rich 进度 spinner ────────────────────────────────────

class _spinner:
    """with 语句友好的 spinner 上下文管理器"""
    def __init__(self, label: str):
        self._progress = Progress(
            SpinnerColumn(), TextColumn("[progress.description]{task.description}")
        )
        self._label = label
        self._task = None

    def __enter__(self):
        self._progress.start()
        self._task = self._progress.add_task(self._label)
        return self

    def __exit__(self, *_):
        self._progress.stop()
