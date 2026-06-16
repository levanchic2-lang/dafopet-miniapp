"""企微 agent 会话状态（SQLite 持久化，多进程共享）。

每个企微 userid 一份上下文：
  current_customer_id / current_pet_id / current_visit_id  — 最近聚焦的对象
  pending_action  — 等待用户「确认」执行的写动作
  touched_at      — 30 分钟无操作视为过期

设计说明：
  - 早期是进程内 dict；上了 uvicorn --workers 多进程后，agent 多轮「复诵→确认」
    会因为前后两条消息落到不同 worker 而丢上下文。改为存进 SQLite（同一份 DB），
    任意 worker 都能读到同一会话。表很小、读写都是单行，开销可忽略。
"""
from __future__ import annotations
import json
import time
from typing import Optional

from sqlalchemy import text

from app.database import engine

_TTL_SECONDS = 30 * 60
_ensured = False


def _now() -> float:
    return time.time()


def _ensure() -> None:
    global _ensured
    if _ensured:
        return
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS wecom_agent_sessions ("
            " userid TEXT PRIMARY KEY,"
            " data_json TEXT NOT NULL DEFAULT '{}',"
            " touched_at REAL NOT NULL DEFAULT 0)"
        ))
    _ensured = True


def _load(conn, userid: str) -> Optional[dict]:
    row = conn.execute(
        text("SELECT data_json, touched_at FROM wecom_agent_sessions WHERE userid=:u"),
        {"u": userid},
    ).fetchone()
    if not row:
        return None
    if (_now() - (row[1] or 0)) > _TTL_SECONDS:
        conn.execute(text("DELETE FROM wecom_agent_sessions WHERE userid=:u"), {"u": userid})
        return None
    try:
        return json.loads(row[0] or "{}")
    except Exception:
        return {}


def _save(conn, userid: str, sess: dict) -> None:
    sess["touched_at"] = _now()
    conn.execute(
        text(
            "INSERT INTO wecom_agent_sessions(userid, data_json, touched_at)"
            " VALUES(:u, :d, :t)"
            " ON CONFLICT(userid) DO UPDATE SET data_json=:d, touched_at=:t"
        ),
        {"u": userid, "d": json.dumps(sess, ensure_ascii=False), "t": sess["touched_at"]},
    )


def get(userid: str) -> dict:
    """读会话，过期则重置返回空。"""
    if not userid:
        return {}
    _ensure()
    with engine.begin() as conn:
        sess = _load(conn, userid)
        return dict(sess) if sess else {}


def touch(userid: str, **kv) -> dict:
    """更新会话字段，刷新 TTL。kv 中 None 值不写入（避免覆盖）。"""
    if not userid:
        return {}
    _ensure()
    with engine.begin() as conn:
        sess = _load(conn, userid) or {}
        for k, v in kv.items():
            if v is not None:
                sess[k] = v
        _save(conn, userid, sess)
        return dict(sess)


def clear(userid: str) -> None:
    if not userid:
        return
    _ensure()
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM wecom_agent_sessions WHERE userid=:u"), {"u": userid})


def set_pending(userid: str, action: str, args: dict, summary: str) -> None:
    """挂起一个待确认的写动作。"""
    touch(userid, pending_action={
        "action": action,
        "args": args,
        "summary": summary,
        "set_at": _now(),
    })


def pop_pending(userid: str) -> Optional[dict]:
    """取出待确认动作（取了就清空，确保不重复执行）。"""
    if not userid:
        return None
    _ensure()
    with engine.begin() as conn:
        sess = _load(conn, userid)
        if not sess:
            return None
        action = sess.pop("pending_action", None)
        _save(conn, userid, sess)
        return action


def is_confirm(text: str) -> bool:
    """识别用户的「确认」回复。"""
    if not text:
        return False
    t = text.strip().lower()
    return t in {"确认", "是", "对", "好", "ok", "yes", "y", "嗯", "嗯嗯", "执行", "做"}


def is_cancel(text: str) -> bool:
    """识别用户的「取消」回复。"""
    if not text:
        return False
    t = text.strip().lower()
    return t in {"取消", "不", "不要", "no", "n", "算了", "撤销"}
