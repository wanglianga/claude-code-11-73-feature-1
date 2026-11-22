"""交易去重器投影：按 订单来源 + 门店流水 + 业务发生时间 合并重发记录。

去重身份（txn_uid）= sha256(order_source | store | store_serial | biz_time)。
- 同一门店重复补传同一流水 → 命中同一 TxnRecord，只累加 receipt_count，金额不变 ⇒ 累计消费不变。
- 修订（supersedes）→ 更新有效金额，revision_count++，留一条 correction 到达记录。
- 删除更正（transaction_delete）→ 置 status=deleted，保留墓碑（tombstone），累计消费扣回该笔。

所有投影可由事件账重放重建（txn_uid 稳定、与事件 id 解耦）。
"""
import hashlib
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models

DEFAULT_ALLOWED_LATENESS_HOURS = 48


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat() if dt.tzinfo else dt.isoformat()


def identity_of(
    *,
    order_source: str,
    store: str,
    store_serial: str | None,
    biz_time: datetime,
    member_key: str | None = None,
    amount: float | None = None,
    category: str | None = None,
) -> str:
    """计算去重身份。有门店流水时严格按 来源|门店|流水|业务时间；无流水时退化为内容身份。"""
    if store_serial:
        raw = f"{order_source}|{store}|{store_serial}|{_iso(biz_time)}"
    else:
        raw = f"{order_source}|{store}|-|{_iso(biz_time)}|{member_key}|{amount}|{category}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _event_identity_fields(ev: models.Event) -> dict:
    p = ev.payload or {}
    return {
        "order_source": p.get("order_source") or ev.source,
        "store": p.get("store") or "UNKNOWN",
        "store_serial": p.get("store_serial") or p.get("order_no") or p.get("serial"),
        "biz_time": ev.biz_time,
        "member_key": p.get("member_key"),
        "amount": float(p.get("amount")) if p.get("amount") is not None else None,
        "category": p.get("category"),
    }


def _record_by_superseded_event(db: Session, event_id: int | None) -> models.TxnRecord | None:
    """通过被修订/被删除事件的到达记录，找回其所属 TxnRecord（保证同一逻辑交易）。"""
    if not event_id:
        return None
    receipt = db.execute(
        select(models.TxnReceipt).where(models.TxnReceipt.event_id == event_id).order_by(models.TxnReceipt.id.desc())
    ).scalars().first()
    if receipt is None:
        return None
    return db.execute(
        select(models.TxnRecord).where(models.TxnRecord.txn_uid == receipt.txn_uid)
    ).scalar_one_or_none()


def _ensure_belongs_edge(db: Session, txn_uid: str, member_key: str, event_id: int, active: bool = True) -> None:
    existing = db.execute(
        select(models.DependencyEdge).where(
            models.DependencyEdge.parent_type == "transaction",
            models.DependencyEdge.parent_key == txn_uid,
            models.DependencyEdge.child_type == "member",
            models.DependencyEdge.child_key == member_key,
            models.DependencyEdge.relation == "belongs",
        )
    ).scalar_one_or_none()
    if existing is None:
        db.add(
            models.DependencyEdge(
                parent_type="transaction",
                parent_key=txn_uid,
                child_type="member",
                child_key=member_key,
                relation="belongs",
                event_id=event_id,
                active=active,
                created_at=datetime.now(timezone.utc),
            )
        )
    else:
        existing.active = active
        existing.event_id = event_id


