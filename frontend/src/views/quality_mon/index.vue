<template>
  <section class="page" data-module="quality_mon">
    <header class="page-head">
      <div>
        <h2>质量监控管理</h2>
        <p class="page-desc">维护分析数据质量监控规则的定义、阈值告警和采样审计。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">登记监控规则</button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">导出质量监控清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无质量监控数据，可先登记监控规则</td>
        </tr>
      </tbody>
    </table>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记监控规则</h3>
        <label v-for="field in columns" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <section class="quality-scan" data-testid="quality-scan-panel">
      <header class="page-head">
        <div>
          <h3>质量扫描与交班日报</h3>
          <p class="page-desc">
            异常率与评分只采用每条规则同批实测的扫描行数；缺分母的历史摘要不参与计算。
          </p>
        </div>
        <div class="page-actions">
          <button class="btn primary" data-testid="run-quality-scan" type="button" @click="runScan">
            执行质量扫描
          </button>
          <button class="btn" data-testid="run-quality-rescan" type="button" @click="runRescan">
            补扫后重新扫描
          </button>
        </div>
      </header>

      <div class="stat-row" v-if="summary">
        <article class="stat-card" data-testid="stat-score">
          <span class="stat-label">质量评分（门槛 {{ summary.复核门槛 }} 分）</span>
          <strong class="stat-value" :class="{ 'error-text': summary.需人工复核 }">
            {{ summary.质量评分 }}
          </strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">实测异常率</span>
          <strong class="stat-value">{{ summary.实测异常率百分比 }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">实测扫描行数</span>
          <strong class="stat-value">{{ summary.总扫描行数 }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">异常行数</span>
          <strong class="stat-value">{{ summary.总异常行数 }}</strong>
        </article>
      </div>

      <h4>批次 × 规则实测结果</h4>
      <table class="data-table" data-testid="rule-result-table">
        <thead>
          <tr>
            <th>批次编号</th><th>规则编号</th><th>校验类型</th><th>扫描行数</th>
            <th>异常行数</th><th>异常率</th><th>数据状态</th><th>实测范围</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in ruleResults" :key="`${row.批次编号}-${row.规则编号}`">
            <td>{{ row.批次编号 }}</td>
            <td>{{ row.规则编号 }}</td>
            <td>{{ row.校验类型 || '—' }}</td>
            <td>{{ row.扫描行数 ?? '缺分母' }}</td>
            <td>{{ row.异常行数 }}</td>
            <td>{{ row.是否实测 ? rowRate(row) : '—' }}</td>
            <td>{{ row.数据状态 }}</td>
            <td>{{ row.实测范围 || '—' }}</td>
          </tr>
          <tr v-if="!ruleResults.length">
            <td colspan="8" class="empty-state">尚未执行质量扫描</td>
          </tr>
        </tbody>
      </table>

      <template v-if="report">
        <h4>交班日报</h4>
        <div class="daily-report" data-testid="daily-report">
          <p><strong>复核结论：</strong>{{ report.复核结论 }}</p>
          <p><strong>覆盖范围：</strong>{{ report.覆盖范围说明 }}</p>
          <p><strong>处理意见：</strong>{{ report.处理意见 }}</p>
          <p v-if="report.缺分母规则.length">
            <strong>分母迟到规则：</strong>
            <span class="error-text">{{ report.缺分母规则.join('、') }}（补扫前不给整批结论）</span>
          </p>
        </div>

        <h4>本班可转交人工复核的异常订单（评分低于门槛时）</h4>
        <table class="data-table" data-testid="transferable-table">
          <thead>
            <tr><th>订单编号</th><th>规则编号</th><th>批次编号</th><th>扫描行数</th><th>异常行数</th><th>依据与实测范围</th><th>操作</th></tr>
          </thead>
          <tbody>
            <tr v-for="order in transferableOrders" :key="order.订单编号">
              <td>{{ order.订单编号 }}</td>
              <td>{{ order.规则编号 }}</td>
              <td>{{ order.批次编号 }}</td>
              <td>{{ order.扫描行数 }}</td>
              <td>{{ order.异常行数 }}</td>
              <td>{{ order.依据 }}</td>
              <td class="row-actions">
                <button class="link" data-testid="transfer-order" type="button" @click="transferOrder(order.订单编号)">
                  转交复核
                </button>
              </td>
            </tr>
            <tr v-if="!transferableOrders.length">
              <td colspan="7" class="empty-state">评分已达门槛或暂无可转交的实测异常订单</td>
            </tr>
          </tbody>
        </table>

        <h4 v-if="transfers.length">已转交记录（补扫后仍保留原始依据）</h4>
        <table v-if="transfers.length" class="data-table" data-testid="transfer-table">
          <thead>
            <tr><th>订单编号</th><th>转交时评分</th><th>转交时异常率</th><th>分母快照</th><th>实测范围快照</th><th>处理人</th><th>状态</th></tr>
          </thead>
          <tbody>
            <tr v-for="t in transfers" :key="t.id">
              <td>{{ t.订单编号 }}</td>
              <td>{{ t.转交时评分 }}</td>
              <td>{{ t.转交时异常率 }}</td>
              <td>{{ t.扫描行数快照 }}</td>
              <td>{{ t.实测范围快照 }}</td>
              <td>{{ t.处理人 }}</td>
              <td>{{ t.状态 }}</td>
            </tr>
          </tbody>
        </table>
      </template>
    </section>

    <footer class="page-foot">
      <span>共 {{ total }} 条质量监控记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>
type RuleResult = {
  批次编号: string
  规则编号: string
  校验类型?: string
  扫描行数: number | null
  异常行数: number
  是否实测: boolean
  数据状态: string
  实测范围?: string
}
type TransferableOrder = {
  订单编号: string
  规则编号: string
  批次编号: string
  扫描行数: number
  异常行数: number
  依据: string
}
type TransferRecord = {
  id: number
  订单编号: string
  转交时评分: number
  转交时异常率: string
  扫描行数快照: number
  实测范围快照: string
  处理人: string
  状态: string
}
type DailyReport = {
  质量评分: number
  复核门槛: number
  复核结论: string
  覆盖范围说明: string
  处理意见: string
  缺分母规则: string[]
}
type Summary = {
  质量评分: number
  复核门槛: number
  实测异常率百分比: string
  总扫描行数: number
  总异常行数: number
  需人工复核: boolean
}

const ENDPOINT = '/api/quality_mon'
const columns = ["规则编号", "监控对象", "校验类型", "阈值下界", "阈值上界", "告警级别", "采样比例", "生效时段"]
const actions = ["启用规则", "暂停规则", "废弃规则"]
const statuses = ["草稿", "已启用", "已暂停", "已废弃"]
const metricNames = ["规则总数", "已启用规则", "已暂停规则"]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const stats = computed(() => [
  { label: metricNames[0], value: total.value },
  { label: metricNames[1], value: rows.value.filter((row) => row.status === statuses[1]).length },
  { label: metricNames[2], value: rows.value.filter((row) => row.status === statuses[statuses.length - 1]).length },
])

function resetFilters() {
  filters.value = {}
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value).toString()
  try {
    const response = await request(`${ENDPOINT}/export?${query}`)
    if (!response.ok) throw new Error('导出失败，请稍后重试')
    const url = URL.createObjectURL(await response.blob())
    const link = document.createElement('a')
    link.href = url
    link.download = 'quality_mon.csv'
    link.click()
    URL.revokeObjectURL(url)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(columns.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const response = await request(ENDPOINT, {
      method: 'POST', body: JSON.stringify(createValues.value),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '登记失败')
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('质量监控动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '质量监控操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('监控规则列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '质量监控列表读取失败'
  }
}

const SCAN_ENDPOINT = '/api/quality_scan'
const summary = ref<Summary | null>(null)
const ruleResults = ref<RuleResult[]>([])
const report = ref<DailyReport | null>(null)
const transferableOrders = ref<TransferableOrder[]>([])
const transfers = ref<TransferRecord[]>([])

function rowRate(row: RuleResult): string {
  if (!row.扫描行数) return '—'
  return `${((row.异常行数 / row.扫描行数) * 100).toFixed(2)}%`
}

async function loadScanData() {
  errorMessage.value = ''
  try {
    const [summaryRes, resultRes, reportRes, transferRes, transferListRes] = await Promise.all([
      request(`${SCAN_ENDPOINT}/summary`),
      request(`${SCAN_ENDPOINT}/results`),
      request(`${SCAN_ENDPOINT}/daily-report`),
      request(`${SCAN_ENDPOINT}/transferable`),
      request(`${SCAN_ENDPOINT}/transfers`),
    ])
    if (!summaryRes.ok || !resultRes.ok || !reportRes.ok || !transferRes.ok || !transferListRes.ok) {
      throw new Error('质量扫描数据读取失败')
    }
    summary.value = await summaryRes.json()
    ruleResults.value = (await resultRes.json()).items ?? []
    report.value = await reportRes.json()
    transferableOrders.value = (await transferRes.json()).items ?? []
    transfers.value = (await transferListRes.json()).items ?? []
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '质量扫描数据读取失败'
  }
}

async function runScan() {
  errorMessage.value = ''
  try {
    const response = await request('/api/pipeline/quality-scan', { method: 'POST' })
    if (!response.ok) throw new Error('质量扫描执行失败')
    await loadScanData()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '质量扫描执行失败'
  }
}

async function runRescan() {
  errorMessage.value = ''
  try {
    const response = await request('/api/pipeline/quality-rescan', { method: 'POST' })
    if (!response.ok) throw new Error('补扫执行失败')
    await loadScanData()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '补扫执行失败'
  }
}

async function transferOrder(orderId: string) {
  errorMessage.value = ''
  try {
    const response = await request(`${SCAN_ENDPOINT}/transfers`, {
      method: 'POST',
      body: JSON.stringify({ order_ids: [orderId], handler: '值班员' }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '订单转交失败')
    await loadScanData()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '订单转交失败'
  }
}

onMounted(() => {
  void reload()
  void loadScanData()
})
</script>
