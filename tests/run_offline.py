"""离线测试 — 零依赖（不调 LLM、不调 API、不读 outputs、不写论文）"""
import sys
import time

sys.path.insert(0, ".")

FAILS = 0
SKIPS = 0
_t_start = time.time()


def ok(label, condition=True, detail=""):
    global FAILS
    if condition:
        print(f"  \u2705 {label}")
    else:
        FAILS += 1
        print(f"  \u274c {label} {detail}")


def skip(label, reason=""):
    global SKIPS
    SKIPS += 1
    print(f"  \u26a0 SKIP {label}{' — ' + reason if reason else ''}")


# ═══════════════════════════════════════════════════════════════
print("=" * 60)
print("CONFIG")
print("=" * 60)

from src.config import get
ok("CONFIG-01 默认值 max_refs_total", bool(get("max_refs_total")), str(get("max_refs_total")))
t = get("min_ref_relevance_score")
ok("CONFIG-02 阈值 min_ref_relevance_score <= 0.02", t is not None and t <= 0.02, f"当前={t}")
ok("CONFIG-03 缺失键返回 None", get("nonexistent_abc_xyz") is None)


# ═══════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
print("MODELS")
print("=" * 60)

from src.models import SectionNode, WritingPlan, Manuscript, ManuscriptSection, _normalize_chapter_title

sn = SectionNode(section_id="s1", title="\u6d4b\u8bd5", bullets=["a", "b"])
ok("MODEL-01 SectionNode", sn.model_dump()["section_id"] == "s1" and len(sn.model_dump()["bullets"]) == 2)
wp = WritingPlan(outline=[sn], keywords=["k"], search_queries=["q"])
ok("MODEL-02 WritingPlan", len(wp.outline) == 1 and wp.keywords == ["k"])
ms = Manuscript(sections=[ManuscriptSection(section_id="s1", title="t", markdown_body="\u6b63\u6587")], version=1)
ok("MODEL-03 Manuscript to_markdown", "\u6b63\u6587" in ms.to_markdown())

# Title normalization
for sid, inp, exp in [
    ("s1", "\u5f15\u8a00", "\u7b2c1\u7ae0 \u5f15\u8a00"),
    ("s3", "\u7b2c3\u7ae0 \u7b2c3\u7ae0 \u7cfb\u7edf\u8bbe\u8ba1", "\u7b2c3\u7ae0 \u7cfb\u7edf\u8bbe\u8ba1"),
    ("s6", "\u7ed3\u8bba", "\u7b2c6\u7ae0 \u7ed3\u8bba"),
    ("abstract_zh", "\u6458\u8981", "\u6458\u8981"),
]:
    got = _normalize_chapter_title(sid, inp)
    ok(f"TITLE {inp} -> {exp}", got == exp, f"got={repr(got)}")

# to_markdown 标题集成
t_ms = Manuscript(sections=[
    ManuscriptSection(section_id="s1", title="\u5f15\u8a00", markdown_body="xx"),
    ManuscriptSection(section_id="s2", title="\u7b2c2\u7ae0 \u7b2c2\u7ae0 \u76f8\u5173\u5de5\u4f5c", markdown_body="yy"),
    ManuscriptSection(section_id="abstract_zh", title="\u6458\u8981", markdown_body="zz"),
], version=1)
t_md = t_ms.to_markdown()
ok("TITLE to_md s1", "## \u7b2c1\u7ae0 \u5f15\u8a00" in t_md)
ok("TITLE to_md s2\u65e0\u91cd\u590d", "## \u7b2c2\u7ae0 \u76f8\u5173\u5de5\u4f5c" in t_md)
ok("TITLE to_md \u6458\u8981\u4e0d\u53d8", "## \u6458\u8981" in t_md)

# H1 anchor
t2 = Manuscript(thesis_title="\u6d4b\u8bd5\u8bba\u6587", sections=[ManuscriptSection(section_id="abstract_zh", title="\u6458\u8981", markdown_body="test")], version=1)
ok("H1 to_md", t2.to_markdown().startswith("# \u6d4b\u8bd5\u8bba\u6587"))


