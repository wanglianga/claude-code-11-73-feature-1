"""依赖定位器：维护并查询 交易 / 会员 / 窗口 / 活动名单 / 预算报表 之间的引用关系。

图中节点以 type:key 标识：
- transaction:txn_uid（逻辑交易）
- member:member_key
- window:window_key（资格窗口快照）
- dataset:{id}（活动名单 / 预算报表）

边语义见 models.DependencyEdge.relation。提供：
- reverse(transaction) → 引用了该交易的所有窗口；
- references(dataset) → 数据集冻结引用的窗口；
- path(transaction → window → dataset) → 校正归因的依赖路径（含水位版本）。
"""
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models


def _edge_json(e: models.DependencyEdge) -> dict:
    return {
        "id": e.id,
        "parent": f"{e.parent_type}:{e.parent_key}",
        "child": f"{e.child_type}:{e.child_key}",
        "relation": e.relation,
        "active": e.active,
        "window_id": e.window_id,
        "event_id": e.event_id,
    }


def windows_counting_transaction(db: Session, txn_uid: str) -> list[models.WindowSnapshot]:
    """反向索引：哪些窗口快照当前计入了该交易。"""
    keys = {
        e.parent_key
        for e in db.execute(
            select(models.DependencyEdge).where(
                models.DependencyEdge.child_type == "transaction",
                models.DependencyEdge.child_key == txn_uid,
                models.DependencyEdge.relation == "counts",
                models.DependencyEdge.active.is_(True),
            )
        ).scalars().all()
    }
    if not keys:
        return []
    return list(
        db.execute(select(models.WindowSnapshot).where(models.WindowSnapshot.window_key.in_(keys))).scalars().all()
    )


def windows_of_member(db: Session, member_key: str) -> list[models.WindowSnapshot]:
    keys = {
        e.parent_key
        for e in db.execute(
            select(models.DependencyEdge).where(
                models.DependencyEdge.parent_type == "window",
                models.DependencyEdge.child_type == "member",
                models.DependencyEdge.child_key == member_key,
                models.DependencyEdge.active.is_(True),
            )
        ).scalars().all()
    }
    if not keys:
        return []
    return list(
        db.execute(select(models.WindowSnapshot).where(models.WindowSnapshot.window_key.in_(keys))).scalars().all()
    )


def mark_transaction_invalidation(db: Session, txn_uid: str, batch_id: int, event_id: int) -> list[models.WindowSnapshot]:
    """沿反向索引把计入该交易的窗口标记失效（incremental 路线）。返回受影响窗口。"""
    windows = windows_counting_transaction(db, txn_uid)
    for w in windows:
        w.invalidated = True
        w.invalidated_by = {"batch_id": batch_id, "txn_uid": txn_uid, "event_id": event_id}
    db.flush()
    return windows


def datasets_referencing_window(db: Session, window_key: str) -> list[models.Dataset]:
    ds_ids = {
        e.parent_key
        for e in db.execute(
            select(models.DependencyEdge).where(
                models.DependencyEdge.parent_type == "dataset",
                models.DependencyEdge.child_type == "window",
                models.DependencyEdge.child_key == window_key,
                models.DependencyEdge.relation == "references",
                models.DependencyEdge.active.is_(True),
            )
        ).scalars().all()
    }
    ids = [int(x) for x in ds_ids if str(x).isdigit()]
    if not ids:
        return []
    return list(db.execute(select(models.Dataset).where(models.Dataset.id.in_(ids))).scalars().all())


def datasets_of_member(db: Session, member_key: str) -> list[models.Dataset]:
    ds_ids = {
        e.parent_key
        for e in db.execute(
            select(models.DependencyEdge).where(
                models.DependencyEdge.parent_type == "dataset",
                models.DependencyEdge.child_type == "member",
                models.DependencyEdge.child_key == member_key,
                models.DependencyEdge.relation == "lists",
                models.DependencyEdge.active.is_(True),
            )
        ).scalars().all()
    }
    ids = [int(x) for x in ds_ids if str(x).isdigit()]
    if not ids:
        return []
    return list(db.execute(select(models.Dataset).where(models.Dataset.id.in_(ids))).scalars().all())


def dependency_path_for_transaction(db: Session, txn_uid: str) -> list[dict]:
    """交易 → 会员 → 窗口 → 数据集 的完整引用路径（每跳标注依赖边类型）。"""
    path: list[dict] = [{"node": f"transaction:{txn_uid}", "relation": "root"}]
    belongs = db.execute(
        select(models.DependencyEdge).where(
            models.DependencyEdge.parent_type == "transaction",
            models.DependencyEdge.parent_key == txn_uid,
            models.DependencyEdge.relation == "belongs",
            models.DependencyEdge.active.is_(True),
        )
    ).scalars().all()
    for b in belongs:
        path.append({"node": f"member:{b.child_key}", "relation": "belongs", "event_id": b.event_id})
        for w in windows_counting_transaction(db, txn_uid):
            if w.member_key != b.child_key:
                continue
            path.append(
                {
                    "node": f"window:{w.window_key}",
                    "relation": "counts",
                    "window_id": w.id,
                    "window_from": w.window_from.isoformat(),
                    "window_to": w.window_to.isoformat(),
                }
            )
            for ds in datasets_referencing_window(db, w.window_key):
                path.append(
                    {
                        "node": f"dataset:{ds.id}",
                        "relation": "references",
                        "kind": ds.kind,
                        "name": ds.name,
                        "frozen": ds.frozen,
                    }
                )
    return path


