"""正式规则 vs 影子草案对比。"""
from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException

from auth import current_user
from db import connect
from validators import require_int

router = APIRouter(prefix="/api/compare", tags=["compare"])


@router.get("/flag/{flag_id}/env/{env_id}")
def compare_flag_env(flag_id: int, env_id: int, authorization: str | None = Header(default=None)):
    current_user(authorization)
    fid = require_int(flag_id, "开关", 1)
    eid = require_int(env_id, "环境", 1)
    with connect() as conn:
        fl = conn.execute("SELECT id, key, title, status FROM flags WHERE id=?", (fid,)).fetchone()
        env = conn.execute("SELECT id, code, title FROM environments WHERE id=?", (eid,)).fetchone()
        if not fl or not env:
            raise HTTPException(404, detail="开关或环境不存在")
        official = conn.execute(
            "SELECT * FROM flag_rules WHERE flag_id=? AND env_id=? AND official=1", (fid, eid)
        ).fetchone()
        rule_shadows = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM rule_shadows WHERE flag_id=? AND env_id=? AND status='open' ORDER BY id DESC",
                (fid, eid),
            ).fetchall()
        ]
        release_shadows = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM release_shadows WHERE flag_id=? AND env_id=? AND status='open' ORDER BY id DESC",
                (fid, eid),
            ).fetchall()
        ]
        kill_shadows = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM kill_shadows WHERE flag_id=? AND env_id=? AND status='open' ORDER BY id DESC",
                (fid, eid),
            ).fetchall()
        ]
        recent_releases = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM releases WHERE flag_id=? AND env_id=? AND official=1 ORDER BY id DESC LIMIT 5",
                (fid, eid),
            ).fetchall()
        ]
    # grayroll-02：未升正影子（规则/发布/熔断任一）一律判漂移，禁止把草案当已对齐的假绿。
    pending = rule_shadows or release_shadows or kill_shadows
    diffs = []
    seen_fields = set()

    def _collect(shadow: dict) -> None:
        # 无正式规则时，open 草案本身即漂移；有正式规则时逐字段比对
        for field in ("kind", "percent", "cohort_id"):
            if field in seen_fields:
                continue
            if not official or official[field] != shadow.get(field):
                diffs.append(
                    {
                        "field": field,
                        "official": official[field] if official else None,
                        "shadow": shadow.get(field),
                    }
                )
                seen_fields.add(field)

    # rule_shadows 与 release_shadows 都须参与漂移裁决（原来只比 release_shadows）
    for sh in rule_shadows:
        _collect(sh)
    for sh in release_shadows:
        _collect(sh)
    aligned = (not pending) and (not diffs)
    return {
        "flag": dict(fl),
        "env": dict(env),
        "official_rule": dict(official) if official else None,
        "open_rule_shadows": rule_shadows,
        "open_release_shadows": release_shadows,
        "open_kill_shadows": kill_shadows,
        "recent_releases": recent_releases,
        "diffs": diffs,
        "has_pending_shadow": bool(pending),
        "aligned": aligned,
    }


@router.get("/matrix")
def compare_matrix(authorization: str | None = Header(default=None)):
    current_user(authorization)
    with connect() as conn:
        flags = [dict(r) for r in conn.execute("SELECT id, key, status FROM flags ORDER BY id").fetchall()]
        envs = [dict(r) for r in conn.execute("SELECT id, code FROM environments ORDER BY risk_level").fetchall()]
        rules = {
            (r["flag_id"], r["env_id"]): dict(r)
            for r in conn.execute("SELECT * FROM flag_rules WHERE official=1").fetchall()
        }
        matrix = []
        for f in flags:
            row = {"flag_id": f["id"], "flag_key": f["key"], "status": f["status"], "cells": []}
            for e in envs:
                rule = rules.get((f["id"], e["id"]))
                row["cells"].append(
                    {
                        "env_id": e["id"],
                        "env_code": e["code"],
                        "has_rule": rule is not None,
                        "percent": rule["percent"] if rule else None,
                        "version": rule["version"] if rule else None,
                        "kind": rule["kind"] if rule else None,
                    }
                )
            matrix.append(row)
    return {"envs": envs, "matrix": matrix}
