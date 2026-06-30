<template>
  <div class="research-dashboard">
    <div class="research-header">
      <div>
        <h2>投研看板</h2>
        <p>基金市场统计、基金分组看板和 ETF 每日跟踪</p>
      </div>
      <div class="header-actions">
        <span v-if="updatedAt" class="updated-time">更新 {{ formatDateTime(updatedAt) }}</span>
        <button class="refresh-btn" :disabled="loading" @click="refreshDashboard">
          {{ loading ? '刷新中...' : '刷新' }}
        </button>
      </div>
    </div>

    <div class="tab-bar">
      <button
        v-for="tab in tabs"
        :key="tab.key"
        class="tab-btn"
        :class="{ active: activeTab === tab.key }"
        @click="activeTab = tab.key"
      >
        {{ tab.label }}
      </button>
    </div>

    <div v-if="loading" class="state-card">正在加载投研数据...</div>
    <div v-else-if="error" class="state-card error">{{ error }}</div>
    <template v-else>
      <section v-show="activeTab === 'market'" class="tab-section">
        <div class="metric-grid">
          <div class="metric-card">
            <span class="metric-label">基金总数 / 覆盖率</span>
            <strong>{{ summary.total_funds || 0 }}</strong>
            <small>覆盖 {{ summary.market_total?.toLocaleString() || '--' }} 只 · {{ formatPercent(summary.coverage_rate) }}</small>
          </div>
          <div class="metric-card">
            <span class="metric-label">4433 通过率</span>
            <strong>{{ formatPercent(summary.pass_4433_rate) }}</strong>
            <small>{{ summary.pass_4433 || 0 }} 只</small>
          </div>
          <div class="metric-card">
            <span class="metric-label">近 1 年正收益占比</span>
            <strong>{{ formatPercent(summary.positive_1y_rate) }}</strong>
          </div>
          <div class="metric-card">
            <span class="metric-label">近 1 年收益中位数</span>
            <strong :class="returnClass(summary.return_1y_median)">
              {{ formatPercent(summary.return_1y_median) }}
            </strong>
          </div>
          <div class="metric-card">
            <span class="metric-label">近 3 月收益中位数</span>
            <strong :class="returnClass(summary.return_3m_median)">
              {{ formatPercent(summary.return_3m_median) }}
            </strong>
          </div>
          <div class="metric-card">
            <span class="metric-label">近 3 年收益中位数</span>
            <strong :class="returnClass(summary.return_3y_median)">
              {{ formatPercent(summary.return_3y_median) }}
            </strong>
          </div>
          <div class="metric-card">
            <span class="metric-label">夏普比率中位数</span>
            <strong>{{ formatNumber(summary.sharpe_1y_median) }}</strong>
          </div>
          <div class="metric-card">
            <span class="metric-label">最大回撤中位数</span>
            <strong class="down">{{ formatPercent(summary.max_drawdown_1y_median) }}</strong>
          </div>
        </div>

        <div class="split-grid">
          <div class="panel">
            <div class="panel-title">基金类型分布</div>
            <table class="data-table">
              <thead>
                <tr>
                  <th>类型</th>
                  <th>数量</th>
                  <th>占比</th>
                  <th>1年中位</th>
                  <th>4433</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="item in typeStats" :key="item.fund_type">
                  <td>{{ item.fund_type }}</td>
                  <td>{{ item.count }}</td>
                  <td>{{ formatPercent(item.ratio) }}</td>
                  <td :class="returnClass(item.return_1y_median)">{{ formatPercent(item.return_1y_median) }}</td>
                  <td>{{ item.pass_4433 }}</td>
                </tr>
              </tbody>
            </table>
          </div>

          <div class="panel">
            <div class="panel-title">投研分组概览</div>
            <div class="group-list">
              <div v-for="item in groupStats" :key="item.key" class="group-row" :class="{ dimmed: item.available === false }">
                <div>
                  <strong>
                    {{ item.name }}
                    <span v-if="item.available === false" class="unavailable-tag">数据不全</span>
                  </strong>
                  <span>{{ item.count }} 只</span>
                </div>
                <div class="bar">
                  <span :style="{ width: Math.min(item.ratio || 0, 100) + '%' }"></span>
                </div>
                <em :class="returnClass(item.return_1y_median)">
                  {{ formatPercent(item.return_1y_median) }}
                </em>
              </div>
            </div>
          </div>
        </div>
      </section>

      <section v-show="activeTab === 'funds'" class="tab-section">
        <div class="card-grid">
          <div v-for="card in fundCards" :key="card.key" class="fund-card" :class="{ unavailable: card.available === false }">
            <div class="fund-card-head">
              <div>
                <h3>
                  {{ card.name }}
                  <span v-if="card.available === false" class="unavailable-tag">数据不全</span>
                </h3>
                <p>{{ card.summary.total }} 只，4433 通过 {{ card.summary.pass_4433 }} 只</p>
              </div>
              <span :class="returnClass(card.summary.return_1y_avg)">
                {{ formatPercent(card.summary.return_1y_avg) }}
              </span>
            </div>
            <table class="compact-table">
              <thead>
                <tr>
                  <th>基金</th>
                  <th>1年</th>
                  <th>夏普</th>
                  <th>回撤</th>
                  <th>波动率</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="fund in card.items" :key="fund.fund_code" @click="viewFund(fund)">
                  <td class="fund-identity">
                    <strong :title="fund.fund_name">{{ displayFundName(fund.fund_name) }}</strong>
                    <span>{{ fund.fund_code }}</span>
                  </td>
                  <td :class="returnClass(fund.return_1y)">{{ formatPercent(fund.return_1y) }}</td>
                  <td>{{ formatNumber(fund.sharpe_ratio_1y) }}</td>
                  <td class="down">{{ formatPercent(fund.max_drawdown_1y) }}</td>
                  <td>{{ formatPercent(fund.volatility_1y) }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </section>

      <section v-show="activeTab === 'etf'" class="tab-section">
        <div class="notice" v-if="etfSummary.source === 'fund_list_cache' || etfSummary.stale">
          {{ etfSummary.net_flow_note || '当前为本地缓存数据，点击刷新获取实时 ETF 行情。' }}
        </div>
        <div class="metric-grid etf-metrics">
          <div class="metric-card">
            <span class="metric-label">ETF 池</span>
            <strong>{{ etfSummary.total || 0 }}</strong>
            <small v-if="etfSummary.with_performance">含业绩 {{ etfSummary.with_performance }} 只</small>
          </div>
          <div class="metric-card">
            <span class="metric-label">实时行情</span>
            <strong>{{ etfSummary.with_estimate || 0 }}</strong>
          </div>
          <div class="metric-card">
            <span class="metric-label">平均涨跌幅</span>
            <strong :class="returnClass(etfSummary.avg_estimate_change)">
              {{ formatPercent(etfSummary.avg_estimate_change) }}
            </strong>
          </div>
          <div class="metric-card">
            <span class="metric-label">1年收益中位</span>
            <strong :class="returnClass(etfSummary.return_1y_median)">
              {{ formatPercent(etfSummary.return_1y_median) }}
            </strong>
          </div>
          <div class="metric-card">
            <span class="metric-label">上涨占比</span>
            <strong>{{ formatPercent(etfSummary.positive_estimate_rate) }}</strong>
          </div>
          <div class="metric-card" v-if="etfSummary.total_amount">
            <span class="metric-label">总成交额</span>
            <strong>{{ formatAmountYi(etfSummary.total_amount) }}</strong>
          </div>
        </div>

        <div class="split-grid">
          <div class="panel">
            <div class="panel-title">ETF 分类统计</div>
            <table class="data-table">
              <thead>
                <tr>
                  <th>分类</th>
                  <th>数量</th>
                  <th>涨跌均值</th>
                  <th>1年中位</th>
                  <th>总成交额</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="item in etfCategories" :key="item.category">
                  <td>{{ item.category }}</td>
                  <td>{{ item.count }}</td>
                  <td :class="returnClass(item.estimate_change_avg)">
                    {{ formatPercent(item.estimate_change_avg) }}
                  </td>
                  <td :class="returnClass(item.return_1y_median)">
                    {{ formatPercent(item.return_1y_median) }}
                  </td>
                  <td>{{ formatAmountYi(item.amount_total) || '--' }}</td>
                </tr>
              </tbody>
            </table>
          </div>

          <div class="panel">
            <div class="panel-title">ETF 每日跟踪（按涨跌幅排序）</div>
            <table class="data-table">
              <thead>
                <tr>
                  <th>基金</th>
                  <th>最新价</th>
                  <th>涨跌幅</th>
                  <th>1年收益</th>
                  <th>成交额</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="fund in etfItems" :key="fund.fund_code" @click="viewFund(fund)">
                  <td class="fund-identity">
                    <strong :title="fund.fund_name">{{ fund.fund_name }}</strong>
                    <span>{{ fund.fund_code }}</span>
                  </td>
                  <td>{{ formatNumber(fund.latest_price) }}</td>
                  <td :class="returnClass(fund.estimate_change)">{{ formatPercent(fund.estimate_change) }}</td>
                  <td :class="returnClass(fund.return_1y)">{{ formatPercent(fund.return_1y) }}</td>
                  <td>{{ formatAmountYi(fund.amount) }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </section>

      <section v-show="activeTab === 'sectors'" class="tab-section">
        <div v-if="industryTaskStatus.running && industryTaskStatus.message" class="state-card compact">
          {{ industryTaskStatus.message }}
          <span v-if="industryTaskStatus.total">
            {{ industryTaskStatus.progress || 0 }}/{{ industryTaskStatus.total }}
          </span>
        </div>

        <!-- Toast 通知 -->
        <transition name="toast-fade">
          <div v-if="showToast" class="sector-toast">
            <span class="toast-icon">✅</span>
            <span>{{ toastMessage }}</span>
            <button class="toast-close" @click="showToast = false">×</button>
          </div>
        </transition>
        <div class="metric-grid">
          <div class="metric-card">
            <span class="metric-label">行业标签</span>
            <strong>{{ industryStats.total || 0 }}</strong>
          </div>
          <div class="metric-card">
            <span class="metric-label">覆盖基金</span>
            <strong>{{ industryStats.fund_count || 0 }}</strong>
          </div>
          <div class="metric-card">
            <span class="metric-label">近 3 月最强</span>
            <strong :class="returnClass(industryTop3m[0]?.return_3m_median)">
              {{ industryTop3m[0]?.industry || '--' }}
            </strong>
            <small>{{ formatPercent(industryTop3m[0]?.return_3m_median) }}</small>
          </div>
          <div class="metric-card">
            <span class="metric-label">近 1 年最强</span>
            <strong :class="returnClass(industryTop1y[0]?.return_1y_median)">
              {{ industryTop1y[0]?.industry || '--' }}
            </strong>
            <small>{{ formatPercent(industryTop1y[0]?.return_1y_median) }}</small>
          </div>
        </div>

        <div class="split-grid sector-grid">
          <div class="panel">
            <div class="panel-title">近 3 月领先行业</div>
            <div class="sector-list">
              <div v-for="item in industryTop3m" :key="item.industry" class="sector-row">
                <div>
                  <strong>{{ item.industry }}</strong>
                  <span>{{ item.fund_count }} 只基金，上涨占比 {{ formatPercent(item.positive_3m_rate) }}</span>
                </div>
                <em :class="returnClass(item.return_3m_median)">{{ formatPercent(item.return_3m_median) }}</em>
              </div>
            </div>
          </div>

          <div class="panel">
            <div class="panel-title">近 1 年领先行业</div>
            <div class="sector-list">
              <div v-for="item in industryTop1y" :key="item.industry" class="sector-row">
                <div>
                  <strong>{{ item.industry }}</strong>
                  <span>{{ item.fund_count }} 只基金，上涨占比 {{ formatPercent(item.positive_1y_rate) }}</span>
                </div>
                <em :class="returnClass(item.return_1y_median)">{{ formatPercent(item.return_1y_median) }}</em>
              </div>
            </div>
          </div>

          <div class="panel sector-full">
            <div class="panel-title">行业走势汇总</div>
            <table class="data-table">
              <thead>
                <tr>
                  <th>行业</th>
                  <th>基金数</th>
                  <th>近3月</th>
                  <th>半年</th>
                  <th>1年</th>
                  <th>3年</th>
                  <th>3月上涨基金占比</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="item in industryItems" :key="item.industry">
                  <td>{{ item.industry }}</td>
                  <td>{{ item.fund_count }}</td>
                  <td :class="returnClass(item.return_3m_median)">{{ formatPercent(item.return_3m_median) }}</td>
                  <td :class="returnClass(item.return_6m_median)">{{ formatPercent(item.return_6m_median) }}</td>
                  <td :class="returnClass(item.return_1y_median)">{{ formatPercent(item.return_1y_median) }}</td>
                  <td :class="returnClass(item.return_3y_median)">{{ formatPercent(item.return_3y_median) }}</td>
                  <td>{{ formatPercent(item.positive_3m_rate) }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </section>
    </template>
  </div>
</template>

<script>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { researchAPI } from '../services/api'

export default {
  name: 'ResearchDashboard',
  emits: ['view-fund'],
  setup(props, { emit }) {
    const loading = ref(false)
    const error = ref('')
    const activeTab = ref('market')
    const dashboard = ref({})
    const updatedAt = ref('')
    const industryTaskStatus = ref({})
    let industryPollTimer = null
    const showToast = ref(false)
    const toastMessage = ref('')
    let toastTimer = null

    const showToastNotification = (message, duration = 4000) => {
      toastMessage.value = message
      showToast.value = true
      if (toastTimer) clearTimeout(toastTimer)
      toastTimer = setTimeout(() => {
        showToast.value = false
      }, duration)
    }

    const tabs = [
      { key: 'market', label: '基金市场统计' },
      { key: 'funds', label: '基金看板' },
      { key: 'etf', label: 'ETF 每日跟踪' },
      { key: 'sectors', label: '板块行情' }
    ]

    const loadDashboard = async () => {
      loading.value = true
      error.value = ''
      try {
        const res = await researchAPI.getDashboard({ limit: 5, etf_limit: 80 })
        dashboard.value = res.data || {}
        industryTaskStatus.value = dashboard.value.industry_performance_task || dashboard.value.industry_performance?.task_status || {}
        if (!dashboard.value.industry_performance || !(dashboard.value.industry_performance.items || []).length) {
          try {
            const industryRes = await researchAPI.getIndustryPerformance()
            dashboard.value = {
              ...dashboard.value,
              industry_performance: industryRes.data || {}
            }
            industryTaskStatus.value = industryRes.data?.task_status || industryTaskStatus.value
          } catch (industryErr) {
            console.warn('行业表现加载失败:', industryErr)
          }
        }
        updatedAt.value = dashboard.value.updated_at || ''
      } catch (err) {
        error.value = err?.response?.data?.error || err?.message || '投研数据加载失败'
      } finally {
        loading.value = false
      }
    }

    const pollIndustryPerformance = () => {
      if (industryPollTimer) return
      industryPollTimer = setInterval(async () => {
        try {
          const res = await researchAPI.getIndustryPerformance()
          dashboard.value = {
            ...dashboard.value,
            industry_performance: res.data || {}
          }
          industryTaskStatus.value = res.data?.task_status || {}
          if (!industryTaskStatus.value.running) {
            clearInterval(industryPollTimer)
            industryPollTimer = null
            await loadDashboard()
            // 弹窗提示完成
            const itemCount = res.data?.items?.length || res.data?.summary?.total || 0
            showToastNotification(`板块行情汇总完成，共 ${itemCount} 个板块`)
          }
        } catch (err) {
          console.warn('板块行情后台刷新状态获取失败:', err)
        }
      }, 3000)
    }

    const refreshDashboard = async () => {
      if (activeTab.value !== 'sectors') {
        await loadDashboard()
        return
      }

      loading.value = true
      error.value = ''
      try {
        const res = await researchAPI.rebuildIndustryPerformance()
        industryTaskStatus.value = res.data?.task_status || { running: true, message: '后台汇总板块行情...' }
        dashboard.value = {
          ...dashboard.value,
          industry_performance: res.data?.data || dashboard.value.industry_performance || {}
        }
        pollIndustryPerformance()
      } catch (err) {
        error.value = err?.response?.data?.error || err?.message || '板块行情刷新启动失败'
      } finally {
        loading.value = false
      }
    }

    const marketStats = computed(() => dashboard.value.market_stats || {})
    const summary = computed(() => marketStats.value.summary || {})
    const typeStats = computed(() => marketStats.value.type_stats || [])
    const groupStats = computed(() => marketStats.value.group_stats || [])
    const fundCards = computed(() => dashboard.value.fund_dashboard?.cards || [])
    const etfTracking = computed(() => dashboard.value.etf_tracking || {})
    const etfSummary = computed(() => etfTracking.value.summary || {})
    const etfCategories = computed(() => etfTracking.value.categories || [])
    const etfItems = computed(() => etfTracking.value.items || [])
    const industryPerformance = computed(() => dashboard.value.industry_performance || {})
    const industryStats = computed(() => industryPerformance.value.summary || {})
    const industryItems = computed(() => industryPerformance.value.items || [])
    const industryTop3m = computed(() => industryPerformance.value.top_3m || [])
    const industryTop1y = computed(() => industryPerformance.value.top_1y || [])

    const formatPercent = (value) => {
      if (value === null || value === undefined || value === '') return '--'
      const num = Number(value)
      if (!Number.isFinite(num)) return '--'
      return `${num > 0 ? '+' : ''}${num.toFixed(2)}%`
    }

    const formatNumber = (value) => {
      if (value === null || value === undefined || value === '') return '--'
      const num = Number(value)
      return Number.isFinite(num) ? num.toFixed(2) : '--'
    }

    const displayFundName = (name, maxLength = 10) => {
      const text = String(name || '')
      return text.length > maxLength ? `${text.slice(0, maxLength)}...` : text
    }

    const formatDateTime = (value) => {
      if (!value) return ''
      const date = new Date(value)
      return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN')
    }

    const formatAmountYi = (value) => {
      const num = Number(value)
      if (!Number.isFinite(num)) return '--'
      return `${(num / 100000000).toFixed(2)}亿`
    }

    const returnClass = (value) => {
      const num = Number(value)
      if (!Number.isFinite(num)) return ''
      return num > 0 ? 'up' : num < 0 ? 'down' : ''
    }

    const viewFund = (fund) => {
      if (fund?.fund_code) emit('view-fund', fund.fund_code)
    }

    onMounted(loadDashboard)
    onUnmounted(() => {
      if (industryPollTimer) clearInterval(industryPollTimer)
    })

    return {
      loading,
      error,
      activeTab,
      tabs,
      updatedAt,
      summary,
      typeStats,
      groupStats,
      fundCards,
      etfSummary,
      etfCategories,
      etfItems,
      industryStats,
      industryItems,
      industryTop3m,
      industryTop1y,
      industryTaskStatus,
      showToast,
      toastMessage,
      showToastNotification,
      loadDashboard,
      refreshDashboard,
      formatPercent,
      formatNumber,
      formatAmountYi,
      displayFundName,
      formatDateTime,
      returnClass,
      viewFund
    }
  }
}
</script>

<style scoped>
.research-dashboard {
  background: #fff;
  border-radius: 12px;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.08);
  overflow: hidden;
  min-height: calc(100vh - 120px);
}

.research-header {
  display: flex;
  justify-content: space-between;
  gap: 16px;
  padding: 18px 24px;
  border-bottom: 1px solid #e5e7eb;
}

.research-header h2 {
  margin: 0 0 4px;
  font-size: 22px;
}

.research-header p {
  margin: 0;
  color: #6b7280;
  font-size: 13px;
}

.header-actions {
  display: flex;
  align-items: center;
  gap: 12px;
}

.updated-time {
  color: #9ca3af;
  font-size: 12px;
}

.refresh-btn {
  border: 1px solid #1677ff;
  border-radius: 8px;
  background: #1677ff;
  color: #fff;
  padding: 8px 16px;
  cursor: pointer;
}

.refresh-btn:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}

.tab-bar {
  display: flex;
  gap: 8px;
  padding: 14px 24px 0;
}

.tab-btn {
  border: 1px solid #e5e7eb;
  background: #fff;
  color: #4b5563;
  border-radius: 8px 8px 0 0;
  padding: 10px 16px;
  cursor: pointer;
  font-weight: 600;
}

.tab-btn.active {
  color: #1677ff;
  border-color: #1677ff;
  background: #f0f5ff;
}

.tab-section {
  padding: 20px 24px 28px;
}

.state-card {
  margin: 24px;
  padding: 48px;
  text-align: center;
  color: #6b7280;
  border: 1px solid #e5e7eb;
  border-radius: 10px;
  background: #f9fafb;
}

.state-card.error {
  color: #b91c1c;
  background: #fff1f2;
}

.metric-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
  gap: 12px;
  margin-bottom: 18px;
}