def path_for_window(db: Session, window: models.WindowSnapshot, evidence_event_ids: list[int], window_from: datetime | None = None) -> dict:
    """单个窗口的完整依赖路径：证据交易 → 会员 → 窗口 → 下游数据集。"""
    txn_edges = db.execute(
        select(models.DependencyEdge).where(
            models.DependencyEdge.parent_type == "window",
            models.DependencyEdge.parent_key == window.window_key,
            models.DependencyEdge.relation == "counts",
        )
    ).scalars().all()
    datasets = datasets_referencing_window(db, window.window_key)
    return {
        "member": window.member_key,
        "window": {
            "window_key": window.window_key,
            "window_id": window.id,
            "window_from": (window_from or window.window_from).isoformat(),
            "window_to": window.window_to.isoformat(),
            "window_days": window.window_days,
            "sys_as_of": window.sys_as_of.isoformat(),
        },
        "evidence_event_ids": sorted(evidence_event_ids),
        "counted_transactions": [
            {"txn_uid": e.child_key, "edge": "window→transaction:counts"} for e in txn_edges
        ],
        "downstream": [
            {"dataset_id": d.id, "kind": d.kind, "name": d.name, "relation": "dataset→window:references", "frozen": d.frozen}
            for d in datasets
        ],
    }


def build_path(
    db: Session,
    *,
    member_key: str,
    window: models.WindowSnapshot | None,
    window_key: str,
    window_from: datetime,
    window_to: datetime,
    window_days: int,
    evidence_event_ids: list[int],
) -> dict:
    """不依赖具体快照行的完整依赖路径（新建快照触发的校正也适用）。"""
    datasets: list[models.Dataset] = []
    counted: list[dict] = []
    if window is not None:
        for e in db.execute(
            select(models.DependencyEdge).where(
                models.DependencyEdge.parent_type == "window",
                models.DependencyEdge.parent_key == window_key,
                models.DependencyEdge.relation == "counts",
            )
        ).scalars().all():
            counted.append({"txn_uid": e.child_key, "edge": "window→transaction:counts"})
        datasets = datasets_referencing_window(db, window_key)
    datasets += [d for d in datasets_of_member(db, member_key) if d not in datasets]
    return {
        "member": member_key,
        "window": {
            "window_key": window_key,
            "window_id": window.id if window is not None else None,
            "window_from": window_from.isoformat(),
            "window_to": window_to.isoformat(),
            "window_days": window_days,
        },
        "evidence_event_ids": sorted(evidence_event_ids),
        "counted_transactions": counted,
        "downstream": [
            {"dataset_id": d.id, "kind": d.kind, "name": d.name, "relation": "dataset→window:references", "frozen": d.frozen}
            for d in datasets
        ],
    }


def register_dataset(
    db: Session,
    *,
    dataset: models.Dataset,
    window_keys: list[str],
    member_keys: list[str],
) -> None:
    """登记数据集 → 窗口 / 会员 的引用边。"""
    now = datetime.now(timezone.utc)
    for wk in window_keys:
        db.add(
            models.DependencyEdge(
                parent_type="dataset", parent_key=str(dataset.id),
                child_type="window", child_key=wk,
                relation="references", active=True, created_at=now,
            )
        )
    for mk in member_keys:
        db.add(
            models.DependencyEdge(
                parent_type="dataset", parent_key=str(dataset.id),
                child_type="member", child_key=mk,
                relation="lists", active=True, created_at=now,
            )
        )
    db.flush()


def reindex(db: Session) -> dict:
    """重建依赖边：交易→会员 由事件重放产生；再据现有窗口快照与数据集补回
    window 边（covers_member / counts）与 dataset 边（references / lists）。

    依赖边包含两类来源：可重建投影（交易 belongs）与冻结产物引用（数据集 references）。
    后者不由 events 单独推导，故重建后需按现存窗口/数据集重新登记，确保反向索引完整。
    """
    from .windowcalc import _sync_edges

    now = datetime.now(timezone.utc)
    # 清空并据交易投影重建 belongs
    db.query(models.DependencyEdge).delete()
    db.flush()
    for rec in db.execute(select(models.TxnRecord)).scalars().all():
        db.add(
            models.DependencyEdge(
                parent_type="transaction", parent_key=rec.txn_uid,
                child_type="member", child_key=rec.member_key,
                relation="belongs", event_id=rec.latest_event_id,
                active=(rec.status == "active"), created_at=now,
            )
        )
    db.flush()
    # 窗口边
    for w in db.execute(select(models.WindowSnapshot)).scalars().all():
        _sync_edges(db, w)
    # 数据集边
    for ds in db.execute(select(models.Dataset)).scalars().all():
        snaps = db.execute(select(models.WindowSnapshot).where(models.WindowSnapshot.id.in_(ds.window_ids or []))).scalars().all()
        window_keys = [s.window_key for s in snaps]
        member_keys = sorted({s.member_key for s in snaps})
        register_dataset(db, dataset=ds, window_keys=window_keys, member_keys=member_keys)
    db.flush()
    return {"dependency_edges": db.query(models.DependencyEdge).count()}


def graph_json(db: Session) -> dict:
    edges = db.execute(select(models.DependencyEdge).order_by(models.DependencyEdge.id)).scalars().all()
    return {"edges": [_edge_json(e) for e in edges], "count": len(edges)}
