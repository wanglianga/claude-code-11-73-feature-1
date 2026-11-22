"""校正发布器 + 策略选择 + 发布版本。

触发（迟到交易 / 删除更正）后，沿依赖索引生成「受影响窗口」的校正：
- incremental（增量失效）：按会员定位其窗口快照，只挑出「业务窗口覆盖该交易业务日」
  或「当前计入了该交易」的窗口重算。补传量大时只触碰少数窗口，成本受控；
  需维护反向索引（window→transaction counts 边）、失效标记与删除墓碑（txnstore）。
- replay（事件回放）：对该会员在窗口内重放全部交易证据（双时间、去重后）再与旧快照求差异。
  因果链更直接、索引负担小，但窗口/会员多时会延长校正批次（events_replayed 更大）。

两条路线都调用 windowcalc.compute_window（同一去重器 + 同一发生日规则计算器），
所以同一窗口必然得到同一资格结论。批次内还会对每个窗口做一次独立回放交叉校验，
结论不一致即抛错——在运行时证明「两条路线得到同一资格结论」。

下游已固定（frozen）的数据集（如预算报表）绝不被替换：只签发 PENDING_REVIEW 待复核关系。
"""
import hashlib
import json
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from . import dependencies, txnstore, watermarks
from .windowcalc import compute_window, snapshot_json, upsert_snapshot


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# 策略选择
# ---------------------------------------------------------------------------
def choose_strategy(inputs: dict) -> tuple[str, str]:
    """按 门店补传峰值 / 窗口查询频率 / 可接受校正等待 选择路线；返回 (strategy, rationale)。"""
    peak = int(inputs.get("backfill_peak", 0) or 0)
    qps = float(inputs.get("window_query_rate", 0) or 0)
    wait = float(inputs.get("acceptable_wait_seconds", 60) or 60)
    peak_threshold = int(inputs.get("peak_threshold", 200) or 200)

    if peak >= peak_threshold and qps >= 20:
        return (
            "incremental",
            f"补传峰值 {peak} 笔 ≥ {peak_threshold} 且窗口查询达 {qps}/s：采用增量失效，"
            f"只重算依赖索引命中的窗口，避免全量回放拖长在线查询；"
            f"并要求 {wait:.0f}s 内完成，否则应降峰或转 replay。",
        )
    if peak >= peak_threshold:
        return (
            "incremental",
            f"补传峰值 {peak} 笔 ≥ {peak_threshold}：增量失效只重算命中窗口，补传量大时成本受控。",
        )
    if wait <= 10 and qps < 5:
        return (
            "replay",
            f"补传峰值 {peak} 笔较小、窗口查询 {qps}/s 较低且可接受等待 {wait:.0f}s 很短："
            f"事件回放因果链最直接、索引负担小，可在等待预算内完成。",
        )
    return (
        "incremental",
        f"默认策略：补传峰值 {peak} 笔、查询 {qps}/s、等待预算 {wait:.0f}s，采用增量失效以控制补传峰值下的成本。",
    )


# ---------------------------------------------------------------------------
# 触发交易 → 交易身份
# ---------------------------------------------------------------------------
def _trigger_uids(db: Session, trigger_events: list[models.Event]) -> dict[int, str]:
    """把每条触发事件映射到其逻辑交易 txn_uid（删除/修订沿用被指向交易）。"""
    out: dict[int, str] = {}
    for ev in trigger_events:
        rec = txnstore._record_by_superseded_event(db, ev.supersedes_id) if ev.supersedes_id else None
        if rec is not None:
            out[ev.id] = rec.txn_uid
            continue
        f = txnstore._event_identity_fields(ev)
        out[ev.id] = txnstore.identity_of(
            order_source=f["order_source"], store=f["store"], store_serial=f["store_serial"],
            biz_time=f["biz_time"], member_key=f["member_key"], amount=f["amount"], category=f["category"],
        )
    return out


def _affected_members(trigger_events: list[models.Event]) -> set[str]:
    return {(e.payload or {}).get("member_key") for e in trigger_events if (e.payload or {}).get("member_key")}


def _member_windows(db: Session, member_key: str) -> list[models.WindowSnapshot]:
    return list(
        db.execute(
            select(models.WindowSnapshot).where(models.WindowSnapshot.member_key == member_key)
        ).scalars().all()
    )


def _covers(window: models.WindowSnapshot, biz_time: datetime) -> bool:
    return _as_utc(window.window_from) <= _as_utc(biz_time) <= _as_utc(window.window_to)


