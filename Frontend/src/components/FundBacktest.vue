<template>
  <div class="fund-backtest">
    <header class="page-title">
      <div>
        <h2>投资回测</h2>
        <p>多策略基金历史回测，按单位净值模拟买卖、持仓和收益表现。</p>
      </div>
      <div class="flow-steps">
        <span class="active">1 选择策略</span>
        <span :class="{ active: currentFundCode }">2 选择基金</span>
        <span :class="{ active: result }">3 查看结果</span>
      </div>
    </header>

    <section class="strategy-section">
      <div class="section-head">
        <div>
          <h3>选择回测方式</h3>
          <p>{{ activeStrategy?.desc }}</p>
        </div>
        <span class="strategy-count">11 项策略/工具</span>
      </div>
      <div class="strategy-board">
        <button
          v-for="item in strategyCards"
          :key="item.key"
          class="strategy-card"
          :class="{ active: selectedStrategy === item.key, disabled: item.disabled }"
          :disabled="item.disabled"
          @click="handleStrategyCard(item)"
          :title="item.desc"
        >
          <span class="strategy-icon">{{ item.icon }}</span>
          <span class="strategy-name">{{ item.name }}</span>
          <span class="strategy-desc">{{ item.desc }}</span>
        </button>
      </div>
    </section>

    <section v-if="error" class="error-message">{{ error }}</section>

    <section class="fund-picker" v-if="!currentFundCode">
      <div class="picker-copy">
        <span class="step-kicker">下一步</span>
        <h3>为「{{ activeStrategy?.name }}」选择回测基金</h3>
        <p>先确定策略，再选择基金。选中基金后即可调整参数并开始回测。</p>
      </div>
      <FundSearch @fund-selected="handleFundSelected" />
    </section>

    <template v-else>
      <section class="selected-fund">
        <div>
          <span>当前基金</span>
          <strong>{{ currentFundCode }}</strong>
          <em v-if="currentFundName">{{ currentFundName }}</em>
        </div>
        <button class="ghost-btn" @click="changeFund">更换基金</button>
      </section>

      <div class="workbench">
        <main class="result-pane">
          <section class="chart-panel">
            <div class="panel-head">
              <div>
                <h3>回测交易图 - {{ currentFundName || currentFundCode }}</h3>
                <p>{{ activeStrategy?.desc }}</p>
              </div>
              <div class="chart-tabs">
                <button :class="{ active: chartType === 'asset' }" @click="chartType = 'asset'">资产</button>
                <button :class="{ active: chartType === 'return' }" @click="chartType = 'return'">收益率</button>
              </div>
            </div>
            <div ref="chartEl" class="chart-container"></div>
            <div v-if="!result && !loading" class="empty-chart">设置参数后点击开始回测</div>
          </section>

          <section class="stats-panel" v-if="result">
            <h3>核心指标统计</h3>
            <table class="stats-table">
              <tbody>
                <tr v-for="row in metricRows" :key="row.label">
                  <td>{{ row.label }}</td>
                  <td :class="row.className">{{ row.value }}</td>
                </tr>
              </tbody>
            </table>
          </section>

          <section class="trades-panel" v-if="result">
            <div class="panel-head compact">
              <h3>买卖记录</h3>
              <button class="ghost-btn" @click="exportTrades">导出CSV</button>
            </div>
            <div class="table-scroll">
              <table class="trade-table">
                <thead>
                  <tr>
                    <th>序号</th>
                    <th>日期</th>
                    <th>类型</th>
                    <th>交易金额</th>
                    <th>持有份额</th>
                    <th>成本(元)</th>
                    <th>单位净值</th>
                    <th>累计净值</th>
                    <th>累计收益(元)</th>
                    <th>累计收益率</th>
                    <th>阶段状态</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="trade in paginatedTrades" :key="trade.index" :class="trade.type">
                    <td>{{ trade.index }}</td>
                    <td>{{ trade.date }}</td>
                    <td>{{ trade.type_label }}</td>
                    <td>{{ formatMoney(trade.amount) }}</td>
                    <td>{{ formatTradeShares(trade.holding_shares) }}</td>
                    <td>{{ formatMoney(trade.cost) }}</td>
                    <td>{{ formatNumber(trade.nav, 4) }}</td>
                    <td>{{ formatNumber(trade.acc_nav, 4) }}</td>
                    <td :class="returnClass(trade.return)">{{ formatMoney(trade.return) }}</td>
                    <td :class="returnClass(trade.return_rate)">{{ formatPercent(trade.return_rate) }}</td>
                    <td>{{ trade.status }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
            <div class="pagination" v-if="totalPages > 1">
              <button @click="currentPage--" :disabled="currentPage === 1">上一页</button>
              <span>{{ currentPage }} / {{ totalPages }}</span>
              <button @click="currentPage++" :disabled="currentPage === totalPages">下一页</button>
            </div>
          </section>
        </main>

        <aside class="params-panel">
          <div class="side-title">
            <strong>{{ activeStrategy?.name }}</strong>
            <template v-if="selectedStrategy === 'target_profit_plan'">
              <div class="strategy-note title-note">
                <span>按参考工具口径计算份额和收益，申购/赎回费率不参与定盈买卖记录。阶段达标后，下一交易日自动开启新阶段。</span>
              </div>
            </template>
            <span v-else>{{ activeStrategy?.desc }}</span>
          </div>

          <label class="field" v-if="selectedStrategy !== 'double_down'">
            <span>总计划资金</span>
            <div class="input-unit">
              <input type="number" v-model.number="form.capital" min="1" step="1000" />
              <b>元</b>
            </div>
          </label>

          <div class="field-row" v-if="selectedStrategy !== 'double_down'">
            <label class="field">
              <span>起始日期</span>
              <input type="date" v-model="form.startDate" :max="form.endDate" />
            </label>
            <label class="field">
              <span>结束日期</span>
              <input type="date" v-model="form.endDate" :min="form.startDate" :max="today" />
            </label>
          </div>

          <label class="field" v-if="selectedStrategy !== 'target_profit_plan' && selectedStrategy !== 'double_down'">
            <span>申购/赎回费率</span>
            <div class="input-unit">
              <input type="number" v-model.number="form.feeRate" min="0" max="2" step="0.01" />
              <b>%</b>
            </div>
          </label>

          <template v-if="selectedStrategy === 'target_profit_plan'">
            <div class="target-mode">
              <label :class="['mode-option', { active: form.targetPlanType === 'manual' }]">
                <input type="radio" value="manual" v-model="form.targetPlanType" @change="applyTargetPlanPreset" />
                <span>手工配参数</span>
              </label>
              <label :class="['mode-option', { active: form.targetPlanType === 'auto_small' }]">
                <input type="radio" value="auto_small" v-model="form.targetPlanType" @change="applyTargetPlanPreset" />
                <span>自动抓小鱼</span>
              </label>
              <label :class="['mode-option', { active: form.targetPlanType === 'auto_big' }]">
                <input type="radio" value="auto_big" v-model="form.targetPlanType" @change="applyTargetPlanPreset" />
                <span>自动捞大鱼</span>
              </label>
            </div>

            <label class="field target-field">
              <span>首次买入</span>
              <div class="input-unit">
                <input type="number" v-model.number="form.targetBuyAmount" min="0" step="100" :disabled="targetPlanAuto" />
                <b>元</b>
              </div>
            </label>

            <label class="field target-field">
              <span>盈利达到</span>
              <div class="input-unit">
                <input type="number" v-model.number="form.profitTargetPercent" min="0" step="0.1" :disabled="targetPlanAuto" />
                <b>%</b>
              </div>
              <em>则卖出全部持仓，并在下一交易日开启新阶段。</em>
            </label>

            <label class="field target-field">
              <span>比上一次交易跌幅超过</span>
              <div class="input-unit">
                <input type="number" v-model.number="form.buyDropPercent" min="0" step="0.1" :disabled="targetPlanAuto" />
                <b>%</b>
              </div>
              <em>则继续买入一笔。</em>
            </label>

            <label class="field target-field">
              <span>连续买入时，每次增加</span>
              <div class="input-unit">
                <input type="number" v-model.number="form.buyIncreasePercent" min="0" step="0.1" :disabled="targetPlanAuto" />
                <b>%</b>
              </div>
              <em>为 0 时，每次都按首次买入金额执行。</em>
            </label>

            <label class="field target-field">
              <span>最后一笔买入，涨幅达到</span>
              <div class="input-unit">
                <input type="number" v-model.number="form.lastBuyRiseSellPercent" min="0" step="0.1" :disabled="targetPlanAuto" />
                <b>%</b>
              </div>
              <em>若当前阶段有多笔持仓，则卖出最后一笔回笼资金。</em>
            </label>

            <label class="field target-field">
              <span>最多连续卖出</span>
              <div class="input-unit">
                <input type="number" v-model.number="form.targetMaxConsecutiveSell" min="1" step="1" :disabled="targetPlanAuto" />
                <b>笔</b>
              </div>
            </label>

            <div class="start-rule">
              <strong>首次启动规则</strong>
              <label>
                <input type="radio" value="immediate" v-model="form.startRule" />
                <span>首次无条件启动</span>
              </label>
              <label>
                <input type="radio" value="ma10_above_ma60" v-model="form.startRule" />
                <span>MA10 在 MA60 之上则启动</span>
              </label>
              <label>
                <input type="radio" value="price_gt" v-model="form.startRule" />
                <span>价格大于指定净值则启动</span>
              </label>
              <label>
                <input type="radio" value="price_lt" v-model="form.startRule" />
                <span>价格小于指定净值则启动</span>
              </label>
            </div>

            <label class="field target-field" v-if="form.startRule === 'price_gt' || form.startRule === 'price_lt'">
              <span>启动净值</span>
              <input type="number" v-model.number="form.startPrice" min="0" step="0.0001" />
            </label>
          </template>

          <template v-else-if="selectedStrategy === 'fixed_amount'">
            <label class="field">
              <span>投资频率</span>
              <select v-model="form.frequency">
                <option value="monthly">每月定投</option>
                <option value="weekly">每周定投</option>
                <option value="daily">每日定投</option>
                <option value="lump_sum">一次性买入</option>
              </select>
            </label>
            <label class="field" v-if="form.frequency === 'monthly'">
              <span>每月买入日</span>
              <input type="number" v-model.number="form.monthDay" min="1" max="28" />
            </label>
            <label class="field" v-if="form.frequency === 'weekly'">
              <span>每周买入日</span>
              <select v-model.number="form.weekday">
                <option :value="0">周一</option>
                <option :value="1">周二</option>
                <option :value="2">周三</option>
                <option :value="3">周四</option>
                <option :value="4">周五</option>
              </select>
            </label>
            <money-field label="每期金额" v-model="form.amount" />
            <money-field label="首次额外买入" v-model="form.initialAmount" />
          </template>

          <template v-else-if="selectedStrategy === 'double_down'">
            <money-field label="每年计划资金" v-model="form.doubleAnnualCapital" />
            <label class="field">
              <span>定投最小间隔</span>
              <div class="input-unit">
                <input type="number" v-model.number="form.doubleMinIntervalDays" min="6" step="1" />
                <b>天</b>
              </div>
            </label>
            <label class="field">
              <span>最多可以使用未来</span>
              <div class="input-unit">
                <input type="number" v-model.number="form.doubleFutureYears" min="0" step="1" />
                <b>年</b>
              </div>
            </label>

            <div class="start-rule double-targets">
              <strong>投资目标</strong>
              <label>
                <input type="radio" value="double" v-model="form.doubleTargetType" />
                <span>达到翻倍</span>
              </label>
              <label>
                <input type="radio" value="annual_return" v-model="form.doubleTargetType" />
                <span>年化收益大于</span>
              </label>
              <percent-field v-if="form.doubleTargetType === 'annual_return'" label="" v-model="form.doubleTargetPercent" />
              <label>
                <input type="radio" value="absolute_return" v-model="form.doubleTargetType" />
                <span>绝对涨幅大于</span>
              </label>
              <percent-field v-if="form.doubleTargetType === 'absolute_return'" label="" v-model="form.doubleTargetPercent" />
            </div>

            <label class="field">
              <span>达到目标后，且回落</span>
              <div class="input-unit">
                <input type="number" v-model.number="form.doubleSellDrawdownPercent" min="0" step="0.1" />
                <b>%</b>
              </div>
            </label>

            <label class="check-field">
              <input type="checkbox" v-model="form.doubleDynamicDrawdown" />
              <span>动态增加回落值</span>
            </label>

            <div class="field-row">
              <label class="field">
                <span>起始日期</span>
                <input type="date" v-model="form.startDate" :max="form.endDate" />
              </label>
              <label class="field">
                <span>结束日期</span>
                <input type="date" v-model="form.endDate" :min="form.startDate" :max="today" />
              </label>
            </div>
          </template>

          <template v-else-if="selectedStrategy === 'grid'">
            <money-field label="每格金额" v-model="form.baseAmount" />
            <percent-field label="网格间距" v-model="form.gridStepPercent" />
            <percent-field label="止盈卖出阈值" v-model="form.sellProfitPercent" />
            <label class="field">
              <span>最多连续卖出</span>
              <input type="number" v-model.number="form.maxConsecutiveSell" min="1" step="1" />
            </label>
          </template>

          <template v-else-if="selectedStrategy === 'ma_timing'">
            <label class="field">
              <span>均线周期</span>
              <select v-model.number="form.maDays">
                <option :value="10">MA10</option>
                <option :value="20">MA20</option>
                <option :value="60">MA60</option>
              </select>
            </label>
            <money-field label="基础买入金额" v-model="form.baseAmount" />
            <label class="field">
              <span>低于均线加仓倍数</span>
              <input type="number" v-model.number="form.belowMaFactor" min="0" step="0.1" />
            </label>
            <percent-field label="高于均线减仓比例" v-model="form.aboveMaSellPercent" />
          </template>

          <template v-else-if="selectedStrategy === 'trend_timing'">
            <money-field label="基础买入金额" v-model="form.baseAmount" />
            <label class="field">
              <span>趋势观察天数</span>
              <input type="number" v-model.number="form.lookbackDays" min="3" step="1" />
            </label>
            <percent-field label="向上趋势阈值" v-model="form.trendThresholdPercent" />
            <percent-field label="转弱减仓阈值" v-model="form.downtrendThresholdPercent" />
            <percent-field label="转弱减仓比例" v-model="form.downtrendSellPercent" />
          </template>

          <template v-else-if="selectedStrategy === 'rocket_plan'">
            <money-field label="基础买入金额" v-model="form.baseAmount" />
            <percent-field label="回撤触发阈值" v-model="form.dropTriggerPercent" />
            <label class="field">
              <span>每档加速倍数</span>
              <input type="number" v-model.number="form.boostFactor" min="0" step="0.5" />
            </label>
            <label class="field">
              <span>最高加速倍数</span>
              <input type="number" v-model.number="form.maxMultiplier" min="1" step="1" />
            </label>
          </template>

          <template v-else-if="selectedStrategy === 'ai_plan'">
            <money-field label="基础买入金额" v-model="form.baseAmount" />
            <label class="field">
              <span>智能观察天数</span>
              <input type="number" v-model.number="form.lookbackDays" min="3" step="1" />
            </label>
            <percent-field label="低位加仓阈值" v-model="form.dipTriggerPercent" />
            <label class="field">
              <span>低位加仓倍数</span>
              <input type="number" v-model.number="form.dipFactor" min="1" step="0.1" />
            </label>
            <percent-field label="风控减仓比例" v-model="form.riskOffSellPercent" />
          </template>

          <template v-else-if="selectedStrategy === 'dynamic_balance'">
            <percent-field label="目标基金仓位" v-model="form.targetFundPercent" />
            <percent-field label="再平衡偏离阈值" v-model="form.rebalanceThresholdPercent" />
            <label class="field">
              <span>检查频率</span>
              <select v-model="form.balanceFrequency">
                <option value="monthly">每月</option>
                <option value="weekly">每周</option>
                <option value="daily">每日</option>
              </select>
            </label>
          </template>

          <template v-else-if="selectedStrategy === 'two_eight_rotation'">
            <label class="field">
              <span>轮动观察天数</span>
              <input type="number" v-model.number="form.lookbackDays" min="3" step="1" />
            </label>
            <percent-field label="进攻仓位" v-model="form.strongTargetPercent" />
            <percent-field label="防守仓位" v-model="form.weakTargetPercent" />
            <percent-field label="切换动量阈值" v-model="form.switchThresholdPercent" />
          </template>

          <template v-else-if="selectedStrategy === 'buy_hold'">
            <money-field label="买入持有金额" v-model="form.buyHoldAmount" />
          </template>

          <template v-else-if="selectedStrategy === 'kpi_analysis'">
            <money-field label="KPI测算金额" v-model="form.amount" />
            <label class="field">
              <span>测算频率</span>
              <select v-model="form.frequency">
                <option value="monthly">每月</option>
                <option value="weekly">每周</option>
                <option value="daily">每日</option>
              </select>
            </label>
          </template>

          <div class="action-row">
            <button class="primary-btn" @click="runBacktest" :disabled="loading">
              {{ loading ? '计算中...' : '开始回测' }}
            </button>
            <button class="ghost-btn" @click="resetParams" :disabled="loading">重置</button>
          </div>
        </aside>
      </div>
    </template>
  </div>
</template>

<script>
import { ref, computed, watch, onMounted, onUnmounted, nextTick, h } from 'vue'
import * as echarts from 'echarts'
import { backtestAPI } from '../services/api'
import FundSearch from './FundSearch.vue'

const MoneyField = {
  props: ['modelValue', 'label', 'disabled'],
  emits: ['update:modelValue'],
  render() {
    return h('label', { class: 'field' }, [
      h('span', this.label),
      h('div', { class: 'input-unit' }, [
        h('input', {
          type: 'number',
          min: '0',
          step: '100',
          value: this.modelValue,
          disabled: this.disabled,
          onInput: event => this.$emit('update:modelValue', Number(event.target.value))
        }),
        h('b', '元')
      ])
    ])
  }
}

const PercentField = {
  props: ['modelValue', 'label', 'disabled'],
  emits: ['update:modelValue'],
  render() {
    return h('label', { class: 'field' }, [
      h('span', this.label),
      h('div', { class: 'input-unit' }, [
        h('input', {
          type: 'number',
          min: '0',
          step: '0.1',
          value: this.modelValue,
          disabled: this.disabled,
          onInput: event => this.$emit('update:modelValue', Number(event.target.value))
        }),
        h('b', '%')
      ])
    ])
  }
}

export default {
  name: 'FundBacktest',
  components: { FundSearch, MoneyField, PercentField },
  props: {
    fundCode: {
      type: String,
      default: ''
    }
  },
  setup(props) {
    const today = new Date().toISOString().slice(0, 10)
    const threeYearsAgo = new Date(Date.now() - 3 * 365 * 24 * 60 * 60 * 1000).toISOString().slice(0, 10)
    const currentFundCode = ref(props.fundCode || '')
    const currentFundName = ref('')
    const selectedStrategy = ref('target_profit_plan')
    const loading = ref(false)
    const error = ref('')
    const result = ref(null)
    const chartEl = ref(null)
    const chartType = ref('asset')
    const currentPage = ref(1)
    const pageSize = 30
    let chartInstance = null

    const strategyCards = [
      { key: 'target_profit_plan', name: '定盈计划回测', icon: '盈', desc: '跌时吸筹、涨到目标落袋，支持手工、小鱼和大鱼计划。' },
      { key: 'fixed_amount', name: '定额计划回测', icon: '￥', desc: '按固定金额和周期持续买入。' },
      { key: 'double_down', name: '翻倍定投回测', icon: '◎', desc: '按年度资金计划低位定投，达成目标后可等待回落再止盈。' },
      { key: 'grid', name: '网格计划回测', icon: '#', desc: '跌破网格买入，反弹达到阈值卖出。' },
      { key: 'ma_timing', name: '均线定投回测', icon: 'MA', desc: '根据 MA10/MA20/MA60 判断加仓和减仓。' },
      { key: 'trend_timing', name: '趋势定投回测', icon: '↗', desc: '根据近 N 日趋势强弱调整买卖。' },
      { key: 'rocket_plan', name: '火箭计划回测', icon: '↑', desc: '回撤越深，买入金额按档位加速。' },
      { key: 'ai_plan', name: '智能计划回测', icon: 'AI', desc: '结合趋势、均线和回撤做确定性风控。' },
      { key: 'two_eight_rotation', name: '二八轮动回测', icon: '2/8', desc: '强趋势提高仓位，弱趋势切换防守。' },
      { key: 'dynamic_balance', name: '动态平衡回测', icon: '≈', desc: '按目标仓位自动买入或卖出再平衡。' },
      { key: 'buy_hold', name: '收益对比', icon: 'PK', desc: '买入持有基准，用于和主动策略做对比。' },
      { key: 'kpi_analysis', name: 'KPI分析', icon: 'KPI', desc: '按回测指标生成收益、回撤、利用率评估。' },
      { key: 'download', name: '下载', icon: '▦', desc: '导出当前回测的交易记录 CSV。' }
    ]

    const activeStrategy = computed(() => strategyCards.find(item => item.key === selectedStrategy.value))
    const targetPlanAuto = computed(() => form.value.targetPlanType === 'auto_small' || form.value.targetPlanType === 'auto_big')

    const form = ref(defaultForm())

    watch(() => props.fundCode, code => {
      if (code) currentFundCode.value = code
    })

    watch(() => [form.value.capital, form.value.targetPlanType], () => {
      if (targetPlanAuto.value) applyTargetPlanPreset()
    })

    watch(chartType, () => updateChart())
    watch(result, async val => {
      if (val) {
        await nextTick()
        initChart()
      }
    })

    const metricRows = computed(() => {
      if (!result.value) return []
      const s = result.value.summary
      return [
        { label: '持有天数', value: `${s.holding_days || 0} 天` },
        { label: '最大成本', value: `${formatMoney(s.max_cost)} 元` },
        { label: '累计收益', value: `${formatMoney(s.total_return)} 元`, className: returnClass(s.total_return) },
        { label: '平均投入资金', value: `${formatMoney(s.average_invested)} 元` },
        { label: '投入资金收益率', value: formatPercent(s.return_rate), className: returnClass(s.return_rate) },
        { label: '投入资金年化收益率', value: formatPercent(s.annual_return), className: returnClass(s.annual_return) },
        { label: '总计划资金', value: `${formatMoney(s.total_capital)} 元` },
        { label: '计划资金收益率', value: formatPercent(s.plan_return_rate ?? s.return_rate), className: returnClass(s.plan_return_rate ?? s.return_rate) },
        { label: '计划资金年化收益率', value: formatPercent(s.plan_annual_return ?? s.annual_return), className: returnClass(s.plan_annual_return ?? s.annual_return) },
        { label: '最大回撤', value: formatPercent(s.max_drawdown), className: 'negative' },
        { label: '资金平均利用率', value: formatPercent(s.capital_usage_rate) },
        { label: '买入次数', value: `${s.buy_count || 0} 次` },
        { label: '卖出次数', value: `${s.sell_count || 0} 次` },
        { label: '阶段完成次数', value: `${s.completed_cycles || 0} 次` }
      ]
    })

    const paginatedTrades = computed(() => {
      const trades = result.value?.trades || []
      const start = (currentPage.value - 1) * pageSize
      return trades.slice(start, start + pageSize)
    })

    const totalPages = computed(() => {
      const trades = result.value?.trades || []
      return Math.ceil(trades.length / pageSize)
    })

    function defaultForm() {
      return {
        capital: 10000,
        startDate: threeYearsAgo,
        endDate: today,
        feeRate: 0.15,
        frequency: 'monthly',
        monthDay: 1,
        weekday: 0,
        amount: 1000,
        initialAmount: 0,
        baseAmount: 1000,
        dropTriggerPercent: 3,
        multiplier: 2,
        maxMultiplier: 4,
        doubleAnnualCapital: 12000,
        doubleMinIntervalDays: 12,
        doubleFutureYears: 2,
        doubleTargetType: 'double',
        doubleTargetPercent: 20,
        doubleSellDrawdownPercent: 0,
        doubleDynamicDrawdown: false,
        gridStepPercent: 3,
        sellProfitPercent: 5,
        maxConsecutiveSell: 2,
        maDays: 20,
        belowMaFactor: 1.5,
        aboveMaSellPercent: 20,
        lookbackDays: 20,
        trendThresholdPercent: 2,
        downtrendThresholdPercent: 2,
        downtrendSellPercent: 15,
        boostFactor: 1,
        dipTriggerPercent: 4,
        dipFactor: 1.6,
        riskOffSellPercent: 12,
        targetFundPercent: 60,
        rebalanceThresholdPercent: 5,
        balanceFrequency: 'monthly',
        strongTargetPercent: 80,
        weakTargetPercent: 20,
        switchThresholdPercent: 1.5,
        buyHoldAmount: 10000,
        targetPlanType: 'manual',
        profitTargetPercent: 10,
        buyDropPercent: 3,
        targetBuyAmount: 1000,
        buyIncreasePercent: 10,
        lastBuyRiseSellPercent: 6,
        targetMaxConsecutiveSell: 2,
        startRule: 'immediate',
        startPrice: 1,
        minTradeIntervalDays: 5
      }
    }

    function applyTargetPlanPreset() {
      if (form.value.targetPlanType === 'auto_small') {
        form.value.targetBuyAmount = Math.round(form.value.capital / 10)
        form.value.profitTargetPercent = 10
        form.value.buyDropPercent = 3
        form.value.buyIncreasePercent = 10
        form.value.lastBuyRiseSellPercent = 6
        form.value.targetMaxConsecutiveSell = 2
      } else if (form.value.targetPlanType === 'auto_big') {
        form.value.targetBuyAmount = Math.round(form.value.capital / 5)
        form.value.profitTargetPercent = 20
        form.value.buyDropPercent = 5
        form.value.buyIncreasePercent = 20
        form.value.lastBuyRiseSellPercent = 10
        form.value.targetMaxConsecutiveSell = 2
      }
    }

    function handleStrategyCard(item) {
      if (item.key === 'download') {
        exportTrades()
        return
      }
      selectStrategy(item.key)
    }

    function selectStrategy(key) {
      selectedStrategy.value = key
      result.value = null
      error.value = ''
      currentPage.value = 1
    }

    function handleFundSelected(fund) {
      currentFundCode.value = fund.CODE || fund.fund_code || fund.code || ''
      currentFundName.value = fund.NAME || fund.fund_name || fund.name || ''
      result.value = null
      error.value = ''
    }

    function changeFund() {
      currentFundCode.value = ''
      currentFundName.value = ''
      result.value = null
      error.value = ''
    }

    function resetParams() {
      form.value = defaultForm()
      result.value = null
      error.value = ''
      currentPage.value = 1
    }

    function buildParams() {
      const f = form.value
      if (selectedStrategy.value === 'target_profit_plan') {
        return {
          plan_type: f.targetPlanType,
          profit_target_percent: f.profitTargetPercent,
          buy_drop_percent: f.buyDropPercent,
          buy_amount: f.targetBuyAmount,
          buy_increase_percent: f.buyIncreasePercent,
          last_buy_rise_sell_percent: f.lastBuyRiseSellPercent,
          max_consecutive_sell: f.targetMaxConsecutiveSell,
          start_rule: f.startRule,
          start_price: f.startPrice,
          min_trade_interval_days: f.minTradeIntervalDays
        }
      }
      if (selectedStrategy.value === 'fixed_amount') {
        return {
          frequency: f.frequency,
          investment_type: f.frequency,
          investment_day: f.frequency === 'weekly' ? f.weekday : f.monthDay,
          weekday: f.weekday,
          month_day: f.monthDay,
          amount: f.amount,
          initial_amount: f.initialAmount
        }
      }
      if (selectedStrategy.value === 'double_down') {
        return {
          annual_plan_capital: f.doubleAnnualCapital,
          min_trade_interval_days: Math.max(Number(f.doubleMinIntervalDays) || 12, 6),
          future_years: Math.max(Number(f.doubleFutureYears) || 0, 0),
          target_type: f.doubleTargetType,
          target_percent: f.doubleTargetPercent,
          sell_drawdown_percent: f.doubleSellDrawdownPercent,
          dynamic_drawdown: f.doubleDynamicDrawdown,
          multiplier: 2,
          max_multiplier: 2,
          drop_step_percent: 5,
          opportunity_drawdown_percent: 12,
          initial_low_band_percent: 8,
          lot_take_profit_percent: 10,
          restart_after_sell_drop_percent: 3
        }
      }
      if (selectedStrategy.value === 'grid') {
        return {
          base_amount: f.baseAmount,
          grid_step_percent: f.gridStepPercent,
          sell_profit_percent: f.sellProfitPercent,
          max_consecutive_sell: f.maxConsecutiveSell,
          start_condition: 'immediate'
        }
      }
      if (selectedStrategy.value === 'ma_timing') {
        return {
          frequency: 'weekly',
          ma_days: f.maDays,
          base_amount: f.baseAmount,
          below_ma_factor: f.belowMaFactor,
          above_ma_sell_percent: f.aboveMaSellPercent
        }
      }
      if (selectedStrategy.value === 'trend_timing') {
        return {
          frequency: 'weekly',
          base_amount: f.baseAmount,
          lookback_days: f.lookbackDays,
          trend_threshold_percent: f.trendThresholdPercent,
          downtrend_threshold_percent: f.downtrendThresholdPercent,
          downtrend_sell_percent: f.downtrendSellPercent
        }
      }
      if (selectedStrategy.value === 'rocket_plan') {
        return {
          frequency: 'weekly',
          base_amount: f.baseAmount,
          drop_trigger_percent: f.dropTriggerPercent,
          boost_factor: f.boostFactor,
          max_multiplier: f.maxMultiplier
        }
      }
      if (selectedStrategy.value === 'ai_plan') {
        return {
          frequency: 'weekly',
          base_amount: f.baseAmount,
          lookback_days: f.lookbackDays,
          dip_trigger_percent: f.dipTriggerPercent,
          dip_factor: f.dipFactor,
          risk_off_sell_percent: f.riskOffSellPercent
        }
      }
      if (selectedStrategy.value === 'dynamic_balance') {
        return {
          frequency: f.balanceFrequency,
          target_fund_percent: f.targetFundPercent,
          rebalance_threshold_percent: f.rebalanceThresholdPercent
        }
      }
      if (selectedStrategy.value === 'two_eight_rotation') {
        return {
          frequency: 'weekly',
          lookback_days: f.lookbackDays,
          strong_target_percent: f.strongTargetPercent,
          weak_target_percent: f.weakTargetPercent,
          switch_threshold_percent: f.switchThresholdPercent
        }
      }
      if (selectedStrategy.value === 'buy_hold') {
        return {
          amount: f.buyHoldAmount || f.capital
        }
      }
      if (selectedStrategy.value === 'kpi_analysis') {
        return {
          frequency: f.frequency,
          amount: f.amount,
          month_day: f.monthDay,
          weekday: f.weekday
        }
      }
      return {
        frequency: 'weekly',
        base_amount: f.baseAmount,
        lookback_days: f.lookbackDays,
        trend_threshold_percent: f.trendThresholdPercent,
        downtrend_threshold_percent: f.downtrendThresholdPercent,
        downtrend_sell_percent: f.downtrendSellPercent
      }
    }

    function exportTrades() {
      if (!result.value || !result.value.trades?.length) {
        error.value = '请先完成一次回测，再导出交易记录'
        return
      }
      const headers = ['序号', '日期', '类型', '交易金额', '持有份额', '成本', '单位净值', '累计收益', '累计收益率', '阶段状态']
      const rows = result.value.trades.map(trade => [
        trade.index,
        trade.date,
        trade.type_label,
        trade.amount,
        trade.holding_shares,
        trade.cost,
        trade.nav,
        trade.return,
        `${trade.return_rate}%`,
        trade.status
      ])
      const csv = [headers, ...rows]
        .map(row => row.map(cell => `"${String(cell ?? '').replace(/"/g, '""')}"`).join(','))
        .join('\n')
      const blob = new Blob([`\uFEFF${csv}`], { type: 'text/csv;charset=utf-8;' })
      const url = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = url
      link.download = `${currentFundCode.value || 'fund'}-${selectedStrategy.value}-backtest.csv`
      link.style.display = 'none'
      document.body.appendChild(link)
      link.click()
      document.body.removeChild(link)
      window.setTimeout(() => URL.revokeObjectURL(url), 0)
      error.value = ''
    }

    async function runBacktest() {
      if (!currentFundCode.value) {
        error.value = '请先为当前回测方式选择基金'
        return
      }
      if (!form.value.startDate || !form.value.endDate) {
        error.value = '请选择起止日期'
        return
      }
      loading.value = true
      error.value = ''
      result.value = null
      currentPage.value = 1
      try {
        const requestCapital = selectedStrategy.value === 'double_down'
          ? getDoublePlanTotalCapital()
          : form.value.capital
        const response = await backtestAPI.runStrategy({
          fund_code: currentFundCode.value,
          strategy_type: selectedStrategy.value,
          start_date: form.value.startDate,
          end_date: form.value.endDate,
          capital: requestCapital,
          fee_rate: selectedStrategy.value === 'double_down' ? 0 : form.value.feeRate,
          params: buildParams()
        })
        result.value = response.data
      } catch (err) {
        error.value = err.response?.data?.error || err.response?.data?.message || '回测失败，请稍后重试'
      } finally {
        loading.value = false
      }
    }

    function getDoublePlanTotalCapital() {
      const start = new Date(form.value.startDate)
      const end = new Date(form.value.endDate)
      const annual = Math.max(Number(form.value.doubleAnnualCapital) || 0, 0)
      const futureYears = Math.max(Number(form.value.doubleFutureYears) || 0, 0)
      if (!Number.isFinite(start.getTime()) || !Number.isFinite(end.getTime()) || annual <= 0) {
        return form.value.capital
      }
      const diffDays = Math.max((end.getTime() - start.getTime()) / (24 * 60 * 60 * 1000), 0)
      const elapsedYears = Math.max(Math.floor(diffDays / 365.25) + 1, 1)
      return annual * (elapsedYears + futureYears)
    }

    function initChart() {
      if (!chartEl.value || !result.value) return
      if (chartInstance) chartInstance.dispose()
      chartInstance = echarts.init(chartEl.value)
      updateChart()
    }

    function updateChart() {
      if (!chartInstance || !result.value) return
      const timeline = result.value.timeline || []
      const signals = result.value.signals || []
      const dates = timeline.map(item => item.date)
      const signalMap = new Map(signals.map(item => [item.date, item]))
      const buyPoints = []
      const sellPoints = []
      timeline.forEach((item, index) => {
        const signal = signalMap.get(item.date)
        if (signal?.type === 'buy') buyPoints.push({ coord: [index, item.nav], value: '买' })
        if (signal?.type === 'sell') sellPoints.push({ coord: [index, item.nav], value: '卖' })
      })

      const series = [
        {
          name: '单位净值',
          type: 'line',
          data: timeline.map(item => item.nav),
          yAxisIndex: 0,
          smooth: true,
          lineStyle: { color: '#e53935', width: 2 },
          itemStyle: { color: '#e53935' },
          markPoint: {
            symbolSize: 34,
            data: [
              ...buyPoints.map(p => ({ ...p, itemStyle: { color: '#16a34a' } })),
              ...sellPoints.map(p => ({ ...p, itemStyle: { color: '#f59e0b' } }))
            ],
            label: { color: '#fff', fontWeight: 'bold' }
          }
        }
      ]

      if (chartType.value === 'asset') {
        const investedColor = '#64748b'
        const assetColor = '#1677ff'
        series.push(
          {
            name: '累计投入',
            type: 'line',
            data: timeline.map(item => item.invested),
            yAxisIndex: 1,
            lineStyle: { color: investedColor, width: 1.5 },
            itemStyle: { color: investedColor }
          },
          {
            name: '总资产',
            type: 'line',
            data: timeline.map(item => item.total_asset),
            yAxisIndex: 1,
            lineStyle: { color: assetColor, width: 2 },
            itemStyle: { color: assetColor }
          }
        )
      } else {
        const returnColor = '#1677ff'
        series.push({
          name: '累计收益率',
          type: 'line',
          data: timeline.map(item => item.return_rate),
          yAxisIndex: 1,
          lineStyle: { color: returnColor, width: 2 },
          itemStyle: { color: returnColor }
        })
      }

      chartInstance.setOption({
        tooltip: { trigger: 'axis' },
        legend: { top: 8 },
        grid: { left: 48, right: 58, top: 54, bottom: 42 },
        xAxis: { type: 'category', data: dates, axisLabel: { formatter: v => v.slice(5) } },
        yAxis: [
          { type: 'value', name: '净值', scale: true },
          {
            type: 'value',
            name: chartType.value === 'asset' ? '金额' : '收益率',
            axisLabel: { formatter: chartType.value === 'asset' ? v => `${Math.round(v / 1000)}k` : '{value}%' }
          }
        ],
        dataZoom: [{ type: 'inside' }, { height: 18, bottom: 10 }],
        series
      }, true)
    }

    function formatMoney(value) {
      const num = Number(value)
      return Number.isFinite(num) ? num.toFixed(2) : '0.00'
    }

    function formatNumber(value, digits = 2) {
      const num = Number(value)
      return Number.isFinite(num) ? num.toFixed(digits) : '--'
    }

    function formatTradeShares(value) {
      const num = Number(value)
      if (!Number.isFinite(num)) return '--'
      return activeStrategy.value?.key === 'target_profit_plan' ? String(Math.floor(num)) : num.toFixed(4)
    }

    function formatPercent(value) {
      const num = Number(value)
      return Number.isFinite(num) ? `${num.toFixed(2)}%` : '--'
    }

    function returnClass(value) {
      const num = Number(value)
      if (!Number.isFinite(num) || num === 0) return ''
      return num > 0 ? 'positive' : 'negative'
    }

    onMounted(() => {
      window.addEventListener('resize', resizeChart)
    })

    onUnmounted(() => {
      window.removeEventListener('resize', resizeChart)
      if (chartInstance) chartInstance.dispose()
    })

    function resizeChart() {
      if (chartInstance) chartInstance.resize()
    }

    return {
      today,
      currentFundCode,
      currentFundName,
      selectedStrategy,
      activeStrategy,
      strategyCards,
      targetPlanAuto,
      form,
      loading,
      error,
      result,
      chartEl,
      chartType,
      metricRows,
      currentPage,
      paginatedTrades,
      totalPages,
      handleStrategyCard,
      exportTrades,
      applyTargetPlanPreset,
      selectStrategy,
      handleFundSelected,
      changeFund,
      resetParams,
      runBacktest,
      formatMoney,
      formatNumber,
      formatTradeShares,
      formatPercent,
      returnClass
    }
  }
}
</script>

<style scoped>
.fund-backtest {
  background: #f8fafc;
  border-radius: 12px;
  padding: 0;
}

.page-title,
.panel-head,
.selected-fund,
.section-head,
.action-row,
.pagination {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.page-title h2 {
  margin: 0;
  color: #1f2937;
  font-size: 24px;
}

.page-title {
  padding: 18px 20px;
  border-radius: 12px;
  background: linear-gradient(135deg, #1677ff 0%, #0958d9 100%);
  color: #fff;
  box-shadow: 0 8px 22px rgba(22, 119, 255, 0.22);
}

.page-title h2 {
  color: #fff;
}

.page-title p,
.panel-head p {
  margin: 4px 0 0;
  color: #6b7280;
  font-size: 13px;
}

.page-title p {
  color: rgba(255, 255, 255, 0.82);
}

.flow-steps {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  justify-content: flex-end;
}

.flow-steps span {
  padding: 6px 10px;
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.14);
  color: rgba(255, 255, 255, 0.72);
  font-size: 12px;
  font-weight: 600;
}

.flow-steps span.active {
  background: #fff;
  color: #1677ff;
}

.fund-picker {
  margin-top: 16px;
  background: #fff;
  border: 1px solid #e5e7eb;
  border-radius: 12px;
  padding: 18px;
  box-shadow: 0 4px 12px rgba(15, 23, 42, 0.06);
}

.picker-copy {
  margin-bottom: 14px;
}

.picker-copy h3,
.section-head h3 {
  margin: 0;
  color: #1f2937;
  font-size: 17px;
}

.picker-copy p,
.section-head p {
  margin: 5px 0 0;
  color: #6b7280;
  font-size: 13px;
}

.step-kicker,
.strategy-count {
  display: inline-flex;
  align-items: center;
  width: fit-content;
  padding: 3px 8px;
  margin-bottom: 8px;
  border-radius: 999px;
  background: #eff6ff;
  color: #1677ff;
  font-size: 12px;
  font-weight: 700;
}

.selected-fund {
  margin-top: 16px;
  background: #fff;
  border: 1px solid #e5e7eb;
  border-radius: 12px;
  padding: 12px 14px;
  box-shadow: 0 4px 12px rgba(15, 23, 42, 0.05);
}

.selected-fund div {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
  flex-wrap: wrap;
}

.selected-fund span {
  color: #64748b;
}

.selected-fund strong {
  color: #1677ff;
}

.selected-fund em {
  color: #374151;
  font-style: normal;
}

.strategy-section {
  margin-top: 16px;
  background: #fff;
  border: 1px solid #e5e7eb;
  border-radius: 12px;
  padding: 16px;
  box-shadow: 0 4px 12px rgba(15, 23, 42, 0.05);
}

.strategy-board {
  display: grid;
  grid-template-columns: repeat(5, minmax(150px, 1fr));
  gap: 10px;
  margin-top: 14px;
}

.strategy-card {
  position: relative;
  min-height: 104px;
  border: 1px solid #e5e7eb;
  border-radius: 10px;
  background: #fff;
  cursor: pointer;
  color: #1f2937;
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  justify-content: flex-start;
  gap: 7px;
  padding: 13px;
  text-align: left;
  transition: all 0.18s ease;
}

.strategy-card:hover:not(.disabled),
.strategy-card.active {
  border-color: #1677ff;
  background: linear-gradient(180deg, #f7fbff 0%, #fff 100%);
  box-shadow: 0 8px 18px rgba(22, 119, 255, 0.12);
  transform: translateY(-1px);
}

.strategy-card.disabled {
  color: #9ca3af;
  cursor: not-allowed;
}

.strategy-icon {
  width: 34px;
  height: 34px;
  border-radius: 8px;
  display: grid;
  place-items: center;
  color: #1677ff;
  background: #eff6ff;
  font-weight: 800;
  font-size: 13px;
}

.strategy-name {
  font-size: 14px;
  font-weight: 700;
}

.strategy-desc {
  color: #6b7280;
  font-size: 12px;
  line-height: 1.45;
}

.workbench {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 294px;
  gap: 16px;
  margin-top: 16px;
}

.result-pane {
  min-width: 0;
}

.chart-panel,
.stats-panel,
.trades-panel,
.params-panel {
  border: 1px solid #e5e7eb;
  background: #fff;
  border-radius: 12px;
  box-shadow: 0 4px 12px rgba(15, 23, 42, 0.05);
}

.chart-panel {
  position: relative;
  padding: 16px;
}

.panel-head h3,
.stats-panel h3,
.trades-panel h3 {
  margin: 0;
  color: #1f2937;
  font-size: 16px;
}

.chart-tabs {
  display: flex;
  background: #f1f5f9;
  border-radius: 8px;
  padding: 3px;
  flex-shrink: 0;
}

.chart-tabs button {
  border: 0;
  border-radius: 6px;
  background: transparent;
  padding: 6px 12px;
  cursor: pointer;
  color: #64748b;
  font-weight: 600;
}

.chart-tabs button.active {
  background: #1677ff;
  color: #fff;
}

.chart-container {
  height: 420px;
  margin-top: 14px;
}

.empty-chart {
  position: absolute;
  inset: 78px 16px 16px;
  display: grid;
  place-items: center;
  color: #94a3b8;
  background: linear-gradient(#fff, #fff), repeating-linear-gradient(0deg, transparent, transparent 46px, #eef2f7 47px);
}

.error-message {
  margin-top: 14px;
  padding: 12px;
  border: 1px solid #fecaca;
  border-radius: 8px;
  background: #fff1f2;
  color: #dc2626;
}

.stats-panel,
.trades-panel {
  margin-top: 16px;
  padding: 14px;
}

.stats-table,
.trade-table {
  width: 100%;
  border-collapse: collapse;
  margin-top: 12px;
  font-size: 13px;
}

.stats-table td,
.trade-table th,
.trade-table td {
  border-bottom: 1px solid #e5e7eb;
  padding: 9px 10px;
  text-align: left;
  white-space: nowrap;
}

.stats-table tr:first-child td {
  border-top: 0;
}

.stats-table td:first-child {
  width: 36%;
  color: #4b5563;
}

.stats-table td:last-child {
  color: #1f2937;
  font-weight: 600;
}

.trade-table th {
  background: #f8fafc;
  color: #475569;
  font-weight: 600;
}

.trade-table tr.buy td:nth-child(3) {
  color: #16a34a;
  font-weight: 700;
}

.trade-table tr.sell td:nth-child(3) {
  color: #f59e0b;
  font-weight: 700;
}

.table-scroll {
  overflow-x: auto;
}

.params-panel {
  align-self: start;
  position: sticky;
  top: 82px;
  padding: 14px;
}

.side-title {
  margin: -14px -14px 14px;
  padding: 14px;
  border-bottom: 1px solid #e5e7eb;
  background: linear-gradient(180deg, #f8fbff 0%, #fff 100%);
  border-radius: 12px 12px 0 0;
}

.side-title strong {
  display: block;
  color: #1f2937;
  font-size: 15px;
}

.side-title span {
  display: block;
  margin-top: 4px;
  color: #6b7280;
  font-size: 12px;
  line-height: 1.45;
}

.field,
:deep(.field),
.field-row {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-bottom: 12px;
}

.action-row {
  margin-top: 14px;
  justify-content: space-between;
}

.action-row .primary-btn {
  flex: 1;
}

.action-row .ghost-btn {
  min-width: 72px;
}

.field-row {
  display: grid;
  grid-template-columns: 1fr;
}

.field span,
:deep(.field span) {
  color: #1f2937;
  font-size: 13px;
  font-weight: 600;
}

.field input,
:deep(.field input),
.field select,
:deep(.field select) {
  width: 100%;
  height: 36px;
  border: 1px solid #cbd5e1;
  border-radius: 8px;
  padding: 0 10px;
  font-size: 13px;
  background: #fff;
  transition: all 0.18s;
  box-sizing: border-box;
  color: #1f2937;
  font-family: inherit;
}

.field input:focus,
:deep(.field input:focus),
.field select:focus,
:deep(.field select:focus) {
  outline: none;
  border-color: #1677ff;
  box-shadow: 0 0 0 3px rgba(22, 119, 255, 0.1);
}

.field input:disabled,
:deep(.field input:disabled),
.field select:disabled,
:deep(.field select:disabled) {
  background: #f8fafc;
  color: #64748b;
  cursor: not-allowed;
}

.strategy-note {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin: 2px 0 14px;
  padding: 10px 12px;
  border: 1px solid #dbeafe;
  border-radius: 8px;
  background: #eff6ff;
  color: #1e3a8a;
}

.strategy-note strong {
  font-size: 13px;
}

.strategy-note span {
  font-size: 12px;
  line-height: 1.55;
}

.title-note {
  margin: 12px 0 0;
}

.side-title .title-note strong {
  color: #1e3a8a;
  font-size: 13px;
}

.side-title .title-note span {
  margin-top: 0;
  color: #31559a;
  line-height: 1.6;
}

.target-mode {
  display: grid;
  grid-template-columns: 1fr;
  gap: 8px;
  margin-bottom: 16px;
}

.mode-option,
.start-rule label {
  display: flex;
  align-items: center;
  gap: 8px;
  cursor: pointer;
  color: #1f2937;
}

.mode-option {
  min-height: 34px;
  padding: 7px 10px;
  border: 1px solid #dbe3ef;
  border-radius: 8px;
  background: #fff;
  transition: all 0.18s;
}

.mode-option.active {
  border-color: #1677ff;
  background: #f0f7ff;
  color: #0958d9;
  font-weight: 700;
}

.mode-option input,
.start-rule input {
  width: 15px;
  height: 15px;
  accent-color: #1677ff;
  flex: 0 0 auto;
}

.target-field {
  margin-bottom: 16px;
}

.target-field em {
  color: #64748b;
  font-size: 12px;
  font-style: normal;
  line-height: 1.45;
}

.start-rule {
  display: flex;
  flex-direction: column;
  gap: 10px;
  margin: 4px 0 14px;
}

.start-rule strong {
  color: #1f2937;
  font-size: 13px;
}

.start-rule span {
  font-size: 13px;
  font-weight: 600;
}

.double-targets {
  gap: 8px;
}

.double-targets :deep(.field) {
  margin: -4px 0 4px 24px;
}

.double-targets :deep(.field > span) {
  display: none;
}

.check-field {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 0 0 14px;
  color: #1f2937;
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
}

.check-field input {
  width: 15px;
  height: 15px;
  accent-color: #1677ff;
}

.input-unit,
:deep(.input-unit) {
  position: relative;
  width: 100%;
}

.input-unit input,
:deep(.input-unit input) {
  padding-right: 42px;
}

.input-unit b,
:deep(.input-unit b) {
  position: absolute;
  right: 10px;
  top: 50%;
  transform: translateY(-50%);
  color: #475569;
  font-size: 13px;
  font-weight: 500;
  line-height: 1;
  pointer-events: none;
}

.primary-btn,
.ghost-btn,
.pagination button {
  border: 1px solid #1677ff;
  border-radius: 8px;
  padding: 8px 14px;
  cursor: pointer;
  font-weight: 600;
  transition: all 0.18s;
}

.primary-btn {
  background: linear-gradient(135deg, #1677ff 0%, #0958d9 100%);
  color: #fff;
  box-shadow: 0 4px 12px rgba(22, 119, 255, 0.22);
}

.ghost-btn,
.pagination button {
  background: #fff;
  color: #1677ff;
}

.primary-btn:hover:not(:disabled),
.ghost-btn:hover:not(:disabled),
.pagination button:hover:not(:disabled) {
  transform: translateY(-1px);
}

.primary-btn:disabled,
.ghost-btn:disabled,
.pagination button:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}

.positive {
  color: #ef4444 !important;
}

.negative {
  color: #16a34a !important;
}

.pagination {
  justify-content: center;
  margin-top: 12px;
}

@media (max-width: 1200px) {
  .strategy-board {
    grid-template-columns: repeat(4, minmax(96px, 1fr));
  }

  .workbench {
    grid-template-columns: 1fr;
  }

  .params-panel {
    position: static;
  }
}

@media (max-width: 640px) {
  .fund-backtest {
    padding: 12px;
  }

  .page-title,
  .panel-head {
    align-items: flex-start;
    flex-direction: column;
  }

  .strategy-board {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .chart-container {
    height: 320px;
  }
}
</style>
