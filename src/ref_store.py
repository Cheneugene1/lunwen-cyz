"""
文献池（ReferenceStore）
支持内存操作 + 可选 SQLite 持久化
提供：add, merge, list_pinned, by_id, all_refs, summary
"""

import json
import logging
import sqlite3
from pathlib import Path
from typing import List, Optional

from .models import Reference
from .dedup import deduplicate

logger = logging.getLogger(__name__)


# ── 运行时关键词同义词映射（LLM 生成 + 硬编码兜底）────────────

def _build_synonym_map(keywords: List[str]) -> dict[str, list[str]]:
    """
    用 LLM 将中文关键词翻译为英文学术同义词子串列表。
    失败时返回空 dict（caller 退回到硬编码 _KW_EN_MAP）。
    """
    if not keywords:
        return {}

    zh_keywords = [k for k in keywords if any("\u4e00" <= c <= "\u9fff" for c in k)]
    if not zh_keywords:
        return {}

    import re as _re

    system = (
        "你是一个学术术语翻译专家。给定一组中文关键词，为每个词生成2-5个在英文学术论文"
        "标题/摘要中最常见的同义英文词组（小写，不含标点）。输出纯JSON。"
    )
    user = (
        f"中文关键词：{', '.join(zh_keywords[:20])}\n\n"
        '输出示例：{{"单片机": ["microcontroller", "mcu"], "温湿度": ["temperature humidity"]}}'
    )

    try:
        from .llm import build_messages, chat_json
        messages = build_messages(system, user)
        raw = chat_json(messages, temperature=0.1, max_tokens=1024)
    except Exception as e:
        logger.info("关键词同义词 LLM 调用失败: %s，回退硬编码映射", e)
        return {}

    if not raw or not isinstance(raw, dict):
        logger.info("关键词同义词 LLM 返回空/非dict，回退硬编码映射")
        return {}

    result: dict[str, list[str]] = {}
    for k, v in raw.items():
        if not isinstance(v, list):
            continue
        subs = [str(x).strip().lower() for x in v if str(x).strip()]
        if subs:
            result[str(k).strip().lower()] = subs

    if result:
        logger.info(
            "关键词同义词 LLM 生成 %d 键: %s",
            len(result),
            ", ".join(f"{k}→{v[:2]}" for k, v in list(result.items())[:5]),
        )
    return result