.metric-card {
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  padding: 14px;
  background: #fff;
}

.metric-label {
  display: block;
  color: #6b7280;
  font-size: 12px;
  margin-bottom: 8px;
}

.metric-card strong {
  display: block;
  font-size: 24px;
  line-height: 1.2;
}

.metric-card small {
  color: #9ca3af;
}

.split-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 16px;
}

.panel {
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  overflow: hidden;
  background: #fff;
}

.panel-title {
  padding: 12px 14px;
  font-weight: 700;
  border-bottom: 1px solid #e5e7eb;
  background: #f8fafc;
}

.data-table,
.compact-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}

.compact-table {
  table-layout: fixed;
}

.compact-table th:first-child,
.compact-table td:first-child {
  width: 34%;
}

.compact-table th:not(:first-child),
.compact-table td:not(:first-child) {
  white-space: nowrap;
}

.compact-table th:nth-child(2),
.compact-table td:nth-child(2) {
  width: 20%;
}

.compact-table th:nth-child(3),
.compact-table td:nth-child(3) {
  width: 12%;
}

.compact-table th:nth-child(4),
.compact-table td:nth-child(4) {
  width: 18%;
}

.compact-table th:nth-child(5),
.compact-table td:nth-child(5) {
  width: 16%;
}

.data-table th,
.data-table td,
.compact-table th,
.compact-table td {
  padding: 10px 8px;
  border-bottom: 1px solid #f1f5f9;
  text-align: left;
  vertical-align: middle;
}