# ═══════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
print("WRITER")
print("=" * 60)

from src.writing.postprocess import _fix_citation_position

r = _fix_citation_position("\u5b9e\u9a8c\u9a8c\u8bc1\u4e86\u3002\n[1]\n\u5206\u6790")
ok("WRIT-01 \u8de8\u884c\u5f15\u7528", "[1]\u3002" in r or "[1]\n\u3002" in r, repr(r[:80]))
r2 = _fix_citation_position("\u7ed3\u679c\u3002[1]")
ok("WRIT-02 \u6807\u70b9\u524d\u5f15\u7528", "[1]\u3002" in r2 or "\u7ed3\u679c[1]\u3002" in r2, repr(r2))

from src.writing.helpers import _strip_revision_artifacts
strip_cases = [
    ("Part 1 [\u6b63\u6587]\uff1a\n3.1 \u5185\u5bb9\nPart 2 [\u4fee\u6539\u65e5\u5fd7]\uff1ax", "3.1 \u5185\u5bb9"),
    ("\u7eaf\u6b63\u6587\u6ca1\u6709\u6807\u7b7e", "\u7eaf\u6b63\u6587\u6ca1\u6709\u6807\u7b7e"),
    ("Part One [\u6b63\u6587]\uff1a\n\u6b63\u5e38\u6587\u672c\n\nPart Two [\u4fee\u6539\u65e5\u5fd7]\uff1adone", "\u6b63\u5e38\u6587\u672c"),
]
for inp, exp in strip_cases:
    got = _strip_revision_artifacts(inp)
    ok(f"STRIP {exp[:20]}", got.strip() == exp.strip(), f"got={repr(got[:60])} exp={repr(exp[:60])}")

from src.writing.helpers import _ensure_subsections_present, _align_subsections_titles
sn3 = SectionNode(section_id="s3", title="\u7b2c3\u7ae0", subsections=[
    SectionNode(section_id="s3_1", title="3.1 \u67b6\u6784"),
    SectionNode(section_id="s3_2", title="3.2 \u6a21\u5757"),
])
body_f = "### 3.1 \u67b6\u6784\n\nxx\n\n### 3.2 \u6a21\u5757\n\nyy"
body_m = "### 3.1 \u67b6\u6784\n\nxx"
ok("ENSURE-01 \u9f50\u5168\u4e0d\u6539", _ensure_subsections_present(body_f, sn3) == body_f)
r_m = _ensure_subsections_present(body_m, sn3)
ok("ENSURE-02 \u8865\u5168\u7f3a\u5931", "### 3.2 \u6a21\u5757" in r_m and "> **\u5f85\u64b0\u5199" in r_m)

# Fuzzy matching
sn_f = SectionNode(section_id="s4", title="\u7b2c4\u7ae0", subsections=[
    SectionNode(section_id="s4_1", title="4.1 \u6d41\u7a0b"),
    SectionNode(section_id="s4_2", title="4.2 \u5b9e\u73b0"),
])
r_f = _ensure_subsections_present("### 4.1 \u6d41\u7a0b\n\nxx\n\n### 4.2 \u5b9e\u73b0\u4e0e\u90e8\u7f72\n\nyy", sn_f)
ok("ENSURE-03 \u6a21\u7cca\u5339\u914d\u4e0d\u8bef\u8865", "> **\u5f85\u64b0\u5199" not in r_f)

# Alignment
sn_a = SectionNode(section_id="s4", title="\u7b2c4\u7ae0", subsections=[
    SectionNode(section_id="s4_2", title="4.2 \u6a21\u5757\u5b9e\u73b0"),
])
_align_subsections_titles("### 4.2 \u6a21\u5757\u5b9e\u73b0\u4e0e\u90e8\u7f72\n\nyy", sn_a)
ok("ALIGN \u6807\u9898\u5bf9\u9f50", sn_a.subsections[0].title == "4.2 \u6a21\u5757\u5b9e\u73b0\u4e0e\u90e8\u7f72", f"got={sn_a.subsections[0].title}")

