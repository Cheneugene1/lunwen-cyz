"""
自适应检索模块
数据源（均免费）：OpenAlex / Crossref / arXiv / Semantic Scholar

改进策略（v2）：
  1. 每查询每源减少到 8 条，避免海量堆积
  2. 结果写入文献池前做相关性评分过滤（TF-IDF 简化版）
  3. 来源质量黑名单：屏蔽低质量预印本平台和出版商
  4. 全局总量上限 max_refs_total（默认 60），超出只保留最相关
  5. 单源失败不阻断；全部失败走仅手动文献路径
"""

import hashlib
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Optional

import httpx

from .config import get
from .models import Reference, WritingPlan
from .ref_store import ReferenceStore

logger = logging.getLogger(__name__)

# HTTP 超时配置（秒）
_TIMEOUT = httpx.Timeout(connect=5.0, read=20.0, write=5.0, pool=5.0)

# 内存级检索缓存：query_hash → List[Reference]
_cache: dict[str, List[Reference]] = {}

# ── 低质量来源黑名单 ───────────────────────────────────────────
# 域名/出版商名关键词（小写匹配）
_LOW_QUALITY_DOMAINS = {
    "ssrn.com", "techrxiv.org", "researchgate.net",
    "academia.edu", "preprints.org", "biorxiv.org",
    "medrxiv.org", "chemrxiv.org",
}
_LOW_QUALITY_PUBLISHERS = {
    "hans", "hans publishers", "汉斯", "科学出版社国际版",
    "open access", "scirp",  # Scientific Research Publishing，质量参差
}
# 明显噪声标题特征
_NOISE_TITLE_PATTERNS = [
    re.compile(r"^abstracts?\b", re.I),          # "Abstracts" 期刊摘要集
    re.compile(r"_supp\d*[-_\.]"),               # 补充材料文件名
    re.compile(r"proceedings of the .{0,20}(?:annual|international) conference", re.I),
    re.compile(r"^\d{4} .{0,10}conference", re.I),
]


def _cache_key(source: str, query: str) -> str:
    return hashlib.md5(f"{source}:{query}".encode()).hexdigest()


def _is_low_quality(ref: Reference) -> bool:
    """判断文献是否来自低质量来源"""
    url_lower = (ref.url or "").lower()
    venue_lower = (ref.venue or "").lower()
    title_lower = (ref.title or "").lower()

    # 域名黑名单
    if any(d in url_lower for d in _LOW_QUALITY_DOMAINS):
        return True

    # 出版商黑名单
    if any(p in venue_lower for p in _LOW_QUALITY_PUBLISHERS):
        return True

    # 噪声标题
    for pat in _NOISE_TITLE_PATTERNS:
        if pat.search(ref.title or ""):
            return True

    # 无标题/无作者且无 DOI
    if not ref.title or (not ref.authors and not ref.doi):
        return True

    return False


# ── 相关性评分 ────────────────────────────────────────────────

def _relevance_score(ref: Reference, keywords: List[str], synonym_map: dict | None = None) -> float:
    """
    对文献与关键词的相关性打分（0.0–1.0）。
    匹配字段：title（权重3）+ abstract（权重1）+ venue（权重0.5）

    注意：keywords 列表可能包含中文词（如"单片机"）。
    英文文献无法被中文词命中，但只要标题/摘要非空，就给予一个最低保底分 0.1，
    确保英文文献不会被"语言不匹配"误杀——质量过滤由 cull_poor_quality 承担。

    synonym_map: 中文→英文学术同义词子串映射（如 "微多普勒特征"→"micro-doppler feature"）；
    用于解决中文关键词无法直接匹配英文标题的问题。
    """
    if not keywords:
        return 0.5  # 无关键词时不过滤

    text_parts = [
        ((ref.title or "").lower(), 3.0),
        ((ref.abstract or "")[:500].lower(), 1.0),
        ((ref.venue or "").lower(), 0.5),
    ]

    total_weight = sum(w for _, w in text_parts)
    score = 0.0

    # 直接匹配：关键词子串在文本中出现
    for text, weight in text_parts:
        matched = sum(1 for kw in keywords if kw.lower() in text)
        score += weight * (matched / max(len(keywords), 1))

    computed = min(score / total_weight, 1.0)

    # 同义词匹配：直接匹配为 0 时用 synonym_map 再试一次
    if computed == 0.0 and synonym_map:
        for text, weight in text_parts:
            if not text:
                continue
            for kw, synonyms in synonym_map.items():
                if any(syn.lower() in text for syn in synonyms):
                    score += weight * 0.6  # 同义词命中权重低于直接匹配
                    break
        computed = min(score / total_weight, 1.0)

    # 保底：只要有标题，给 0.02 分保底（极低），防止中英文不匹配导致全被丢弃
    # 真正的质量过滤（佚名/汉斯/无DOI等）由 ref_store.cull_poor_quality 在写作前处理
    has_content = bool((ref.title or "").strip())
    return max(computed, 0.02) if has_content else computed