class ReferenceStore:
    """
    内存文献池，可选通过 db_path 持久化到 SQLite。
    add / merge 后自动去重。
    """

    def __init__(self, db_path: Optional[Path] = None):
        # 内存存储：id → Reference
        self._store: dict[str, Reference] = {}
        self._db_path = db_path
        if db_path:
            self._init_db(db_path)

    # ── 持久化 ──────────────────────────────────────────────

    def _init_db(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path))
        conn.execute("""
            CREATE TABLE IF NOT EXISTS refs (
                id TEXT PRIMARY KEY,
                data TEXT NOT NULL
            )
        """)
        conn.commit()
        # 加载已有数据
        for row in conn.execute("SELECT data FROM refs"):
            ref = Reference.model_validate_json(row[0])
            self._store[ref.id] = ref
        conn.close()
        logger.info("ReferenceStore: loaded %d refs from %s", len(self._store), path)

    def _persist(self, ref: Reference):
        if not self._db_path:
            return
        conn = sqlite3.connect(str(self._db_path))
        conn.execute(
            "INSERT OR REPLACE INTO refs (id, data) VALUES (?, ?)",
            (ref.id, ref.model_dump_json()),
        )
        conn.commit()
        conn.close()

    # ── 增删查 ──────────────────────────────────────────────

    def add(self, ref: Reference) -> Reference:
        """添加单条文献（写入前先与现有列表去重合并）"""
        all_refs = list(self._store.values()) + [ref]
        deduped = deduplicate(all_refs)
        self._store = {r.id: r for r in deduped}
        # 如果 ref 被合并掉了，找到合并后的记录
        surviving = next(
            (r for r in deduped if ref.id == r.id or
             (r.title.lower().strip() == ref.title.lower().strip())),
            ref
        )
        self._persist(surviving)
        return surviving

    def merge(self, refs: List[Reference]) -> int:
        """批量导入，返回去重后新增条数"""
        before = len(self._store)
        all_refs = list(self._store.values()) + refs
        deduped = deduplicate(all_refs)
        self._store = {r.id: r for r in deduped}
        added = len(self._store) - before
        if self._db_path:
            for r in deduped:
                self._persist(r)
        logger.info("ReferenceStore.merge: +%d new (total %d)", added, len(self._store))
        return added

    def by_id(self, ref_id: str) -> Optional[Reference]:
        return self._store.get(ref_id)

    def list_pinned(self) -> List[Reference]:
        return [r for r in self._store.values() if r.pinned]

    def all_refs(self) -> List[Reference]:
        """返回所有文献，pinned 优先"""
        refs = list(self._store.values())
        refs.sort(key=lambda r: (not r.pinned, r.title))
        return refs

    def __len__(self) -> int:
        return len(self._store)

    def cull_poor_quality(
        self,
        keywords: Optional[List[str]] = None,
        max_total: int = 40,
        min_refs_to_keep: int = 0,
        synonym_map: Optional[dict[str, list[str]]] = None,
    ) -> int:
        """
        深度清洗文献池，移除低质量文献后裁剪到 max_total。
        返回被移除的条数。

        过滤规则（按严重程度）：
        1. 无作者或作者名非法 → 硬过滤
        2. 黑名单出版商/URL → 硬过滤
        3. 标题噪声（过短/摘要集/会议录等）→ 硬过滤
        4. venue 缺失（无期刊名+无DOI / J类无期刊名）→ 保留但标记 low_confidence
        5. 关键词命中数过滤（默认≥2，含中→英同义词扩展；不足时放宽到≥1并标记 low_confidence）
        6. 超过 max_total 时按相关性评分裁剪
        """
        import re

        VENUE_BLACKLIST = {
            "hans", "当代水电科技", "academic frontiers publishing",
            "knowledge repository", "doaj", "sciepub",
            "science publishing group", "open access library",
        }
        URL_BLACKLIST = {
            "ssrn.com", "techrxiv.org", "researchgate.net",
            "academia.edu", "preprints.org",
        }
        NOISE_TITLE_PATTERN = re.compile(
            r"^(abstracts?|proceedings?|editorial|foreword|index|table of contents)\s*$",
            re.I
        )

        def _is_poor(ref: "Reference") -> bool:
            if not ref.authors:
                return True

            valid_authors = [
                a for a in ref.authors
                if len(a.strip()) >= 2 and not a.strip().isdigit()
            ]
            if not valid_authors:
                return True

            venue_l = (ref.venue or "").lower()
            if any(bl in venue_l for bl in VENUE_BLACKLIST):
                return True

            url_l = (ref.url or "").lower()
            if any(bl in url_l for bl in URL_BLACKLIST):
                return True

            title = (ref.title or "").strip()
            if not title or len(title) < 5:
                return True
            if NOISE_TITLE_PATTERN.match(title):
                return True
            # 标题异常检测：截断/占位/机器生成数据
            title_l = title.lower()
            _BAD_TITLE_TOKENS = [
                "core id", "undefined", "null", "missing",
                "unknown", "no title", "untitled", "test",
            ]
            if any(tok in title_l for tok in _BAD_TITLE_TOKENS):
                return True
            # 全大写且无空格（如 COREBASEDDESIGN）大概率是数据错误
            # CJK 字符不参与大小写变化，排除中文/日文标题（否则"基于STM32..."被误判）
            cjk_chars = sum(1 for c in title if "一" <= c <= "鿿" or "぀" <= c <= "ヿ")
            if (
                cjk_chars == 0
                and title.upper() == title
                and " " not in title
                and len(title) > 15
            ):
                return True

            return False

        # 关键词→英文同义子串映射：中文关键词无法命中英文标题/摘要时，用同义词扩展匹配
        _KW_EN_MAP: dict[str, list[str]] = {
            "单片机": ["microcontroller", "mcu", "single chip", "embedded controller",
                      "cortex-m", "cortex m", "microcomputer", "microprocessor"],
            "stm32": ["stm32", "stm32f", "cortex-m", "cortex m", "arm cortex",
                     "stmicroelectronics", "hal library"],
            "温湿度": ["temperature humidity", "temperature and humidity",
                      "humidity temperature", "temp humidity", "dht",
                      "thermal humidity", "hygrothermal"],
            "温度": ["temperature", "thermal", "temp", "thermometer", "thermocouple"],
            "湿度": ["humidity", "moisture", "humid", "hygrometer", "dew point", "relative humidity"],
            "土壤湿度": ["soil moisture", "soil humidity", "soil water", "moisture sensor",
                        "soil water content", "volumetric water", "tensiometer"],
            "传感器": ["sensor", "transducer", "detector", "sensing", "probe",
                      "sensing element", "measurement device", "gauge"],
            "数据采集": ["data acquisition", "daq", "data collection", "data logging",
                        "sampling", "adc", "analog to digital", "signal acquisition",
                        "measurement system", "datalogger"],
            "检测": ["detection", "detecting", "measurement", "sensing", "monitoring",
                    "inspection", "identification", "diagnosis"],
            "监控": ["monitoring", "surveillance", "tracking", "detection", "measurement",
                    "observation", "supervision", "real-time monitor"],
            "物联网": ["iot", "internet of things", "wireless sensor", "wsn", "smart",
                      "connected device", "m2m", "machine to machine"],
            "智能": ["smart", "intelligent", "automated", "automatic", "fuzzy", "adaptive",
                    "cognitive", "self-tuning"],
            "灌溉": ["irrigation", "watering", "water management", "agriculture", "farming",
                    "drip irrigation", "sprinkler", "water-saving"],
            "农业": ["agriculture", "farming", "crop", "greenhouse", "cultivation",
                    "precision agriculture", "smart farming", "agritech"],
            "控制": ["control", "controller", "regulation", "pid", "fuzzy", "feedback",
                    "closed-loop", "open-loop", "actuator", "driver"],
            "无线": ["wireless", "wifi", "bluetooth", "ble", "zigbee", "lora", "rf",
                    "nrf24l01", "esp8266", "esp32", "radio frequency"],
            "显示": ["display", "lcd", "oled", "screen", "interface", "hmi",
                    "touch screen", "tft", "led display", "dashboard"],
            "通信": ["communication", "transmission", "protocol", "network", "serial",
                    "uart", "i2c", "spi", "rs232", "rs485", "modbus", "can bus", "mqtt"],
            "报警": ["alarm", "alert", "warning", "notification", "buzzer",
                    "threshold alarm", "audible alert", "sms alert"],
            "低功耗": ["low power", "low-power", "energy efficient", "battery",
                      "sleep mode", "power saving", "ultra-low", "energy harvesting"],
            "嵌入式": ["embedded", "microcontroller", "mcu", "firmware", "real-time",
                      "bare metal", "arm", "rtos", "freertos"],
            # ── 新增高频选题词 ──
            "设计": ["design", "implementation", "development", "fabrication",
                    "construction", "prototype", "architecture"],
            "系统": ["system", "platform", "framework", "infrastructure", "architecture"],
            "硬件": ["hardware", "circuit", "board", "pcb", "schematic",
                    "electronic", "wiring", "breadboard", "prototype board"],
            "软件": ["software", "firmware", "program", "code", "application",
                    "embedded software", "keil", "iar", "stm32cubeide"],
            "算法": ["algorithm", "method", "approach", "technique", "scheme",
                    "strategy", "computation", "processing"],
            "滤波": ["filter", "filtering", "kalman", "smoothing", "denoising",
                    "moving average", "median filter", "low-pass", "digital filter"],
            "模糊": ["fuzzy", "fuzzy logic", "fuzzy inference", "membership",
                    "defuzzification", "fuzzification"],
            "pid": ["pid", "proportional integral", "feedback control", "pid controller",
                   "pid tuning", "pi controller"],
            "云平台": ["cloud", "cloud platform", "iot platform", "server",
                      "thingspeak", "alibaba cloud", "aws iot", "onenet"],
            "定时": ["timer", "timing", "clock", "scheduling", "periodic",
                    "time interval", "time-based"],
            "中断": ["interrupt", "isr", "handler", "interrupt service",
                    "external interrupt", "timer interrupt"],
            "串口": ["serial", "uart", "usart", "rs232", "rs485",
                    "serial port", "serial communication"],
            "adc": ["adc", "analog to digital", "analog input", "a/d converter",
                   "sampling", "analog signal"],
            "pwm": ["pwm", "pulse width modulation", "duty cycle", "pulse signal",
                   "motor speed", "brightness control"],
            "继电器": ["relay", "actuator", "switch", "electromagnetic relay",
                      "solid state relay", "ssr"],
            "水泵": ["pump", "water pump", "actuator", "motor pump",
                    "peristaltic", "diaphragm pump"],
            "led": ["led", "light", "indicator", "light emitting", "led display",
                   "lighting", "illumination"],
            "电源": ["power", "supply", "regulator", "battery", "voltage regulator",
                    "power management", "dc-dc", "buck converter"],
            "仿真": ["simulation", "simulink", "proteus", "modeling", "emulation",
                    "multisim", "virtual prototype"],
            "驱动": ["driver", "actuator", "motor", "motor driver", "l298n",
                    "l293d", "uln2003", "h-bridge"],
            "电机": ["motor", "actuator", "servo", "stepper", "dc motor",
                    "brushless", "bldc", "motor control"],
            "定时器": ["timer", "counter", "timing", "prescaler",
                      "pwm generation", "input capture", "output compare"],
            "信号": ["signal", "waveform", "pulse", "voltage", "current",
                    "analog signal", "digital signal", "signal conditioning"],
            "实时": ["real-time", "real time", "rtos", "online", "instantaneous",
                    "live monitoring", "concurrent"],
            "远程": ["remote", "distance", "wireless", "telemetry",
                    "remote monitoring", "telemonitoring"],
            "界面": ["interface", "ui", "gui", "screen", "dashboard",
                    "user interface", "touch screen", "hmi", "display panel"],
            "上位机": ["host", "pc", "computer", "upper computer", "labview",
                      "host computer", "desktop application", "c#", "winform"],
            "精度": ["accuracy", "precision", "resolution", "sensitivity",
                    "error margin", "tolerance"],
            "响应": ["response", "reaction", "feedback", "response time",
                    "settling time", "rise time", "transient"],
        }

        def _is_venue_deficient(ref: "Reference") -> bool:
            return (not ref.venue and not ref.doi) or (
                not ref.venue and _guess_ref_type(ref) == "J"
            )

        before = len(self._store)
        pinned = [r for r in self._store.values() if r.pinned]
        non_pinned = [r for r in self._store.values() if not r.pinned]

        survived = [r for r in non_pinned if not _is_poor(r)]
        culled_count = len(non_pinned) - len(survived)

        venue_deficient_count = 0
        for r in survived:
            if not r.pinned and _is_venue_deficient(r):
                r.low_confidence = True
                venue_deficient_count += 1
        if venue_deficient_count > 0:
            logger.info("venue缺失标记：%d 条 → low_confidence（不再硬过滤）", venue_deficient_count)

        keyword_culled = 0
        fallback_rescued = 0

        if keywords and len(keywords) >= 2:
            kw_lower = [k.lower() for k in keywords]

            # 合并 LLM 运行时同义词到硬编码映射（LLM 映射优先覆盖同键）
            active_kw_map = dict(_KW_EN_MAP)
            if synonym_map:
                active_kw_map.update(synonym_map)

            def _count_hits(ref: "Reference") -> int:
                tl = (ref.title or "").lower()
                al = (ref.abstract or "")[:500].lower()
                combined = f"{tl} {al}"
                hits = 0
                for k in kw_lower:
                    if k in combined:
                        hits += 1
                    else:
                        en_subs = active_kw_map.get(k)
                        if en_subs:
                            if any(es in combined for es in en_subs):
                                hits += 1
                return hits

            scored = [(_count_hits(r), r) for r in survived]

            normal_kept = [r for hits, r in scored if hits >= 2]
            keyword_culled = len(survived) - len(normal_kept)

            if min_refs_to_keep > 0 and len(normal_kept) + len(pinned) < min_refs_to_keep:
                rescued = [r for hits, r in scored if hits == 1]
                if rescued:
                    for r in rescued:
                        r.low_confidence = True
                    normal_kept = normal_kept + rescued
                    keyword_culled -= len(rescued)
                    fallback_rescued = len(rescued)
                    logger.info(
                        "cull 兜底放宽：关键词命中≥1 救回 %d 条（已标记 low_confidence），"
                        "现 pool=%d，命中≥2 的仅 %d 条",
                        fallback_rescued, len(normal_kept) + len(pinned),
                        len(normal_kept) - fallback_rescued,
                    )

            survived = normal_kept
            culled_count += keyword_culled

        if keywords and len(survived) + len(pinned) > max_total:
            keep_n = max(0, max_total - len(pinned))

            def _score(ref: "Reference") -> float:
                title_l = (ref.title or "").lower()
                kw_hits = sum(1 for kw in keywords if kw.lower() in title_l)
                year_bonus = 0.1 if (ref.year or "0") >= "2018" else 0.0
                return kw_hits + year_bonus

            survived.sort(key=_score, reverse=True)
            surplus = len(survived) - keep_n
            survived = survived[:keep_n]

        new_refs = pinned + survived
        self._store = {r.id: r for r in new_refs}

        removed = before - len(self._store)
        parts = [f"{before} → {len(self._store)} 条"]
        if culled_count - keyword_culled > 0:
            parts.append(f"硬过滤 {culled_count - keyword_culled} 条")
        if keyword_culled > 0:
            parts.append(f"关键词命中不足 {keyword_culled} 条")
        if fallback_rescued > 0:
            parts.append(f"兜底救回 {fallback_rescued} 条(low_confidence)")
        if removed - culled_count > 0:
            parts.append(f"超量裁剪 {removed - culled_count} 条")
        logger.info("文献池清洗：%s", " + ".join(parts))
        return removed

    def summary(self) -> str:
        """返回文献池简要统计（供 LLM 上下文使用）"""
        total = len(self._store)
        pinned = sum(1 for r in self._store.values() if r.pinned)
        return f"文献池共 {total} 条（其中手动导入 {pinned} 条）"

    def as_context_text(self, max_refs: int = 60) -> str:
        """
        将文献池格式化为 LLM 上下文文本（写作时引用用）
        最多取 max_refs 条（pinned 优先）
        格式：序号 [类型] 标题 — 作者 (年份)
        """
        refs = self.all_refs()[:max_refs]
        lines = []
        for i, r in enumerate(refs, 1):
            authors_str = "; ".join(r.authors[:3]) + ("等" if len(r.authors) > 3 else "")
            tag = "[手动]" if r.pinned else "[自动]"
            if r.low_confidence:
                tag += "[低信]"
            ref_type = _guess_ref_type(r)
            abstract_short = (r.abstract or "")[:100].replace("\n", " ")
            lines.append(
                f"[{i}]{tag}[{ref_type}] {r.title} — {authors_str} "
                f"({r.year or 'n.d.'}) {abstract_short}"
            )
        return "\n".join(lines)

    def format_thesis_ref_list(self, order_map: dict | None = None) -> str:
        """
        生成符合中国高校毕业论文规范的参考文献列表文本。
        格式：[J] 期刊, [M] 著作, [C] 论文集, [D] 学位论文, [EB/OL] 网页
        标点用英文标点 + 一个空格，作者最多三位加", 等"或", et al."

        order_map: 可选，{老编号: 新编号} 映射表。传入时按新编号顺序输出，
                   且只输出映射表中存在的文献（即正文实际引用过的）。
                   未传入时按 store 原始顺序全部输出。
        """
        if order_map:
            refs_all = self.all_refs()
            new_to_old = {new: old for old, new in order_map.items()}
            lines = []
            for new_num in sorted(new_to_old.keys()):
                old_num = new_to_old[new_num]
                ref = refs_all[old_num - 1] if old_num <= len(refs_all) else None
                if ref is not None:
                    lines.append(f"[{new_num}] {_format_thesis_ref(ref)}")
            return "\n".join(lines) if lines else "（暂无参考文献）"

        refs = self.all_refs()
        lines = []
        for i, r in enumerate(refs, 1):
            lines.append(f"[{i}] {_format_thesis_ref(r)}")
        return "\n".join(lines) if lines else "（暂无参考文献）"


