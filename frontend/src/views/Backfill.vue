<template>
  <div>
    <h2>迟到交易窗口影响与校正</h2>

    <div class="grid cols-4">
      <div class="stat"><div class="num">{{ ov.txn_records ?? 0 }}</div><div class="label">逻辑交易（去重后）</div></div>
      <div class="stat"><div class="num">{{ ov.txn_receipts ?? 0 }}</div><div class="label">到达明细</div></div>
      <div class="stat"><div class="num">{{ ov.window_snapshots ?? 0 }}</div><div class="label">窗口快照</div></div>
      <div class="stat"><div class="num">{{ ov.correction_batches ?? 0 }}</div><div class="label">校正批次</div></div>
    </div>

    <!-- 水位器 -->
    <div class="card" style="margin-top:16px">
      <h3>窗口水位器（允许迟到 / 已结算边界 / 缺失证据）</h3>
      <div class="form-row">
        <label>来源</label>
        <input v-model="lateness.source" placeholder="franchise_offline" style="width:180px" />
        <label>允许迟到(小时)</label>
        <input v-model.number="lateness.hours" type="number" style="width:90px" />
        <button @click="setLateness">更新容忍</button>
        <button class="ghost" @click="recalc">重算边界</button>
      </div>
      <p v-if="uncertainty?.tentative" class="muted">
        <span class="tag bad">暂定影响范围</span>
        {{ uncertainty.reasons.join('；') }}
      </p>
      <table>
        <thead><tr><th>来源</th><th>门店</th><th>容忍(h)</th><th>水位业务时间</th><th>已结算边界</th><th>最近日结</th><th>缺失证据</th><th>水位倒退</th></tr></thead>
        <tbody>
          <tr v-for="w in watermarks" :key="w.source + w.store">
            <td>{{ w.source }}</td>
            <td class="mono">{{ w.store }}</td>
            <td>{{ w.allowed_lateness_hours }}</td>
            <td class="mono">{{ fmt(w.watermark_biz_time) }}</td>
            <td class="mono">{{ fmt(w.settled_biz_time) }}</td>
            <td class="mono">{{ fmt(w.last_daily_close_date) }}</td>
            <td>
              <span v-if="w.missing_evidence.length" class="tag bad">{{ w.missing_evidence.map(m => m.store).join(',') }}</span>
              <span v-else class="tag ok">完整</span>
            </td>
            <td>
              <span v-if="w.regression" class="tag bad" :title="w.regression.reason">倒退</span>
              <span v-else class="muted">—</span>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 去重器 -->
    <div class="card">
      <h3>交易去重器（同 来源+门店流水+业务时间 合并重发，重复补传不改累计消费）</h3>
      <div class="form-row">
        <label>会员</label>
        <input v-model="txnMember" placeholder="可空=全部" @change="loadTxns" />
      </div>
      <div style="overflow:auto">
        <table>
          <thead><tr><th>业务时间</th><th>会员</th><th>来源</th><th>门店</th><th>流水</th><th>金额</th><th>到达次数</th><th>修订</th><th>状态</th><th>墓碑</th><th></th></tr></thead>
          <tbody>
            <tr v-for="t in txns" :key="t.txn_uid">
              <td class="mono">{{ fmt(t.biz_time) }}</td>
              <td class="mono">{{ t.member_key }}</td>
              <td>{{ t.order_source }}</td>
              <td>{{ t.store }}</td>
              <td class="mono">{{ t.store_serial || '—' }}</td>
              <td>¥{{ t.amount }}</td>
              <td class="mono">{{ t.receipt_count }}</td>
              <td class="mono">{{ t.revision_count }}</td>
              <td><span :class="['tag', t.status === 'active' ? 'ok' : 'bad']">{{ t.status }}</span></td>
              <td class="muted">{{ t.tombstone ? '见明细' : '—' }}</td>
              <td><button class="ghost" @click="viewTxn(t.txn_uid)">明细</button></td>
            </tr>
          </tbody>
        </table>
      </div>
      <div v-if="txnDetail" style="margin-top:12px">
        <h4>交易 {{ txnDetail.txn_uid.slice(0, 12) }}… 到达明细</h4>
        <table>
          <thead><tr><th>事件</th><th>类型</th><th>业务时间</th><th>接收时间</th><th>金额</th><th>增量</th></tr></thead>
          <tbody>
            <tr v-for="r in txnDetail.receipts" :key="r.event_id + r.kind">
              <td class="mono">#{{ r.event_id }}</td>
              <td><span class="tag">{{ r.kind }}</span></td>
              <td class="mono">{{ fmt(r.biz_time) }}</td>
              <td class="mono">{{ fmt(r.ingested_at) }}</td>
              <td>¥{{ r.amount }}</td>
              <td class="mono">{{ r.delta_amount }}</td>
            </tr>
          </tbody>
        </table>
        <pre v-if="txnDetail.tombstone">{{ JSON.stringify(txnDetail.tombstone, null, 2) }}</pre>
        <p class="muted">依赖路径：{{ txnDetail.dependency_path.map(p => p.node).join(' → ') }}</p>
      </div>
    </div>

    <!-- 校正运行 -->
    <div class="card">
      <h3>生成校正（沿依赖索引增量失效 / 事件回放，可自动选择）</h3>
      <div class="form-row">
        <label>触发事件 id（逗号分隔）</label>
        <input v-model="corr.event_ids" placeholder="如 19,21" style="width:160px" />
        <label>策略</label>
        <select v-model="corr.strategy">
          <option value="auto">自动选择</option>
          <option value="incremental">增量失效</option>
          <option value="replay">事件回放</option>
        </select>
        <label>补传峰值</label>
        <input v-model.number="corr.backfill_peak" type="number" style="width:90px" />
        <label>窗口查询/s</label>
        <input v-model.number="corr.window_query_rate" type="number" style="width:80px" />
        <label>可接受等待(s)</label>
        <input v-model.number="corr.acceptable_wait_seconds" type="number" style="width:90px" />
        <button @click="runCorrection">运行校正</button>
      </div>
      <p class="muted">自动策略：补传峰值≥200 时选增量失效（成本受控）；峰值小、查询低且等待短时选回放（因果直接）。</p>
    </div>

    <!-- 校正批次 -->
    <div class="card">
      <h3>校正批次（{{ batches.length }}）</h3>
      <table>
        <thead><tr><th>id</th><th>触发</th><th>策略</th><th>状态</th><th>窗口 检查/变更</th><th>回放事件</th><th>耗时(ms)</th><th>发布版本</th><th></th></tr></thead>
        <tbody>
          <tr v-for="b in batches" :key="b.id">
            <td class="mono">#{{ b.id }}</td>
            <td>{{ b.trigger }}</td>
            <td><span class="tag">{{ b.strategy }}</span></td>
            <td><span :class="['tag', b.status === 'final' ? 'ok' : 'bad']">{{ b.status }}</span></td>
            <td class="mono">{{ b.windows_examined }} / {{ b.windows_changed }}</td>
            <td class="mono">{{ b.events_replayed }}</td>
            <td class="mono">{{ b.duration_ms }}</td>
            <td class="mono">{{ b.release_version ? '#' + b.release_version.id : '—' }}</td>
            <td><button class="ghost" @click="viewBatch(b.id)">明细</button></td>
          </tr>
        </tbody>
      </table>
    </div>

    <div v-if="batchDetail" class="card">
      <h3>批次 #{{ batchDetail.id }} · 策略说明</h3>
      <p class="muted">{{ batchDetail.strategy_rationale }}</p>
      <p v-if="batchDetail.uncertainty?.tentative" class="muted">
        <span class="tag bad">暂定</span> {{ batchDetail.uncertainty.reasons.join('；') }}
      </p>
      <h4>受影响窗口（旧 → 新资格，含迟到原因与依赖路径）</h4>
      <table>
        <thead><tr><th>会员</th><th>窗口</th><th>旧</th><th>新</th><th>状态</th><th>迟到原因</th><th>证据事件</th></tr></thead>
        <tbody>
          <tr v-for="w in batchDetail.windows" :key="w.id">
            <td class="mono">{{ w.member_key }}</td>
            <td class="mono">{{ w.window_from.slice(0,10) }} ~ {{ w.window_to.slice(0,10) }}</td>
            <td><span :class="['tag', w.old_tier]">{{ w.old_tier }}</span> ¥{{ w.old_spend }}</td>
            <td><span :class="['tag', w.new_tier]">{{ w.new_tier }}</span> ¥{{ w.new_spend }}</td>
            <td>{{ w.status }}</td>
            <td class="muted">
              <span v-for="(r, i) in w.late_reason.reasons" :key="i">{{ r.label }}（滞后 {{ r.lag_hours }}h）</span>
            </td>
            <td class="mono">{{ w.evidence_event_ids.map(x => '#' + x).join(' ') }}</td>
          </tr>
        </tbody>
      </table>
      <h4 style="margin-top:12px">下游待复核（不替换已固定报表）</h4>
      <table v-if="batchDetail.reviews.length">
        <thead><tr><th>数据集</th><th>类型</th><th>会员</th><th>关系</th><th>影响</th><th>说明</th></tr></thead>
        <tbody>
          <tr v-for="r in batchDetail.reviews" :key="r.id">
            <td class="mono">#{{ r.dataset_id }}</td>
            <td>{{ r.dataset_kind }}</td>
            <td class="mono">{{ r.member_key }}</td>
            <td><span class="tag bad">{{ r.relation }}</span></td>
            <td>{{ r.impact }}</td>
            <td class="muted">{{ r.note }}</td>
          </tr>
        </tbody>
      </table>
      <p v-else class="muted">本批次未触及任何已固定数据集。</p>
      <h4 style="margin-top:12px">发布版本（固定策略与水位）</h4>
      <pre v-if="batchDetail.release_version">{{ JSON.stringify(batchDetail.release_version.decision_inputs, null, 2) }}
