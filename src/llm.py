"""
LLM 客户端封装（v2）
使用 DeepSeek API（OpenAI 兼容格式）

关键改进：
1. Streaming 模式：max_tokens > STREAM_THRESHOLD 时自动切换流式请求
   - 流式传输中每个 chunk 的到达间隔作为 read_timeout 判断依据
   - 只要模型还在持续输出（每 chunk 通常 < 1s），就不会超时
   - 完全解决长文本生成的整体超时问题

2. 独立的超时配置（`config.example.yml`：`deepseek_timeout_*`，避免 Pro 等模型在非流式 JSON 场景 90s 内未下满响应）：
   - 非流式（规划 / TechSpec / 评估 `chat_json` 等）：`connect` + **整段 read**（默认 read=300s，可由 `deepseek_timeout_read_blocking` 配置）
   - 流式（长文本）：`connect` + chunk 间隔 read（默认 120s）

3. 指数退避重试：
   - 连接失败 / 超时：最多重试 MAX_RETRIES 次，间隔指数增长
   - 限流（429）：更长的等待后再重试
"""

import json
import logging
import re
import time
from typing import Any, Optional

import httpx
from openai import OpenAI, APIError, APIConnectionError, RateLimitError, APITimeoutError

from .config import get

logger = logging.getLogger(__name__)

# ── 重试参数 ────────────────────────────────────────────────
MAX_RETRIES = 3         # 最多重试次数（加上首次共 4 次机会）
BASE_DELAY  = 3.0       # 首次重试等待（秒）
MAX_DELAY   = 60.0      # 最长等待上限

# 超过此 max_tokens 值时自动使用流式传输
STREAM_THRESHOLD = 1500


# ── 超时配置（整值秒，可由 YAML 覆盖）────────────────────────────
_WRITE_POOL = 30.0, 10.0  # write_s, pool_s


def _timeout_blocking() -> httpx.Timeout:
    """非流式：整段 body 须在 read 秒内到达（评估/规划 JSON 等）。"""
    c = float(get("deepseek_timeout_connect", 30.0))
    r = float(get("deepseek_timeout_read_blocking", 300.0))
    w, p = _WRITE_POOL
    return httpx.Timeout(connect=c, read=r, write=w, pool=p)


def _timeout_stream() -> httpx.Timeout:
    """流式：相邻 chunk 间隔不得超过 read 秒。"""
    c = float(get("deepseek_timeout_connect", 30.0))
    r = float(get("deepseek_timeout_read_stream", 120.0))
    w, p = _WRITE_POOL
    return httpx.Timeout(connect=c, read=r, write=w, pool=p)


def _get_client(streaming: bool = False) -> OpenAI:
    """构建 OpenAI 兼容客户端（指向 DeepSeek），根据是否流式选择超时"""
    timeout = _timeout_stream() if streaming else _timeout_blocking()
    return OpenAI(
        api_key=get("deepseek_api_key", ""),
        base_url=get("deepseek_base_url", "https://api.deepseek.com"),
        http_client=httpx.Client(timeout=timeout),
        max_retries=0,  # 自己管理重试逻辑
    )


def _model() -> str:
    return get("deepseek_model", "deepseek-chat")


def _backoff_delay(attempt: int) -> float:
    """指数退避：attempt 1→3s, 2→6s, 3→12s，上限 MAX_DELAY"""
    return min(BASE_DELAY * (2 ** (attempt - 1)), MAX_DELAY)


# ── 核心调用：流式传输（解决长文本超时）────────────────────────

def _chat_streaming(
    messages: list[dict],
    temperature: float,
    max_tokens: int,
    **kwargs,
) -> str:
    """
    使用流式传输调用 LLM，适合长文本生成（> STREAM_THRESHOLD tokens）。
    每收到一个 chunk 就立即拼接，不存在整体超时问题。
    """
    client = _get_client(streaming=True)
    parts = []

    stream = client.chat.completions.create(
        model=_model(),
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
        stream=True,
        **kwargs,
    )
    for chunk in stream:
        delta = chunk.choices[0].delta.content if chunk.choices else None
        if delta:
            parts.append(delta)

    return "".join(parts).strip()