def _filter_and_score(
    refs: List[Reference],
    keywords: List[str],
    min_score: float = 0.15,
    synonym_map: dict | None = None,
) -> List[Reference]:
    """
    过滤低质量文献并按相关性排序。
    min_score: 最低相关性分数（低于此值丢弃，默认 0.15）
    synonym_map: 中文→英文同义词映射，注入 _relevance_score 跨语言匹配
    """
    results = []
    for ref in refs:
        if _is_low_quality(ref):
            logger.debug("丢弃低质量文献: %s", ref.title[:50])
            continue
        score = _relevance_score(ref, keywords, synonym_map=synonym_map)
        if score >= min_score:
            results.append((score, ref))

    results.sort(key=lambda x: -x[0])
    return [ref for _, ref in results]


# ── HTTP 工具 ────────────────────────────────────────────────

def _get(url: str, params: dict, max_retries: int = 2) -> Optional[dict]:
    """带指数退避重试的 GET 请求"""
    delay = 1.0
    for attempt in range(1, max_retries + 2):
        try:
            resp = httpx.get(url, params=params, timeout=_TIMEOUT,
                             headers={"User-Agent": "LunWenCYZ/1.0"})
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 429 or resp.status_code >= 500:
                logger.warning("HTTP %d (attempt %d): %s", resp.status_code, attempt, url)
                if attempt <= max_retries:
                    time.sleep(delay * (0.5 + 0.5 * (attempt / max_retries)))
                    delay *= 2
                continue
            logger.warning("HTTP %d (no retry): %s", resp.status_code, url)
            return None
        except Exception as e:
            logger.warning("HTTP 异常 (attempt %d): %s — %s", attempt, url, e)
            if attempt <= max_retries:
                time.sleep(delay)
                delay *= 2
    return None


# ── OpenAlex ────────────────────────────────────────────────

def _search_openalex(query: str, per_page: int = 15) -> List[Reference]:
    ck = _cache_key("openalex", query)
    if ck in _cache:
        return _cache[ck]

    url = "https://api.openalex.org/works"
    params = {
        "search": query,
        "per-page": per_page,
        "select": "id,title,authorships,publication_year,primary_location,doi,abstract_inverted_index",
        # 按相关性排序（OpenAlex 默认）
        "sort": "relevance_score:desc",
    }
    data = _get(url, params)
    if not data:
        return []

    refs = []
    for item in data.get("results", []):
        title = item.get("title") or ""
        if not title:
            continue

        authors = [
            a.get("author", {}).get("display_name", "") or ""
            for a in item.get("authorships", [])
        ]
        authors = [a for a in authors if a]

        year = str(item.get("publication_year", "") or "").strip() or None
        doi = (item.get("doi") or "").replace("https://doi.org/", "").strip() or None

        venue_obj = item.get("primary_location", {}) or {}
        source = venue_obj.get("source", {}) or {}
        venue = source.get("display_name", "") or None

        abstract = _reconstruct_abstract(item.get("abstract_inverted_index"))

        refs.append(Reference(
            title=title,
            authors=authors,
            year=year,
            venue=venue,
            doi=doi,
            url=f"https://doi.org/{doi}" if doi else None,
            abstract=abstract,
            source_tag="openalex",
            pinned=False,
            low_confidence=not year or not authors,
        ))

    _cache[ck] = refs
    logger.info("OpenAlex [%s] → %d 条", query[:40], len(refs))
    return refs