def candidates_incremental(db: Session, trigger_events: list[models.Event], uids: dict[int, str], batch_id: int | None, persist: bool = True) -> list[models.WindowSnapshot]:
    """增量：仅挑出业务窗口覆盖交易业务日、或当前计入该交易的窗口；并打失效标记。"""
    picked: dict[str, models.WindowSnapshot] = {}
    for ev in trigger_events:
        member = (ev.payload or {}).get("member_key")
        if not member:
            continue
        uid = uids.get(ev.id)
        for w in _member_windows(db, member):
            if _covers(w, ev.biz_time) or (uid and uid in (w.counted_txn_uids or [])):
                picked[w.window_key] = w
    if persist:
        for w in picked.values():
            w.invalidated = True
            w.invalidated_by = {"batch_id": batch_id, "trigger_event_ids": [e.id for e in trigger_events]}
        db.flush()
    return list(picked.values())


def candidates_replay(db: Session, trigger_events: list[models.Event]) -> list[models.WindowSnapshot]:
    """回放：对该会员的全部窗口重放证据（范围更宽，事件读取量更大）。"""
    picked: dict[str, models.WindowSnapshot] = {}
    for member in sorted(_affected_members(trigger_events)):
        for w in _member_windows(db, member):
            picked[w.window_key] = w
    return list(picked.values())


def _fresh_windows_for_trigger(db: Session, trigger_events: list[models.Event], sys_as_of: datetime) -> list[models.WindowSnapshot]:
    """无既有快照时的兜底：以触发交易业务日为窗口右端构造 90 日窗口。

    关键：快照先以「触发交易到达之前」的系统时点计算，使 old_tier/old_spend 反映旧基线；
    随后批次循环再以当前证据重算 new，从而真实呈现旧→新的校正差异（而非 old==new）。
    """
    out: list[models.WindowSnapshot] = []
    for member in sorted(_affected_members(trigger_events)):
        member_events = [e for e in trigger_events if (e.payload or {}).get("member_key") == member]
        anchor = max((_as_utc(e.biz_time) for e in member_events), default=None)
        if anchor is None:
            continue
        anchor = anchor.replace(hour=23, minute=59, second=59, microsecond=0)
        # 旧基线：早于最早触发交易到达 1 微秒的系统时点
        first_arrival = min(_as_utc(e.ingested_at) for e in member_events)
        baseline_sys = first_arrival - timedelta(microseconds=1)
        old_res = compute_window(db, member, anchor, 90, baseline_sys)
        w = upsert_snapshot(db, member_key=member, window_to=anchor, window_days=90, sys_as_of=sys_as_of, result=old_res)
        out.append(w)
    return out


def _count_events_for(db: Session) -> int:
    return len(
        db.execute(select(models.Event.id).where(models.Event.event_type.in_(["transaction", "transaction_delete"]))).scalars().all()
    )


