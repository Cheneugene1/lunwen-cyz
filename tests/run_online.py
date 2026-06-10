"""在线测试 — 需 API Key + 网络，不跑不影响离线测试"""

import sys
import time

sys.path.insert(0, ".")

SKIP = 0
FAIL = 0
_t_start = time.time()


def ok(label, condition=True, detail=""):
    global FAIL
    if condition:
        print(f"  \u2705 {label}")
    else:
        FAIL += 1
        print(f"  \u274c {label} {detail}")


def skip(label, reason=""):
    global SKIP
    SKIP += 1
    print(f"  \u26a0 SKIP {label}{' — ' + reason if reason else ''}")


# ═══════════════════════════════════════════════════════════════
print("=" * 60)
print("API HEALTH")
print("=" * 60)

from src.config import get

if not get("deepseek_api_key"):
    print("  \u26a0 无 API Key，跳过全部在线测试")
    print(f"\n{'='*60}")
    print(f"  SKIP: all (no key)")
    print(f"{'='*60}")
    sys.exit(0)

from src.llm import chat_json, build_messages

try:
    msg = build_messages("You are a helpful assistant.", "Reply with JSON: {\"ok\": true}")
    raw = chat_json(msg, temperature=0.1, max_tokens=100)
    ok("LLM JSON call", isinstance(raw, dict) and raw.get("ok") is True, str(raw)[:100])
except Exception as e:
    ok("LLM JSON call", False, str(e)[:100])


# ═══════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
print("RETRIEVAL HEALTH")
print("=" * 60)

from src.retriever import run_search
from src.ref_store import ReferenceStore
from src.models import WritingPlan, SectionNode

store = ReferenceStore()
plan = WritingPlan(
    outline=[SectionNode(section_id="s1", title="Introduction")],
    keywords=["micro-doppler", "radar"],
    search_queries=["micro-Doppler radar target detection"],
)

try:
    result = run_search(store, plan, max_rounds=1, max_per_query=3)
    ok("Retrieval API reachable", result and len(store) > 0, f"got {len(store)} refs")
except Exception as e:
    ok("Retrieval API reachable", False, f"error: {e}")


# ═══════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
print("LLM EVALUATION")
print("=" * 60)

from src.validation.evaluator import evaluate
from src.models import Manuscript, ManuscriptSection

ms = Manuscript(sections=[
    ManuscriptSection(section_id="s1", title="Introduction", markdown_body="This paper studies radar systems. " + "Content. " * 100),
    ManuscriptSection(section_id="s2", title="Related Work", markdown_body="Related work includes prior studies. " + "Work. " * 100),
], version=1)
plan2 = WritingPlan(
    outline=[SectionNode(section_id="s1", title="Introduction"), SectionNode(section_id="s2", title="Related Work")],
    keywords=["radar"], search_queries=["radar"],
)

try:
    ev = evaluate(ms, plan2, store, thesis_mode=False)
    ok("LLM evaluation", ev.score_total > 0, f"score={ev.score_total}")
except Exception as e:
    ok("LLM evaluation", False, f"error: {e}")


# ═══════════════════════════════════════════════════════════════
elapsed = time.time() - _t_start
print("\n" + "=" * 60)
if FAIL == 0:
    status = "\u2705 ALL PASSED" if SKIP == 0 else f"\u2705 PASSED ({SKIP} skipped)"
else:
    status = f"\u274c {FAIL} FAILED"
print(f"  {status} in {elapsed:.1f}s")
print("=" * 60)
sys.exit(0 if FAIL == 0 else 1)
