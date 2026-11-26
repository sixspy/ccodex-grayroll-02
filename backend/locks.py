"""行锁与争用痕（嵌进发布/熔断主写路径）。"""
from __future__ import annotations

import threading
import time

from fastapi import APIRouter, Header, HTTPException

from auth import current_user
from db import connect

router = APIRouter(prefix="/api/locks", tags=["locks"])
TTL = 20.0

# grayroll-02：进程内临界区，把“判锁-占位”串行化，消除并发连接下的 TOCTOU 窗口；
# DB 行锁仍为跨连接可见的事实锁并负责落争用痕。可重入，兼容同会话嵌套占锁。
_GATE = threading.RLock()


def _contend(conn, key: str, loser: str, winner: str) -> None:
    now = time.time()
    conn.execute(
        "INSERT INTO contention_marks(lock_key, loser, winner, created_at) VALUES(?,?,?,?)",
        (key, loser, winner, now),
    )
    conn.commit()
    raise HTTPException(409, detail="发布锁争用，请稍后重试")


def acquire(conn, key: str, holder: str) -> None:
    """抢占同一把行锁。

    grayroll-02 丙路径：进程内 RLock 串行化 + DB 原子占位 + 立即提交，
    保证两路会话并发时只有一方持锁，败方走争用痕分支（409），杜绝无锁双成。
    """
    with _GATE:
        now = time.time()
        row = conn.execute("SELECT * FROM row_locks WHERE lock_key=?", (key,)).fetchone()
        if row and row["expires_at"] > now and row["holder"] != holder:
            _contend(conn, key, holder, row["holder"])
        # 清掉本键过期锁，随后单条原子语句占位：仅当无有效锁时才插入
        conn.execute("DELETE FROM row_locks WHERE lock_key=? AND expires_at<=?", (key, now))
        conn.execute(
            "INSERT INTO row_locks(lock_key, holder, expires_at) "
            "SELECT ?, ?, ? WHERE NOT EXISTS ( "
            "  SELECT 1 FROM row_locks WHERE lock_key=? AND expires_at>? "
            ")",
            (key, holder, now + TTL, key, now),
        )
        conn.commit()  # 锁行先于业务事务落库，并发连接立即可见
        got = conn.execute("SELECT holder FROM row_locks WHERE lock_key=?", (key,)).fetchone()
        if not got or got["holder"] != holder:
            winner = got["holder"] if got else "unknown"
            _contend(conn, key, holder, winner)


def release(conn, key: str, holder: str) -> None:
    with _GATE:
        conn.execute("DELETE FROM row_locks WHERE lock_key=? AND holder=?", (key, holder))


@router.get("/contention")
def list_contention(authorization: str | None = Header(default=None), limit: int = 40):
    current_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM contention_marks ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return {"items": [dict(r) for r in rows]}
