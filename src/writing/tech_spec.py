"""
技术规范文档模块（TechSpec）

解决的核心问题：
  各章节 LLM 独立生成时，会对同一系统的技术细节（传感器型号、通信协议、控制周期等）
  作出不同描述，导致全文自相矛盾。

解决方案：
  1. 在写任何章节之前，先由 LLM 生成一份"技术规范文档（TechSpec）"
  2. 可选：用户提供锁定 JSON（L1），与 LLM 结果深合并，锁定层覆盖同键
  3. TechSpec 以 JSON + 格式化文本两种形式保存
  4. 所有章节（包括摘要）的 prompt 强制包含 TechSpec 文本
  5. LLM 被明确告知：TechSpec 中的事实不得更改

TechSpec 包含内容：
  - 硬件：MCU 型号、所有传感器（型号+类型）、执行器规格、通信模块
  - 软件：架构（无 RTOS / 有 RTOS）、语言、IDE、任务调度方式
  - 算法：控制算法类型、关键参数
  - 网络/APP：云平台、通信协议、APP 开发环境
  - 关键指标：控制精度、响应时间等（可留[实测]占位）
"""

import json
import logging
from typing import Optional

from ..config import get
from ..llm import chat_json, build_messages
from ..models import WritingPlan
from ..ref_store import ReferenceStore

logger = logging.getLogger(__name__)


# ── 生成 TechSpec 的 Prompt ─────────────────────────────────────

_SYSTEM_TECHSPEC = """你是一位嵌入式系统和物联网领域的资深工程师，正在为一篇本科毕业论文制定系统技术规范。

你的任务：根据用户的研究主题和论文大纲，生成一份精确、详细的技术规范文档（JSON格式）。

这份文档将作为整篇论文的"唯一技术事实来源"：
- 所有章节（摘要、引言、设计、实现、实验、结论）必须严格遵守此规范
- 规范中的硬件型号、协议选择、参数数值不得被任何章节随意更改
- 规范中已明确标注的传感器类型（如"电阻式"）不得被任何章节改为其他类型

请输出合法 JSON，结构如下（根据实际系统填写，不得保留示例值）：
{
  "system_name": "系统全称（20字以内）",
  "hardware": {
    "mcu": {
      "model": "具体芯片型号（如STM32F103C8T6）",
      "core": "ARM Cortex-M3",
      "clock_mhz": 72,
      "notes": "选型理由（1句话）"
    },
    "sensors": [
      {
        "name": "传感器功能名称（如土壤湿度传感器）",
        "model": "型号（如YL-69）",
        "type": "工作原理类型（如电阻式/电容式/NTC热敏/DHT数字）",
        "interface": "接口（ADC/UART/I2C/1-Wire等）",
        "notes": "关键注意事项（如：YL-69是电阻式，不是电容式）"
      }
    ],
    "actuators": [
      {
        "name": "执行器名称",
        "spec": "规格参数（电压、功率等）",
        "driver": "驱动方式（如继电器/MOSFET）"
      }
    ],
    "communication_module": {
      "model": "通信模块型号（如ESP8266）",
      "protocol": "通信协议（如Wi-Fi/MQTT）",
      "ble": false
    },
    "display": "显示模块型号（如OLED 0.96寸SSD1306，或null）",
    "power": "电源方案（如5V/2A USB供电）"
  },
  "software": {
    "language": "编程语言（如C语言）",
    "ide": "开发环境（如Keil MDK 5）",
    "arch": "软件架构（如前后台架构（主循环+定时器中断））",
    "rtos": false,
    "rtos_note": "（若rtos为false，此处写：无RTOS，禁止在摘要/任何章节中提及RTOS）",
    "sampling_period_s": 5,
    "control_period_s": 30,
    "filter_algorithm": "滤波算法（如中值平均滤波，连续采样10次去极值取均值）"
  },
  "algorithm": {
    "type": "控制算法（如模糊PID控制算法）",
    "kp_init": 1.5,
    "ki_init": 0.2,
    "kd_init": 0.05,
    "fuzzy_input": ["湿度误差e（单位：%RH）", "误差变化率ec（单位：%RH/s）"],
    "fuzzy_output": ["ΔKp", "ΔKi", "ΔKd"],
    "fuzzy_rules_count": 49,
    "pid_type": "增量式PID",
    "notes": "重要：全文统一使用增量式PID，不得使用位置式PID"
  },
  "cloud_and_app": {
    "cloud_platform": "云平台（如阿里云IoT平台）",
    "app_platform": "APP开发环境（如Android Studio）",
    "app_comm": "APP通信方式（如通过Wi-Fi经MQTT协议与阿里云通信，无蓝牙BLE）",
    "ble_used": false,
    "ble_note": "（若ble_used为false，写：系统无蓝牙BLE模块，禁止在任何章节提及BLE）"
  },
  "key_metrics": {
    "control_accuracy": "控制精度（如±3%RH）",
    "response_time_s": 2,
    "wifi_success_rate": ">98%",
    "water_saving_rate": ">30%（与固定阈值对比实验结论）",
    "cost_note": "硬件成本说明（可写'低成本'，也可给出具体数值）"
  },
  "forbidden_content": [
    "禁止在摘要或任何章节中写'电容式土壤湿度传感器'（实际是电阻式YL-69）",
    "禁止在任何章节写'蓝牙BLE'（系统无蓝牙模块）",
    "禁止在任何章节写'RTOS'或'实时操作系统'（系统无RTOS）",
    "控制周期固定为每30秒一次，采样周期固定为每5秒一次，禁止在其他章节使用不同数值"
  ]
}

重要规则：
1. sensor 的 type 字段必须与实际工作原理一致（YL-69=电阻式，不写电容式）
2. ble: false 时必须明确说明无蓝牙，防止LLM在后续章节编造蓝牙相关内容
3. rtos: false 时必须明确说明无RTOS
4. forbidden_content 列表要列出所有最常见的造假错误，至少5条
5. 所有参数值必须内部自洽（sampling_period和control_period不能在其他字段中出现不同数值）
"""


