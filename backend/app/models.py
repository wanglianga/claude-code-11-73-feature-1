"""数据模型。

架构选型：追加式双时间事件账 + 可重建投影。
- events 为唯一事实来源（append-only，DB 触发器禁止 UPDATE/DELETE）。
- identity_edges / rule_versions / rule_withdrawals / member_directory 均为可重建投影，
  由事件重放生成，可随时通过 /api/admin/rebuild 全量重建。
"""
from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class Event(Base):
    """接入账：追加式双时间事件账。

    每行保存：来源哈希(source_hash)、链式哈希(chain_hash)、两类时间
    (biz_time 业务发生时间 / ingested_at 平台接收时间)、修订链(supersedes_id)、
    幂等键(idempotency_key)。
    """

    __tablename__ = "events"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    idempotency_key = Column(String(128), unique=True, nullable=False)
    source = Column(String(32), nullable=False)  # pos | online | franchise_offline | identity_desk | rule_admin
    event_type = Column(String(32), nullable=False)
    source_hash = Column(String(64), nullable=False)  # sha256(canonical payload)
    chain_hash = Column(String(64), nullable=False)  # sha256(prev chain_hash + row) 防篡改链
    biz_time = Column(DateTime(timezone=True), nullable=False)
    ingested_at = Column(DateTime(timezone=True), nullable=False)
    actor = Column(String(64))
    supersedes_id = Column(BigInteger, ForeignKey("events.id"), nullable=True)
    payload = Column(JSONB, nullable=False)


class IdentityEdge(Base):
    """身份关系图投影：合并/拆分/待核/撤销的有效区间（双时间区间）。"""

    __tablename__ = "identity_edges"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    relation_event_id = Column(BigInteger, ForeignKey("events.id"), nullable=False)
    member_a = Column(String(64), nullable=False)
    member_b = Column(String(64), nullable=False)
    relation = Column(String(16), nullable=False)  # merge | split | pending | revoke
    status = Column(String(16), nullable=False)  # active | split | revoked | pending
    biz_from = Column(DateTime(timezone=True), nullable=False)
    biz_to = Column(DateTime(timezone=True), nullable=True)
    sys_from = Column(DateTime(timezone=True), nullable=False)
    sys_to = Column(DateTime(timezone=True), nullable=True)
    target_edge_id = Column(BigInteger, nullable=True)  # split/revoke 指向的 merge 边
    reason = Column(Text)


class RuleVersion(Base):
    """等级规则版本投影：按门店、商品范围与生效区间修订。"""

    __tablename__ = "rule_versions"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    rule_event_id = Column(BigInteger, ForeignKey("events.id"), nullable=False)
    rule_id = Column(String(64), nullable=False)
    version = Column(Integer, nullable=False)
    supersedes_version = Column(Integer, nullable=True)
    store_scope = Column(JSONB, nullable=False)  # "ALL" 或门店列表
    product_scope = Column(JSONB, nullable=False)  # "ALL" 或品类列表
    window_days = Column(Integer, nullable=False)
    thresholds = Column(JSONB, nullable=False)  # {"SILVER": 300, "GOLD": 1000}
    effective_from = Column(DateTime(timezone=True), nullable=False)
    effective_to = Column(DateTime(timezone=True), nullable=True)
    sys_from = Column(DateTime(timezone=True), nullable=False)


class RuleWithdrawal(Base):
    """规则撤回投影：支持部分（按门店范围）追溯撤回。"""

    __tablename__ = "rule_withdrawals"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    withdraw_event_id = Column(BigInteger, ForeignKey("events.id"), nullable=False)
    rule_id = Column(String(64), nullable=False)
    target_version = Column(Integer, nullable=False)
    store_scope = Column(JSONB, nullable=False)
    withdraw_from = Column(DateTime(timezone=True), nullable=False)
    withdraw_to = Column(DateTime(timezone=True), nullable=True)
    sys_from = Column(DateTime(timezone=True), nullable=False)
    reason = Column(Text)


class MemberDirectory(Base):
    """会员名录投影（仅索引已知会员键，不保存会覆盖历史的“当前主档”）。"""

    __tablename__ = "member_directory"

    member_key = Column(String(64), primary_key=True)
    display_name = Column(String(128))
    first_seen_event_id = Column(BigInteger)


class Semantics(Base):
    """时间语义目录：定义查询口径。"""

    __tablename__ = "semantics"

    code = Column(String(32), primary_key=True)
    name = Column(String(64), nullable=False)
    biz_policy = Column(String(16), nullable=False)  # query
    sys_policy = Column(String(16), nullable=False)  # query | now | both
    description = Column(Text)