from src.writing.postprocess import _clean_horizontal_rules, _downgrade_body_h1
ok("HR clean", "---" not in _clean_horizontal_rules("text\n\n---\n\nmore") and "text" in _clean_horizontal_rules("text\n\n---\n\nmore"))
ok("HR table", _clean_horizontal_rules("|a|b|\n|---|---|\n|d|d|") == "|a|b|\n|---|---|\n|d|d|")
ok("H1 downgrade", _downgrade_body_h1("# bad\n\ntext").startswith("## bad"))

from src.writing.revision_engine import _build_term_lockdown_snapshot
snap = _build_term_lockdown_snapshot({"hardware": {"mcu": {"model": "STC89C52RC"}, "sensors": [{"name": "\u6e29\u6e7f\u5ea6", "model": "DHT11"}]}})
ok("LOCK SNAP", "STC89C52RC" in snap and "DHT11" in snap)


# ═══════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
print("STATIC RULES")
print("=" * 60)

from src.validation.evaluator import _check_thesis_rules


def _mk_ms(**sections) -> Manuscript:
    """快速构造 Manuscript，自动补足缺失章节。"""
    defaults = {
        "abstract_zh": ("\u6458\u8981", "\u6d4b\u8bd5\u6458\u8981" + "x" * 300),
        "s1": ("\u7b2c1\u7ae0 \u5f15\u8a00", "x" * 500),
        "s2": ("\u7b2c2\u7ae0", "x" * 500),
        "s3": ("\u7b2c3\u7ae0", "x" * 600),
        "s4": ("\u7b2c4\u7ae0", "x" * 600),
        "s5": ("\u7b2c5\u7ae0", "x" * 500),
        "s6": ("\u7b2c6\u7ae0", "x" * 100),
    }
    for k, (t, b) in defaults.items():
        if k not in sections:
            sections[k] = (t, b)
    secs = [ManuscriptSection(section_id=k, title=v[0], markdown_body=v[1]) for k, v in sections.items()]
    return Manuscript(sections=secs, version=1)


def _mk_plan(**sections) -> WritingPlan:
    return WritingPlan(
        outline=[SectionNode(section_id=k, title=v[0]) for k, v in sections.items()],
        keywords=["\u6d4b\u8bd5"],
        search_queries=["\u6d4b\u8bd5"],
    )


# MCU 对比语境不误报
m1 = _mk_ms(
    keywords=("\u5173\u952e\u8bcd", "STC89C51;\u6e29\u6e7f\u5ea6"),
    s1=("\u7b2c1\u7ae0 \u5f15\u8a00", "STC89C51\u662f\u4e00\u6b3e\u7ecf\u5178\u7684\u5fae\u63a7\u5236\u5668\u3002" + "x" * 500),
    s2=("\u7b2c2\u7ae0", "\u672c\u6587\u5bf9\u6bd4\u4e86ESP32\u65b9\u6848\u4e0eArduino\u65b9\u6848\u5728\u7269\u8054\u7f51\u573a\u666f\u4e2d\u7684\u5e94\u7528\u2026\u2026" + "x" * 500),
)
p1 = WritingPlan(
    outline=[SectionNode(section_id="s1", title="\u5f15\u8a00", bullets=["\u672c\u6587\u5de5\u4f5c", "\u8bba\u6587\u7ed3\u6784"])],
    keywords=["STC89C51"], search_queries=["t"],
)
issues = _check_thesis_rules(m1, p1)
ok("\u2b50 MCU\u5bf9\u6bd4\u4e0d\u8bef\u62a5", not any(i.rule_id == "mcu_abstract_body_mismatch" for i in issues),
   "mcu_abstract_body_mismatch \u8bef\u62a5" if any(i.rule_id == "mcu_abstract_body_mismatch" for i in issues) else "")
