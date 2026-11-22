#!/usr/bin/env python3
"""迟到交易窗口影响与校正数据服务 —— 端到端验收。

用法: python3 scripts/verify_backfill.py [base_url]
前置: 后端已启动（默认 http://127.0.0.1:8000）。脚本会重置业务数据并灌入基础种子。

覆盖：重复补传（累计消费不变）、跨规则边界迟到（按发生日规则）、已引用预算报表受影响
（签发待复核而非替换）、删除更正（墓碑）、水位异常（倒退/缺日结 → 暂定）、
双路线一致性、以及每个资格变化可回查（原交易/窗口边界/依赖路径/发布版本）。
"""
import json
import sys
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
M5 = "13900000005"      # 孙磊（F）：种子里有 2026-02-03 S001 RX 1200
NEWMEM = "13700000099"  # 跨规则边界测试用的新会员
passed, failed = 0, 0


def call(method, path, body=None, token=None):
    req = urllib.request.Request(
        BASE + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"} | ({"Authorization": f"Bearer {token}"} if token else {}),
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{method} {path} -> {e.code}: {e.read().decode()[:300]}")


def check(name, cond, detail=""):
    global passed, failed
    passed, failed = passed + bool(cond), failed + (not cond)
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + ("" if cond else f"  -> {detail}"))


def window(member, to, days=90):
    return call("POST", "/api/windows/compute", {"member_key": member, "window_to": to, "window_days": days}, token=TOKEN)


def txn_by_serial(serial):
    rows = call("GET", f"/api/txnstore/records?member={M5}", token=TOKEN)
    return next((r for r in rows if r["store_serial"] == serial), None)