def project_transaction(db: Session, ev: models.Event) -> models.TxnRecord:
    """把一条 transaction 事件投影进去重表（初次 / 重复补传 / 修订）。"""
    f = _event_identity_fields(ev)
    superseded = _record_by_superseded_event(db, ev.supersedes_id)
    if superseded is not None:
        # 修订链：沿用被修订交易的身份，避免无门店流水时被拆成两笔
        uid = superseded.txn_uid
    else:
        uid = identity_of(
            order_source=f["order_source"],
            store=f["store"],
            store_serial=f["store_serial"],
            biz_time=f["biz_time"],
            member_key=f["member_key"],
            amount=f["amount"],
            category=f["category"],
        )

    rec = db.execute(select(models.TxnRecord).where(models.TxnRecord.txn_uid == uid)).scalar_one_or_none()
    amount = f["amount"] if f["amount"] is not None else 0.0

    if rec is None:
        rec = models.TxnRecord(
            txn_uid=uid,
            order_source=f["order_source"],
            store=f["store"],
            store_serial=f["store_serial"],
            biz_time=f["biz_time"],
            member_key=f["member_key"] or "UNKNOWN",
            amount=amount,
            category=f["category"],
            status="active",
            receipt_count=1,
            revision_count=0,
            first_event_id=ev.id,
            latest_event_id=ev.id,
            first_ingested_at=ev.ingested_at,
            latest_ingested_at=ev.ingested_at,
            payload=dict(ev.payload or {}),
        )
        db.add(rec)
        db.flush()
        db.add(
            models.TxnReceipt(
                txn_uid=uid,
                event_id=ev.id,
                kind="initial",
                biz_time=ev.biz_time,
                ingested_at=ev.ingested_at,
                amount=amount,
                delta_amount=amount,
                detail={"amount": amount},
            )
        )
        _ensure_belongs_edge(db, uid, rec.member_key, ev.id, active=True)
        return rec

    # 已存在：判断重复补传 / 修订
    member_key = f["member_key"] or rec.member_key
    if superseded is not None or (amount != float(rec.amount)):
        kind = "correction"
        delta = amount - float(rec.amount)
        rec.amount = amount
        rec.revision_count = (rec.revision_count or 0) + 1
        rec.status = "active"  # 修订可复活（若之前被删）
        rec.tombstone = None
    else:
        kind = "duplicate"
        delta = 0.0  # 重复补传：累计消费不变
        rec.receipt_count = (rec.receipt_count or 1) + 1

    rec.member_key = member_key
    rec.latest_event_id = ev.id
    rec.latest_ingested_at = ev.ingested_at
    rec.payload = dict(ev.payload or {})
    db.add(
        models.TxnReceipt(
            txn_uid=uid,
            event_id=ev.id,
            kind=kind,
            biz_time=ev.biz_time,
            ingested_at=ev.ingested_at,
            amount=amount,
            delta_amount=delta,
            detail={"delta": round(delta, 2), "receipt_count": rec.receipt_count},
        )
    )
    _ensure_belongs_edge(db, uid, member_key, ev.id, active=(rec.status == "active"))
    return rec


def project_delete(db: Session, ev: models.Event) -> models.TxnRecord | None:
    """删除更正：定位逻辑交易并置墓碑，不物理删除（保留可回查证据）。"""
    f = _event_identity_fields(ev)
    rec = _record_by_superseded_event(db, ev.supersedes_id)
    if rec is None:
        uid = identity_of(
            order_source=f["order_source"],
            store=f["store"],
            store_serial=f["store_serial"],
            biz_time=f["biz_time"],
            member_key=f["member_key"],
            amount=f["amount"],
            category=f["category"],
        )
        rec = db.execute(select(models.TxnRecord).where(models.TxnRecord.txn_uid == uid)).scalar_one_or_none()
    if rec is None:
        return None

    was_active = rec.status == "active"
    rec.status = "deleted"
    rec.latest_event_id = ev.id
    rec.latest_ingested_at = ev.ingested_at
    rec.tombstone = {
        "event_id": ev.id,
        "reason": (ev.payload or {}).get("reason", "删除更正"),
        "at": _iso(ev.ingested_at),
        "biz_time": _iso(ev.biz_time),
        "member_key": rec.member_key,
    }
    db.add(
        models.TxnReceipt(
            txn_uid=rec.txn_uid,
            event_id=ev.id,
            kind="delete",
            biz_time=ev.biz_time,
            ingested_at=ev.ingested_at,
            amount=float(rec.amount),
            delta_amount=-float(rec.amount) if was_active else 0.0,
            detail={"tombstone": rec.tombstone},
        )
    )
    _ensure_belongs_edge(db, rec.txn_uid, rec.member_key, ev.id, active=False)
    return rec


def receipts_for(db: Session, txn_uid: str) -> list[models.TxnReceipt]:
    return db.execute(
        select(models.TxnReceipt).where(models.TxnReceipt.txn_uid == txn_uid).order_by(models.TxnReceipt.id)
    ).scalars().all()