ok("\u2b50 conclusion_intro \u4e0d\u8bef\u62a5", not any(i.rule_id == "conclusion_intro_gap" for i in issues))

# 摘要太短
m_short = _mk_ms(abstract_zh=("\u6458\u8981", "\u77ed\u6458\u8981"))
i2 = _check_thesis_rules(m_short, p1)
ok("\u2b50 abstract_too_short", any(i.rule_id == "abstract_too_short" for i in i2))

# 摘要含引用
m_cite = _mk_ms(abstract_zh=("\u6458\u8981", "\u672c\u6587\u7814\u7a76[1]\u63d0\u51fa\u4e86\u2026\u2026" + "x" * 300))
i3 = _check_thesis_rules(m_cite, p1)
ok("\u2b50 abstract_citation_markers", any(i.rule_id == "abstract_citation_markers" for i in i3))

# 占位符残留
m_ph = _mk_ms(abstract_zh=("\u6458\u8981", "\uff08\u672c\u90e8\u5206\u751f\u6210\u5931\u8d25\uff0c\u8bf7\u91cd\u8bd5\uff09" + "x" * 300))
i4 = _check_thesis_rules(m_ph, p1)
ok("\u2b50 placeholder_residual", any("placeholder_residual" in (i.rule_id or "") for i in i4))

# 小节缺失
s3_node = SectionNode(section_id="s3", title="\u7b2c3\u7ae0 \u786c\u4ef6\u8bbe\u8ba1", subsections=[
    SectionNode(section_id="s3_1", title="3.1 \u603b\u4f53\u67b6\u6784"),
    SectionNode(section_id="s3_2", title="3.2 \u6838\u5fc3\u6a21\u5757"),
])
p2 = WritingPlan(outline=[SectionNode(section_id="s1", title="\u5f15\u8a00"), s3_node], keywords=["t"], search_queries=["t"])
m_miss = _mk_ms(s3=("\u7b2c3\u7ae0", "### 3.1 \u603b\u4f53\u67b6\u6784\n\n\u5185\u5bb9" + "x" * 500))
i5 = _check_thesis_rules(m_miss, p2)
ok("\u2b50 missing_subsections", any(i.rule_id == "missing_subsections" for i in i5))

# ref_store=None 不崩溃
i6 = _check_thesis_rules(_mk_ms(), p1)
ok("ref_store=None no crash", True)


# ═══════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
print("PLANNER")
print("=" * 60)

from src.planner import _normalize_outline_titles, _clean_top_level_subsections

plan1 = WritingPlan(
    outline=[
        SectionNode(section_id="s1", title="\u5f15\u8a00"),
        SectionNode(section_id="s2", title="\u6587\u732e\u7efc\u8ff0"),
        SectionNode(section_id="s3", title="\u7b2c3\u7ae0 \u7b2c3\u7ae0 \u7cfb\u7edf\u8bbe\u8ba1"),
        SectionNode(section_id="s6", title="\u7ed3\u8bba"),
        SectionNode(section_id="acknowledgment", title="\u81f4\u8c22"),
    ],
    keywords=["\u6d4b\u8bd5"], search_queries=["\u6d4b\u8bd5"],
)
_normalize_outline_titles(plan1)
titles = {s.section_id: s.title for s in plan1.outline}
ok("PLAN s1", titles["s1"] == "\u7b2c1\u7ae0 \u5f15\u8a00")
ok("PLAN s3\u53bb\u91cd", titles["s3"] == "\u7b2c3\u7ae0 \u7cfb\u7edf\u8bbe\u8ba1")
ok("PLAN s6", titles["s6"] == "\u7b2c6\u7ae0 \u7ed3\u8bba")
ok("PLAN \u6458\u8981\u4e0d\u53d8", titles.get("acknowledgment") == "\u81f4\u8c22")