def _reconstruct_abstract(inverted_index: Optional[dict]) -> Optional[str]:
    """将 OpenAlex 倒排索引还原为摘要文本"""
    if not inverted_index:
        return None
    word_pos: list[tuple[int, str]] = []
    for word, positions in inverted_index.items():
        for pos in positions:
            word_pos.append((pos, word))
    word_pos.sort(key=lambda x: x[0])
    return " ".join(w for _, w in word_pos)


# ── Crossref ────────────────────────────────────────────────

def _search_crossref(query: str, rows: int = 15) -> List[Reference]:
    ck = _cache_key("crossref", query)
    if ck in _cache:
        return _cache[ck]

    url = "https://api.crossref.org/works"
    params: dict = {
        "query": query,
        "rows": rows,
        "sort": "relevance",
        "order": "desc",
    }
    mailto = get("crossref_mailto", "")
    if mailto and "@" in mailto:
        params["mailto"] = mailto

    data = _get(url, params)
    if not data:
        return []

    refs = []
    for item in (data.get("message", {}) or {}).get("items", []):
        title_list = item.get("title") or []
        title = title_list[0] if title_list else ""
        if not title:
            continue

        authors_raw = item.get("author", []) or []
        authors = []
        for a in authors_raw:
            family = a.get("family", "")
            given = a.get("given", "")
            name = f"{given} {family}".strip() if given else family
            if name:
                authors.append(name)

        year = None
        pub_date = item.get("published-print") or item.get("published-online") or {}
        date_parts = (pub_date.get("date-parts") or [[]])[0]
        if date_parts:
            year = str(date_parts[0])

        doi = item.get("DOI", "").strip() or None
        venue_list = item.get("container-title") or []
        venue = venue_list[0] if venue_list else None
        abstract = item.get("abstract", "").strip() or None

        refs.append(Reference(
            title=title,
            authors=authors,
            year=year,
            venue=venue,
            doi=doi,
            url=f"https://doi.org/{doi}" if doi else None,
            abstract=abstract,
            source_tag="crossref",
            pinned=False,
            low_confidence=not year or not authors,
        ))

    _cache[ck] = refs
    logger.info("Crossref [%s] → %d 条", query[:40], len(refs))
    return refs


# ── arXiv ────────────────────────────────────────────────────

def _search_arxiv(query: str, max_results: int = 6) -> List[Reference]:
    ck = _cache_key("arxiv", query)
    if ck in _cache:
        return _cache[ck]

    url = "http://export.arxiv.org/api/query"
    params = {
        "search_query": f"all:{query}",
        "start": 0,
        "max_results": max_results,
        "sortBy": "relevance",
    }
    try:
        resp = httpx.get(url, params=params, timeout=_TIMEOUT)
        if resp.status_code != 200:
            return []
        text = resp.text
    except Exception as e:
        logger.warning("arXiv 请求失败: %s", e)
        return []

    refs = _parse_arxiv_atom(text)
    _cache[ck] = refs
    logger.info("arXiv [%s] → %d 条", query[:40], len(refs))
    return refs


def _parse_arxiv_atom(xml_text: str) -> List[Reference]:
    """用 re 简单解析 arXiv Atom XML"""
    refs = []
    entries = re.findall(r"<entry>(.*?)</entry>", xml_text, re.DOTALL)
    for entry in entries:
        def get_tag(tag: str) -> str:
            m = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", entry, re.DOTALL)
            return m.group(1).strip() if m else ""

        title = re.sub(r"\s+", " ", get_tag("title"))
        abstract = re.sub(r"\s+", " ", get_tag("summary"))
        url = ""
        for link_m in re.finditer(r'<link[^>]+href="([^"]+)"', entry):
            href = link_m.group(1)
            if "abs" in href:
                url = href
                break

        authors = re.findall(r"<name>(.*?)</name>", entry)
        pub_m = re.search(r"<published>(\d{4})", entry)
        year = pub_m.group(1) if pub_m else None
        id_m = re.search(r"<id>.*?/abs/([^<]+)</id>", entry)
        arxiv_id = id_m.group(1).strip() if id_m else None
        doi = f"arXiv:{arxiv_id}" if arxiv_id else None

        if not title:
            continue

        refs.append(Reference(
            title=title,
            authors=authors,
            year=year,
            doi=doi,
            url=url or None,
            abstract=abstract or None,
            source_tag="arxiv",
            pinned=False,
            low_confidence=not year or not authors,
        ))
    return refs


# ── Semantic Scholar ─────────────────────────────────────────