def main():
    global TOKEN
    TOKEN = call("POST", "/api/login", {"username": "admin", "password": "admin123"})["token"]
    call("POST", "/api/admin/reset", token=TOKEN)
    call("POST", "/api/seed", token=TOKEN)

    # ---------- 0. 冻结预算报表（引用 M5 的 2026-02 窗口）----------
    # 系统水位选 2026-04-01：此时规则 v2 已可见（02 月起 S001 GOLD 线 1500），
    # 但迟到交易（04-22 到达）尚未入账 —— 冻结的是「当时可见」的 SILVER 1200。
    ds = call("POST", "/api/datasets", {
        "name": "2026-02月末门店预算", "kind": "budget_report",
        "biz_to": "2026-02-28T23:59:00Z", "sys_as_of": "2026-04-01T00:00:00Z",
        "members": [M5], "min_tier": "SILVER",
    }, token=TOKEN)
    check("冻结预算报表引用 M5 窗口", len(ds["window_ids"]) == 1 and ds["frozen"], ds["window_ids"])
    pre = window(M5, "2026-02-28T23:59:00Z")
    check("迟到前 M5@02-28 = SILVER 1200（v2 GOLD 线 1500）",
          pre["tier"] == "SILVER" and pre["spend"] == 1200.0, pre)

    # ---------- 1. 迟到交易（加盟店离线补传）----------
    late = call("POST", "/api/events", {
        "source": "franchise_offline", "event_type": "transaction",
        "biz_time": "2026-02-20T10:00:00Z", "ingested_at": "2026-04-22T10:00:00Z",
        "payload": {"member_key": M5, "member_name": "孙磊", "store": "S001",
                    "store_serial": "S001-APR-1", "category": "OTC", "amount": 800,
                    "order_source": "franchise_offline", "batch": "S001-04"},
    }, token=TOKEN)["event"]
    check("迟到交易入账", late["id"] > 0, late)

    after = window(M5, "2026-02-28T23:59:00Z")
    check("迟到后 M5@02-28 = GOLD 2000", after["tier"] == "GOLD" and after["spend"] == 2000.0, after)

    # 补齐 S001/S002 门店日结证据（覆盖已结算边界），使本次校正可判定为 final 而非暂定。
    # franchise_offline 来源在种子里还有 S002 交易（seed-t4），故两店都需日结证据才能消除不确定。
    for store in ("S001", "S002"):
        call("POST", "/api/events", {
            "source": "franchise_offline", "event_type": "store_daily_close",
            "biz_time": "2026-02-28T23:59:00Z", "ingested_at": "2026-03-01T02:00:00Z",
            "payload": {"store": store, "close_date": "2026-02-28T00:00:00Z"},
        }, token=TOKEN)

    # ---------- 2. 重复补传：累计消费不变 ----------
    dup = call("POST", "/api/events", {
        "source": "franchise_offline", "event_type": "transaction",
        "biz_time": "2026-02-20T10:00:00Z", "ingested_at": "2026-04-25T10:00:00Z",
        "payload": {"member_key": M5, "member_name": "孙磊", "store": "S001",
                    "store_serial": "S001-APR-1", "category": "OTC", "amount": 800,
                    "order_source": "franchise_offline", "batch": "S001-04-REPLAY"},
    }, token=TOKEN)
    check("重复补传生成新到达事件（幂等键不同）", dup["created"] is True and dup["event"]["id"] != late["id"], dup["event"]["id"])
    rec = txn_by_serial("S001-APR-1")
    check("去重器合并为同一逻辑交易：到达次数=2", rec and rec["receipt_count"] == 2, rec and rec["receipt_count"])
    check("去重后金额不变（800，未翻倍）", rec and rec["amount"] == 800.0, rec and rec["amount"])
    after_dup = window(M5, "2026-02-28T23:59:00Z")
    check("重复补传后累计消费不变 = 2000", after_dup["spend"] == 2000.0 and len(after_dup["counted_txn_uids"]) == 2, after_dup)

    # ---------- 3. 校正（增量失效）+ 已引用报表 → 待复核 ----------
    batch = call("POST", "/api/corrections/run", {
        "event_ids": [late["id"]], "strategy": "auto",
        "backfill_peak": 20, "window_query_rate": 2, "acceptable_wait_seconds": 30,
    }, token=TOKEN)
    check("校正批次：策略=增量失效，状态=final", batch["strategy"] == "incremental" and batch["status"] == "final", (batch["strategy"], batch["status"]))
    cw = next((w for w in batch["windows"] if w["member_key"] == M5), None)
    check("受影响窗口旧新资格 SILVER 1200 → GOLD 2000",
          cw and cw["old_tier"] == "SILVER" and cw["old_spend"] == 1200.0 and cw["new_tier"] == "GOLD" and cw["new_spend"] == 2000.0, cw)
    check("校正窗口标注迟到原因（超容忍/放弃已结算）", cw and cw["late_reason"]["count"] >= 1, cw and cw["late_reason"])
    check("每个变化可回查原交易（证据事件含迟到事件 id）", cw and late["id"] in (cw["evidence_event_ids"] or []), cw and cw["evidence_event_ids"])
    dp = (cw or {}).get("dependency_path") or {}
    check("依赖路径含窗口边界", dp.get("window", {}).get("window_from") and dp.get("window", {}).get("window_to"), dp.get("window"))
    check("依赖路径含下游预算报表引用", any(d["kind"] == "budget_report" and d["dataset_id"] == ds["id"] for d in dp.get("downstream", [])), dp.get("downstream"))
    rv = batch.get("release_version") or {}
    check("发布版本固定策略与水位快照", rv.get("strategy") == "incremental" and bool(rv.get("watermark_snapshot")) and bool(rv.get("content_hash")), rv.get("id"))

    revs = [r for r in batch["reviews"] if r["dataset_id"] == ds["id"]]
    check("已引用预算报表签发待复核关系（PENDING_REVIEW）", revs and revs[0]["relation"] == "PENDING_REVIEW", revs)
    check("待复核影响类型 = 冻结引用复核", revs and revs[0]["impact"] == "FROZEN_REFERENCE_REVIEW", revs and revs[0]["impact"])
    ds_after = call("GET", f"/api/datasets/{ds['id']}", token=TOKEN)
    check("原预算报表内容未被替换（仍为冻结快照）",
          ds_after["frozen"] and ds_after["content"] == ds["content"] and ds_after["status"] == "frozen", ds_after["content"])

    # ---------- 4. 删除更正（墓碑）----------
    dele = call("POST", "/api/events", {
        "source": "franchise_offline", "event_type": "transaction_delete",
        "biz_time": "2026-02-20T10:00:00Z", "ingested_at": "2026-04-28T10:00:00Z",
        "supersedes_id": late["id"],
        "payload": {"member_key": M5, "store": "S001", "store_serial": "S001-APR-1",
                    "order_source": "franchise_offline", "reason": "门店重复流水核对后删除"},
    }, token=TOKEN)["event"]
    rec = txn_by_serial("S001-APR-1")
    check("删除更正保留墓碑（不物理删除）", rec and rec["status"] == "deleted" and rec["tombstone"] and rec["tombstone"]["event_id"] == dele["id"], rec and rec["tombstone"])
    after_del = window(M5, "2026-02-28T23:59:00Z")
    check("删除后窗口回落 SILVER 1200", after_del["tier"] == "SILVER" and after_del["spend"] == 1200.0, after_del)
    dbatch = call("POST", "/api/corrections/run", {
        "event_ids": [dele["id"]], "strategy": "incremental",
        "backfill_peak": 10, "window_query_rate": 1, "acceptable_wait_seconds": 30,
    }, token=TOKEN)
    dcw = next((w for w in dbatch["windows"] if w["member_key"] == M5), None)
    check("删除更正校正：GOLD 2000 → SILVER 1200",
          dcw and dcw["old_tier"] == "GOLD" and dcw["new_tier"] == "SILVER", dcw)
    check("删除更正亦签发待复核关系", any(r["dataset_id"] == ds["id"] for r in dbatch["reviews"]), dbatch["reviews"])

    # ---------- 5. 跨规则边界迟到：按发生日规则 ----------
    # 生意 2026-01-31（v2 生效于 02-01 之前 → v1 适用）；若误用接收日规则(v3, GOLD 1300)会判 SILVER
    cl = call("POST", "/api/events", {
        "source": "franchise_offline", "event_type": "transaction",
        "biz_time": "2026-01-31T12:00:00Z", "ingested_at": "2026-04-20T09:00:00Z",
        "payload": {"member_key": NEWMEM, "store": "S001", "store_serial": "S001-XRULE-1",
                    "category": "OTC", "amount": 1100, "order_source": "franchise_offline"},
    }, token=TOKEN)["event"]
    w = window(NEWMEM, "2026-03-31T23:59:00Z")
    versions = {g["rule_version"] for g in w["rule_breakdown"]}
    check("跨规则边界：01-31 交易按发生日规则 v1（非接收日 v3）", versions == {1}, w["rule_breakdown"])
    check("跨规则边界：1100 在 v1 下判 GOLD（若用 v3/1300 会误判 SILVER）", w["tier"] == "GOLD" and w["spend"] == 1100.0, w)

    # ---------- 6. 双路线一致性（同一固定窗口）----------
    parity = call("GET", f"/api/corrections/parity/preview?member={M5}&window_to=2026-02-28T23:59:00Z&window_days=90", token=TOKEN)
    check("增量失效与事件回放得到同一资格结论", parity["equal"] and parity["incremental"]["result"]["tier"] == parity["replay"]["result"]["tier"], parity["conclusion"])
    check("增量路线经反向索引发现该窗口", parity["incremental"]["discovered"], parity["incremental"])
    check("两条路线结论字段完全一致", parity["incremental"]["result"]["spend"] == parity["replay"]["result"]["spend"], (parity["incremental"]["result"]["spend"], parity["replay"]["result"]["spend"]))

    # ---------- 6a. 显式 replay 策略批次端到端（同窗口结论与增量一致，发布版本固定 strategy）----------
    # 复用删除事件作为触发，强制 replay 路线；不改动消费，只验证路线与版本固定
    rb2 = call("POST", "/api/corrections/run", {
        "event_ids": [dele["id"]], "strategy": "replay",
    }, token=TOKEN)
    rcw = next((w for w in rb2["windows"] if w["member_key"] == M5), None)
    check("replay 策略真实批次运行成功且事件回放量>0", rb2["strategy"] == "replay" and rb2["events_replayed"] > 0, (rb2["strategy"], rb2["events_replayed"]))
    check("replay 与增量对同一窗口结论一致（SILVER 1200）", rcw and rcw["new_tier"] == "SILVER" and rcw["new_spend"] == 1200.0, rcw and (rcw["new_tier"], rcw["new_spend"]))
    check("发布版本固定 replay 策略", rb2["release_version"]["strategy"] == "replay", rb2["release_version"]["strategy"])
    # 删除已使窗口回落到报表冻结态，replay 重算为 no-op（不再产生变更 → 不重复签发待复核）
    check("无变更的 replay 批次不重复签发待复核（幂等边界）", rcw and rcw["status"] == "unchanged" and rb2["reviews"] == [], (rcw and rcw["status"], rb2["reviews"]))

    # ---------- 6b. 无既有快照的兜底：仍呈现旧→新差异 ----------
    # 为新会员补一笔在窗口内的迟到交易，此前无任何窗口快照
    late2 = call("POST", "/api/events", {
        "source": "franchise_offline", "event_type": "transaction",
        "biz_time": "2026-02-10T10:00:00Z", "ingested_at": "2026-04-23T10:00:00Z",
        "payload": {"member_key": NEWMEM, "store": "S001", "store_serial": "S001-XRULE-2",
                    "category": "OTC", "amount": 600, "order_source": "franchise_offline"},
    }, token=TOKEN)["event"]
    fb = call("POST", "/api/corrections/run", {
        "event_ids": [late2["id"]], "strategy": "incremental",
        "backfill_peak": 5, "window_query_rate": 1, "acceptable_wait_seconds": 30,
    }, token=TOKEN)
    fcw = next((w for w in fb["windows"] if w["member_key"] == NEWMEM), None)
    # 该会员已有 03-31 窗口（v1 1100 GOLD）；迟到 02-10 600 进入后窗口/判断仍在，差异应如实反映
    check("无快照兜底仍产出校正窗口并给出旧基线", fcw is not None and "old_tier" in fcw, fcw and fcw["window_key"])

    # ---------- 7. 水位异常：倒退 + 缺日结 → 暂定 ----------
    call("POST", "/api/events", {
        "source": "franchise_offline", "event_type": "source_watermark",
        "biz_time": "2026-01-01T00:00:00Z", "ingested_at": "2026-05-01T00:00:00Z",
        "payload": {"source": "franchise_offline", "watermark_biz_time": "2026-01-01T00:00:00Z", "reason": "来源重新上报旧水位"},
    }, token=TOKEN)
    wms = call("GET", "/api/watermarks", token=TOKEN)
    fr = next((x for x in wms if x["source"] == "franchise_offline" and x["store"] == "*"), None)
    check("水位倒退被标记且不回退已结算边界", fr and fr["regression"] and fr["watermark_biz_time"] >= "2026-02-20", fr and (fr["regression"], fr["watermark_biz_time"]))
    unc = call("GET", "/api/watermarks/uncertainty", token=TOKEN)
    check("系统性不确定性被识别（水位倒退/缺日结）", unc["tentative"] and len(unc["regressions"]) >= 1, unc)
    tb = call("POST", "/api/corrections/run", {
        "event_ids": [cl["id"]], "strategy": "auto",
        "backfill_peak": 10, "window_query_rate": 1, "acceptable_wait_seconds": 30,
    }, token=TOKEN)
    check("水位异常下校正只给暂定范围", tb["status"] == "tentative", tb["status"])
    check("暂定范围说明不确定性", bool(tb["uncertainty"]["reasons"]), tb["uncertainty"]["reasons"])

    # 补齐 S001 日结 → 缺失证据消除（至少该门店从缺失清单移除）
    call("POST", "/api/events", {
        "source": "pos", "event_type": "store_daily_close",
        "biz_time": "2026-02-28T23:59:00Z", "ingested_at": "2026-03-01T01:00:00Z",
        "payload": {"store": "S001", "close_date": "2026-02-28T00:00:00Z"},
    }, token=TOKEN)
    call("POST", "/api/watermarks/recalc", token=TOKEN)
    wms2 = call("GET", "/api/watermarks", token=TOKEN)
    pos = next((x for x in wms2 if x["source"] == "pos" and x["store"] == "*"), None)
    check("补齐门店日结后该门店缺失证据消除", pos is not None and all(m["store"] != "S001" for m in pos["missing_evidence"]), pos and pos["missing_evidence"])

    # ---------- 8. 投影重建后结论一致 + 账本完整 ----------
    rb = call("POST", "/api/admin/rebuild", token=TOKEN)
    check("重建含去重器/依赖图投影", rb["txn_records"] >= 10 and rb["txn_receipts"] >= rb["txn_records"] and rb["dependency_edges"] > 0, rb)
    after_rb = window(M5, "2026-02-28T23:59:00Z")
    check("重建后 M5@02-28 结论一致 = SILVER 1200", after_rb["tier"] == "SILVER" and after_rb["spend"] == 1200.0, after_rb)
    v = call("GET", "/api/admin/ledger/verify", token=TOKEN)
    check("链式哈希完整", v["ok"], v)

    # ---------- 9. 去重器可回查到达明细 ----------
    rec = txn_by_serial("S001-APR-1")
    det = call("GET", f"/api/txnstore/records/{rec['txn_uid']}", token=TOKEN)
    kinds = [r["kind"] for r in det["receipts"]]
    check("到达明细可回查（initial/duplicate/delete）", "initial" in kinds and "duplicate" in kinds and "delete" in kinds, kinds)
    check("重复补传到达增量为 0", any(r["kind"] == "duplicate" and r["delta_amount"] == 0 for r in det["receipts"]), det["receipts"])

    print(f"\n== 迟到校正验收: {passed} 通过, {failed} 失败 ==")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    TOKEN = None
    main()