# ── 参考文献类型推断 ──────────────────────────────────────────

def _guess_ref_type(ref: "Reference") -> str:
    """
    根据文献元数据推断类型标识：
    J（期刊）/ M（著作）/ C（论文集）/ D（学位论文）/ EB/OL（网页）
    """
    from .models import Reference
    title_lower = (ref.title or "").lower()
    venue_lower = (ref.venue or "").lower()
    source = ref.source_tag or ""

    # arXiv 预印本：视为期刊/会议论文
    if ref.doi and ref.doi.startswith("arXiv:"):
        return "J"

    # 学位论文特征词
    thesis_words = ("thesis", "dissertation", "学位论文", "硕士", "博士", "毕业论文")
    if any(w in title_lower or w in venue_lower for w in thesis_words):
        return "D"

    # 会议/论文集特征词
    conf_words = ("conference", "proceedings", "workshop", "symposium",
                  "会议", "论文集", "proc.", "proc ")
    if any(w in venue_lower for w in conf_words):
        return "C"

    # 网页
    if source == "openalex" and not ref.venue:
        pass  # 不能仅凭来源判断
    if ref.url and not ref.doi and not ref.venue:
        return "EB/OL"

    # 有 venue（期刊名）→ 期刊论文
    if ref.venue:
        return "J"

    # 默认为期刊
    return "J"


