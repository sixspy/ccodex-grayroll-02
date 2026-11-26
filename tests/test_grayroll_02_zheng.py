"""grayroll-02 红测丙：锁服务 —— 两路会话抢同一开关爬坡锁，败方零正式改。

红的含义：当前 apply-next 完全不持锁，两个会话可同时推进，无锁双成，
且 contention_marks 没有任何争用痕。

绿的口径：
- 同 flag+env 的爬坡锁 lock_key = flag-schedule:{fid}:{eid}；
- 持锁方推进成功（200），争用方拿锁即 409，仅留 contention_marks 痕；
- 败方正式 flag_rules/releases/游标零改；
- 锁在请求结束后释放，后续请求可正常获取。
"""
from __future__ import annotations

import json
import time
import uuid

from fastapi.testclient import TestClient

from app import app
from db import connect


def _headers(client, username, password):
    r = client.post("/api/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _make_flag(client, headers):
    key = f"g02.zheng.{uuid.uuid4().hex[:10]}"
    r = client.post(
        "/api/flags",
        headers=headers,
        json={"key": key, "title": "丙路争用夹具", "description": "grayroll-02", "owner": "admin"},
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_zheng_two_sessions_contend_same_schedule_lock():
    with TestClient(app) as client:
        admin_h = _headers(client, "admin", "admin123")
        writer_h = _headers(client, "writer", "writer123")
        fid = _make_flag(client, admin_h)
        eid = 1
        # 合法爬坡序列：0 -> 10 -> 30（增量均 <=20）
        r = client.post(
            "/api/schedules",
            headers=admin_h,
            json={
                "flag_id": fid,
                "env_id": eid,
                "steps_json": json.dumps([{"percent": 10}, {"percent": 30}]),
                "note": "grayroll-02 争用夹具",
            },
        )
        assert r.status_code == 200, r.text
        sid = r.json()["id"]
        lock_key = f"flag-schedule:{fid}:{eid}"

        with connect() as conn:
            marks_before = conn.execute(
                "SELECT COUNT(*) c FROM contention_marks WHERE lock_key=?", (lock_key,)
            ).fetchone()["c"]
            rules_before = [
                dict(x)
                for x in conn.execute(
                    "SELECT id, percent, version FROM flag_rules WHERE flag_id=? AND env_id=? AND official=1",
                    (fid, eid),
                ).fetchall()
            ]

        # 会话 A（admin）先持锁不释放，模拟两路同时抢修
        now = time.time()
        with connect() as conn:
            conn.execute(
                "INSERT INTO row_locks(lock_key, holder, expires_at) VALUES(?,?,?)",
                (lock_key, "admin", now + 20.0),
            )
            conn.commit()

        # 会话 B（writer）抢同一把锁：必须 409，而不是无锁双成
        resp_b = client.post(f"/api/schedules/{sid}/apply-next", headers=writer_h, json={})
        assert resp_b.status_code == 409, resp_b.text

        with connect() as conn:
            marks_after = conn.execute(
                "SELECT COUNT(*) c FROM contention_marks WHERE lock_key=?", (lock_key,)
            ).fetchone()["c"]
            rules_after = [
                dict(x)
                for x in conn.execute(
                    "SELECT id, percent, version FROM flag_rules WHERE flag_id=? AND env_id=? AND official=1",
                    (fid, eid),
                ).fetchall()
            ]
            sched = dict(
                conn.execute(
                    "SELECT cursor_step, status FROM rollout_schedules WHERE id=?", (sid,)
                ).fetchone()
            )
            mark = conn.execute(
                "SELECT loser, winner FROM contention_marks WHERE lock_key=? ORDER BY id DESC LIMIT 1",
                (lock_key,),
            ).fetchone()

        # 争用痕：败方 writer，胜方 admin
        assert marks_after == marks_before + 1, "败方必须只留争用痕"
        assert dict(mark)["loser"] == "writer"
        assert dict(mark)["winner"] == "admin"
        # 败方零正式改
        assert rules_after == rules_before, "争用败方不得改正式规则"
        assert sched["cursor_step"] == 0, "争用败方不得推进游标"

        # A 释放锁后，B 可正常推进，证明锁是同一把且可回收
        with connect() as conn:
            conn.execute("DELETE FROM row_locks WHERE lock_key=? AND holder='admin'", (lock_key,))
            conn.commit()
        ok = client.post(f"/api/schedules/{sid}/apply-next", headers=writer_h, json={})
        assert ok.status_code == 200, ok.text
        assert ok.json()["applied_percent"] == 10

        with connect() as conn:
            leftover = conn.execute(
                "SELECT COUNT(*) c FROM row_locks WHERE lock_key=?", (lock_key,)
            ).fetchone()["c"]
        assert leftover == 0, "请求结束必须释放爬坡锁"
