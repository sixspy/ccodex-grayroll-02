#!/usr/bin/env bash
# grayroll-02 端到端独验收（对真实 HTTP 服务打口，不用 TestClient）
set -u
BASE="http://127.0.0.1:8027"
pass=0; fail=0
ok(){ echo "  [PASS] $1"; pass=$((pass+1)); }
no(){ echo "  [FAIL] $1"; fail=$((fail+1)); }

login(){ curl -s -X POST "$BASE/api/login" -H 'Content-Type: application/json' \
  -d "{\"username\":\"$1\",\"password\":\"$2\"}" | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])'; }

ADMIN="Authorization: Bearer $(login admin admin123)"
WRITER="Authorization: Bearer $(login writer writer123)"
echo "登录沿用 admin/writer/viewer：admin token 与 writer token 均取得"

# 用新开关做隔离夹具
FKEY="g02.e2e.$(date +%s)"
FID=$(curl -s -X POST "$BASE/api/flags" -H "$ADMIN" -H 'Content-Type: application/json' \
  -d "{\"key\":\"$FKEY\",\"title\":\"E2E夹具\",\"owner\":\"admin\"}" | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
EID=1
SID=$(curl -s -X POST "$BASE/api/schedules" -H "$ADMIN" -H 'Content-Type: application/json' \
  -d "{\"flag_id\":$FID,\"env_id\":$EID,\"steps_json\":\"[{\\\"percent\\\":10},{\\\"percent\\\":50},{\\\"percent\\\":30}]\",\"note\":\"e2e\"}" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')

echo "== 段1 超步拒写（甲路径）=="
curl -s -X POST "$BASE/api/schedules/$SID/apply-next" -H "$ADMIN" -H 'Content-Type: application/json' -d '{}' >/dev/null  # 首步 10% 合法
RESP=$(curl -s -w '|%{http_code}' -X POST "$BASE/api/schedules/$SID/apply-next" -H "$ADMIN" -H 'Content-Type: application/json' -d '{}')
CODE="${RESP##*|}"; BODY="${RESP%|*}"
[ "$CODE" = "400" ] && ok "超步 10->50 返回 400" || no "超步应 400，实 $CODE $BODY"
echo "$BODY" | grep -q "20" && ok "错误明细含 20 个百分点封顶" || no "明细缺封顶说明: $BODY"
PCT=$(python3 -c "
import sqlite3;c=sqlite3.connect('data/app.db');c.row_factory=sqlite3.Row
r=c.execute('SELECT percent,version FROM flag_rules WHERE flag_id=? AND env_id=? AND official=1',($FID,$EID)).fetchone()
cur=c.execute('SELECT cursor_step FROM rollout_schedules WHERE id=?',($SID,)).fetchone()['cursor_step']
print(dict(r)['percent'], dict(r)['version'], cur)")
echo "    正式规则/游标现状: $PCT"
[ "$PCT" = "10 1 1" ] && ok "拒写后正式规则仍 10% v1、游标停在 1（正式零增）" || no "正式账被污染: $PCT"

echo "== 段2 合法步进后求值比例与发布版本同源 =="
# 先建一个合法计划（当前游标 1 的下一步是 50 仍超步，新建 10->30 序列）
SID2=$(curl -s -X POST "$BASE/api/schedules" -H "$ADMIN" -H 'Content-Type: application/json' \
  -d "{\"flag_id\":$FID,\"env_id\":2,\"steps_json\":\"[{\\\"percent\\\":10},{\\\"percent\\\":30}]\",\"note\":\"e2e-ok\"}" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
curl -s -X POST "$BASE/api/schedules/$SID2/apply-next" -H "$ADMIN" -H 'Content-Type: application/json' -d '{}' >/dev/null
curl -s -X POST "$BASE/api/schedules/$SID2/apply-next" -H "$ADMIN" -H 'Content-Type: application/json' -d '{}' >/dev/null
SRC=$(python3 -c "
import sqlite3;c=sqlite3.connect('data/app.db');c.row_factory=sqlite3.Row
rule=c.execute('SELECT percent,version FROM flag_rules WHERE flag_id=? AND env_id=2 AND official=1',($FID,)).fetchone()
rel=c.execute('SELECT version FROM releases WHERE flag_id=? AND env_id=2 AND official=1 ORDER BY id DESC LIMIT 1',($FID,)).fetchone()
print(dict(rule)['percent'], dict(rule)['version'], dict(rel)['version'])")
echo "    规则/发布版本: $SRC"
[ "$SRC" = "30 2 2" ] && ok "正式规则 30% v2 与发布账 v2 跨表同源" || no "同源失败: $SRC"
EV=$(curl -s -X POST "$BASE/api/evaluate" -H "$ADMIN" -H 'Content-Type: application/json' \
  -d "{\"flag_key\":\"$FKEY\",\"env_code\":\"staging\",\"subject_key\":\"u-e2e-1\"}")
echo "$EV" | python3 -c 'import sys,json;d=json.load(sys.stdin);assert d["percent"]==30 and d["version"]==2,d' \
  && ok "求值页返回 percent=30 version=2，与正式规则同源" || no "求值页漂移: $EV"

echo "== 段3 双会话争用同开关爬坡锁，败方零正式改 =="
LOCK="flag-schedule:$FID:1"
python3 -c "
import sqlite3,time;c=sqlite3.connect('data/app.db')
c.execute('INSERT INTO row_locks(lock_key,holder,expires_at) VALUES(?,?,?)',('$LOCK','admin',time.time()+20));c.commit()"
MARK_BEFORE=$(python3 -c "
import sqlite3;c=sqlite3.connect('data/app.db')
print(c.execute(\"SELECT COUNT(*) FROM contention_marks WHERE lock_key='$LOCK'\").fetchone()[0])")
RCODE=$(curl -s -o /tmp/g02_loser.json -w '%{http_code}' -X POST "$BASE/api/schedules/$SID/apply-next" \
  -H "$WRITER" -H 'Content-Type: application/json' -d '{}')
[ "$RCODE" = "409" ] && ok "争用败方返回 409" || no "败方应 409，实 $RCODE"
MARK_AFTER=$(python3 -c "
import sqlite3;c=sqlite3.connect('data/app.db')
print(c.execute(\"SELECT COUNT(*) FROM contention_marks WHERE lock_key='$LOCK'\").fetchone()[0])")
[ "$MARK_AFTER" = "$((MARK_BEFORE+1))" ] && ok "败方只留一条争用痕" || no "争用痕异常 $MARK_BEFORE->$MARK_AFTER"
STILL=$(python3 -c "
import sqlite3;c=sqlite3.connect('data/app.db');c.row_factory=sqlite3.Row
r=c.execute('SELECT percent,version FROM flag_rules WHERE flag_id=? AND env_id=1 AND official=1',($FID,)).fetchone()
print(dict(r)['percent'], dict(r)['version'])")
[ "$STILL" = "10 1" ] && ok "败方正式规则零改（仍 10% v1）" || no "败方污染正式账: $STILL"

echo "== 五页同源取数（熔断/求值/覆盖/历史/开关）=="
for p in "/api/kill" "/api/evaluate/board" "/api/overrides" "/api/history" "/api/flags"; do
  SC=$(curl -s -o /dev/null -w '%{http_code}' "$BASE$p" -H "$ADMIN")
  [ "$SC" = "200" ] && ok "GET $p 200" || no "GET $p -> $SC"
done
echo
echo "汇总: PASS=$pass FAIL=$fail"
exit $fail