def generate_tech_spec(
    plan: WritingPlan,
    store: ReferenceStore,
    user_request: str,
) -> dict:
    """
    在开始写作之前，为整篇论文生成一份技术规范文档（TechSpec）。
    返回 dict（JSON-compatible）。

    失败时返回空 dict（写作模块会以"无TechSpec约束"模式继续）。
    """
    outline_summary = "\n".join(
        f"- {s.section_id}: {s.title}（要点：{', '.join(s.bullets[:3])}）"
        for s in plan.outline
    )
    kw_text = "；".join(plan.keywords[:5]) if plan.keywords else "（未提供）"
    ref_summary = store.summary()

    # 提取文档摘要（若有文档上下文）
    doc_context = ""
    if hasattr(plan, "_doc_summary") and plan._doc_summary:
        doc_context = f"## 项目文档摘要（开题/中期报告内容，权威性最高）\n{plan._doc_summary[:1500]}\n\n"

    user_prompt = (
        f"## 研究主题\n{user_request[:600]}\n\n"
        f"{doc_context}"
        f"## 论文大纲\n{outline_summary}\n\n"
        f"## 关键词\n{kw_text}\n\n"
        f"## 当前文献池\n{ref_summary}\n\n"
        "## 任务\n"
        "根据以上信息，生成一份完整的技术规范文档（JSON格式）。\n"
        "【重要】如果提供了'项目文档摘要'，必须以其中的技术事实为最高权威来源，\n"
        "不得与项目文档矛盾。例如项目文档明确说'YL-69是电阻式传感器'，就不能写电容式。\n"
        "只使用项目文档中实际提到的模块，不要添加文档中没有提到的功能（如没有提到Wi-Fi就不要加）。\n"
        "请基于项目文档推断技术选型，不确定的参数给出最合理的估计值，之后全文须保持一致。\n"
        "尤其注意：传感器类型（电阻/电容）、通信协议（Wi-Fi/BLE/无网络）、"
        "是否使用RTOS，必须在JSON中明确标注，并写入 forbidden_content。"
    )

    messages = build_messages(_SYSTEM_TECHSPEC, user_prompt)
    raw = chat_json(messages, temperature=0.2, max_tokens=6000)

    if not raw:
        logger.warning("TechSpec 生成失败，写作将在无约束模式下进行")
        return {}

    logger.info("TechSpec 生成成功：%s", raw.get("system_name", "未命名系统"))
    return raw


