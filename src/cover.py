"""
封面与目录生成模块
- 封面：填充用户提供的学生信息（题目/学号/姓名/指导教师等）
- 目录：根据 WritingPlan.outline 自动生成 Markdown 格式目录
"""

from dataclasses import dataclass, field
from typing import Optional

from .config import get
from .models import WritingPlan


@dataclass
class CoverInfo:
    """封面所需信息（由用户输入或命令行参数提供）"""
    title_zh: str = ""             # 中文题目
    title_en: str = ""             # 英文题目
    school: str = ""               # 学院
    major: str = ""                # 专业班级
    student_id: str = ""           # 学号
    student_name: str = ""         # 学生姓名
    advisor: str = ""              # 指导教师
    year_month: str = ""           # 年月（如 2026年6月）
    thesis_category: str = "论文"  # 论文 / 设计


def collect_cover_info_interactive() -> CoverInfo:
    """
    在 CLI 中交互式收集封面信息。
    用户可直接回车跳过非必填字段，后续手动补写。
    """
    from rich.console import Console
    from rich.panel import Panel
    console = Console()

    console.print(Panel(
        "请填写封面信息（直接回车可跳过，之后在 Markdown 中手动补写）",
        title="📋 封面信息",
        border_style="blue",
    ))

    def ask(prompt: str, default: str = "") -> str:
        val = input(f"  {prompt}（默认：{default or '空'}）: ").strip()
        return val or default

    school_default = get("thesis_school", "沈阳工业大学")
    category_default = get("thesis_category", "论文")

    return CoverInfo(
        title_zh=ask("中文题目"),
        title_en=ask("英文题目"),
        school=ask("学院", ""),
        major=ask("专业班级", ""),
        student_id=ask("学号", ""),
        student_name=ask("学生姓名", ""),
        advisor=ask("指导教师", ""),
        year_month=ask("年月", "2026年6月"),
        thesis_category=ask(
            f"类型（论文/设计）", category_default
        ),
    )


def render_cover(info: CoverInfo) -> str:
    """
    生成封面 Markdown 文本。
    使用居中对齐提示（Markdown 本身不强制排版，最终 Word/PDF 格式化时再处理）。
    """
    school_name = get("thesis_school", "沈阳工业大学")
    category = info.thesis_category or "论文"

    title_zh = info.title_zh or "（请填写中文题目）"
    title_en = info.title_en or "（Please Fill in the English Title）"

    lines = [
        "<!-- ========== 封面 ========== -->",
        "",
        f"# {school_name}本科生毕业{category}",
        "",
        "---",
        "",
        f"**中文题目：** {title_zh}",
        "",
        f"**英文题目：** {title_en}",
        "",
        "---",
        "",
        f"| 项目 | 内容 |",
        f"|------|------|",
        f"| 学院 | {info.school or '（请填写）'} |",
        f"| 专业班级 | {info.major or '（请填写）'} |",
        f"| 学号 | {info.student_id or '（请填写）'} |",
        f"| 学生姓名 | {info.student_name or '（请填写）'} |",
        f"| 指导教师 | {info.advisor or '（请填写）'} |",
        f"| 完成时间 | {info.year_month or '（请填写）'} |",
        "",
        "---",
        "",
        "<!-- ========== 封面结束 ========== -->",
        "",
    ]
    return "\n".join(lines)


def render_toc(plan: WritingPlan, has_abstract: bool = True) -> str:
    """
    根据 WritingPlan.outline 自动生成 Markdown 格式目录。
    毕业论文规范：摘要/Abstract 用罗马数字页，正文从第1页起。
    （Markdown 版本仅给出结构，页码需在 Word 导出时更新）
    """
    lines = [
        "<!-- ========== 目录 ========== -->",
        "",
        "# 目录",
        "",
    ]

    roman_sections = {"abstract_zh", "abstract_en", "keywords"}
    page_counter = {"roman": 1, "arabic": 1}

    for sec in plan.outline:
        sid = sec.section_id

        # 前置页（摘要等）用小写罗马数字
        if sid in roman_sections:
            page_str = _to_roman(page_counter["roman"])
            page_counter["roman"] += 1
            lines.append(f"{sec.title}{'.' * (50 - len(sec.title))} {page_str}")
            continue

        # 参考文献、致谢
        if sid in ("refs", "acknowledgment"):
            lines.append(f"{sec.title}{'.' * (50 - len(sec.title))} [页码]")
            continue

        # 正文章节（第1章-第6章）
        lines.append(f"{sec.title}{'.' * (50 - len(sec.title))} [页码]")
        # 添加子节占位（基于 bullets 条目，每条作为一个二级条目）
        if sec.bullets and sid.startswith("s"):
            for i, bullet in enumerate(sec.bullets[:4], 1):
                # 提取子节标题（取 bullet 前12字）
                sub_title = f"  {sec.section_id.replace('s', '')}.{i} {bullet[:20]}"
                lines.append(f"{sub_title}{'.' * max(5, 45 - len(sub_title))} [页码]")

    lines += ["", "<!-- ========== 目录结束 ========== -->", ""]
    return "\n".join(lines)


def _to_roman(n: int) -> str:
    """将整数转为小写罗马数字（1-20）"""
    mapping = [
        (10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i")
    ]
    result = ""
    for value, numeral in mapping:
        while n >= value:
            result += numeral
            n -= value
    return result
