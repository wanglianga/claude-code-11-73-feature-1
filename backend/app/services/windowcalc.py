"""资格窗口计算器：按交易发生日对应的规则计算「近 N 日消费」。

与冻结基线 eligibility.py 的关键区别：
- 基线按评估日(biz_t)解析一个规则版本套用到整个窗口；
- 本计算器对窗口内**每笔交易**按其业务发生日 txn.biz_time 解析当时生效的规则版本，
  再按规则分组累计 —— 因此迟到交易跨越规则生效日时，使用的是它发生日的规则，而非接收日规则。

证据来自交易去重器（txnstore.reconstruct_asof），重复补传金额不变、删除更正被墓碑剔除。
两种校正路线（增量失效 / 事件回放）都调用本计算器，故同一窗口必然得到同一结论。
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from . import identity, rules
from .eligibility import tier_for_spend, tier_rank
from .txnstore import reconstruct_asof


def window_key_of(member_key: str, window_to: datetime, window_days: int) -> str:
    iso = window_to.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if window_to.tzinfo else window_to.isoformat()
    return f"{member_key}|{iso}|{window_days}"


def _as_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def compute_window(
    db: Session,
    member_key: str,
    window_to: datetime,
    window_days: int,
    sys_as_of: datetime,
    *,
    events: list[models.Event] | None = None,
    cluster: set[str] | None = None,
) -> dict:
    """计算会员在 [window_to - window_days, window_to] 的资格与消费（按发生日规则）。"""
    window_to = _as_utc(window_to)
    sys_as_of = _as_utc(sys_as_of)
    window_from = window_to - timedelta(days=window_days)

    if events is None:
        events = db.execute(
            select(models.Event).where(models.Event.event_type.in_(("transaction", "transaction_delete")))
        ).scalars().all()
    state = reconstruct_asof(events, sys_as_of)
    if cluster is None:
        cluster = identity.cluster_at(db, member_key, window_to, sys_as_of)

    groups: dict[str, dict] = {}
    total = 0.0
    counted_uids, counted_events = [], []
    for rec in state.values():
        if rec["status"] != "active":
            continue
        if rec["member_key"] not in cluster:
            continue
        biz = _as_utc(rec["biz_time"])
        if not (window_from <= biz <= window_to):
            continue
        rv = rules.rule_for_store(db, rec["store"], biz, sys_as_of)
        if rv is None:
            continue
        if not rules.product_in_scope(rv.product_scope, rec.get("category") or ""):
            continue
        gkey = f"{rv.rule_id}@v{rv.version}"
        grp = groups.setdefault(
            gkey,
            {
                "rule_id": rv.rule_id,
                "rule_version": rv.version,
                "rule_event_id": rv.rule_event_id,
                "thresholds": rv.thresholds,
                "stores": set(),
                "spend": 0.0,
                "counted_txn_uids": [],
                "counted_event_ids": [],
            },
        )
        grp["stores"].add(rec["store"])
        grp["spend"] += float(rec["amount"])
        grp["counted_txn_uids"].append(rec["txn_uid"])
        grp["counted_event_ids"].append(rec["latest_event_id"])
        total += float(rec["amount"])
        counted_uids.append(rec["txn_uid"])
        counted_events.append(rec["latest_event_id"])

    breakdown = []
    best_tier = "NONE"
    for g in groups.values():
        g["stores"] = sorted(g["stores"])
        g["counted_txn_uids"].sort()
        g["counted_event_ids"].sort()
        g["spend"] = round(g["spend"], 2)
        g["tier"] = tier_for_spend(g["thresholds"], g["spend"])
        breakdown.append(g)
        if tier_rank(g["tier"]) > tier_rank(best_tier):
            best_tier = g["tier"]
    breakdown.sort(key=lambda x: (x["rule_id"], x["rule_version"]))

    return {
        "member_key": member_key,
        "cluster": sorted(cluster),
        "window_from": window_from.isoformat(),
        "window_to": window_to.isoformat(),
        "window_days": window_days,
        "sys_as_of": sys_as_of.isoformat(),
        "tier": best_tier,
        "spend": round(total, 2),
        "counted_txn_uids": sorted(counted_uids),
        "counted_event_ids": sorted(counted_events),
        "rule_breakdown": breakdown,
    }


def upsert_snapshot(
    db: Session,
    *,
    member_key: str,
    window_to: datetime,
    window_days: int,
    sys_as_of: datetime,
    semantics: str = "AS_KNOWN_NOW",
    result: dict | None = None,
    batch_id: int | None = None,
) -> models.WindowSnapshot:
    """计算（或写入给定结果）并物化窗口快照，同时维护依赖边。"""
    window_to = _as_utc(window_to)
    sys_as_of = _as_utc(sys_as_of)
    if result is None:
        result = compute_window(db, member_key, window_to, window_days, sys_as_of)
    key = window_key_of(member_key, window_to, window_days)
    now = datetime.now(timezone.utc)
    snap = db.execute(select(models.WindowSnapshot).where(models.WindowSnapshot.window_key == key)).scalar_one_or_none()
    window_from = window_to - timedelta(days=window_days)
    if snap is None:
        snap = models.WindowSnapshot(
            window_key=key,
            member_key=member_key,
            window_from=window_from,
            window_to=window_to,
            window_days=window_days,
            sys_as_of=sys_as_of,
            semantics=semantics,
            tier=result["tier"],
            spend=result["spend"],
            counted_event_ids=result["counted_event_ids"],
            counted_txn_uids=result["counted_txn_uids"],
            rule_breakdown=result["rule_breakdown"],
            invalidated=False,
            last_batch_id=batch_id,
            created_at=now,
            updated_at=now,
        )
        db.add(snap)
        db.flush()
    else:
        snap.window_from = window_from
        snap.sys_as_of = sys_as_of
        snap.semantics = semantics
        snap.tier = result["tier"]
        snap.spend = result["spend"]
        snap.counted_event_ids = result["counted_event_ids"]
        snap.counted_txn_uids = result["counted_txn_uids"]
        snap.rule_breakdown = result["rule_breakdown"]
        snap.invalidated = False
        snap.invalidated_by = None
        snap.last_batch_id = batch_id
        snap.updated_at = now
    db.flush()
    _sync_edges(db, snap)
    return snap


def _sync_edges(db: Session, snap: models.WindowSnapshot) -> None:
    """维护 window→member 与 window→transaction 依赖边（反向索引）。"""
    now = datetime.now(timezone.utc)
    existing = db.execute(
        select(models.DependencyEdge).where(
            models.DependencyEdge.parent_type == "window", models.DependencyEdge.parent_key == snap.window_key
        )
    ).scalars().all()
    for e in existing:
        db.delete(e)
    db.flush()
    db.add(
        models.DependencyEdge(
            parent_type="window", parent_key=snap.window_key,
            child_type="member", child_key=snap.member_key,
            relation="covers_member", window_id=snap.id, active=True, created_at=now,
        )
    )
    for uid in snap.counted_txn_uids or []:
        db.add(
            models.DependencyEdge(
                parent_type="window", parent_key=snap.window_key,
                child_type="transaction", child_key=uid,
                relation="counts", window_id=snap.id, active=True, created_at=now,
            )
        )
    db.flush()


def snapshot_json(snap: models.WindowSnapshot) -> dict:
    return {
        "id": snap.id,
        "window_key": snap.window_key,
        "member_key": snap.member_key,
        "window_from": _as_utc(snap.window_from).isoformat(),
        "window_to": _as_utc(snap.window_to).isoformat(),
        "window_days": snap.window_days,
        "sys_as_of": _as_utc(snap.sys_as_of).isoformat(),
        "semantics": snap.semantics,
        "tier": snap.tier,
        "spend": float(snap.spend),
        "counted_txn_uids": snap.counted_txn_uids or [],
        "counted_event_ids": snap.counted_event_ids or [],
        "rule_breakdown": snap.rule_breakdown or [],
        "invalidated": snap.invalidated,
        "invalidated_by": snap.invalidated_by,
        "last_batch_id": snap.last_batch_id,
        "updated_at": _as_utc(snap.updated_at).isoformat() if snap.updated_at else None,
    }


def list_snapshots(db: Session, member_key: str | None = None) -> list[dict]:
    q = select(models.WindowSnapshot).order_by(models.WindowSnapshot.window_to, models.WindowSnapshot.member_key)
    rows = db.execute(q).scalars().all()
    if member_key:
        rows = [r for r in rows if r.member_key == member_key]
    return [snapshot_json(r) for r in rows]