content_hash: {{ batchDetail.release_version.content_hash }}</pre>
    </div>

    <!-- 数据集 -->
    <div class="card">
      <h3>已冻结数据集（活动名单 / 预算报表）</h3>
      <div class="form-row">
        <input v-model="dsForm.name" placeholder="名称" style="width:200px" />
        <select v-model="dsForm.kind">
          <option value="activity_list">活动名单</option>
          <option value="budget_report">预算报表</option>
        </select>
        <input v-model="dsForm.biz_to" placeholder="业务截止" style="width:190px" />
        <input v-model="dsForm.sys_as_of" placeholder="系统水位" style="width:190px" />
        <select v-model="dsForm.min_tier">
          <option>SILVER</option><option>GOLD</option>
        </select>
        <button @click="createDataset">冻结数据集</button>
      </div>
      <table>
        <thead><tr><th>id</th><th>名称</th><th>类型</th><th>业务截止</th><th>窗口</th><th>状态</th><th>内容摘要</th></tr></thead>
        <tbody>
          <tr v-for="d in datasets" :key="d.id">
            <td class="mono">#{{ d.id }}</td>
            <td>{{ d.name }}</td>
            <td><span class="tag">{{ d.kind }}</span></td>
            <td class="mono">{{ fmt(d.biz_to) }}</td>
            <td class="mono">{{ (d.window_ids || []).length }}</td>
            <td><span :class="['tag', d.status === 'frozen' ? 'ok' : '']">{{ d.status }}</span></td>
            <td class="muted mono" style="max-width:340px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" :title="JSON.stringify(d.content)">
              {{ d.kind === 'activity_list' ? (d.content.members || []).length + ' 名会员' : Object.keys(d.content.stores || {}).join(',') }}
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 待复核 -->
    <div class="card">
      <h3>待复核关系（{{ reviews.length }}）</h3>
      <table v-if="reviews.length">
        <thead><tr><th>批次</th><th>数据集</th><th>会员</th><th>关系</th><th>影响</th><th>状态</th><th></th></tr></thead>
        <tbody>
          <tr v-for="r in reviews" :key="r.id">
            <td class="mono">#{{ r.batch_id }}</td>
            <td class="mono">#{{ r.dataset_id }} {{ r.dataset_kind }}</td>
            <td class="mono">{{ r.member_key }}</td>
            <td><span class="tag bad">{{ r.relation }}</span></td>
            <td>{{ r.impact }}</td>
            <td>{{ r.status }}</td>
            <td><button class="ghost" @click="ackReview(r.id)">标记已复核</button></td>
          </tr>
        </tbody>
      </table>
      <p v-else class="muted">暂无待复核关系。</p>
    </div>

    <!-- 发布版本 -->
    <div class="card">
      <h3>发布版本（不可变）</h3>
      <table>
        <thead><tr><th>id</th><th>标签</th><th>策略</th><th>批次</th><th>内容哈希</th><th>创建</th></tr></thead>
        <tbody>
          <tr v-for="r in releases" :key="r.id">
            <td class="mono">#{{ r.id }}</td>
            <td>{{ r.label }}</td>
            <td><span class="tag">{{ r.strategy }}</span></td>
            <td class="mono">{{ r.batch_id ? '#' + r.batch_id : '—' }}</td>
            <td class="mono" :title="r.content_hash">{{ r.content_hash.slice(0, 12) }}…</td>
            <td class="mono">{{ fmt(r.created_at) }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api'

const ov = ref({})
const watermarks = ref([])
const uncertainty = ref(null)
const txns = ref([])
const txnMember = ref('')
const txnDetail = ref(null)
const batches = ref([])
const batchDetail = ref(null)
const datasets = ref([])
const reviews = ref([])
const releases = ref([])
const lateness = ref({ source: 'franchise_offline', hours: 48 })
const corr = ref({ event_ids: '', strategy: 'auto', backfill_peak: 20, window_query_rate: 2, acceptable_wait_seconds: 60 })
const dsForm = ref({ name: '', kind: 'budget_report', biz_to: '2026-02-28T23:59:00Z', sys_as_of: '2026-03-01T00:00:00Z', min_tier: 'SILVER' })

function fmt(s) { return s ? s.replace('T', ' ').slice(0, 19) : '' }

async function load() {
  ov.value = await api.get('/overview')
  watermarks.value = await api.get('/watermarks')
  uncertainty.value = await api.get('/watermarks/uncertainty')
  batches.value = await api.get('/corrections')
  datasets.value = await api.get('/datasets')
  reviews.value = await api.get('/reviews')
  releases.value = await api.get('/releases')
  await loadTxns()
}
async function loadTxns() {
  const q = txnMember.value ? `?member=${txnMember.value}` : ''
  txns.value = await api.get('/txnstore/records' + q)
}
async function setLateness() {
  await api.post(`/watermarks/${lateness.value.source}/lateness`, { allowed_lateness_hours: lateness.value.hours })
  await load()
}
async function recalc() {
  watermarks.value = await api.post('/watermarks/recalc')
  uncertainty.value = await api.get('/watermarks/uncertainty')
}
async function viewTxn(uid) { txnDetail.value = await api.get(`/txnstore/records/${uid}`) }
async function runCorrection() {
  const ids = corr.value.event_ids.split(',').map(x => Number(x.trim())).filter(Boolean)
  if (!ids.length) { alert('请填写触发事件 id'); return }
  const body = { event_ids: ids, strategy: corr.value.strategy, backfill_peak: corr.value.backfill_peak, window_query_rate: corr.value.window_query_rate, acceptable_wait_seconds: corr.value.acceptable_wait_seconds }
  const b = await api.post('/corrections/run', body)
  batchDetail.value = b
  await load()
}
async function viewBatch(id) { batchDetail.value = await api.get(`/corrections/${id}`) }
async function createDataset() {
  await api.post('/datasets', dsForm.value)
  dsForm.value.name = ''
  await load()
}
async function ackReview(id) {
  await api.post(`/reviews/${id}/status`, { status: 'acknowledged' })
  await load()
}
onMounted(load)
</script>