_S2_FIELDS = "title,authors,year,venue,journal,externalIds,abstract,url,publicationTypes,citationCount"


def _search_semantic_scholar(query: str, per_page: int = 25) -> List[Reference]:
    ck = _cache_key("semantic_scholar", query)
    if ck in _cache:
        return _cache[ck]

    api_key = get("semantic_scholar_api_key", "").strip()
    headers = {"User-Agent": "LunWenCYZ/1.0"}
    if api_key:
        headers["x-api-key"] = api_key

    url = "https://api.semanticscholar.org/graph/v1/paper/search"
    params = {
        "query": query,
        "limit": per_page,
        "fields": _S2_FIELDS,
    }

    try:
        resp = httpx.get(url, params=params, headers=headers, timeout=_TIMEOUT)
        # Key 403 时降级到无 Key 模式重试
        if resp.status_code == 403 and api_key:
            logger.warning("Semantic Scholar 403（Key 可能未激活），降级为无 Key 模式重试")
            headers.pop("x-api-key", None)
            resp = httpx.get(url, params=params, headers=headers, timeout=_TIMEOUT)
        if resp.status_code == 200:
            data = resp.json()
        else:
            logger.warning("Semantic Scholar HTTP %d: %s", resp.status_code, query[:40])
            return []
    except Exception as e:
        logger.warning("Semantic Scholar 请求失败: %s", e)
        return []

    refs = []
    for item in data.get("data", []):
        title = (item.get("title") or "").strip()
        if not title:
            continue

        authors = []
        for a in item.get("authors", []) or []:
            name = (a.get("name") or "").strip()
            if name:
                authors.append(name)

        year = str(item.get("year", "") or "").strip() or None

        venue = None
        journal = item.get("journal") or {}
        if journal.get("name"):
            venue = journal["name"].strip() or None
        if not venue:
            venue = (item.get("venue") or "").strip() or None

        doi = None
        ext_ids = item.get("externalIds") or {}
        if ext_ids.get("DOI"):
            doi = ext_ids["DOI"].strip()

        paper_url = (item.get("url") or "").strip() or None

        abstract = (item.get("abstract") or "").strip() or None

        refs.append(Reference(
            title=title,
            authors=authors,
            year=year,
            venue=venue,
            doi=doi,
            url=paper_url,
            abstract=abstract,
            source_tag="semantic_scholar",
            pinned=False,
            low_confidence=not year or not authors,
        ))

    _cache[ck] = refs
    logger.info("Semantic Scholar [%s] → %d 条", query[:40], len(refs))
    return refs


# ── 全局文献池裁剪 ─────────────────────────────────────────────

def _trim_store_to_limit(store: ReferenceStore, keywords: List[str], max_total: int):
    """
    当文献池超过 max_total 条时，按相关性评分保留最高的 max_total 条。
    pinned 文献始终保留。
    """
    all_refs = store.all_refs()
    if len(all_refs) <= max_total:
        return

    pinned = [r for r in all_refs if r.pinned]
    non_pinned = [r for r in all_refs if not r.pinned]

    # 对非 pinned 文献评分排序
    scored = [(r, _relevance_score(r, keywords)) for r in non_pinned]
    scored.sort(key=lambda x: -x[1])

    keep_count = max(0, max_total - len(pinned))
    kept_non_pinned = [r for r, _ in scored[:keep_count]]

    # 重建 store（仅内存操作）
    new_refs = pinned + kept_non_pinned
    store._store = {r.id: r for r in new_refs}
    logger.info("文献池裁剪：%d → %d 条（最大上限 %d）",
                len(all_refs), len(store), max_total)


# ── 关键词 → 泛化检索词映射 ─────────────────────────────────

