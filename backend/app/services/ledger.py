"""接入账服务：追加式双时间事件账的写入与校验。

- 幂等键去重（唯一约束 + 查询返回已有事件）。
- 链式哈希：pg 顾问锁串行化追加，chain_hash = sha256(prev_chain_hash | source_hash | 时间 | 类型)。
- 写入后同步更新可重建投影（身份图 / 规则版本 / 名录）；投影可随时由事件全量重放重建。
"""
import hashlib
import json
from datetime import datetime, timezone

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from .. import models

ADVISORY_LOCK_KEY = 730001


def canonical_payload(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def source_hash_of(payload: dict) -> str:
    return hashlib.sha256(canonical_payload(payload).encode("utf-8")).hexdigest()


def _chain_hash(prev: str, source_hash: str, event_type: str, biz_time: datetime, ingested_at: datetime) -> str:
    raw = "|".join(
        [prev, source_hash, event_type, biz_time.isoformat(), ingested_at.isoformat()]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def ingest_event(
    db: Session,
    *,
    source: str,
    event_type: str,
    biz_time: datetime,
    payload: dict,
    actor: str | None = None,
    supersedes_id: int | None = None,
    idempotency_key: str | None = None,
    ingested_at: datetime | None = None,
) -> tuple[models.Event, bool]:
    """写入一条事件。返回 (event, created)。幂等键命中已有记录时不重复写入。"""
    ingested_at = ingested_at or datetime.now(timezone.utc)
    idem = idempotency_key or hashlib.sha256(
        f"{source}|{event_type}|{biz_time.isoformat()}|{source_hash_of(payload)}".encode()
    ).hexdigest()[:32]

    existing = db.execute(
        select(models.Event).where(models.Event.idempotency_key == idem)
    ).scalar_one_or_none()
    if existing is not None:
        return existing, False

    # 串行化追加，保证链式哈希一致（并发安全）
    db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": ADVISORY_LOCK_KEY})

    prev = db.execute(
        select(models.Event).order_by(models.Event.id.desc()).limit(1)
    ).scalar_one_or_none()
    prev_hash = prev.chain_hash if prev else "GENESIS"
    shash = source_hash_of(payload)
    chash = _chain_hash(prev_hash, shash, event_type, biz_time, ingested_at)

    ev = models.Event(
        idempotency_key=idem,
        source=source,
        event_type=event_type,
        source_hash=shash,
        chain_hash=chash,
        biz_time=biz_time,
        ingested_at=ingested_at,
        actor=actor,
        supersedes_id=supersedes_id,
        payload=payload,
    )
    db.add(ev)
    db.flush()  # 取得 id
    apply_to_projections(db, ev)
    db.commit()
    db.refresh(ev)
    return ev, True


def apply_to_projections(db: Session, ev: models.Event) -> None:
    """按事件类型更新投影表（投影 = 事件的物化缓存，可重建）。"""
    from . import identity, rules, txnstore, watermarks  # 延迟导入避免循环

    if ev.event_type == "transaction":
        key = ev.payload.get("member_key")
        if key:
            row = db.get(models.MemberDirectory, key)
            if row is None:
                db.add(
                    models.MemberDirectory(
                        member_key=key,
                        display_name=ev.payload.get("member_name"),
                        first_seen_event_id=ev.id,
                    )
                )
            elif ev.payload.get("member_name") and not row.display_name:
                row.display_name = ev.payload["member_name"]
        txnstore.project_transaction(db, ev)
        watermarks.on_transaction_ingest(db, ev)
    elif ev.event_type == "transaction_delete":
        txnstore.project_delete(db, ev)
        # 删除通知的业务时间是「通知时刻」，不代表来源业务水位，故不推进来源水位。
    elif ev.event_type == "store_daily_close":
        watermarks.on_daily_close(db, ev)
    elif ev.event_type == "source_watermark":
        watermarks.on_watermark_report(db, ev)
    elif ev.event_type in ("identity_merge", "identity_split", "identity_pending", "identity_revoke"):
        identity.apply_identity_event(db, ev)
        for k in identity.keys_of(ev):
            if db.get(models.MemberDirectory, k) is None:
                db.add(models.MemberDirectory(member_key=k, first_seen_event_id=ev.id))
    elif ev.event_type in ("rule_upsert", "rule_withdraw"):
        rules.apply_rule_event(db, ev)


def rebuild_projections(db: Session) -> dict:
    """从事件账全量重放，重建所有投影。证明投影可重建、账本是唯一事实来源。"""
    for tbl in (
        models.IdentityEdge,
        models.RuleVersion,
        models.RuleWithdrawal,
        models.MemberDirectory,
        models.TxnReceipt,
        models.TxnRecord,
        models.DependencyEdge,
    ):
        db.query(tbl).delete()
    # 重置水位（保留允许迟到配置，重建后重算边界与缺失证据）
    for w in db.query(models.SourceWatermark).all():
        w.watermark_biz_time = None
        w.watermark_ingested_at = None
        w.settled_biz_time = None
        w.last_daily_close_date = None
        w.missing_evidence = []
        w.regression = None
    db.flush()
    events = db.execute(select(models.Event).order_by(models.Event.id)).scalars().all()
    for ev in events:
        apply_to_projections(db, ev)
        db.flush()  # 让后续事件的重放能看到本事件的投影行

    # 重建完成后再补齐缺失门店日结证据（依赖交易投影）
    from . import dependencies as dep_mod
    from . import watermarks as wm_mod

    wm_mod.recalc_all(db)
    # 依赖图：事件重放只重建 transaction→member；窗口/数据集引用需按现存产物补回
    dep_mod.reindex(db)
    db.commit()
    return {
        "events_replayed": len(events),
        "identity_edges": db.query(models.IdentityEdge).count(),
        "rule_versions": db.query(models.RuleVersion).count(),
        "rule_withdrawals": db.query(models.RuleWithdrawal).count(),
        "members": db.query(models.MemberDirectory).count(),
        "txn_records": db.query(models.TxnRecord).count(),
        "txn_receipts": db.query(models.TxnReceipt).count(),
        "dependency_edges": db.query(models.DependencyEdge).count(),
    }


def verify_chain(db: Session) -> dict:
    """校验链式哈希完整性（防篡改）。"""
    events = db.execute(select(models.Event).order_by(models.Event.id)).scalars().all()
    prev = "GENESIS"
    broken_at = None
    for ev in events:
        expect = _chain_hash(prev, ev.source_hash, ev.event_type, ev.biz_time, ev.ingested_at)
        if expect != ev.chain_hash:
            broken_at = ev.id
            break
        if ev.source_hash != source_hash_of(ev.payload):
            broken_at = ev.id
            break
        prev = ev.chain_hash
    return {"ok": broken_at is None, "checked": len(events), "broken_at": broken_at}