def record_json(db: Session, rec: models.TxnRecord, with_receipts: bool = False) -> dict:
    from .dependencies import dependency_path_for_transaction

    out = {
        "id": rec.id,
        "txn_uid": rec.txn_uid,
        "order_source": rec.order_source,
        "store": rec.store,
        "store_serial": rec.store_serial,
        "biz_time": rec.biz_time.isoformat(),
        "member_key": rec.member_key,
        "amount": float(rec.amount),
        "category": rec.category,
        "status": rec.status,
        "receipt_count": rec.receipt_count,
        "revision_count": rec.revision_count,
        "first_event_id": rec.first_event_id,
        "latest_event_id": rec.latest_event_id,
        "first_ingested_at": rec.first_ingested_at.isoformat(),
        "latest_ingested_at": rec.latest_ingested_at.isoformat(),
        "tombstone": rec.tombstone,
        "dependency_path": dependency_path_for_transaction(db, rec.txn_uid),
    }
    if with_receipts:
        out["receipts"] = [
            {
                "event_id": r.event_id,
                "kind": r.kind,
                "biz_time": r.biz_time.isoformat(),
                "ingested_at": r.ingested_at.isoformat(),
                "amount": float(r.amount) if r.amount is not None else None,
                "delta_amount": float(r.delta_amount) if r.delta_amount is not None else None,
                "detail": r.detail,
            }
            for r in receipts_for(db, txn_uid=rec.txn_uid)
        ]
    return out


# ----------------------------------------------------------------------------
# 双时间重建：给定系统时点，按到达顺序重放交易事件，得到「当时可见」的逻辑交易集合。
# 两种校正路线（增量失效 / 事件回放）都调用本函数，保证同一资格结论。
# 去重语义与 DB 投影一致：同 来源|门店|流水|业务时间 合并，重复补传金额不变。
# ----------------------------------------------------------------------------
def reconstruct_asof(events: list[models.Event], sys_as_of: datetime) -> dict[str, dict]:
    """返回 {txn_uid: {...}}，仅纳入 ingested_at <= sys_as_of 的到达。"""
    state: dict[str, dict] = {}
    ev_to_uid: dict[int, str] = {}
    ordered = sorted(
        (e for e in events if e.ingested_at <= sys_as_of and e.event_type in ("transaction", "transaction_delete")),
        key=lambda e: (e.ingested_at, e.id),
    )
    for e in ordered:
        f = _event_identity_fields(e)
        superseded_uid = ev_to_uid.get(e.supersedes_id) if e.supersedes_id else None
        if superseded_uid:
            uid = superseded_uid
        else:
            uid = identity_of(
                order_source=f["order_source"],
                store=f["store"],
                store_serial=f["store_serial"],
                biz_time=f["biz_time"],
                member_key=f["member_key"],
                amount=f["amount"],
                category=f["category"],
            )
        ev_to_uid[e.id] = uid
        amount = f["amount"] if f["amount"] is not None else 0.0

        if e.event_type == "transaction_delete":
            rec = state.get(uid)
            if rec is not None:
                rec["status"] = "deleted"
                rec["latest_event_id"] = e.id
                rec["latest_ingested_at"] = e.ingested_at
                rec["event_ids"].append(e.id)
                rec["tombstone"] = {
                    "event_id": e.id,
                    "reason": (e.payload or {}).get("reason", "删除更正"),
                    "at": e.ingested_at.isoformat(),
                }
            continue

        rec = state.get(uid)
        if rec is None:
            state[uid] = {
                "txn_uid": uid,
                "order_source": f["order_source"],
                "store": f["store"],
                "store_serial": f["store_serial"],
                "biz_time": f["biz_time"],
                "member_key": f["member_key"] or "UNKNOWN",
                "amount": amount,
                "category": f["category"],
                "status": "active",
                "receipt_count": 1,
                "revision_count": 0,
                "first_event_id": e.id,
                "latest_event_id": e.id,
                "first_ingested_at": e.ingested_at,
                "latest_ingested_at": e.ingested_at,
                "event_ids": [e.id],
                "tombstone": None,
            }
        else:
            if superseded_uid or amount != rec["amount"]:
                rec["amount"] = amount
                rec["status"] = "active"
                rec["revision_count"] += 1
                rec["tombstone"] = None
            else:
                rec["receipt_count"] += 1  # 重复补传：金额不变
            rec["latest_event_id"] = e.id
            rec["latest_ingested_at"] = e.ingested_at
            rec["event_ids"].append(e.id)
    return state