# ── 核心调用：非流式（短请求）────────────────────────────────

def _chat_blocking(
    messages: list[dict],
    temperature: float,
    max_tokens: int,
    response_format: Optional[dict] = None,
) -> str:
    """非流式调用，适合 max_tokens ≤ STREAM_THRESHOLD 的短请求（如 JSON 规划）"""
    client = _get_client(streaming=False)
    kwargs: dict[str, Any] = {
        "model": _model(),
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if response_format:
        kwargs["response_format"] = response_format

    resp = client.chat.completions.create(**kwargs)
    return (resp.choices[0].message.content or "").strip()


# ── 公开接口 ────────────────────────────────────────────────

def chat(
    messages: list[dict],
    temperature: float = 0.7,
    max_tokens: int = 4096,
    response_format: Optional[dict] = None,
    force_streaming: bool = False,
) -> str:
    """
    向 DeepSeek 发送对话请求，返回助手回复文本。

    自动选择模式：
    - max_tokens > STREAM_THRESHOLD 或 force_streaming=True → 流式传输
    - 否则 → 非流式（适合 JSON 等需要原子完整响应的场景）

    失败时指数退避重试 MAX_RETRIES 次，仍失败则抛出 RuntimeError。
    """
    use_stream = force_streaming or (max_tokens > STREAM_THRESHOLD and not response_format)

    last_err: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 2):
        try:
            if use_stream:
                return _chat_streaming(messages, temperature, max_tokens)
            else:
                return _chat_blocking(messages, temperature, max_tokens, response_format)

        except RateLimitError as e:
            # 429 限流：等待更长时间
            wait = min(_backoff_delay(attempt) * 3, MAX_DELAY)
            logger.warning("LLM 限流 429 (attempt %d/%d)，等待 %.0fs 后重试",
                           attempt, MAX_RETRIES + 1, wait)
            last_err = e
            if attempt <= MAX_RETRIES:
                time.sleep(wait)

        except (APIConnectionError, APITimeoutError) as e:
            wait = _backoff_delay(attempt)
            logger.warning("LLM 连接/超时错误 (attempt %d/%d)：%s，等待 %.0fs 后重试",
                           attempt, MAX_RETRIES + 1, type(e).__name__, wait)
            last_err = e
            if attempt <= MAX_RETRIES:
                time.sleep(wait)

        except APIError as e:
            # 5xx 等服务端错误，也重试
            if hasattr(e, "status_code") and e.status_code and e.status_code < 500:
                # 4xx（非429）不重试
                raise
            wait = _backoff_delay(attempt)
            logger.warning("LLM API 错误 (attempt %d/%d)：%s，等待 %.0fs 后重试",
                           attempt, MAX_RETRIES + 1, e, wait)
            last_err = e
            if attempt <= MAX_RETRIES:
                time.sleep(wait)

    raise RuntimeError(
        f"LLM 调用失败，已重试 {MAX_RETRIES} 次。最后错误：{last_err}"
    ) from last_err