class Publication(Base):
    """发布库：冻结证据水位、身份关系图版本与规则集合版本。"""

    __tablename__ = "publications"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    name = Column(String(128), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)
    biz_as_of = Column(DateTime(timezone=True), nullable=False)
    sys_as_of = Column(DateTime(timezone=True), nullable=False)
    watermark_event_id = Column(BigInteger, nullable=False)  # 证据水位
    identity_graph_version = Column(BigInteger, nullable=False)  # 关系图冻结版本(最大身份事件id)
    rule_set_version = Column(BigInteger, nullable=False)  # 规则集合冻结版本(最大规则事件id)
    semantics_code = Column(String(32), nullable=False)


class PublicationRow(Base):
    __tablename__ = "publication_rows"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    publication_id = Column(BigInteger, ForeignKey("publications.id"), nullable=False)
    member_key = Column(String(64), nullable=False)
    tier = Column(String(16), nullable=False)
    spend = Column(Numeric(14, 2), nullable=False)
    detail = Column(JSONB, nullable=False)


class Diff(Base):
    __tablename__ = "diffs"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    pub_a = Column(BigInteger, ForeignKey("publications.id"), nullable=False)
    pub_b = Column(BigInteger, ForeignKey("publications.id"), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)


class DiffItem(Base):
    """差异归因：把资格变化连到具体事件。"""

    __tablename__ = "diff_items"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    diff_id = Column(BigInteger, ForeignKey("diffs.id"), nullable=False)
    member_key = Column(String(64), nullable=False)
    before = Column(JSONB, nullable=False)
    after = Column(JSONB, nullable=False)
    causes = Column(JSONB, nullable=False)  # [{event_id, cause, detail}]


class Impact(Base):
    """影响索引：变化对营销名单、门店预算、训练样本的下游影响。"""

    __tablename__ = "impacts"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    diff_id = Column(BigInteger, ForeignKey("diffs.id"), nullable=False)
    dimension = Column(String(16), nullable=False)  # marketing | budget | training
    entity = Column(String(128), nullable=False)  # 下游实体键
    member_key = Column(String(64), nullable=False)
    change_type = Column(String(32), nullable=False)
    before = Column(JSONB)
    after = Column(JSONB)
    attributed_event_ids = Column(JSONB)


class User(Base):
    __tablename__ = "users"

    username = Column(String(64), primary_key=True)
    password = Column(String(128), nullable=False)
    role = Column(String(32), nullable=False)
    token = Column(String(128), nullable=False)


# ============================================================================
# 迟到交易窗口影响与校正数据服务
#
# 以下表均为「可重建投影」或「校正产物」：
# - txn_records / txn_receipts：交易去重投影（按订单来源+门店流水+业务发生时间合并重发）。
# - source_watermarks：窗口水位器（允许迟到 / 已结算边界 / 缺失证据 / 倒退标记）。
# - window_snapshots：资格窗口快照（按发生日规则计算 90 日消费的物化结果）。
# - dependency_edges：依赖定位器的引用图（正向 + 反向索引，支持墓碑）。
# - datasets：已冻结的下游数据集（活动名单 / 预算报表 / 训练样本）。
# - correction_batches / correction_windows / downstream_reviews：校正发布产物。
# - release_versions：把策略选择与水位固定到发布版本。
# ============================================================================


class TxnRecord(Base):
    """交易去重投影：一笔逻辑交易对应一行。

    去重身份 = sha256(订单来源 | 门店 | 门店流水 | 业务发生时间)。
    同一门店重复补传同一流水时命中同一行，只累加 receipt_count，不改变 amount，
    从而保证累计消费不变。删除更正把 status 置为 deleted 并保留墓碑（tombstone）。
    """

    __tablename__ = "txn_records"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    txn_uid = Column(String(64), unique=True, nullable=False, index=True)
    order_source = Column(String(32), nullable=False)
    store = Column(String(32), nullable=False)
    store_serial = Column(String(64), nullable=True)
    biz_time = Column(DateTime(timezone=True), nullable=False, index=True)
    member_key = Column(String(64), nullable=False, index=True)
    amount = Column(Numeric(14, 2), nullable=False)
    category = Column(String(32))
    status = Column(String(16), nullable=False)  # active | deleted
    receipt_count = Column(Integer, nullable=False, default=1)  # 含重复补传的到达次数
    revision_count = Column(Integer, nullable=False, default=0)
    first_event_id = Column(BigInteger, nullable=False)
    latest_event_id = Column(BigInteger, nullable=False)
    first_ingested_at = Column(DateTime(timezone=True), nullable=False)
    latest_ingested_at = Column(DateTime(timezone=True), nullable=False)
    payload = Column(JSONB, nullable=False)
    tombstone = Column(JSONB, nullable=True)  # 删除更正墓碑：{event_id, reason, at}


