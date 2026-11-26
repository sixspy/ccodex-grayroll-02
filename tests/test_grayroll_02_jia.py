"""grayroll-02 红测甲：爬坡服务写口 —— 超步整笔拒写，正式账实零增。

规程：
- 夹具先复现 search.vector_recall 式 10% -> 50% 单步跳变超 20。
- 红的含义：当前 apply-next 对超步不设防，正式 flag_rules 被改写。
- 绿的口径（甲路径封死）：HTTP 400 整笔拒写，flag_rules/releases/游标全不动。
- 合法步进（增量 <=20）同事务落正式规则与发布账，且版本跨表同源。
"""
from __future__ import annotations

import time
import uuid

from fastapi.testclient import TestClient

from app import app
from db import connect


def _headers(client, username="admin", password="admin123"):
    r = client.post("/api/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _make_flag(client, headers):
    key = f"g02.jia.{uuid.uuid4().hex[:10]}"
    r = client.post(
        "/api/flags",
        headers=headers,
        json={"key": key, "title": "甲路超步夹具", "description": "grayroll-02", "owner": "admin"},
    )
    assert r.status_code == 200, r.text
    return r.json()["id"], key


def _create_schedule(client, headers, fid, eid, steps):
    r = client.post(
        "/api/schedules",
        headers=headers,
        json={
            "flag_id": fid,
            "env_id": eid,
            "steps_json": __import__("json").dumps(steps),
            "note": "grayroll-02 超步夹具",
        },
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _snapshot(conn, fid, eid):
    rules = [
        dict(r)
        for r in conn.execute(
            "SELECT id, percent, version, official FROM flag_rules WHERE flag_id=? AND env_id=? AND official=1",
            (fid, eid),
        ).fetchall()
    ]
    rel_count = conn.execute(
        "SELECT COUNT(*) c FROM releases WHERE flag_id=? AND env_id=? AND official=1", (fid, eid)
    ).fetchone()["c"]
    sched = conn.execute(
        "SELECT cursor_step, status FROM rollout_schedules WHERE flag_id=? AND env_id=?", (fid, eid)
    ).fetchall()
    return {"rules": rules, "rel_count": rel_count, "sched": [dict(s) for s in sched]}


def test_jia_overstep_rejected_official_untouched():
    """10% -> 50%（增量 40 > 20）必须整笔拒写，正式账实零增零改。"""
    with TestClient(app) as client:
        headers = _headers(client)
        fid, key = _make_flag(client, headers)
        eid = 1
        sid = _create_schedule(client, headers, fid, eid, [{"percent": 10}, {"percent": 50}])

        # 先合法首步落到 10%，制造“当前正式比例 10%”的基线
        first = client.post(f"/api/schedules/{sid}/apply-next", headers=headers, json={})
        assert first.status_code == 200, first.text
        assert first.json()["applied_percent"] == 10

        with connect() as conn:
            before = _snapshot(conn, fid, eid)
        assert before["rules"] and before["rules"][0]["percent"] == 10

        # 红测焦点：超步（10 -> 50，增量 40）当前会被写进正式规则
        resp = client.post(f"/api/schedules/{sid}/apply-next", headers=headers, json={})

        # 甲路径：超步整笔拒写
        assert resp.status_code == 400, resp.text
        assert "20" in resp.json()["detail"]

        with connect() as conn:
            after = _snapshot(conn, fid, eid)
        # 正式规则零增零改：仍是同一行、10%、版本未升
        assert after["rules"] == before["rules"], "超步不得改写正式 flag_rules"
        assert after["rel_count"] == before["rel_count"], "超步不得补发布账"
        assert after["sched"] == before["sched"], "超步不得推进爬坡游标/状态"

        # 历史/审计不得留下“推进成功”痕
        with connect() as conn:
            bad = conn.execute(
                "SELECT COUNT(*) c FROM change_history WHERE flag_id=? AND action='schedule_step'",
                (fid,),
            ).fetchone()["c"]
        assert bad == 1, "超步拒写只允许首步那一条推进历史，不得新增"


def test_jia_boundary_twenty_allowed_and_same_source():
    """增量恰好 20 合法；合法步进后规则版本与发布账版本跨表同源。"""
    with TestClient(app) as client:
        headers = _headers(client)
        fid, key = _make_flag(client, headers)
        eid = 2
        sid = _create_schedule(client, headers, fid, eid, [{"percent": 10}, {"percent": 30}])

        first = client.post(f"/api/schedules/{sid}/apply-next", headers=headers, json={})
        assert first.status_code == 200, first.text
        second = client.post(f"/api/schedules/{sid}/apply-next", headers=headers, json={})
        # 10 -> 30 增量恰好 20，必须放行
        assert second.status_code == 200, second.text
        assert second.json()["applied_percent"] == 30

        with connect() as conn:
            rule = conn.execute(
                "SELECT percent, version FROM flag_rules WHERE flag_id=? AND env_id=? AND official=1",
                (fid, eid),
            ).fetchone()
            rel = conn.execute(
                "SELECT version FROM releases WHERE flag_id=? AND env_id=? AND official=1 "
                "ORDER BY id DESC LIMIT 1",
                (fid, eid),
            ).fetchone()
        assert dict(rule)["percent"] == 30
        assert rel is not None, "合法爬坡必须同事务写发布账"
        assert dict(rel)["version"] == dict(rule)["version"], "发布版本须与规则版本同源"

        # 求值页读到的比例/版本与正式规则同源
        ev = client.post(
            "/api/evaluate",
            headers=headers,
            json={"flag_key": key, "env_code": "staging", "subject_key": "u-jia-1"},
        )
        assert ev.status_code == 200, ev.text
        body = ev.json()
        assert body["percent"] == 30
        assert body["version"] == dict(rule)["version"]
