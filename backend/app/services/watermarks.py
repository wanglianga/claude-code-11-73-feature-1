"""窗口水位器：每个来源的允许迟到、已结算边界与缺失证据。

- 允许迟到 allowed_lateness：来源级配置（小时）。已结算边界 settled = 水位业务时间 - 允许迟到。
- 水位 watermark_biz_time：按来源单调推进（来源上报 + 交易到达共同推进）。
- 水位倒退 regression：来源显式上报一个早于已记录水位的值时标记，禁止静默回退。
- 缺失证据 missing_evidence：门店在已结算边界之前存在交易、却缺少对应门店日结时列出。

水位倒退或门店日结缺失时，上层校正只能产出「暂定(tentative)」影响范围并说明不确定性。
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from .txnstore import DEFAULT_ALLOWED_LATENESS_HOURS


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _row(db: Session, source: str, store: str = "*") -> models.SourceWatermark | None:
    return db.execute(
        select(models.SourceWatermark).where(
            models.SourceWatermark.source == source, models.SourceWatermark.store == store
        )
    ).scalar_one_or_none()


def _get_or_create(db: Session, source: str, store: str = "*", allowed_lateness_hours: int | None = None) -> models.SourceWatermark:
    row = _row(db, source, store)
    if row is None:
        row = models.SourceWatermark(
            source=source,
            store=store,
            allowed_lateness_hours=allowed_lateness_hours or DEFAULT_ALLOWED_LATENESS_HOURS,
            missing_evidence=[],
            version=1,
            updated_at=_utcnow(),
        )
        db.add(row)
        db.flush()
    elif allowed_lateness_hours is not None:
        row.allowed_lateness_hours = allowed_lateness_hours
    return row


def configure(db: Session, source: str, allowed_lateness_hours: int) -> models.SourceWatermark:
    row = _get_or_create(db, source, "*", allowed_lateness_hours=allowed_lateness_hours)
    recalc_source(db, source)
    db.commit()
    db.refresh(row)
    return row


def _advance_watermark(db: Session, source: str, biz_time: datetime) -> None:
    row = _get_or_create(db, source, "*")
    biz_time = _as_utc(biz_time)
    if row.watermark_biz_time is None or biz_time > _as_utc(row.watermark_biz_time):
        row.watermark_biz_time = biz_time
        row.watermark_ingested_at = _utcnow()
        row.settled_biz_time = biz_time - timedelta(hours=row.allowed_lateness_hours)
        row.version = (row.version or 1) + 1
        row.updated_at = _utcnow()
    db.flush()


def on_transaction_ingest(db: Session, ev: models.Event) -> None:
    _advance_watermark(db, ev.source, ev.biz_time)


def on_daily_close(db: Session, ev: models.Event) -> None:
    """门店日结证据：记录该门店最近一次日结日期，并推进来源水位。"""
    p = ev.payload or {}
    store = p.get("store", "UNKNOWN")
    close_date = p.get("close_date")
    row = _get_or_create(db, ev.source, store)
    dt = _as_utc(ev.biz_time)
    if close_date:
        try:
            parsed = datetime.fromisoformat(str(close_date).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            dt = parsed
        except ValueError:
            pass
    if row.last_daily_close_date is None or dt > _as_utc(row.last_daily_close_date):
        row.last_daily_close_date = dt
        row.version = (row.version or 1) + 1
        row.updated_at = _utcnow()
    _advance_watermark(db, ev.source, ev.biz_time)
    db.flush()


def on_watermark_report(db: Session, ev: models.Event) -> None:
    """来源显式上报水位；若早于已记录水位则标记倒退，但不静默回退。"""
    p = ev.payload or {}
    source = p.get("source", ev.source)
    row = _get_or_create(db, source, "*")
    raw = p.get("watermark_biz_time")
    if not raw:
        return
    claimed = _as_utc(datetime.fromisoformat(str(raw).replace("Z", "+00:00")))
    if row.watermark_biz_time is not None and claimed < _as_utc(row.watermark_biz_time):
        row.regression = {
            "event_id": ev.id,
            "claimed_watermark": claimed.isoformat(),
            "held_watermark": _as_utc(row.watermark_biz_time).isoformat(),
            "at": ev.ingested_at.isoformat(),
            "reason": p.get("reason") or "来源上报水位早于已记录水位",
        }
        # 保留较高水位，不回退（避免把已结算边界错误前移）
    else:
        row.watermark_biz_time = claimed
        row.watermark_ingested_at = ev.ingested_at
        row.settled_biz_time = claimed - timedelta(hours=row.allowed_lateness_hours)
        row.regression = None
    row.version = (row.version or 1) + 1
    row.updated_at = _utcnow()
    db.flush()


def recalc_source(db: Session, source: str) -> dict:
    """重新计算某来源的已结算边界与缺失门店日结证据。"""
    head = _get_or_create(db, source, "*")
    if head.watermark_biz_time is not None:
        head.settled_biz_time = _as_utc(head.watermark_biz_time) - timedelta(hours=head.allowed_lateness_hours)

    settled = _as_utc(head.settled_biz_time) if head.settled_biz_time else None
    missing: list[dict] = []
    if settled is not None:
        # 该来源在已结算边界之前有交易的门店，必须有覆盖到边界的日结证据
        txn_stores = {
            r.store
            for r in db.execute(
                select(models.TxnRecord).where(
                    models.TxnRecord.order_source == source,
                    models.TxnRecord.biz_time <= settled,
                )
            ).scalars().all()
        }
        for store in sorted(txn_stores):
            srow = _row(db, source, store)
            last_close = _as_utc(srow.last_daily_close_date) if (srow and srow.last_daily_close_date) else None
            if last_close is None or last_close < settled:
                missing.append(
                    {
                        "store": store,
                        "required_close_by": settled.isoformat(),
                        "last_close": last_close.isoformat() if last_close else None,
                        "gap": "门店日结缺失：该门店在已结算边界前有交易但无覆盖边界的日结证据",
                    }
                )
    head.missing_evidence = missing
    head.updated_at = _utcnow()
    db.flush()
    return {
        "source": source,
        "settled_biz_time": _as_utc(head.settled_biz_time).isoformat() if head.settled_biz_time else None,
        "missing_evidence": missing,
        "regression": head.regression,
    }


def recalc_all(db: Session) -> None:
    sources = {r.source for r in db.execute(select(models.SourceWatermark)).scalars().all()}
    for source in sources:
        recalc_source(db, source)


def uncertainty_of(db: Session, sources: set[str] | None = None) -> dict:
    """汇总不确定性：水位倒退与缺失门店日结证据。

    先按当前交易与日结证据重算相关来源的已结算边界与缺失证据，避免读到陈旧的
    missing_evidence（正常写交易只推进水位，不重算缺失清单）。
    """
    targets = sources if sources is not None else {
        r.source for r in db.execute(select(models.SourceWatermark)).scalars().all()
    }
    for source in sorted(targets):
        recalc_source(db, source)
    rows = db.execute(select(models.SourceWatermark).where(models.SourceWatermark.store == "*")).scalars().all()
    if sources is not None:
        rows = [r for r in rows if r.source in sources]
    regressions = [
        {"source": r.source, **(r.regression or {})} for r in rows if r.regression
    ]
    missing = [
        {"source": r.source, **m} for r in rows for m in (r.missing_evidence or [])
    ]
    reasons = []
    if regressions:
        reasons.append("来源水位倒退：已保留较高水位，暂不回退已结算边界")
    if missing:
        reasons.append("门店日结证据缺失：相关日期后的影响范围可能不完整")
    return {
        "tentative": bool(regressions or missing),
        "regressions": regressions,
        "missing_evidence": missing,
        "reasons": reasons,
    }


def lateness_reason(db: Session, ev: models.Event, prior_events: list[models.Event]) -> dict:
    """计算某交易的迟到属性（相对其到达前的来源水位与已结算边界）。"""
    p = ev.payload or {}
    source = p.get("source") or ev.source
    row = _row(db, source, "*")
    allowed = row.allowed_lateness_hours if row else DEFAULT_ALLOWED_LATENESS_HOURS

    # 到达前的历史水位 = 更早到达（ingested_at, id 更小）的同类事件的最大业务时间
    prior_watermark = None
    for other in prior_events:
        if other.id == ev.id:
            continue
        if (other.ingested_at, other.id) >= (ev.ingested_at, ev.id):
            continue
        t = _as_utc(other.biz_time)
        if prior_watermark is None or t > prior_watermark:
            prior_watermark = t
    prior_settled = prior_watermark - timedelta(hours=allowed) if prior_watermark else None

    biz_t = _as_utc(ev.biz_time)
    lag = _as_utc(ev.ingested_at) - biz_t
    lag_hours = round(lag.total_seconds() / 3600.0, 2)
    beyond_tolerance = lag_hours > allowed
    beyond_settled = bool(prior_settled and biz_t < prior_settled)
    return {
        "source": source,
        "allowed_lateness_hours": allowed,
        "lag_hours": lag_hours,
        "beyond_tolerance": beyond_tolerance,
        "beyond_settled_boundary": beyond_settled,
        "prior_watermark": prior_watermark.isoformat() if prior_watermark else None,
        "prior_settled_boundary": prior_settled.isoformat() if prior_settled else None,
        "label": (
            "放弃已结算窗口后到达"
            if beyond_settled
            else ("超出允许迟到" if beyond_tolerance else "容忍范围内迟到")
        ),
    }


def watermark_json(row: models.SourceWatermark) -> dict:
    return {
        "source": row.source,
        "store": row.store,
        "allowed_lateness_hours": row.allowed_lateness_hours,
        "watermark_biz_time": _as_utc(row.watermark_biz_time).isoformat() if row.watermark_biz_time else None,
        "watermark_ingested_at": _as_utc(row.watermark_ingested_at).isoformat() if row.watermark_ingested_at else None,
        "settled_biz_time": _as_utc(row.settled_biz_time).isoformat() if row.settled_biz_time else None,
        "last_daily_close_date": _as_utc(row.last_daily_close_date).isoformat() if row.last_daily_close_date else None,
        "missing_evidence": row.missing_evidence or [],
        "regression": row.regression,
        "version": row.version,
        "updated_at": _as_utc(row.updated_at).isoformat() if row.updated_at else None,
    }


def list_watermarks(db: Session) -> list[dict]:
    rows = db.execute(select(models.SourceWatermark).order_by(models.SourceWatermark.source, models.SourceWatermark.store)).scalars().all()
    return [watermark_json(r) for r in rows]


def snapshot(db: Session) -> dict:
    """发布版本冻结用的水位快照。"""
    rows = db.execute(select(models.SourceWatermark).order_by(models.SourceWatermark.source, models.SourceWatermark.store)).scalars().all()
    return {f"{r.source}:{r.store}": watermark_json(r) for r in rows}
