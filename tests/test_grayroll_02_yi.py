"""grayroll-02 红测乙：对比服务 —— 未升正超步草案不得显示“已对齐”假绿。

红的含义：当前 /api/compare 只对 release_shadows 算 diff，且没有顶层对齐裁决；
rule_shadows 等未升正草案不参与漂移判断，页面可把漂移态误判成已对齐（假绿）。

绿的口径：
- 存在任一 open 影子（含 rule_shadows）或任一字段差异时，aligned 必须为 False；
- diffs 必须覆盖 rule_shadows 与 release_shadows 的 kind/percent/cohort_id；
- 无正式规则但有 open 影子时同样不得假绿；
- 全部草案清空（升正/无草案）后才允许 aligned=True。
"""
from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from app import app


def _headers(client, username="admin", password="admin123"):
    r = client.post("/api/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _make_flag(client, headers):
    key = f"g02.yi.{uuid.uuid4().hex[:10]}"
    r = client.post(
        "/api/flags",
        headers=headers,
        json={"key": key, "title": "乙路假绿夹具", "description": "grayroll-02", "owner": "admin"},
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_yi_open_rule_shadow_is_drift_not_aligned():
    with TestClient(app) as client:
        headers = _headers(client)
        fid = _make_flag(client, headers)
        eid = 1

        # 先落一条正式规则 10%（甲路径同事务发布）
        pub = client.post(
            "/api/releases/publish",
            headers=headers,
            json={
                "flag_id": fid,
                "env_id": eid,
                "kind": "percent",
                "percent": 10,
                "summary": "乙路基线正式十",
                "use_shadow": False,
            },
        )
        assert pub.status_code == 200, pub.text

        # 未升正的影子绑定草案：50%，相对正式 10% 漂移 40（超步量级）
        sh = client.post(
            "/api/rules/shadow-bind",
            headers=headers,
            json={"flag_id": fid, "env_id": eid, "kind": "percent", "percent": 50, "cohort_id": None},
        )
        assert sh.status_code == 200, sh.text

        cmp = client.get(f"/api/compare/flag/{fid}/env/{eid}", headers=headers)
        assert cmp.status_code == 200, cmp.text
        body = cmp.json()

        # 红测焦点：未升正草案存在时，绝不允许假绿
        assert body["aligned"] is False, "open 影子未升正，对比页不得裁决为已对齐"
        assert body["has_pending_shadow"] is True
        # rule_shadow 的 percent 漂移必须进 diffs，禁止漏判
        fields = {d["field"]: d for d in body["diffs"]}
        assert "percent" in fields, "rule_shadow 比例漂移必须出现在 diffs"
        assert fields["percent"]["official"] == 10
        assert fields["percent"]["shadow"] == 50


def test_yi_clean_state_is_aligned_green():
    """无任何 open 影子且无差异时，才允许 aligned=True（真绿）。"""
    with TestClient(app) as client:
        headers = _headers(client)
        fid = _make_flag(client, headers)
        eid = 2
        pub = client.post(
            "/api/releases/publish",
            headers=headers,
            json={
                "flag_id": fid,
                "env_id": eid,
                "kind": "percent",
                "percent": 20,
                "summary": "乙路干净基线",
                "use_shadow": False,
            },
        )
        assert pub.status_code == 200, pub.text

        cmp = client.get(f"/api/compare/flag/{fid}/env/{eid}", headers=headers)
        assert cmp.status_code == 200, cmp.text
        body = cmp.json()
        assert body["has_pending_shadow"] is False
        assert body["diffs"] == []
        assert body["aligned"] is True, "无草案无差异才是真绿"


def test_yi_shadow_without_official_rule_not_green():
    """正式规则缺失但挂着 open 影子，也不得假绿。"""
    with TestClient(app) as client:
        headers = _headers(client)
        fid = _make_flag(client, headers)
        eid = 3
        sh = client.post(
            "/api/rules/shadow-bind",
            headers=headers,
            json={"flag_id": fid, "env_id": eid, "kind": "percent", "percent": 40, "cohort_id": None},
        )
        assert sh.status_code == 200, sh.text

        cmp = client.get(f"/api/compare/flag/{fid}/env/{eid}", headers=headers)
        assert cmp.status_code == 200, cmp.text
        body = cmp.json()
        assert body["official_rule"] is None
        assert body["aligned"] is False, "正式规则缺席 + open 草案，禁止假绿"
