"""grayroll-02 并发闸：两路会话同时对同一开关+环境打 apply-next 争用写口。

用法（仓库根目录，PYTHONPATH 指向 backend）：
    python3 scripts/schedule_step_cap_02.py

判定：
- 两个请求不得同时 2xx（禁止无锁双成）；
- 恰好一成一败，败方状态码 409 且 contention_marks 留痕；
- 正式 flag_rules 只允许被推进一次（版本/比例只变一次）。

退出码 0 = 争用口径通过；非 0 = 未通过。
"""
from __future__ import annotations

import json
import sys
import threading
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from fastapi.testclient import TestClient  # noqa: E402

from app import app  # noqa: E402
from db import connect  # noqa: E402


def _token(client: TestClient, username: str, password: str) -> dict:
    r = client.post("/api/login", json={"username": username, "password": password})
    r.raise_for_status()
    return {"Authorization": f"Bearer {r.json()['token']}"}


def main() -> int:
    barrier = threading.Barrier(2)
    results: list[dict] = []

    with TestClient(app) as client:
        admin_h = _token(client, "admin", "admin123")
        writer_h = _token(client, "writer", "writer123")

        key = f"g02.script.{uuid.uuid4().hex[:10]}"
        fid = client.post(
            "/api/flags",
            headers=admin_h,
            json={"key": key, "title": "并发闸夹具", "description": "grayroll-02", "owner": "admin"},
        ).json()["id"]
        eid = 1
        sid = client.post(
            "/api/schedules",
            headers=admin_h,
            json={
                "flag_id": fid,
                "env_id": eid,
                "steps_json": json.dumps([{"percent": 10}, {"percent": 30}]),
                "note": "grayroll-02 并发双请求",
            },
        ).json()["id"]
        lock_key = f"flag-schedule:{fid}:{eid}"

        with connect() as conn:
            before = [
                dict(r)
                for r in conn.execute(
                    "SELECT id, percent, version FROM flag_rules "
                    "WHERE flag_id=? AND env_id=? AND official=1",
                    (fid, eid),
                ).fetchall()
            ]
            marks_before = conn.execute(
                "SELECT COUNT(*) c FROM contention_marks WHERE lock_key=?", (lock_key,)
            ).fetchone()["c"]

        def fire(name: str, headers: dict) -> None:
            barrier.wait()  # 两路同时起跑
            r = client.post(f"/api/schedules/{sid}/apply-next", headers=headers, json={})
            results.append({"name": name, "status": r.status_code, "body": r.text[:200]})

        t1 = threading.Thread(target=fire, args=("admin", admin_h))
        t2 = threading.Thread(target=fire, args=("writer", writer_h))
        t1.start(); t2.start()
        t1.join(); t2.join()

        with connect() as conn:
            after = [
                dict(r)
                for r in conn.execute(
                    "SELECT id, percent, version FROM flag_rules "
                    "WHERE flag_id=? AND env_id=? AND official=1",
                    (fid, eid),
                ).fetchall()
            ]
            marks_after = conn.execute(
                "SELECT COUNT(*) c FROM contention_marks WHERE lock_key=?", (lock_key,)
            ).fetchone()["c"]
            cursor = conn.execute(
                "SELECT cursor_step FROM rollout_schedules WHERE id=?", (sid,)
            ).fetchone()["cursor_step"]
            locks_left = conn.execute(
                "SELECT COUNT(*) c FROM row_locks WHERE lock_key=?", (lock_key,)
            ).fetchone()["c"]

    print("== 并发双请求结果 ==")
    for r in results:
        print(f"  {r['name']}: HTTP {r['status']} {r['body']}")
    print(f"  争用痕增量: {marks_after - marks_before}")
    print(f"  正式规则 before={before} after={after}")
    print(f"  游标 cursor_step={cursor}，残留锁={locks_left}")

    ok = True
    statuses = sorted(r["status"] for r in results)
    if statuses != [200, 409]:
        print(f"[FAIL] 期望一成(200)一争用(409)，实际 {statuses}（无锁双成即判负）")
        ok = False
    if marks_after - marks_before != 1:
        print("[FAIL] 败方须恰好留一条争用痕")
        ok = False
    if cursor != 1:
        print("[FAIL] 游标只允许被胜方推进一次")
        ok = False
    if len(after) != 1 or after[0]["percent"] != 10:
        print("[FAIL] 正式规则只允许被推进一次到 10%")
        ok = False
    if locks_left != 0:
        print("[FAIL] 爬坡锁使用后必须释放")
        ok = False

    print("== 结果:", "PASS" if ok else "FAIL", "==")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
