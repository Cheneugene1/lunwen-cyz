"""
对话记忆模块
进程内状态 + 可选 SQLite 持久化
功能：
  - 记录对话历史（轮次截断）
  - 保存会话快照（阶段、大纲、文献池哈希、草稿版本号）
  - 超长时触发「对话摘要」子步骤（通过 LLM 压缩）
"""

import hashlib
import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Optional

from .config import get

logger = logging.getLogger(__name__)

# 每轮对话结构：{"role": "user"/"assistant"/"system", "content": "..."}
Turn = dict


class ConversationMemory:
    """
    对话记忆管理器。
    - turns：最近 N 轮对话
    - system_rule：系统规则（始终保留在上下文最前）
    - long_term_summary：过长历史的 LLM 压缩摘要
    - session_meta：会话元信息（阶段、版本号等）
    """

    def __init__(self, session_id: str, db_path: Optional[Path] = None):
        self.session_id = session_id
        self._turns: list[Turn] = []
        self.system_rule: str = (
            "你是一位专业的学术论文写作智能助手，擅长理解用户研究需求、"
            "规划论文结构、检索文献并撰写高质量学术论文。"
        )
        self.long_term_summary: str = ""
        self.session_meta: dict = {
            "phase": "INIT",
            "draft_version": 0,
            "ref_pool_hash": "",
            "outline_snapshot": "",
        }
        self._db_path = db_path
        if db_path:
            self._init_db(db_path)
            self._load_session()

    # ── SQLite 持久化 ───────────────────────────────────────────

    def _init_db(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path))
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                turns TEXT,
                long_term_summary TEXT,
                session_meta TEXT,
                updated_at REAL
            )
        """)
        conn.commit()
        conn.close()

    def _load_session(self):
        conn = sqlite3.connect(str(self._db_path))
        row = conn.execute(
            "SELECT turns, long_term_summary, session_meta FROM sessions WHERE session_id=?",
            (self.session_id,)
        ).fetchone()
        conn.close()
        if row:
            self._turns = json.loads(row[0] or "[]")
            self.long_term_summary = row[1] or ""
            self.session_meta = json.loads(row[2] or "{}")
            logger.info("会话 %s 已从数据库加载", self.session_id)

    def _save_session(self):
        if not self._db_path:
            return
        conn = sqlite3.connect(str(self._db_path))
        conn.execute(
            """INSERT OR REPLACE INTO sessions
               (session_id, turns, long_term_summary, session_meta, updated_at)
               VALUES (?, ?, ?, ?, ?)""",
            (
                self.session_id,
                json.dumps(self._turns, ensure_ascii=False),
                self.long_term_summary,
                json.dumps(self.session_meta, ensure_ascii=False),
                time.time(),
            ),
        )
        conn.commit()
        conn.close()

    # ── 对话操作 ────────────────────────────────────────────────

    def add_user(self, text: str):
        self._turns.append({"role": "user", "content": text})
        self._maybe_truncate()
        self._save_session()

    def add_assistant(self, text: str):
        self._turns.append({"role": "assistant", "content": text})
        self._maybe_truncate()
        self._save_session()

    def _maybe_truncate(self):
        """超过最大轮次时，将旧对话压缩为摘要（调用 LLM）"""
        max_turns = int(get("conversation_max_recent_turns", 20))
        if len(self._turns) <= max_turns:
            return

        # 取出要压缩的部分
        overflow = self._turns[: len(self._turns) - max_turns]
        self._turns = self._turns[-max_turns:]

        # 尝试 LLM 摘要（失败则拼接文本）
        try:
            from .llm import chat, build_messages
            history_text = "\n".join(
                f"[{t['role']}]: {t['content'][:200]}" for t in overflow
            )
            prompt = f"请用中文简要总结以下对话历史（100–200字）：\n\n{history_text}"
            summary = chat(build_messages(
                "你是对话摘要助手，请精炼总结对话内容。", prompt
            ), temperature=0.3, max_tokens=300)
        except Exception as e:
            logger.warning("对话摘要失败: %s，使用文本拼接", e)
            summary = "（历史摘要）" + " | ".join(
                t["content"][:50] for t in overflow
            )

        # 追加到 long_term_summary
        self.long_term_summary = (self.long_term_summary + "\n" + summary).strip()
        logger.info("对话摘要已更新，压缩了 %d 条历史", len(overflow))

    # ── 构建 LLM 上下文消息列表 ─────────────────────────────────

    def build_context_messages(
        self,
        extra_system: str = "",
    ) -> list[Turn]:
        """
        返回供 LLM 调用的消息列表：
        [system规则] + [长期摘要（若有）] + [最近 N 轮]
        """
        system_content = self.system_rule
        if extra_system:
            system_content += "\n\n" + extra_system
        if self.long_term_summary:
            system_content += f"\n\n## 历史对话摘要\n{self.long_term_summary}"

        messages: list[Turn] = [
            {"role": "system", "content": system_content}
        ]
        messages.extend(self._turns)
        return messages

    # ── 元信息更新 ─────────────────────────────────────────────

    def update_phase(self, phase: str):
        self.session_meta["phase"] = phase
        self._save_session()

    def update_draft_version(self, version: int):
        self.session_meta["draft_version"] = version
        self._save_session()

    def update_outline_snapshot(self, outline_md: str):
        h = hashlib.md5(outline_md.encode()).hexdigest()[:8]
        self.session_meta["outline_snapshot"] = h
        self._save_session()

    def update_ref_pool_hash(self, count: int):
        self.session_meta["ref_pool_hash"] = str(count)
        self._save_session()

    # ── 快捷读取 ───────────────────────────────────────────────

    @property
    def current_phase(self) -> str:
        return self.session_meta.get("phase", "INIT")

    def get_summary(self) -> str:
        """返回给规划模块用的对话摘要文本"""
        return self.long_term_summary or ""

    def recent_user_messages(self, n: int = 3) -> list[str]:
        """返回最近 n 条用户消息内容"""
        user_turns = [t["content"] for t in self._turns if t["role"] == "user"]
        return user_turns[-n:]