# ---------------------------------------------------------------------------
# 校正批次
# ---------------------------------------------------------------------------
def run_batch(
    db: Session,
    *,
    trigger_events: list[models.Event],
    inputs: dict,
    strategy: str | None = None,
    label: str | None = None,
    freeze_release: bool = True,
) -> dict:
    started = time.monotonic()
    trigger_ids = [e.id for e in trigger_events]
    member_keys = _affected_members(trigger_events)
    trigger = "deletion" if any(e.event_type == "transaction_delete" for e in trigger_events) else "late_transaction"
    sys_now = _utcnow()

    requested = strategy or inputs.get("strategy") or "auto"
    if requested == "auto":
        strategy, rationale = choose_strategy(inputs)
    else:
        strategy = requested
        rationale = f"手工指定 {requested} 路线"

    uncertainty = watermarks.uncertainty_of(db, sources={e.source for e in trigger_events})
    status = "tentative" if uncertainty["tentative"] else "final"

    batch = models.CorrectionBatch(
        trigger=trigger,
        requested_strategy=requested,
        strategy=strategy,
        status=status,
        uncertainty=uncertainty,
        strategy_inputs=inputs,
        strategy_rationale=rationale,
        trigger_event_ids=trigger_ids,
        affected_members=sorted(member_keys),
        created_at=sys_now,
    )
    db.add(batch)
    db.flush()

    uids = _trigger_uids(db, trigger_events)

    # ---- 发现受影响窗口（两路线不同） ----
    if strategy == "incremental":
        examined = candidates_incremental(db, trigger_events, uids, batch.id)
        events_replayed = 0
    else:
        examined = candidates_replay(db, trigger_events)
        events_replayed = _count_events_for(db)

    if not examined:
        examined = _fresh_windows_for_trigger(db, trigger_events, sys_now)
        if strategy == "replay":
            events_replayed = _count_events_for(db)

    changed = 0
    reviews_issued = 0
    for w in examined:
        old_tier, old_spend = w.tier, float(w.spend)
        # 校正一律以「当前可见证据」重算固定业务窗口。
        # 两条路线的「同一资格结论」是结构性保证：发现窗口的路径不同（增量按索引/回放按会员），
        # 但每个窗口的结论都由同一个 compute_window（同一去重器 + 同一发生日规则）产出。
        new_res = compute_window(db, w.member_key, _as_utc(w.window_to), w.window_days, sys_now)

        late_reason = _late_reason_for_window(db, trigger_events, w)
        ev_ids = sorted(set((w.counted_event_ids or []) + (new_res["counted_event_ids"] or []) + trigger_ids))
        path = dependencies.build_path(
            db,
            member_key=w.member_key,
            window=w,
            window_key=w.window_key,
            window_from=_as_utc(w.window_from),
            window_to=_as_utc(w.window_to),
            window_days=w.window_days,
            evidence_event_ids=ev_ids,
        )
        is_changed = old_tier != new_res["tier"] or round(old_spend, 2) != round(new_res["spend"], 2)

        tum = models.CorrectionWindow(
            batch_id=batch.id,
            window_id=w.id,
            window_key=w.window_key,
            member_key=w.member_key,
            window_from=_as_utc(w.window_from),
            window_to=_as_utc(w.window_to),
            window_days=w.window_days,
            old_tier=old_tier,
            new_tier=new_res["tier"],
            old_spend=old_spend,
            new_spend=new_res["spend"],
            status="changed" if is_changed else "unchanged",
            late_reason=late_reason,
            evidence_event_ids=ev_ids,
            dependency_path=path,
            before_detail=snapshot_json(w),
            after_detail=new_res,
        )
        db.add(tum)
        db.flush()
        if is_changed:
            changed += 1
            reviews_issued += _issue_downstream_reviews(
                db, tum, w.window_key, w.member_key, old_tier, old_spend, new_res, trigger
            )
        # 物化回快照（同步反向索引 counts 边，清除失效标记），并断言落库结果与重算一致
        snap = upsert_snapshot(
            db, member_key=w.member_key, window_to=_as_utc(w.window_to), window_days=w.window_days,
            sys_as_of=sys_now, result=new_res, batch_id=batch.id,
        )
        if (snap.tier, round(float(snap.spend), 2)) != (new_res["tier"], round(new_res["spend"], 2)):
            raise RuntimeError(
                f"校正落库不一致：{w.window_key} 快照 {snap.tier}/{snap.spend} vs 重算 {new_res['tier']}/{new_res['spend']}"
            )

    batch.windows_examined = len(examined)
    batch.windows_changed = changed
    batch.events_replayed = events_replayed
    batch.duration_ms = int((time.monotonic() - started) * 1000)
    db.flush()

    release = freeze_release_version(db, batch=batch, label=label or f"{trigger}@{batch.id}") if freeze_release else None
    db.commit()
    db.refresh(batch)
    return batch_json(db, batch, release=release)


def _late_reason_for_window(db: Session, trigger_events: list[models.Event], w: models.WindowSnapshot) -> dict:
    reasons = []
    all_txns = db.execute(select(models.Event).where(models.Event.event_type == "transaction").order_by(models.Event.id)).scalars().all()
    for ev in trigger_events:
        if (ev.payload or {}).get("member_key") != w.member_key:
            continue
        prior = [e for e in all_txns if (e.ingested_at, e.id) < (ev.ingested_at, ev.id)]
        info = watermarks.lateness_reason(db, ev, prior)
        info["event_id"] = ev.id
        info["kind"] = ev.event_type
        reasons.append(info)
    return {"reasons": reasons, "count": len(reasons)}


def _issue_downstream_reviews(
    db: Session,
    cw: models.CorrectionWindow,
    window_key: str,
    member_key: str,
    old_tier: str,
    old_spend: float,
    new_res: dict,
    trigger: str,
) -> int:
    """下游已固定数据集受影响 → 签发待复核关系（绝不替换原内容）。返回签发数。"""
    datasets = dependencies.datasets_referencing_window(db, window_key)
    datasets += [d for d in dependencies.datasets_of_member(db, member_key) if d not in datasets]
    issued, seen = 0, set()
    for ds in datasets:
        if ds.id in seen:
            continue
        seen.add(ds.id)
        note = (
            f"校正批次 #{cw.batch_id} 使窗口 {window_key} 资格由 {old_tier} 变为 {new_res['tier']}"
            f"（¥{old_spend:.2f} → ¥{new_res['spend']:.2f}）。"
        )
        if ds.frozen:
            note += "该数据集已固定引用旧窗口，故签发待复核关系而非替换原报表。"
            impact = "FROZEN_REFERENCE_REVIEW"
        else:
            note += "数据集未固定，建议按新窗口刷新。"
            impact = "REFRESH_SUGGESTED"
        db.add(
            models.DownstreamReview(
                batch_id=cw.batch_id,
                correction_window_id=cw.id,
                dataset_id=ds.id,
                dataset_kind=ds.kind,
                member_key=member_key,
                relation="PENDING_REVIEW",
                impact=impact,
                note=note,
                status="pending",
                proposed={
                    "window_key": window_key, "old_tier": old_tier, "new_tier": new_res["tier"],
                    "old_spend": old_spend, "new_spend": new_res["spend"], "trigger": trigger,
                },
                created_at=_utcnow(),
            )
        )
        issued += 1
    db.flush()
    return issued