.compact-table th:not(:first-child),
.compact-table td:not(:first-child) {
  text-align: right;
}

.data-table th,
.compact-table th {
  color: #6b7280;
  background: #fbfdff;
  font-weight: 600;
}

.data-table tbody tr,
.compact-table tbody tr {
  cursor: pointer;
}

.data-table tbody tr:hover,
.compact-table tbody tr:hover {
  background: #f8fafc;
}

.data-table td span {
  display: block;
  max-width: 220px;
  color: #6b7280;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.fund-identity {
  min-width: 0;
}

.fund-identity strong,
.fund-identity span {
  display: block;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.fund-identity strong {
  color: #111827;
  font-weight: 700;
}

.fund-identity span {
  margin-top: 2px;
  color: #6b7280;
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace;
  font-weight: 400;
}

.group-list {
  padding: 8px 14px 14px;
}

.group-row {
  display: grid;
  grid-template-columns: 150px 1fr 72px;
  align-items: center;
  gap: 12px;
  padding: 10px 0;
  border-bottom: 1px solid #f1f5f9;
}

.group-row:last-child {
  border-bottom: none;
}

.group-row.dimmed {
  opacity: 0.55;
}

.group-row span {
  display: block;
  color: #9ca3af;
  font-size: 12px;
}

.bar {
  height: 8px;
  border-radius: 999px;
  background: #eef2ff;
  overflow: hidden;
}

.bar span {
  display: block;
  height: 100%;
  border-radius: inherit;
  background: #1677ff;
}

.card-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(360px, 1fr));
  gap: 16px;
}