_KEYWORD_EXPANSION = {
    # MCU / 单片机
    "STM32": [
        "ARM Cortex-M microcontroller embedded system",
        "STM32 embedded application sensor",
    ],
    "STM32F103": [
        "ARM Cortex-M3 microcontroller embedded",
        "STM32F103 sensor data acquisition",
    ],
    "单片机": [
        "microcontroller embedded system sensor monitoring",
        "MCU based data acquisition control",
    ],
    "AT89": [
        "8051 microcontroller embedded system",
        "8-bit MCU sensor monitoring",
    ],
    "STC": [
        "8051 microcontroller embedded control",
        "8-bit MCU data acquisition",
    ],
    "ESP32": [
        "ESP32 IoT sensor monitoring",
        "WiFi microcontroller embedded system",
    ],
    "ESP8266": [
        "ESP8266 WiFi sensor data acquisition",
        "IoT wireless monitoring system",
    ],
    "Arduino": [
        "Arduino microcontroller sensor monitoring",
        "open-source hardware embedded system",
    ],
    "Raspberry": [
        "Raspberry Pi embedded system monitoring",
        "single board computer sensor data acquisition",
    ],
    # 传感器
    "DHT11": [
        "digital humidity temperature sensor monitoring",
        "DHT sensor environmental data acquisition",
    ],
    "DHT22": [
        "digital temperature humidity sensor calibration",
        "DHT22 precision environmental monitoring",
    ],
    "DS18B20": [
        "digital temperature sensor one-wire monitoring",
        "DS18B20 precision temperature measurement",
    ],
    "温湿度": [
        "temperature humidity monitoring sensor embedded",
        "environmental parameter data acquisition",
    ],
    "温度": [
        "temperature sensor monitoring embedded system",
        "digital temperature measurement data acquisition",
    ],
    "湿度": [
        "humidity sensor monitoring embedded system",
        "digital humidity measurement data acquisition",
    ],
    "土壤湿度": [
        "soil moisture sensor monitoring embedded",
        "soil humidity measurement irrigation control",
    ],
    "光照": [
        "light intensity sensor monitoring embedded",
        "illumination measurement data acquisition",
    ],
    "气体": [
        "gas sensor monitoring embedded system",
        "air quality detection sensor data acquisition",
    ],
    # 通信
    "WiFi": [
        "wireless sensor network data transmission",
        "WiFi IoT monitoring system communication",
    ],
    "蓝牙": [
        "Bluetooth low energy sensor monitoring",
        "BLE wireless data acquisition embedded",
    ],
    "ZigBee": [
        "ZigBee wireless sensor network monitoring",
        "IEEE 802.15.4 sensor data acquisition",
    ],
    "NB-IoT": [
        "narrowband IoT sensor monitoring",
        "LPWAN wireless data acquisition embedded",
    ],
    "LoRa": [
        "LoRa wireless sensor network monitoring",
        "long range low power IoT data acquisition",
    ],
    # 应用场景
    "灌溉": [
        "irrigation control system sensor monitoring",
        "smart agriculture water management embedded",
    ],
    "农业": [
        "smart agriculture sensor monitoring IoT",
        "precision farming embedded system data acquisition",
    ],
    "监控": [
        "monitoring system sensor data acquisition embedded",
        "real-time monitoring IoT embedded control",
    ],
    "智能家居": [
        "smart home sensor monitoring IoT",
        "home automation embedded system control",
    ],
    "物联网": [
        "IoT sensor monitoring data acquisition",
        "Internet of Things embedded system application",
    ],
    "数据采集": [
        "data acquisition system sensor embedded",
        "DAQ monitoring measurement embedded system",
    ],
    "LCD": [
        "LCD display embedded system interface",
        "character display microcontroller interface",
    ],
    "OLED": [
        "OLED display embedded system interface",
        "graphic display microcontroller SPI I2C",
    ],
}

_GENERIC_EXPANSIONS = [
    "embedded system sensor monitoring data acquisition",
    "microcontroller based environmental monitoring IoT",
    "real-time sensor data acquisition embedded control",
]


def _generate_expanded_queries(keywords: List[str], max_queries: int = 8) -> List[str]:
    """
    从关键词列表生成泛化检索词。
    优先从映射表匹配，不足时补充通用泛化词。
    去重，限制 max_queries 条。
    """
    seen: set[str] = set()
    expanded: list[str] = []

    kw_lower = [k.lower() for k in keywords]

    for kw in keywords:
        mapped = _KEYWORD_EXPANSION.get(kw) or _KEYWORD_EXPANSION.get(kw.lower())
        if mapped:
            for q in mapped:
                q_lower = q.lower()
                if q_lower not in seen:
                    seen.add(q_lower)
                    expanded.append(q)
                    if len(expanded) >= max_queries:
                        return expanded

    for generic in _GENERIC_EXPANSIONS:
        g_lower = generic.lower()
        if g_lower not in seen:
            seen.add(g_lower)
            expanded.append(generic)
            if len(expanded) >= max_queries:
                return expanded

    return expanded[:max_queries]


