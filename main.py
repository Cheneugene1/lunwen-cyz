"""
论文写作智能体 — CLI 入口
用法：
  python main.py                                        # 纯对话模式
  python main.py --files report.docx proposal.pdf      # 带文档
  python main.py --files report.docx --refs refs.bib   # 带文档 + 手动文献
  python main.py --request "研究主题：..." --files ... # 带预设需求
"""

import argparse
import logging
import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel

# 设置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
# 减少 httpx 和 openai 的调试噪音
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("openai").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

console = Console()


def _check_config() -> bool:
    """检查 DeepSeek API Key 是否已配置"""
    from src.config import get
    api_key = get("deepseek_api_key", "")
    if not api_key or api_key in ("PASTE_IN_LOCAL_SECRETS", ""):
        console.print(Panel(
            "未找到有效的 DeepSeek API Key。\n\n"
            "请按以下步骤配置：\n"
            "1. 复制 config/config.example.yml → config/local.secrets.yml\n"
            "2. 编辑 local.secrets.yml，填入真实的 deepseek_api_key\n\n"
            "local.secrets.yml 已被 .gitignore 忽略，不会提交到 Git。",
            title="⚠ 配置缺失",
            border_style="red",
        ))
        return False
    return True


def _parse_args():
    parser = argparse.ArgumentParser(
        description="论文写作智能体 — 对话式学术论文生成工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例：
  python main.py
  python main.py --files 开题报告.docx --refs references.bib
  python main.py --request "写一篇关于图神经网络的综述" --files design.pdf

分阶段调试：
  python main.py --phase plan --files 开题报告.docx   # 只生成框架
  python main.py --phase draft --plan outputs/plan_xxx.json  # 续跑初稿
  python main.py --phase eval --plan outputs/plan_xxx.json --paper outputs/paper_v1.md  # 独立评测
        """,
    )
    parser.add_argument(
        "--files", nargs="*", metavar="FILE",
        help="上传的文档文件（.docx/.pdf/.pptx/.txt/.xlsx/.csv/图片）",
    )
    parser.add_argument(
        "--refs", nargs="*", metavar="REF_FILE",
        help="手动文献文件（.bib 或 .csv）",
    )
    parser.add_argument(
        "--request", type=str, default="",
        metavar="TEXT",
        help="预设的论文写作需求描述",
    )
    parser.add_argument(
        "--output-dir", type=str, default="outputs",
        help="论文输出目录（默认 outputs/）",
    )
    parser.add_argument(
        "--locked-tech-spec",
        type=str,
        default=None,
        metavar="PATH",
        help="用户锁定 TechSpec JSON 路径（省略则用配置 locked_tech_spec_path；传空字符串则不读锁定文件）",
    )
    parser.add_argument(
        "--session", type=str, default=None,
        help="会话 ID（用于恢复历史对话）",
    )
    parser.add_argument(
        "--no-db", action="store_true",
        help="禁用 SQLite 持久化（仅内存模式）",
    )
    # ── 分阶段调试 ──────────────────────────────────────────────
    parser.add_argument(
        "--phase", type=str, default="",
        metavar="PHASE",
        help="运行到指定阶段停止: plan / draft / eval / revise / done",
    )
    parser.add_argument(
        "--plan", type=str, default="",
        metavar="PLAN_JSON",
        help="已有规划 JSON 路径（续跑 draft/eval/revise）",
    )
    parser.add_argument(
        "--paper", type=str, default="",
        metavar="PAPER_MD",
        help="已有论文 Markdown 路径（独立评测/修订）",
    )
    # ── 封面信息（毕业论文模式快速填写，省去交互询问步骤）
    cover_group = parser.add_argument_group("封面信息（毕业论文模式可选）")
    cover_group.add_argument("--title-zh",   default="", help="中文题目")
    cover_group.add_argument("--title-en",   default="", help="英文题目")
    cover_group.add_argument("--school",     default="", help="学院")
    cover_group.add_argument("--major",      default="", help="专业班级")
    cover_group.add_argument("--student-id", default="", help="学号")
    cover_group.add_argument("--student-name", default="", help="学生姓名")
    cover_group.add_argument("--advisor",    default="", help="指导教师")
    cover_group.add_argument("--year-month", default="", help="年月（如 2026年6月）")
    return parser.parse_args()


def _collect_request_interactive() -> str:
    """交互式收集用户研究需求"""
    console.print(Panel(
        "欢迎使用论文写作智能体！\n\n"
        "请描述您的研究主题和需求，例如：\n"
        "• 论文类型（综述/实验论文/设计报告）\n"
        "• 研究问题与目标\n"
        "• 目标期刊/课题要求\n"
        "• 特殊格式要求",
        title="📝 论文写作智能体",
        border_style="blue",
    ))
    console.print("[dim]输入您的需求（可多行，输入空行完成）：[/dim]")

    lines = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if not line.strip() and lines:
            break
        lines.append(line)

    return "\n".join(lines).strip()


def _auto_detect_doc_folder() -> list[str]:
    """
    自动扫描 doc/ 文件夹，返回支持的文件列表。
    如果 doc/ 存在且有内容，自动加载（无需用户手动 --files）。
    """
    from pathlib import Path
    from src.parser import SUPPORTED_SUFFIXES

    doc_dir = Path("doc")
    if not doc_dir.is_dir():
        return []

    files = []
    for f in sorted(doc_dir.iterdir()):
        if f.suffix.lower() in SUPPORTED_SUFFIXES and not f.name.startswith("."):
            files.append(str(f))

    return files


def _collect_files_interactive() -> tuple[list[str], list[str]]:
    """
    交互式询问文件路径。
    如果 doc/ 目录存在，自动提示加载其中的文件。
    """
    # 自动检测 doc/ 目录
    auto_docs = _auto_detect_doc_folder()
    if auto_docs:
        console.print(
            f"\n[green]自动检测到 doc/ 文件夹，包含 {len(auto_docs)} 个文件：[/green]"
        )
        for f in auto_docs:
            console.print(f"  [dim]{f}[/dim]")
        ans = input("是否使用这些文件作为论文素材？(Y/n): ").strip().lower()
        if ans in ("", "y", "yes"):
            console.print("[green]✓ 已加载 doc/ 文件夹中的所有文档[/green]")
            doc_files = auto_docs
        else:
            console.print("[dim]跳过自动加载，请手动指定：[/dim]")
            doc_input = input("文档文件路径（空格分隔多个）：").strip()
            doc_files = [f.strip() for f in doc_input.split() if f.strip()] if doc_input else []
    else:
        console.print("\n[dim]是否有文档文件要上传？（如 开题报告.docx，直接回车跳过）[/dim]")
        doc_input = input("文档文件路径（空格分隔多个）：").strip()
        doc_files = [f.strip() for f in doc_input.split() if f.strip()] if doc_input else []

    console.print("[dim]是否有参考文献文件？（.bib 或 .csv，直接回车跳过）[/dim]")
    ref_input = input("文献文件路径（空格分隔多个）：").strip()
    ref_files = [f.strip() for f in ref_input.split() if f.strip()] if ref_input else []

    return doc_files, ref_files


def _run_phase(target_phase: str, args):
    """
    分阶段运行模式：根据 --phase 只执行指定阶段。
    支持 --plan（续跑）和 --paper（独立评测/修订）。
    """
    from pathlib import Path
    from src.controller import AgentController

    phase = target_phase.lower()

    # 独立评测 / 需要 plan + paper
    if phase in ("eval", "revise"):
        if not args.plan:
            console.print("[red]--phase eval/revise 需要 --plan 参数[/red]")
            sys.exit(1)
        if not args.paper:
            console.print(f"[red]--phase {phase} 需要 --paper 参数[/red]")
            sys.exit(1)

    controller = AgentController(
        doc_files=args.files or [],
        ref_files=args.refs or [],
        user_request=args.request or "调试模式",
        session_id=args.session,
        db_path=Path(args.output_dir),  # 目录路径 → controller 自动创建 session_{id}.db
        output_dir=Path(args.output_dir),
        locked_tech_spec_path=args.locked_tech_spec,
    )

    console.print(f"[cyan]分阶段模式：→ {phase.upper()}[/cyan]")

    output = controller.run_to_phase(
        target_phase,
        plan_path=args.plan,
        paper_path=args.paper,
    )

    if output:
        console.print(f"\n[bold green]输出：{output}[/bold green]")


def main():
    args = _parse_args()

    # 检查配置
    if not _check_config():
        sys.exit(1)

    # 分阶段模式：指定了 --phase
    target_phase = args.phase.strip()
    if target_phase:
        return _run_phase(target_phase, args)

    # 收集文件
    doc_files = args.files or []
    ref_files = args.refs  or []

    # 若未通过命令行提供文件，交互式询问
    if not doc_files and not ref_files and not args.request:
        doc_files, ref_files = _collect_files_interactive()

    # 收集研究需求
    user_request = args.request.strip()
    if not user_request:
        user_request = _collect_request_interactive()

    if not user_request:
        console.print("[red]需求为空，程序退出[/red]")
        sys.exit(1)

    # 数据库路径：
    # - --no-db：纯内存，不持久化
    # - --session SESSION_ID：从该 session 的 DB 恢复（继续上次会话）
    # - 默认（新会话）：由 controller 根据 session_id 自动创建 db（持久化，供 --phase eval 调试）
    if args.no_db:
        db_path = None
    elif args.session:
        db_path = Path("outputs") / f"session_{args.session}.db"
    else:
        db_path = Path(args.output_dir)  # 目录路径 → controller 自动创建 session_{id}.db

    # 构建封面信息（如果命令行有任何封面参数则不再交互询问）
    from src.cover import CoverInfo
    cover_args = [args.title_zh, args.title_en, args.school,
                  args.major, args.student_id, args.student_name,
                  args.advisor, args.year_month]
    if any(v.strip() for v in cover_args):
        cover_info = CoverInfo(
            title_zh=args.title_zh,
            title_en=args.title_en,
            school=args.school,
            major=args.major,
            student_id=args.student_id,
            student_name=args.student_name,
            advisor=args.advisor,
            year_month=args.year_month or "2026年6月",
        )
    else:
        cover_info = None  # controller 中会交互询问

    # 启动控制器
    from src.controller import AgentController
    controller = AgentController(
        doc_files=doc_files,
        ref_files=ref_files,
        user_request=user_request,
        session_id=args.session,
        db_path=db_path,
        output_dir=Path(args.output_dir),
        cover_info=cover_info,
        locked_tech_spec_path=args.locked_tech_spec,
    )

    # 提示用户如何用 --session 继续本次会话
    if not args.session and not args.no_db:
        console.print(
            f"[dim]（本次为全新会话，文献不持久化。"
            f"如需继续本次会话，下次运行时加 --session {controller.session_id}）[/dim]"
        )

    console.print(f"\n[bold]会话 ID：{controller.session_id}[/bold]")
    console.print("[dim]（可用 --session 参数在后续对话中恢复此会话）[/dim]")
    if getattr(controller, "_diag", None) and controller._diag.enabled:
        console.print(
            f"[dim]运行诊断 JSONL：{controller._diag.log_path}[/dim]"
            "\n[dim]查看摘要：python -m src.diagnosis "
            f'"{controller._diag.log_path}"[/dim]\n'
        )
    else:
        console.print()

    output_file = controller.run()

    if output_file:
        console.print(f"\n[bold green]论文已保存到：{output_file}[/bold green]")
    else:
        console.print("\n[yellow]流程未完成或被中断[/yellow]")


if __name__ == "__main__":
    main()