def _format_authors_thesis(authors: list, is_foreign: bool = False) -> str:
    """
    毕业论文参考文献作者格式：
    - 中文：姓在前，名在后（王小明）
    - 外文：姓在前，名缩写在后（Smith A B），最多三位，超出写", et al"或", 等"
    """
    if not authors:
        return ""
    shown = authors[:3]
    extra = len(authors) > 3
    result = ", ".join(shown)
    if extra:
        # 根据第一作者是否含中文判断
        has_chinese = any("\u4e00" <= c <= "\u9fff" for c in (shown[0] if shown else ""))
        result += ", 等" if has_chinese else ", et al"
    return result


def _format_thesis_ref(ref: "Reference") -> str:
    """
    生成单条毕业论文格式参考文献字符串。
    根据类型套用对应模板。
    """
    from .models import Reference
    ref_type = _guess_ref_type(ref)
    authors = _format_authors_thesis(ref.authors)
    title = ref.title or "（无题）"
    year = ref.year or "n.d."
    venue = ref.venue or ""
    doi_url = f" https://doi.org/{ref.doi}" if ref.doi and not ref.doi.startswith("arXiv") else (
        f" {ref.url}" if ref.url else ""
    )

    if ref_type == "J":
        # 期刊: 作者. 标题[J]. 期刊名, 年, 卷(期): 页.
        return f"{authors}. {title}[J]. {venue or '（期刊未知）'}, {year}.{doi_url}"

    elif ref_type == "M":
        # 著作: 作者. 书名[M]. 出版地: 出版社, 年.
        return f"{authors}. {title}[M]. （出版地）: （出版社）, {year}."

    elif ref_type == "C":
        # 论文集: 作者. 标题[C]. 会议名. 出版地: 出版社, 年.
        return f"{authors}. {title}[C]. {venue or '（会议名）'}. {year}."

    elif ref_type == "D":
        # 学位论文: 作者. 标题[D]. 城市: 学校, 年.
        return f"{authors}. {title}[D]. （所在城市）: （学校名称）, {year}."

    else:
        # EB/OL: 作者. 标题[EB/OL]. (更新日期)[引用日期]. URL
        url_str = ref.url or doi_url or "（URL未知）"
        return f"{authors}. {title}[EB/OL]. {url_str}"