def chat_json(
    messages: list[dict],
    temperature: float = 0.3,
    max_tokens: int = 8000,
) -> dict:
    """
    调用 LLM 并期望获得合法 JSON 对象。
    JSON 场景均为短请求（规划/评估），强制使用非流式。
    策略：
      1. 带 response_format=json_object 调用
      2. 返回空则降级为不带 response_format 再调一次
      3. 解析失败则追加修复提示再调用一次
      4. 仍失败返回空字典
    """
    # JSON 场景强制非流式
    def _call(msgs, temp, toks, *, use_format: bool = True) -> str:
        fmt = {"type": "json_object"} if use_format else None
        return _chat_blocking(msgs, temp, toks, response_format=fmt)

    def _try_parse(text: str) -> dict | None:
        text = text.strip()
        if not text:
            return None
        # 策略1：直接解析
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        # 策略2：提取 ```json ... ``` 或 ``` ... ``` 代码块
        m = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                pass
        # 策略3：查找第一个 { 到最后一个 }
        first_brace = text.find("{")
        last_brace = text.rfind("}")
        if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
            try:
                return json.loads(text[first_brace:last_brace + 1])
            except json.JSONDecodeError:
                pass
        # 策略4：截断恢复——补全缺失的括号和引号
        if first_brace != -1:
            try:
                return _salvage_truncated_json(text[first_brace:])
            except json.JSONDecodeError:
                pass
        return None

    last_err = None
    # 第一次尝试（带 response_format，强制 JSON 输出）
    for attempt in range(1, MAX_RETRIES + 2):
        try:
            text = _call(messages, temperature, max_tokens, use_format=True)
            break
        except (APIConnectionError, APITimeoutError, RateLimitError, APIError) as e:
            wait = _backoff_delay(attempt)
            logger.warning("chat_json attempt %d 失败：%s，等待 %.0fs", attempt, e, wait)
            last_err = e
            if attempt <= MAX_RETRIES:
                time.sleep(wait)
    else:
        logger.error("chat_json 多次调用均失败：%s", last_err)
        return {}

    result = _try_parse(text)
    if result is not None:
        return result

    # 降级：response_format 偶发返回空串，退回到无约束调用
    if not text.strip():
        logger.warning("chat_json 返回空响应，降级为不带 response_format 重试")
        try:
            text = _call(messages, temperature, max_tokens, use_format=False)
            result = _try_parse(text)
            if result is not None:
                return result
            if text.strip():
                logger.warning("降级响应仍非合法 JSON（前300字符）：%s", text[:300].replace("\n", "\\n"))
        except Exception as e:
            logger.error("降级调用失败: %s", e)

    # 诊断：记录原始响应用于排查
    logger.warning("LLM 首次响应非合法 JSON（前300字符）：%s", text[:300].replace("\n", "\\n"))

    # 修复尝试
    logger.warning("LLM 返回非 JSON，尝试修复调用...")
    fix_messages = messages + [
        {"role": "assistant", "content": text},
        {
            "role": "user",
            "content": (
                "你的上一条回复不是合法的 JSON。请仅输出一个合法的 JSON 对象，"
                "不要包含任何解释或 markdown 代码块标记。"
            ),
        },
    ]
    text2 = ""
    try:
        text2 = _call(fix_messages, 0.1, max_tokens)
        result = _try_parse(text2)
        if result is not None:
            return result
    except Exception as e:
        logger.error("LLM 修复调用失败: %s", e)

    # 修复调用也无内容，再降级一次（不带 response_format）
    if not text2.strip():
        try:
            text2 = _call(fix_messages, 0.1, max_tokens, use_format=False)
            result = _try_parse(text2)
            if result is not None:
                return result
        except Exception as e:
            logger.error("降级修复调用失败: %s", e)

    logger.error("LLM 两次均未返回合法 JSON（修复响应前300字符）：%s",
                 (text2 or "修复调用未执行")[:300].replace("\n", "\\n"))
    return {}


def _salvage_truncated_json(text: str) -> dict:
    """
    截断 JSON 恢复：字符级扫描补齐缺失的 } 和 ]，处理末尾被截断的字符串值。
    """
    s = text.strip()
    if not s or s[0] != "{":
        raise json.JSONDecodeError("not a JSON object", s, 0)

    depth_brace = 0
    depth_bracket = 0
    in_string = False
    escaped = False

    for i, ch in enumerate(s):
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth_brace += 1
        elif ch == "}":
            depth_brace = max(0, depth_brace - 1)
        elif ch == "[":
            depth_bracket += 1
        elif ch == "]":
            depth_bracket = max(0, depth_bracket - 1)

    # 末尾在字符串内 → 回溯删掉不完整的字段
    if in_string:
        s = s[: s.rfind('"')]

    # 补齐末尾括号
    s = s.rstrip(", \t\n\r")
    if depth_bracket > 0:
        s += "]" * depth_bracket
    if depth_brace > 0:
        s += "}" * depth_brace

    return json.loads(s)


def build_messages(system: str, user: str) -> list[dict]:
    """构造简单的 system + user 消息列表"""
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