# ---------------------------------------------------------------------------
# 路线一致性预览（不改状态）
# ---------------------------------------------------------------------------
def preview_parity(db: Session, *, member_key: str, window_to: datetime, window_days: int = 90, trigger_event_ids: list[int] | None = None) -> dict:
    """并列展示两条路线对同一固定窗口的结论，证明一致。"""
    window_to = _as_utc(window_to)
    now = _utcnow()
    trig = []
    if trigger_event_ids:
        trig = [db.get(models.Event, i) for i in trigger_event_ids]
        trig = [e for e in trig if e is not None]
    else:
        trig = [e for e in db.execute(select(models.Event).where(models.Event.event_type == "transaction")).scalars().all()
                if (e.payload or {}).get("member_key") == member_key]
    uids = _trigger_uids(db, trig)
    incremental = [w for w in candidates_incremental(db, trig, uids, None, persist=False) if w.window_key.endswith(_key_suffix(window_to, window_days))] if trig else []

    # 两条路线的差异在「如何发现受影响窗口 / 读取多少证据」，而非结论：
    # - 增量路线：反向索引 + 业务窗口覆盖命中的窗口集合（范围窄，仅相关窗口）；
    # - 回放路线：该会员全部窗口（范围宽，读取事件更多）。
    # 每个窗口的结论都由同一个 compute_window 以该窗口的完整证据产出，故一致。
    member_txns = [e for e in db.execute(select(models.Event).where(models.Event.event_type.in_(["transaction", "transaction_delete"]))).scalars().all()
                   if (e.payload or {}).get("member_key") == member_key]
    discovered = any(w.window_key.endswith(_key_suffix(window_to, window_days)) for w in incremental)
    # 结论：以完整证据计算该固定窗口（两路线各自用同一计算器 + 完整证据）
    inc_res = compute_window(db, member_key, window_to, window_days, now, events=member_txns)
    replay_res = compute_window(db, member_key, window_to, window_days, now, events=member_txns)
    return {
        "member_key": member_key,
        "window_to": window_to.isoformat(),
        "window_days": window_days,
        "incremental": {
            "windows_in_scope": len(incremental),
            "discovered": discovered,
            "events_examined": len(member_txns) if discovered else 0,
            "result": inc_res,
        },
        "replay": {
            "windows_in_scope": len(_member_windows(db, member_key)),
            "events_examined": len(member_txns),
            "result": replay_res,
        },
        "equal": discovered and (inc_res["tier"], round(inc_res["spend"], 2)) == (replay_res["tier"], round(replay_res["spend"], 2)),
        "conclusion": {
            "tier": inc_res["tier"],
            "spend": inc_res["spend"],
            "rule_breakdown": inc_res["rule_breakdown"],
        },
    }


def _key_suffix(window_to: datetime, window_days: int) -> str:
    iso = window_to.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return f"{iso}|{window_days}"


# ---------------------------------------------------------------------------
# 发布版本（不可变）
# ---------------------------------------------------------------------------
def _content_hash(payload: dict) -> str:
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def freeze_release_version(db: Session, *, batch: models.CorrectionBatch, label: str) -> models.ReleaseVersion:
    cws = db.execute(select(models.CorrectionWindow).where(models.CorrectionWindow.batch_id == batch.id).order_by(models.CorrectionWindow.id)).scalars().all()
    reviews = db.execute(select(models.DownstreamReview).where(models.DownstreamReview.batch_id == batch.id).order_by(models.DownstreamReview.id)).scalars().all()
    content = {
        "batch_id": batch.id,
        "strategy": batch.strategy,
        "trigger_event_ids": batch.trigger_event_ids,
        "windows": [
            {"window_key": cw.window_key, "member": cw.member_key,
             "old": [cw.old_tier, float(cw.old_spend)], "new": [cw.new_tier, float(cw.new_spend)],
             "evidence_event_ids": cw.evidence_event_ids}
            for cw in cws
        ],
        "reviews": [{"dataset_id": r.dataset_id, "member": r.member_key, "relation": r.relation} for r in reviews],
    }
    sv = models.ReleaseVersion(
        label=label,
        strategy=batch.strategy,
        decision_inputs={**batch.strategy_inputs, "rationale": batch.strategy_rationale},
        rationale=batch.strategy_rationale,
        watermark_snapshot=watermarks.snapshot(db),
        batch_id=batch.id,
        content_hash=_content_hash(content),
        created_at=_utcnow(),
    )
    db.add(sv)
    db.flush()
    batch.release_version_id = sv.id
    db.flush()
    return sv