class TxnReceipt(Base):
    """交易到达明细：每次到达（初次/重复补传/更正/删除）都留一条回查记录。"""

    __tablename__ = "txn_receipts"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    txn_uid = Column(String(64), nullable=False, index=True)
    event_id = Column(BigInteger, nullable=False, index=True)
    kind = Column(String(16), nullable=False)  # initial | duplicate | correction | delete
    biz_time = Column(DateTime(timezone=True), nullable=False)
    ingested_at = Column(DateTime(timezone=True), nullable=False)
    amount = Column(Numeric(14, 2))
    delta_amount = Column(Numeric(14, 2))  # 相对上一有效金额的变化（重复补传为 0）
    detail = Column(JSONB)


class SourceWatermark(Base):
    """窗口水位器：每个来源(可按门店)的允许迟到、已结算边界与缺失证据。

    已结算边界 = 水位业务时间 - 允许迟到。业务时间早于该边界却被接收的证据属于「超容忍迟到」，
    需在校正原因中标注；来源水位倒退或门店日结证据缺失时，影响范围只能标为「暂定」。
    """

    __tablename__ = "source_watermarks"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    source = Column(String(32), nullable=False, index=True)
    store = Column(String(32), nullable=False, default="*")  # '*' 表示来源级汇总
    allowed_lateness_hours = Column(Integer, nullable=False, default=48)
    watermark_biz_time = Column(DateTime(timezone=True), nullable=True)
    watermark_ingested_at = Column(DateTime(timezone=True), nullable=True)
    settled_biz_time = Column(DateTime(timezone=True), nullable=True)  # 已结算边界
    last_daily_close_date = Column(DateTime(timezone=True), nullable=True)
    missing_evidence = Column(JSONB, nullable=False, default=list)  # 缺失门店日结的日期清单
    regression = Column(JSONB, nullable=True)  # 水位倒退证据
    version = Column(Integer, nullable=False, default=1)
    updated_at = Column(DateTime(timezone=True), nullable=False)


class WindowSnapshot(Base):
    """资格窗口快照：对某会员在 [window_from, window_to] 内按交易发生日规则计算的物化结果。"""

    __tablename__ = "window_snapshots"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    window_key = Column(String(64), unique=True, nullable=False, index=True)
    member_key = Column(String(64), nullable=False, index=True)
    window_from = Column(DateTime(timezone=True), nullable=False)
    window_to = Column(DateTime(timezone=True), nullable=False, index=True)
    window_days = Column(Integer, nullable=False)
    sys_as_of = Column(DateTime(timezone=True), nullable=False)
    semantics = Column(String(16), nullable=False, default="AS_KNOWN_NOW")
    tier = Column(String(16), nullable=False)
    spend = Column(Numeric(14, 2), nullable=False)
    counted_event_ids = Column(JSONB, nullable=False, default=list)
    counted_txn_uids = Column(JSONB, nullable=False, default=list)
    rule_breakdown = Column(JSONB, nullable=False, default=list)  # 按发生日规则分组的明细
    invalidated = Column(Boolean, nullable=False, default=False)
    invalidated_by = Column(JSONB, nullable=True)
    last_batch_id = Column(BigInteger, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False)


class DependencyEdge(Base):
    """依赖定位器的引用图。parent → child 一条边，可按任一端反查。

    relation：
    - belongs       transaction → member
    - covers_member window → member
    - counts        window → transaction（该窗口计入了该交易）
    - references    dataset → window（数据集冻结引用窗口）
    - lists         dataset → member（数据集包含会员）
    """

    __tablename__ = "dependency_edges"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    parent_type = Column(String(16), nullable=False, index=True)
    parent_key = Column(String(128), nullable=False, index=True)
    child_type = Column(String(16), nullable=False, index=True)
    child_key = Column(String(128), nullable=False, index=True)
    relation = Column(String(24), nullable=False)
    window_id = Column(BigInteger, nullable=True)
    event_id = Column(BigInteger, nullable=True)
    active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), nullable=False)


