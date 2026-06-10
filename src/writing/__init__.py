"""撰写子包：按大纲生成与修订论文正文。"""
from .draft_engine import draft_manuscript
from .helpers import _build_ref_list_section
from .postprocess import postprocess_manuscript, reorder_citations_by_first_appearance
from .revision_engine import check_revision_compliance, revise_manuscript, stubborn_targeted_fix
from .term_map import build_global_term_map
from .writer import parse_manuscript_from_md

__all__ = [
    "build_global_term_map",
    "check_revision_compliance",
    "draft_manuscript",
    "parse_manuscript_from_md",
    "postprocess_manuscript",
    "reorder_citations_by_first_appearance",
    "revise_manuscript",
    "stubborn_targeted_fix",
    "_build_ref_list_section",
]