def run_expanded_search(
    store: ReferenceStore,
    keywords: List[str],
    synonym_map: dict | None = None,
) -> int:
    """
    用泛化检索词进行一次补充检索（OpenAlex + Crossref + Semantic Scholar，不打 arXiv）。
    返回新增文献条数（去重后）。
    用于主检索后文献池仍不足 min_references 时的补充。
    synonym_map: 中文→英文同义词，注入相关性打分以解决跨语言匹配
    """
    queries = _generate_expanded_queries(keywords)
    if not queries:
        logger.info("泛化搜索：无可用泛化词，跳过")
        return 0

    logger.info("泛化搜索：%d 条泛化词 → %s ...", len(queries), queries[:3])
    min_score = float(get("min_ref_relevance_score", 0.03))

    any_success = False

    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = []
        for q in queries:
            futures.append(executor.submit(_search_openalex, q, per_page=10))
            futures.append(executor.submit(_search_crossref, q, rows=10))
            futures.append(executor.submit(_search_semantic_scholar, q, per_page=12))

        raw_refs: list[Reference] = []
        for future in as_completed(futures):
            try:
                result = future.result()
                if result:
                    raw_refs.extend(result)
                    any_success = True
            except Exception as e:
                logger.warning("泛化搜索子任务异常: %s", e)

    if not any_success or not raw_refs:
        logger.info("泛化搜索：无结果返回")
        return 0

    filtered = _filter_and_score(raw_refs, keywords, min_score=min_score, synonym_map=synonym_map)
    logger.info("泛化搜索：%d 条原始 → %d 条通过相关性过滤",
                len(raw_refs), len(filtered))

    added = store.merge(filtered)
    logger.info("泛化搜索：新增 %d 条（去重后），文献池共 %d 条", added, len(store))
    return added


# ── 主入口 ────────────────────────────────────────────────────

def run_search(
    plan: WritingPlan,
    store: ReferenceStore,
    synonym_map: dict | None = None,
) -> bool:
    """
    执行一轮自适应检索。
    - 每源每词只取 8 条（arXiv 6 条）
    - 检索结果先过滤低质量、再按相关性打分、再合并
    - 去重后入库，裁剪由 controller 中的 cull_poor_quality 统一负责
    - Semantic Scholar 使用语义搜索（embedding），对 CS/EE 领域覆盖率更高
    - synonym_map: 中文→英文同义词，注入相关性打分以解决跨语言匹配
    返回：True = 至少一个源有结果；False = 全部失败
    """
    queries = plan.search_queries
    if not queries:
        logger.warning("检索词为空，跳过检索")
        return False

    keywords = plan.keywords or []
    min_score = float(get("min_ref_relevance_score", 0.03))

    any_success = False

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = []
        for q in queries:
            futures.append(executor.submit(_search_openalex, q))
            futures.append(executor.submit(_search_crossref, q))
            futures.append(executor.submit(_search_semantic_scholar, q))
            # arXiv 只对纯英文查询
            if not any("\u4e00" <= c <= "\u9fff" for c in q):
                futures.append(executor.submit(_search_arxiv, q))

        raw_refs: List[Reference] = []
        for future in as_completed(futures):
            try:
                result = future.result()
                if result:
                    raw_refs.extend(result)
                    any_success = True
            except Exception as e:
                logger.warning("检索子任务异常: %s", e)

    if raw_refs:
        # 过滤低质量 + 相关性评分
        filtered = _filter_and_score(raw_refs, keywords, min_score=min_score, synonym_map=synonym_map)
        discarded = len(raw_refs) - len(filtered)
        logger.info("相关性过滤：%d 条原始 → %d 条通过（丢弃 %d 条）",
                    len(raw_refs), len(filtered), discarded)

        if not filtered and discarded > 0:
            logger.warning(
                "0 条通过相关性过滤（阈值=%s，原始=%d 条）。"
                "若关键词为中文且论文为英文学术文献，请检查 min_ref_relevance_score 和同义词映射。",
                min_score, len(raw_refs),
            )

        added = store.merge(filtered)
        logger.info("本轮检索新增 %d 条文献（去重后），文献池共 %d 条", added, len(store))

    return any_success
