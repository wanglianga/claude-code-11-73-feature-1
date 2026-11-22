"""迟到交易窗口影响与校正数据服务 API 路由。

挂载到 /api 下：
- 去重器：交易注册表与到达明细（重复补传不改累计消费）
- 水位器：来源允许迟到 / 已结算边界 / 缺失门店日结 / 倒退
- 窗口：按发生日规则计算的近 90 日窗口快照
- 依赖图：正向 / 反向引用与依赖路径
- 数据集：冻结的活动名单 / 预算报表
- 校正：批次、待复核关系、发布版本、路线一致性预览
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models
from .db import get_db
from .services import corrections, dependencies, txnstore, watermarks
from .services import windowcalc
from .services.corrections import _utcnow, _as_utc

router = APIRouter(prefix="/api")


def auth(db: Session = Depends(get_db), authorization: str = Header(default="")):
    """复用主应用的令牌校验（延迟导入避免与 main 循环依赖）。"""
    from .main import current_user

    return current_user(authorization=authorization, db=db)


def _parse(s: str) -> datetime:
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# 交易去重器
# ---------------------------------------------------------------------------
@router.get("/txnstore/records")
def list_txn_records(
    member: str | None = None,
    source: str | None = None,
    status: str | None = None,
    db: Session = Depends(get_db),
    user=Depends(auth),
):
    rows = db.execute(select(models.TxnRecord).order_by(models.TxnRecord.biz_time, models.TxnRecord.id)).scalars().all()
    out = []
    for r in rows:
        if member and r.member_key != member:
            continue
        if source and r.order_source != source:
            continue
        if status and r.status != status:
            continue
        out.append(txnstore.record_json(db, r))
    return out


@router.get("/txnstore/records/{txn_uid}")
def get_txn_record(txn_uid: str, db: Session = Depends(get_db), user=Depends(auth)):
    rec = db.execute(select(models.TxnRecord).where(models.TxnRecord.txn_uid == txn_uid)).scalar_one_or_none()
    if rec is None:
        raise HTTPException(404, "交易不存在")
    return txnstore.record_json(db, rec, with_receipts=True)


# ---------------------------------------------------------------------------
# 水位器
# ---------------------------------------------------------------------------
@router.get("/watermarks")
def list_watermarks(db: Session = Depends(get_db), user=Depends(auth)):
    return watermarks.list_watermarks(db)


class LatenessIn(BaseModel):
    allowed_lateness_hours: int


@router.post("/watermarks/{source}/lateness")
def set_lateness(source: str, body: LatenessIn, db: Session = Depends(get_db), user=Depends(auth)):
    row = watermarks.configure(db, source, body.allowed_lateness_hours)
    return watermarks.watermark_json(row)


@router.post("/watermarks/recalc")
def recalc_watermarks(db: Session = Depends(get_db), user=Depends(auth)):
    watermarks.recalc_all(db)
    db.commit()
    return watermarks.list_watermarks(db)


@router.get("/watermarks/uncertainty")
def get_uncertainty(db: Session = Depends(get_db), user=Depends(auth)):
    return watermarks.uncertainty_of(db)


# ---------------------------------------------------------------------------
# 窗口快照
# ---------------------------------------------------------------------------
@router.get("/windows")
def list_windows(member: str | None = None, db: Session = Depends(get_db), user=Depends(auth)):
    return windowcalc.list_snapshots(db, member_key=member)


class WindowIn(BaseModel):
    member_key: str
    window_to: str
    window_days: int = 90
    sys_as_of: str | None = None


@router.post("/windows/compute")
def compute_window(body: WindowIn, db: Session = Depends(get_db), user=Depends(auth)):
    sys_t = _parse(body.sys_as_of) if body.sys_as_of else _utcnow()
    res = windowcalc.compute_window(db, body.member_key, _parse(body.window_to), body.window_days, sys_t)
    return res


@router.post("/windows/snapshot")
def snapshot_window(body: WindowIn, db: Session = Depends(get_db), user=Depends(auth)):
    sys_t = _parse(body.sys_as_of) if body.sys_as_of else _utcnow()
    snap = windowcalc.upsert_snapshot(
        db, member_key=body.member_key, window_to=_parse(body.window_to),
        window_days=body.window_days, sys_as_of=sys_t,
    )
    db.commit()
    return windowcalc.snapshot_json(snap)


# ---------------------------------------------------------------------------
# 依赖图
# ---------------------------------------------------------------------------
@router.get("/dependencies")
def get_dependencies(db: Session = Depends(get_db), user=Depends(auth)):
    return dependencies.graph_json(db)


@router.get("/dependencies/transaction/{txn_uid}")
def transaction_dependencies(txn_uid: str, db: Session = Depends(get_db), user=Depends(auth)):
    return {
        "txn_uid": txn_uid,
        "path": dependencies.dependency_path_for_transaction(db, txn_uid),
        "windows": [windowcalc.snapshot_json(w) for w in dependencies.windows_counting_transaction(db, txn_uid)],
    }


# ---------------------------------------------------------------------------
# 数据集（冻结）
# ---------------------------------------------------------------------------
@router.get("/datasets")
def list_datasets(kind: str | None = None, db: Session = Depends(get_db), user=Depends(auth)):
    rows = db.execute(select(models.Dataset).order_by(models.Dataset.id)).scalars().all()
    if kind:
        rows = [r for r in rows if r.kind == kind]
    return [dataset_json(r) for r in rows]


@router.get("/datasets/{did}")
def get_dataset(did: int, db: Session = Depends(get_db), user=Depends(auth)):
    ds = db.get(models.Dataset, did)
    if ds is None:
        raise HTTPException(404, "数据集不存在")
    return dataset_json(ds)


def dataset_json(ds: models.Dataset) -> dict:
    return {
        "id": ds.id,
        "name": ds.name,
        "kind": ds.kind,
        "biz_to": _as_utc(ds.biz_to).isoformat(),
        "sys_as_of": _as_utc(ds.sys_as_of).isoformat(),
        "window_days": ds.window_days,
        "window_ids": ds.window_ids,
        "content": ds.content,
        "frozen": ds.frozen,
        "status": ds.status,
        "release_version_id": ds.release_version_id,
        "created_at": _as_utc(ds.created_at).isoformat(),
    }


# ---------------------------------------------------------------------------
# 校正批次 / 待复核 / 发布版本
# ---------------------------------------------------------------------------
class CorrectionIn(BaseModel):
    batch_event_id: int | None = None
    event_ids: list[int] | None = None
    strategy: str = "auto"
    backfill_peak: int = 0
    window_query_rate: float = 1.0
    acceptable_wait_seconds: float = 60.0
    peak_threshold: int = 200
    label: str | None = None


@router.post("/corrections/run", status_code=201)
def run_correction(body: CorrectionIn, db: Session = Depends(get_db), user=Depends(auth)):
    ids = body.event_ids or ([body.batch_event_id] if body.batch_event_id else [])
    if not ids:
        raise HTTPException(400, "需提供 event_ids 或 batch_event_id")
    trigger_events = [db.get(models.Event, i) for i in ids]
    if any(e is None for e in trigger_events):
        raise HTTPException(404, "触发事件不存在")
    inputs = {
        "strategy": body.strategy,
        "backfill_peak": body.backfill_peak,
        "window_query_rate": body.window_query_rate,
        "acceptable_wait_seconds": body.acceptable_wait_seconds,
        "peak_threshold": body.peak_threshold,
    }
    return corrections.run_batch(db, trigger_events=trigger_events, inputs=inputs, strategy=body.strategy, label=body.label)


@router.get("/corrections")
def list_corrections(db: Session = Depends(get_db), user=Depends(auth)):
    return corrections.list_batches(db)


@router.get("/corrections/{bid}")
def get_correction(bid: int, db: Session = Depends(get_db), user=Depends(auth)):
    batch = db.get(models.CorrectionBatch, bid)
    if batch is None:
        raise HTTPException(404, "校正批次不存在")
    return corrections.batch_json(db, batch)


@router.get("/reviews")
def list_reviews(status: str | None = None, db: Session = Depends(get_db), user=Depends(auth)):
    return corrections.list_reviews(db, status=status)


class ReviewStatusIn(BaseModel):
    status: str  # acknowledged | resolved


@router.post("/reviews/{rid}/status")
def update_review(rid: int, body: ReviewStatusIn, db: Session = Depends(get_db), user=Depends(auth)):
    r = db.get(models.DownstreamReview, rid)
    if r is None:
        raise HTTPException(404, "待复核关系不存在")
    r.status = body.status
    db.commit()
    return corrections.review_json(r)


@router.get("/releases")
def list_releases(db: Session = Depends(get_db), user=Depends(auth)):
    return corrections.list_releases(db)


@router.get("/releases/{rid}")
def get_release(rid: int, db: Session = Depends(get_db), user=Depends(auth)):
    rel = db.get(models.ReleaseVersion, rid)
    if rel is None:
        raise HTTPException(404, "发布版本不存在")
    return corrections.release_json(rel)


@router.get("/corrections/parity/preview")
def parity_preview(member: str, window_to: str, window_days: int = 90, event_ids: str | None = None, db: Session = Depends(get_db), user=Depends(auth)):
    ids = [int(x) for x in event_ids.split(",")] if event_ids else None
    return corrections.preview_parity(db, member_key=member, window_to=_parse(window_to), window_days=window_days, trigger_event_ids=ids)
