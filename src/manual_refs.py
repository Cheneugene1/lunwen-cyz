"""
手动参考文献导入模块
支持：
  - BibTeX .bib（首选）
  - CSV（固定表头：title,authors,year,venue,doi,url,language,abstract）
解析后文献：source_tag="manual_file", pinned=True
缺 year 或 authors 时：low_confidence=True
"""

import csv
import logging
import uuid
from pathlib import Path
from typing import List

from .models import Reference
from .ref_store import ReferenceStore

logger = logging.getLogger(__name__)

# CSV 必须包含的列
CSV_REQUIRED = {"title"}
CSV_OPTIONAL = {"authors", "year", "venue", "doi", "url", "language", "abstract"}


# ── BibTeX 解析 ────────────────────────────────────────────────

def _parse_bib(path: Path) -> List[Reference]:
    """解析 .bib 文件，返回 Reference 列表"""
    try:
        import bibtexparser
        with open(path, encoding="utf-8", errors="replace") as f:
            bib_db = bibtexparser.load(f)
    except Exception as e:
        logger.error("BibTeX 解析失败 %s: %s", path.name, e)
        return []

    refs = []
    for entry in bib_db.entries:
        # 提取字段（bibtexparser 字段名均小写）
        title = entry.get("title", "").strip()
        if not title:
            logger.warning("BibTeX 条目缺 title，跳过：%s", entry.get("ID", "?"))
            continue

        # 作者：bibtexparser 用 " and " 分隔
        raw_authors = entry.get("author", "")
        authors = [a.strip() for a in raw_authors.split(" and ") if a.strip()] \
            if raw_authors else []

        year = str(entry.get("year", "")).strip() or None
        venue = (entry.get("journal") or entry.get("booktitle") or "").strip() or None
        doi = entry.get("doi", "").strip() or None
        url = entry.get("url", "").strip() or None
        abstract = entry.get("abstract", "").strip() or None

        low_confidence = not year or not authors

        ref = Reference(
            id=str(uuid.uuid4()),
            title=title,
            authors=authors,
            year=year,
            venue=venue,
            doi=doi,
            url=url,
            abstract=abstract,
            source_tag="manual_file",
            pinned=True,
            low_confidence=low_confidence,
            raw=str(entry),
        )
        refs.append(ref)

    logger.info("BibTeX 解析完成：%s → %d 条", path.name, len(refs))
    return refs


# ── CSV 解析 ───────────────────────────────────────────────────

def _parse_csv(path: Path) -> List[Reference]:
    """解析规范 CSV 文献文件"""
    refs = []
    try:
        with open(path, encoding="utf-8", errors="replace", newline="") as f:
            reader = csv.DictReader(f)
            if not reader.fieldnames:
                logger.warning("CSV 文件为空或无表头：%s", path.name)
                return []

            headers = {h.strip().lower() for h in (reader.fieldnames or [])}
            if "title" not in headers:
                logger.error("CSV 缺少必填列 'title'：%s", path.name)
                return []

            for row in reader:
                # 统一小写 key
                row = {k.strip().lower(): (v or "").strip() for k, v in row.items()}

                title = row.get("title", "").strip()
                if not title:
                    continue

                # 作者：用 ";" 分隔多名
                raw_authors = row.get("authors", "")
                authors = [a.strip() for a in raw_authors.split(";") if a.strip()]

                year = row.get("year", "").strip() or None
                venue = row.get("venue", "").strip() or None
                doi = row.get("doi", "").strip() or None
                url = row.get("url", "").strip() or None
                language = row.get("language", "").strip() or None
                abstract = row.get("abstract", "").strip() or None

                low_confidence = not year or not authors

                ref = Reference(
                    id=str(uuid.uuid4()),
                    title=title,
                    authors=authors,
                    year=year,
                    venue=venue,
                    doi=doi,
                    url=url,
                    language=language,
                    abstract=abstract,
                    source_tag="manual_file",
                    pinned=True,
                    low_confidence=low_confidence,
                )
                refs.append(ref)
    except Exception as e:
        logger.error("CSV 解析失败 %s: %s", path.name, e)

    logger.info("CSV 解析完成：%s → %d 条", path.name, len(refs))
    return refs


# ── 主入口 ────────────────────────────────────────────────────

def ingest_manual_refs(
    file_paths: List[str | Path],
    store: ReferenceStore,
) -> int:
    """
    解析手动文献文件并写入 ReferenceStore。
    返回成功导入（去重后新增）的条数。
    支持混合传入多个 .bib / .csv 文件。
    """
    all_refs: List[Reference] = []
    for fp in file_paths:
        path = Path(fp)
        if not path.exists():
            logger.warning("文献文件不存在，跳过：%s", path)
            continue
        suffix = path.suffix.lower()
        if suffix == ".bib":
            all_refs.extend(_parse_bib(path))
        elif suffix == ".csv":
            all_refs.extend(_parse_csv(path))
        else:
            logger.warning("手动文献不支持格式 %s，跳过：%s", suffix, path.name)

    if not all_refs:
        return 0

    added = store.merge(all_refs)
    return added