def format_tech_spec_for_prompt(spec: dict) -> str:
    """
    将 TechSpec dict 格式化为 LLM 可读的文本块，注入每个章节的 prompt 中。
    采用简洁、强调格式，方便 LLM 遵守。
    """
    if not spec:
        return ""

    lines = [
        "═══════════════════════════════════════",
        "【技术规范文档 - 全文必须严格遵守，不得违反以下任何事实】",
        "═══════════════════════════════════════",
    ]

    # 系统名称
    if spec.get("system_name"):
        lines.append(f"系统名称：{spec['system_name']}")

    # 硬件
    hw = spec.get("hardware", {})
    if hw:
        lines.append("\n【硬件规格】")
        mcu = hw.get("mcu", {})
        if mcu:
            lines.append(f"  核心控制器：{mcu.get('model', '')} "
                         f"（{mcu.get('core', '')}，{mcu.get('clock_mhz', '')}MHz）")

        sensors = hw.get("sensors", [])
        for s in sensors:
            note = f" ⚠ 注意：{s['notes']}" if s.get("notes") else ""
            lines.append(f"  {s.get('name', '')}：型号 {s.get('model', '')}，"
                         f"类型：{s.get('type', '')}，接口：{s.get('interface', '')}{note}")

        actuators = hw.get("actuators", [])
        for a in actuators:
            lines.append(f"  {a.get('name', '')}：{a.get('spec', '')}，"
                         f"驱动：{a.get('driver', '')}")

        comm = hw.get("communication_module", {})
        if comm:
            ble_note = "（系统无蓝牙BLE，禁止提及BLE）" if not comm.get("ble", True) else ""
            lines.append(f"  通信模块：{comm.get('model', '')}，"
                         f"协议：{comm.get('protocol', '')} {ble_note}")

    # 软件
    sw = spec.get("software", {})
    if sw:
        lines.append("\n【软件规格】")
        rtos_note = "⚠ 无RTOS，禁止在任何章节提及RTOS或实时操作系统" if not sw.get("rtos", True) else f"使用RTOS"
        lines.append(f"  架构：{sw.get('arch', '')} — {rtos_note}")
        lines.append(f"  语言/IDE：{sw.get('language', '')} / {sw.get('ide', '')}")
        lines.append(f"  ⚠ 采样周期：每 {sw.get('sampling_period_s', '?')} 秒一次"
                     f"（全文统一，禁止出现其他数值）")
        lines.append(f"  ⚠ 控制周期：每 {sw.get('control_period_s', '?')} 秒执行一次"
                     f"（全文统一，禁止出现其他数值）")
        if sw.get("filter_algorithm"):
            lines.append(f"  滤波算法：{sw['filter_algorithm']}")

    # 算法
    algo = spec.get("algorithm", {})
    if algo:
        lines.append("\n【控制算法】")
        lines.append(f"  算法类型：{algo.get('type', '')} （{algo.get('pid_type', '')}）")
        lines.append(f"  初始参数：Kp={algo.get('kp_init', '')}, "
                     f"Ki={algo.get('ki_init', '')}, Kd={algo.get('kd_init', '')}")
        if algo.get("fuzzy_rules_count"):
            lines.append(f"  模糊规则数：{algo['fuzzy_rules_count']}条")
        if algo.get("notes"):
            lines.append(f"  ⚠ {algo['notes']}")

    # 网络/APP
    ca = spec.get("cloud_and_app", {})
    if ca:
        lines.append("\n【网络与APP】")
        lines.append(f"  云平台：{ca.get('cloud_platform', '')}")
        lines.append(f"  APP：{ca.get('app_platform', '')}，"
                     f"通信方式：{ca.get('app_comm', '')}")
        if not ca.get("ble_used", True):
            lines.append(f"  ⚠ 系统无蓝牙BLE，禁止在任何章节提及BLE通信协议")

    # 关键指标
    km = spec.get("key_metrics", {})
    if km:
        lines.append("\n【关键性能指标（实验章节须与此一致）】")
        for k, v in km.items():
            if k != "cost_note":
                lines.append(f"  {k}: {v}")

    # 禁止内容
    forbidden = spec.get("forbidden_content", [])
    if forbidden:
        lines.append("\n【❌ 禁止在任何章节出现的内容】")
        for item in forbidden:
            lines.append(f"  ❌ {item}")

    lines.append("═══════════════════════════════════════\n")
    return "\n".join(lines)