.fund-card {
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  overflow: hidden;
  background: #fff;
}

.fund-card-head {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  padding: 14px;
  border-bottom: 1px solid #e5e7eb;
  background: #f8fafc;
}

.fund-card-head h3 {
  margin: 0 0 4px;
  font-size: 16px;
}

.unavailable-tag {
  display: inline-block;
  margin-left: 6px;
  padding: 1px 6px;
  border-radius: 4px;
  background: #fef3c7;
  color: #92400e;
  font-size: 11px;
  font-weight: 500;
  vertical-align: middle;
}

.fund-card.unavailable .fund-card-head {
  background: #fffbeb;
}

.fund-card-head p {
  margin: 0;
  color: #6b7280;
  font-size: 12px;
}

.fund-card-head > span {
  font-weight: 800;
  white-space: nowrap;
}

.notice {
  margin-bottom: 14px;
  padding: 10px 12px;
  border: 1px solid #fde68a;
  border-radius: 8px;
  background: #fffbeb;
  color: #92400e;
  font-size: 13px;
}

.sector-grid {
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
}

.sector-full {
  grid-column: 1 / -1;
}

.sector-list {
  padding: 8px 14px 14px;
}

.sector-list.compact {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
  gap: 0 16px;
}

.sector-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 12px;
  align-items: center;
  padding: 10px 0;
  border-bottom: 1px solid #f1f5f9;
}