plan2 = WritingPlan(
    outline=[
        SectionNode(section_id="s3", title="\u7b2c3\u7ae0"),
        SectionNode(section_id="s3_1", title="3.1 \u67b6\u6784"),
        SectionNode(section_id="s3_2", title="3.2 \u6a21\u5757"),
        SectionNode(section_id="s4", title="\u7b2c4\u7ae0"),
        SectionNode(section_id="s4_1", title="4.1 \u6d41\u7a0b"),
    ],
    keywords=["\u6d4b\u8bd5"], search_queries=["\u6d4b\u8bd5"],
)
_clean_top_level_subsections(plan2)
ids = {s.section_id for s in plan2.outline}
ok("PLAN \u79fb\u9664s3_1", "s3_1" not in ids and "s4_1" not in ids and "s3" in ids)
s3 = next(s for s in plan2.outline if s.section_id == "s3")
ok("PLAN s3_1\u8fc1\u5165s3", "s3_1" in {ss.section_id for ss in s3.subsections})

# Outline hard rules — thesis_mode=True 需要完整6章结构
from src.planner import _check_outline_hard_rules
p_good = WritingPlan(
    outline=[
        SectionNode(section_id="abstract_zh", title="\u6458\u8981", bullets=["\u76ee\u7684", "\u65b9\u6cd5", "\u7ed3\u679c"]),
        SectionNode(section_id="abstract_en", title="Abstract", bullets=["purpose", "method", "result"]),
        SectionNode(section_id="s1", title="\u7b2c1\u7ae0 \u5f15\u8a00", bullets=["\u7814\u7a76\u80cc\u666f", "\u7814\u7a76\u610f\u4e49"]),
        SectionNode(section_id="s2", title="\u7b2c2\u7ae0 \u7efc\u8ff0", bullets=["\u76f8\u5173\u5de5\u4f5c", "\u5b58\u5728\u95ee\u9898"]),
        SectionNode(section_id="s3", title="\u7b2c3\u7ae0 \u65b9\u6cd5", bullets=["\u65b9\u6cd5\u8bbe\u8ba1", "\u5b9e\u9a8c\u8bbe\u7f6e"]),
        SectionNode(section_id="s4", title="\u7b2c4\u7ae0 \u5b9e\u9a8c", bullets=["\u5b9e\u9a8c\u7ed3\u679c", "\u5206\u6790"]),
        SectionNode(section_id="s5", title="\u7b2c5\u7ae0 \u8ba8\u8bba", bullets=["\u7ed3\u679c\u8ba8\u8bba", "\u5c40\u9650\u6027"]),
        SectionNode(section_id="s6", title="\u7b2c6\u7ae0 \u7ed3\u8bba", bullets=["\u5de5\u4f5c\u603b\u7ed3", "\u521b\u65b0\u8d21\u732e"]),
        SectionNode(section_id="acknowledgment", title="\u81f4\u8c22", bullets=["\u611f\u8c22\u6307\u5bfc"]),
    ],
    keywords=["\u5fae\u591a\u666e\u52d2", "\u96f7\u8fbe", "\u65f6\u9891\u5206\u6790", "\u76ee\u6807\u68c0\u6d4b"],
    search_queries=["\u5fae\u591a\u666e\u52d2 \u96f7\u8fbe"],
)
issues_o = _check_outline_hard_rules(p_good, thesis_mode=True)
ok("OUTLINE good", not any(i["severity"] == "error" for i in issues_o),
   str([(i["rule_id"], i["severity"]) for i in issues_o if i["severity"] == "error"]))

p_empty = WritingPlan(outline=[], keywords=[], search_queries=[])
issues_empty = _check_outline_hard_rules(p_empty, thesis_mode=False)
ok("OUTLINE empty", any(i["rule_id"] == "OUTLINE_EMPTY" for i in issues_empty))


# ═══════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
print("FIXTURES: Markdown")
print("=" * 60)

