"""企微 agent 会话状态（in-memory，进程内）。

每个企微 userid 一份上下文：
  current_customer_id / current_pet_id / current_visit_id  — 最近聚焦的对象
  pending_action  — 等待用户「确认」执行的写动作
  expires_at      — 30 分钟无操作清空

设计选择：
  - 内存 dict，重启丢失（MVP 够用；以后可换 SQLite/Redis）
  - 用 threading.Lock 防多进程下竞态（uvicorn 单 worker 用不到，留底）
"""
from __future__ import annotations
import threading
import time
from typing import Optional


_STORE: dict[str, dict] = {}
_LOCK = threading.Lock()
_TTL_SECONDS = 30 * 60


def _now() -> float:
    return time.time()


def get(userid: str) -> dict:
    """读会话，过期则重置返回空。"""
    if not userid:
        return {}
    with _LOCK:
        sess = _STORE.get(userid)
        if not sess:
            return {}
        if (_now() - sess.get("touched_at", 0)) > _TTL_SECONDS:
            _STORE.pop(userid, None)
            return {}
        return dict(sess)  # 返回副本


def touch(userid: str, **kv) -> dict:
    """更新会话字段，刷新 TTL。kv 中 None 值不写入（避免覆盖）。"""
    if not userid:
        return {}
    with _LOCK:
        sess = _STORE.setdefault(userid, {})
        for k, v in kv.items():
            if v is not None:
                sess[k] = v
        sess["touched_at"] = _now()
        return dict(sess)


def clear(userid: str) -> None:
    with _LOCK:
        _STORE.pop(userid, None)


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
    with _LOCK:
        sess = _STORE.get(userid)
        if not sess:
            return None
        action = sess.pop("pending_action", None)
        sess["touched_at"] = _now()
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