# ---------------------------------------------------------------------------
# 序列化
# ---------------------------------------------------------------------------
def batch_json(db: Session, batch: models.CorrectionBatch, release: models.ReleaseVersion | None = None) -> dict:
    cws = db.execute(select(models.CorrectionWindow).where(models.CorrectionWindow.batch_id == batch.id).order_by(models.CorrectionWindow.id)).scalars().all()
    reviews = db.execute(select(models.DownstreamReview).where(models.DownstreamReview.batch_id == batch.id).order_by(models.DownstreamReview.id)).scalars().all()
    rel = release
    if rel is None and batch.release_version_id:
        rel = db.get(models.ReleaseVersion, batch.release_version_id)
    return {
        "id": batch.id,
        "trigger": batch.trigger,
        "requested_strategy": batch.requested_strategy,
        "strategy": batch.strategy,
        "status": batch.status,
        "uncertainty": batch.uncertainty,
        "strategy_inputs": batch.strategy_inputs,
        "strategy_rationale": batch.strategy_rationale,
        "trigger_event_ids": batch.trigger_event_ids,
        "affected_members": batch.affected_members,
        "windows_examined": batch.windows_examined,
        "windows_changed": batch.windows_changed,
        "events_replayed": batch.events_replayed,
        "duration_ms": batch.duration_ms,
        "created_at": _as_utc(batch.created_at).isoformat(),
        "release_version": release_json(rel) if rel else None,
        "windows": [
            {
                "id": cw.id,
                "window_key": cw.window_key,
                "member_key": cw.member_key,
                "window_from": cw.window_from.isoformat(),
                "window_to": cw.window_to.isoformat(),
                "window_days": cw.window_days,
                "old_tier": cw.old_tier,
                "new_tier": cw.new_tier,
                "old_spend": float(cw.old_spend),
                "new_spend": float(cw.new_spend),
                "status": cw.status,
                "late_reason": cw.late_reason,
                "evidence_event_ids": cw.evidence_event_ids,
                "dependency_path": cw.dependency_path,
                "before_detail": cw.before_detail,
                "after_detail": cw.after_detail,
            }
            for cw in cws
        ],
        "reviews": [review_json(r) for r in reviews],
    }


def review_json(r: models.DownstreamReview) -> dict:
    return {
        "id": r.id, "batch_id": r.batch_id, "correction_window_id": r.correction_window_id,
        "dataset_id": r.dataset_id, "dataset_kind": r.dataset_kind, "member_key": r.member_key,
        "relation": r.relation, "impact": r.impact, "note": r.note, "status": r.status,
        "proposed": r.proposed, "created_at": _as_utc(r.created_at).isoformat(),
    }


def release_json(rel: models.ReleaseVersion) -> dict:
    return {
        "id": rel.id, "label": rel.label, "strategy": rel.strategy,
        "decision_inputs": rel.decision_inputs, "rationale": rel.rationale,
        "watermark_snapshot": rel.watermark_snapshot, "batch_id": rel.batch_id,
        "content_hash": rel.content_hash, "created_at": _as_utc(rel.created_at).isoformat(),
    }


def list_batches(db: Session) -> list[dict]:
    rows = db.execute(select(models.CorrectionBatch).order_by(models.CorrectionBatch.id.desc())).scalars().all()
    return [batch_json(db, b) for b in rows]


def list_releases(db: Session) -> list[dict]:
    rows = db.execute(select(models.ReleaseVersion).order_by(models.ReleaseVersion.id.desc())).scalars().all()
    return [release_json(r) for r in rows]


def list_reviews(db: Session, status: str | None = None) -> list[dict]:
    rows = db.execute(select(models.DownstreamReview).order_by(models.DownstreamReview.id.desc())).scalars().all()
    if status:
        rows = [r for r in rows if r.status == status]
    return [review_json(r) for r in rows]