from tests.fixture_runner import run_all_fixtures
f_ok = run_all_fixtures()
if not f_ok:
    FAILS += 1


# ═══════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
print("FIXTURES: Outline YAML")
print("=" * 60)

from tests.outline_fixture_runner import run_all_outline_fixtures
o_ok = run_all_outline_fixtures()
if not o_ok:
    FAILS += 1


# ═══════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
print("WRITER-EVAL ALIGNMENT")
print("=" * 60)

# 引用位置闭环：写端修正 → 评估端不再报
from src.writing.postprocess import _fix_citation_position
from src.validation.evaluator import _check_thesis_rules

bad_text = "\u672c\u6587\u63d0\u51fa\u4e86\u4e00\u79cd\u65b0\u65b9\u6cd5\u3002[1]\u5b9e\u9a8c\u7ed3\u679c\u8868\u660e\u2026\u2026" + "x" * 500
ms_before = Manuscript(sections=[
    ManuscriptSection(section_id="abstract_zh", title="\u6458\u8981", markdown_body="\u6458\u8981\u5185\u5bb9" + "x" * 300),
    ManuscriptSection(section_id="s1", title="\u7b2c1\u7ae0 \u5f15\u8a00", markdown_body=bad_text),
    ManuscriptSection(section_id="s2", title="\u7b2c2\u7ae0", markdown_body="x" * 500),
    ManuscriptSection(section_id="s3", title="\u7b2c3\u7ae0", markdown_body="x" * 600),
    ManuscriptSection(section_id="s4", title="\u7b2c4\u7ae0", markdown_body="x" * 600),
    ManuscriptSection(section_id="s5", title="\u7b2c5\u7ae0", markdown_body="x" * 500),
    ManuscriptSection(section_id="s6", title="\u7b2c6\u7ae0", markdown_body="x" * 100),
], version=1)

p_cl = WritingPlan(
    outline=[SectionNode(section_id="s1", title="\u5f15\u8a00", bullets=["\u672c\u6587\u5de5\u4f5c", "\u8bba\u6587\u7ed3\u6784"])],
    keywords=["\u6d4b\u8bd5"], search_queries=["t"],
)

# Step 1: 写端修正
fixed_body = _fix_citation_position(bad_text)
ms_after = Manuscript(sections=[
    ManuscriptSection(section_id="abstract_zh", title="\u6458\u8981", markdown_body="\u6458\u8981\u5185\u5bb9" + "x" * 300),
    ManuscriptSection(section_id="s1", title="\u7b2c1\u7ae0 \u5f15\u8a00", markdown_body=fixed_body),
] + [ManuscriptSection(section_id=s, title=s, markdown_body="x" * 500) for s in ["s2", "s3", "s4", "s5", "s6"]], version=1)

# Step 2: 评估
before_issues = _check_thesis_rules(ms_before, p_cl)
after_issues = _check_thesis_rules(ms_after, p_cl)

before_cit = [i for i in before_issues if i.rule_id == "citation_after_punct"]
after_cit = [i for i in after_issues if i.rule_id == "citation_after_punct"]

ok("ALIGN-01 \u5f15\u7528\u4f4d\u7f6e\u95ed\u73af", len(before_cit) > 0,
   "\u5199\u7aef\u5904\u7406\u524d\u5e94\u62a5\u9519")
ok("ALIGN-02 \u5f15\u7528\u4fee\u6b63\u540e\u4e0d\u62a5", len(after_cit) == 0,
   f"\u4ecd\u62a5 {len(after_cit)} \u6761" if after_cit else "")


# ═══════════════════════════════════════════════════════════════
elapsed = time.time() - _t_start
print("\n" + "=" * 60)
if FAILS == 0:
    print(f"  PASSED in {elapsed:.1f}s")
else:
    print(f"  {FAILS} TEST(S) FAILED in {elapsed:.1f}s")
print("=" * 60)
sys.exit(0 if FAILS == 0 else 1)