.sector-row:last-child {
  border-bottom: none;
}

.sector-row strong {
  display: block;
  color: #111827;
}

.sector-row span {
  display: block;
  margin-top: 2px;
  color: #6b7280;
  font-size: 12px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.sector-row em {
  font-style: normal;
  font-weight: 800;
  white-space: nowrap;
}

.up {
  color: #ef4444;
}

.down {
  color: #10b981;
}

@media (max-width: 1100px) {
  .split-grid {
    grid-template-columns: 1fr;
  }

  .research-header {
    flex-direction: column;
  }
}

/* Toast 通知 */
.sector-toast {
  position: fixed;
  top: 24px;
  left: 50%;
  transform: translateX(-50%);
  z-index: 2000;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 12px 20px;
  background: #fff;
  border: 1px solid #e5e7eb;
  border-radius: 10px;
  box-shadow: 0 8px 30px rgba(15, 23, 42, 0.16);
  font-size: 14px;
  color: #111827;
  white-space: nowrap;
}

.toast-icon {
  font-size: 16px;
}

.toast-close {
  width: 24px;
  height: 24px;
  border: none;
  border-radius: 6px;
  background: #f3f4f6;
  color: #6b7280;
  font-size: 16px;
  line-height: 1;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: background 0.15s;
}

.toast-close:hover {
  background: #e5e7eb;
}

.toast-fade-enter-active,
.toast-fade-leave-active {
  transition: all 0.3s ease;
}

.toast-fade-enter-from {
  opacity: 0;
  transform: translateX(-50%) translateY(-12px);
}

.toast-fade-leave-to {
  opacity: 0;
  transform: translateX(-50%) translateY(-12px);
}

@media (max-width: 720px) {
  .tab-bar {
    overflow-x: auto;
  }

  .card-grid {
    grid-template-columns: 1fr;
  }

  .group-row {
    grid-template-columns: 110px 1fr 64px;
  }
}
</style>