class Dataset(Base):
    """已冻结的下游数据集。校正发布器绝不替换其内容，只签发待复核关系。"""

    __tablename__ = "datasets"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    name = Column(String(128), nullable=False)
    kind = Column(String(24), nullable=False, index=True)  # activity_list | budget_report
    biz_to = Column(DateTime(timezone=True), nullable=False)
    sys_as_of = Column(DateTime(timezone=True), nullable=False)
    window_days = Column(Integer, nullable=False, default=90)
    window_ids = Column(JSONB, nullable=False, default=list)  # 冻结引用的窗口快照 id
    content = Column(JSONB, nullable=False)  # 物化内容（不可被校正覆盖）
    frozen = Column(Boolean, nullable=False, default=True)
    status = Column(String(16), nullable=False, default="frozen")
    release_version_id = Column(BigInteger, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False)


class CorrectionBatch(Base):
    """一次校正批次：记录触发、策略、水位快照与（暂定）影响范围。"""

    __tablename__ = "correction_batches"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    trigger = Column(String(32), nullable=False)  # late_transaction | deletion | manual
    requested_strategy = Column(String(16), nullable=False)  # auto | incremental | replay
    strategy = Column(String(16), nullable=False)  # incremental | replay
    status = Column(String(16), nullable=False)  # final | tentative
    uncertainty = Column(JSONB)
    strategy_inputs = Column(JSONB)
    strategy_rationale = Column(Text)
    trigger_event_ids = Column(JSONB, nullable=False, default=list)
    affected_members = Column(JSONB, nullable=False, default=list)
    windows_examined = Column(Integer, nullable=False, default=0)
    windows_changed = Column(Integer, nullable=False, default=0)
    events_replayed = Column(Integer, nullable=False, default=0)
    duration_ms = Column(Integer, nullable=False, default=0)
    release_version_id = Column(BigInteger, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False)


class CorrectionWindow(Base):
    """单个受影响窗口的校正明细：旧/新资格、迟到原因、依赖路径与证据。"""

    __tablename__ = "correction_windows"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    batch_id = Column(BigInteger, ForeignKey("correction_batches.id"), nullable=False, index=True)
    window_id = Column(BigInteger, nullable=True)
    window_key = Column(String(64), nullable=False)
    member_key = Column(String(64), nullable=False)
    window_from = Column(DateTime(timezone=True), nullable=False)
    window_to = Column(DateTime(timezone=True), nullable=False)
    window_days = Column(Integer, nullable=False)
    old_tier = Column(String(16), nullable=False)
    new_tier = Column(String(16), nullable=False)
    old_spend = Column(Numeric(14, 2), nullable=False)
    new_spend = Column(Numeric(14, 2), nullable=False)
    status = Column(String(16), nullable=False)  # changed | unchanged
    late_reason = Column(JSONB, nullable=False, default=dict)
    evidence_event_ids = Column(JSONB, nullable=False, default=list)
    dependency_path = Column(JSONB, nullable=False, default=list)
    before_detail = Column(JSONB)
    after_detail = Column(JSONB)


class DownstreamReview(Base):
    """下游待复核关系：已固定的报表/名单受影响时签发此关系，绝不替换原内容。"""

    __tablename__ = "downstream_reviews"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    batch_id = Column(BigInteger, ForeignKey("correction_batches.id"), nullable=False, index=True)
    correction_window_id = Column(BigInteger, nullable=True)
    dataset_id = Column(BigInteger, nullable=False, index=True)
    dataset_kind = Column(String(24), nullable=False)
    member_key = Column(String(64), nullable=False)
    relation = Column(String(24), nullable=False, default="PENDING_REVIEW")
    impact = Column(String(32), nullable=False)
    note = Column(Text)
    status = Column(String(16), nullable=False, default="pending")
    proposed = Column(JSONB)
    created_at = Column(DateTime(timezone=True), nullable=False)


class ReleaseVersion(Base):
    """发布版本：把策略选择及其输入、水位快照与校正内容哈希固定下来（版本不可变）。"""

    __tablename__ = "release_versions"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    label = Column(String(128), nullable=False)
    strategy = Column(String(16), nullable=False)
    decision_inputs = Column(JSONB, nullable=False, default=dict)
    rationale = Column(Text)
    watermark_snapshot = Column(JSONB, nullable=False, default=dict)
    batch_id = Column(BigInteger, nullable=True)
    content_hash = Column(String(64), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, index=True)
