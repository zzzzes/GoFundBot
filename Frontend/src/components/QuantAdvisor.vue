<template>
  <div class="quant-advisor">
    <!-- 顶部导航标签 -->
    <div class="quant-tabs">
      <button
        v-for="tab in tabs" :key="tab.id"
        class="quant-tab"
        :class="{ active: activeTab === tab.id }"
        @click="activeTab = tab.id"
      >
        {{ tab.icon }} {{ tab.name }}
      </button>
    </div>

    <!-- Tab 1: 多因子打分 -->
    <div v-if="activeTab === 'score'" class="quant-panel">
      <div class="panel-header">
        <h2>📊 基金综合打分</h2>
        <p>收益 · 风险 · 经理 · 稳定性 四维评估，一键输出买入建议和仓位</p>
      </div>

      <div class="score-controls">
        <div class="control-group">
          <label>风险偏好</label>
          <select v-model="scoreParams.risk_profile">
            <option value="conservative">🛡️ 稳健型 — 保本优先</option>
            <option value="balanced" selected>⚖️ 平衡型 — 攻守兼备</option>
            <option value="aggressive">🚀 进取型 — 追求高收益</option>
            <option value="momentum">📈 动量型 — 追趋势</option>
          </select>
        </div>
        <div class="control-group">
          <label>总资金（元）</label>
          <input v-model.number="scoreParams.total_capital" type="number" min="1000" step="10000" />
        </div>
        <div class="control-group">
          <label>基金类型</label>
          <select v-model="scoreParams.fund_type">
            <option value="">全部</option>
            <option value="股票型">股票型</option>
            <option value="混合型">混合型</option>
            <option value="指数型">指数型</option>
            <option value="债券型">债券型</option>
            <option value="QDII">QDII</option>
          </select>
        </div>
        <div class="control-group">
          <label>返回数量</label>
          <input v-model.number="scoreParams.top_n" type="number" min="5" max="50" />
        </div>
        <button class="btn-primary" @click="runScoring" :disabled="scoring">
          {{ scoring ? '⏳ 计算中...' : '🔍 开始打分' }}
        </button>
      </div>

      <!-- 结果 -->
      <div v-if="scoreResult" class="score-results">
        <h3>综合排名 Top{{ scoreResult.data.length }}</h3>
        <div class="score-table-wrapper">
          <table class="score-table">
            <thead>
              <tr>
                <th>#</th>
                <th>基金</th>
                <th>类型</th>
                <th>综合</th>
                <th>收益</th>
                <th>风险</th>
                <th>经理</th>
                <th>稳定</th>
                <th>建议仓位</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(s, i) in scoreResult.data" :key="s.fund_code"
                :class="{ 'top-pick': i < 3 }">
                <td><span class="rank-badge">{{ i + 1 }}</span></td>
                <td class="fund-name-cell">
                  <a href="#" @click.prevent="$emit('view-fund', s.fund_code)">{{ s.fund_name }}</a>
                  <span class="fund-code">{{ s.fund_code }}</span>
                </td>
                <td>{{ s.fund_type }}</td>
                <td><strong :style="{ color: scoreColor(s.composite_score) }">{{ s.composite_score }}</strong></td>
                <td>{{ s.return_score }}</td>
                <td>{{ s.risk_score }}</td>
                <td>{{ s.manager_score }}</td>
                <td>{{ s.stability_score }}</td>
                <td>
                  <span class="position-badge">{{ s.suggested_position_pct }}%</span>
                  <br><small>≈{{ formatMoney(s.suggested_amount) }}</small>
                </td>
                <td><span :class="'op-badge op-' + s.operation">{{ opLabel(s.operation) }}</span></td>
              </tr>
            </tbody>
          </table>
        </div>

        <!-- 仓位分配图 -->
        <div v-if="scoreResult.data.length >= 3" class="allocation-summary">
          <h4>💰 建议仓位分配</h4>
          <div class="allocation-bars">
            <div v-for="s in scoreResult.data.slice(0, 8)" :key="s.fund_code" class="alloc-bar-item">
              <span class="alloc-name" :title="s.fund_name">{{ s.fund_name.slice(0, 8) }}</span>
              <div class="alloc-bar-track">
                <div class="alloc-bar-fill" :style="{ width: s.suggested_position_pct * 4 + 'px', background: scoreColor(s.composite_score) }"></div>
              </div>
              <span class="alloc-pct">{{ s.suggested_position_pct }}%</span>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- Tab 2: 智能买入策略 -->
    <div v-if="activeTab === 'backtest'" class="quant-panel">
      <div class="panel-header">
        <h2>🎯 智能买入策略对比</h2>
        <p>四种策略同时回测，选出最适合你的买入方式</p>
      </div>

      <div class="score-controls">
        <div class="control-group">
          <label>基金代码</label>
          <input v-model="btParams.fund_code" placeholder="如 161725" />
        </div>
        <div class="control-group">
          <label>起始日期</label>
          <input v-model="btParams.start_date" type="date" />
        </div>
        <div class="control-group">
          <label>每月投入（元）</label>
          <input v-model.number="btParams.base_amount" type="number" min="100" step="500" />
        </div>
        <div class="control-group">
          <label>手续费（%）</label>
          <input v-model.number="btParams.fee_rate" type="number" step="0.01" min="0" />
        </div>
        <button class="btn-primary" @click="runBacktest" :disabled="backtesting">
          {{ backtesting ? '⏳ 回测中...' : '📈 对比回测' }}
        </button>
      </div>

      <div v-if="backtestResult" class="backtest-results">
        <div class="strategy-cards">
          <div v-for="(r, key) in backtestResult" :key="key" class="strategy-card">
            <h4>{{ strategyLabel(key) }}</h4>
            <div class="strategy-metrics">
              <div class="metric">
                <span class="metric-label">最终收益率</span>
                <span class="metric-value" :class="r.final_return_rate > 0 ? 'positive' : 'negative'">
                  {{ (r.final_return_rate || 0).toFixed(2) }}%
                </span>
              </div>
              <div class="metric">
                <span class="metric-label">年化收益率</span>
                <span class="metric-value" :class="r.annualized_return > 0 ? 'positive' : 'negative'">
                  {{ (r.annualized_return || 0).toFixed(2) }}%
                </span>
              </div>
              <div class="metric">
                <span class="metric-label">最大回撤</span>
                <span class="metric-value negative">{{ (r.max_drawdown || 0).toFixed(2) }}%</span>
              </div>
              <div class="metric">
                <span class="metric-label">总投入</span>
                <span class="metric-value">{{ formatMoney(r.total_invested) }}</span>
              </div>
              <div class="metric">
                <span class="metric-label">最终市值</span>
                <span class="metric-value">{{ formatMoney(r.final_value) }}</span>
              </div>
              <div class="metric">
                <span class="metric-label">操作次数</span>
                <span class="metric-value">{{ r.investment_count }}</span>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- Tab 3: 卖出信号 -->
    <div v-if="activeTab === 'exit'" class="quant-panel">
      <div class="panel-header">
        <h2>🚨 动态卖出信号</h2>
        <p>五维信号灯，综合判断是否该减仓或清仓</p>
      </div>

      <div class="score-controls">
        <div class="control-group">
          <label>基金代码</label>
          <input v-model="exitCode" placeholder="如 161725" @keyup.enter="checkExitSignals" />
        </div>
        <button class="btn-primary" @click="checkExitSignals" :disabled="exitChecking">
          {{ exitChecking ? '⏳ 检测中...' : '🔍 检测信号' }}
        </button>
      </div>

      <div v-if="exitResult" class="exit-result">
        <div class="exit-summary" :class="'exit-' + exitResult.recommendation">
          <span class="exit-big-emoji">{{ exitEmoji(exitResult.recommendation) }}</span>
          <div>
            <h3>综合建议：{{ exitResult.recommendation }}</h3>
            <p>卖出信号评分：{{ exitResult.exit_score }}/100</p>
          </div>
        </div>

        <div class="signal-list">
          <div v-for="sig in exitResult.signals" :key="sig.name" class="signal-card" :class="'sig-' + sig.level">
            <div class="signal-indicator">
              <span v-if="sig.level === 'red'" class="dot red"></span>
              <span v-else-if="sig.level === 'yellow'" class="dot yellow"></span>
              <span v-else class="dot green"></span>
            </div>
            <div class="signal-content">
              <strong>{{ sig.name }}</strong>
              <div class="signal-score-bar">
                <div class="signal-score-fill" :style="{ width: sig.score + '%', background: levelColor(sig.level) }"></div>
              </div>
              <p>{{ sig.detail }}</p>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- Tab 4: AI 投资顾问 -->
    <div v-if="activeTab === 'advisor'" class="quant-panel">
      <div class="panel-header">
        <h2>🤖 AI 投资顾问</h2>
        <p>告诉我你的情况，AI 帮你完成从选基到卖出的全流程规划</p>
      </div>

      <div class="advisor-form">
        <div class="form-row">
          <div class="control-group">
            <label>💵 可用资金（元）</label>
            <input v-model.number="advisorParams.total_capital" type="number" min="1000" step="10000" />
          </div>
          <div class="control-group">
            <label>🎯 风险偏好</label>
            <select v-model="advisorParams.risk_profile">
              <option value="conservative">稳健型</option>
              <option value="balanced" selected>平衡型</option>
              <option value="aggressive">进取型</option>
            </select>
          </div>
          <div class="control-group">
            <label>⏰ 投资期限</label>
            <select v-model="advisorParams.investment_horizon">
              <option value="short">1年以内</option>
              <option value="medium">1-3年</option>
              <option value="long" selected>3年以上</option>
            </select>
          </div>
        </div>
        <div class="form-row">
          <div class="control-group" style="flex: 3;">
            <label>📝 投资目标</label>
            <input v-model="advisorParams.investment_goal" placeholder="如：为退休准备、攒首付、子女教育金..." />
          </div>
        </div>
        <button class="btn-primary btn-large" @click="runAdvisor" :disabled="advising">
          {{ advising ? '🤔 AI 分析中...' : '🤖 让 AI 帮我分析' }}
        </button>
      </div>

      <div v-if="advisorResult" class="advisor-result">
        <div v-if="advisorResult.ai_analysis" class="ai-report markdown-body">
          <div v-if="advisorResult.ai_analysis.portfolio_plan" class="report-section">
            <h3>📋 投资组合方案</h3>
            <p>{{ advisorResult.ai_analysis.portfolio_plan.summary }}</p>
            <table class="score-table">
              <thead><tr><th>基金</th><th>建议仓位</th><th>理由</th></tr></thead>
              <tbody>
                <tr v-for="f in advisorResult.ai_analysis.portfolio_plan.funds" :key="f.fund_code">
                  <td><strong>{{ f.fund_name }}</strong> ({{ f.fund_code }})</td>
                  <td>{{ f.allocation_pct }}%</td>
                  <td>{{ f.reason }}</td>
                </tr>
              </tbody>
            </table>
          </div>

          <div v-if="advisorResult.ai_analysis.buy_strategy" class="report-section">
            <h3>🎯 买入策略</h3>
            <p style="white-space: pre-line;">{{ advisorResult.ai_analysis.buy_strategy }}</p>
          </div>

          <div v-if="advisorResult.ai_analysis.risk_management" class="report-section">
            <h3>🛡️ 风险管理</h3>
            <p style="white-space: pre-line;">{{ advisorResult.ai_analysis.risk_management }}</p>
          </div>

          <div v-if="advisorResult.ai_analysis.rebalance_rule" class="report-section">
            <h3>🔄 再平衡规则</h3>
            <p style="white-space: pre-line;">{{ advisorResult.ai_analysis.rebalance_rule }}</p>
          </div>

          <div v-if="advisorResult.ai_analysis.learning_plan" class="report-section">
            <h3>📚 新手学习路径</h3>
            <p style="white-space: pre-line;">{{ advisorResult.ai_analysis.learning_plan }}</p>
          </div>
        </div>
        <div v-else class="no-ai">
          <p>⚠️ AI 服务未配置。请设置 DeepSeek API Key 后重试。</p>
          <p style="font-size: 0.9rem; color: var(--secondary);">也可以先查看下面的基金打分表：</p>
          <div class="score-table-wrapper" style="margin-top: 1rem;">
            <table class="score-table">
              <thead><tr><th>#</th><th>基金</th><th>综合分</th><th>建议仓位</th><th>操作</th></tr></thead>
              <tbody>
                <tr v-for="(s, i) in (advisorResult.scores || []).slice(0, 10)" :key="s.fund_code">
                  <td>{{ i + 1 }}</td>
                  <td><strong>{{ s.fund_name }}</strong> ({{ s.fund_code }})</td>
                  <td>{{ s.composite_score }}</td>
                  <td>{{ s.suggested_position_pct }}%</td>
                  <td>{{ opLabel(s.operation) }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </div>

    <!-- 新手指南 -->
    <div class="newbie-guide" v-if="activeTab === 'score'">
      <details>
        <summary>💡 新手怎么看这些指标？</summary>
        <div class="guide-content">
          <p><strong>综合得分</strong>：越高越好。>80 分强烈推荐，65-80 可考虑，50-65 观望，<50 避开</p>
          <p><strong>收益因子</strong>：过去涨得好不好。但过去≠未来，要结合风险看</p>
          <p><strong>风险因子</strong>：跌得狠不狠。夏普比率高+回撤低 = 性价比高</p>
          <p><strong>经理因子</strong>：开车的人怎么样。从业越久、年化回报越高越好</p>
          <p><strong>稳定性因子</strong>：是不是一直好，还是偶尔爆发。排名越稳定越可靠</p>
          <p><strong>建议仓位</strong>：按风险预算算出你应该投多少。波动大的基金少投</p>
        </div>
      </details>
    </div>
  </div>
</template>

<script>
import { ref, reactive } from 'vue'
import api from '../services/api.js'

export default {
  name: 'QuantAdvisor',
  emits: ['view-fund'],
  setup() {
    const activeTab = ref('score')

    const tabs = [
      { id: 'score', name: '打分排名', icon: '📊' },
      { id: 'backtest', name: '买入策略', icon: '🎯' },
      { id: 'exit', name: '卖出信号', icon: '🚨' },
      { id: 'advisor', name: 'AI顾问', icon: '🤖' },
    ]

    // --- Tab 1: 打分 ---
    const scoreParams = reactive({
      risk_profile: 'balanced',
      total_capital: 100000,
      fund_type: '',
      top_n: 20,
    })
    const scoring = ref(false)
    const scoreResult = ref(null)

    const runScoring = async () => {
      scoring.value = true
      try {
        const res = await api.post('/quant/score-funds', {
          risk_profile: scoreParams.risk_profile,
          total_capital: scoreParams.total_capital,
          fund_type: scoreParams.fund_type || undefined,
          top_n: scoreParams.top_n,
        })
        scoreResult.value = res.data
      } catch (e) {
        alert('打分失败: ' + (e.response?.data?.error || e.message))
      } finally {
        scoring.value = false
      }
    }

    // --- Tab 2: 回测 ---
    const btParams = reactive({
      fund_code: '161725',
      start_date: '2020-01-01',
      base_amount: 1000,
      fee_rate: 0.15,
    })
    const backtesting = ref(false)
    const backtestResult = ref(null)

    const runBacktest = async () => {
      backtesting.value = true
      backtestResult.value = null
      const strategies = ['dca', 'value_averaging', 'grid', 'adaptive']
      const results = {}
      try {
        for (const s of strategies) {
          const res = await api.post('/quant/backtest-advanced', {
            fund_code: btParams.fund_code,
            start_date: btParams.start_date,
            strategy: s,
            base_amount: btParams.base_amount,
            fee_rate: btParams.fee_rate,
          })
          results[s] = res.data
        }
        backtestResult.value = results
      } catch (e) {
        alert('回测失败: ' + (e.response?.data?.error || e.message))
      } finally {
        backtesting.value = false
      }
    }

    // --- Tab 3: 卖出信号 ---
    const exitCode = ref('161725')
    const exitChecking = ref(false)
    const exitResult = ref(null)

    const checkExitSignals = async () => {
      if (!exitCode.value.trim()) return
      exitChecking.value = true
      try {
        const res = await api.get(`/quant/exit-signals/${exitCode.value.trim()}`)
        exitResult.value = res.data
      } catch (e) {
        alert('检测失败: ' + (e.response?.data?.error || e.message))
      } finally {
        exitChecking.value = false
      }
    }

    // --- Tab 4: AI 顾问 ---
    const advisorParams = reactive({
      total_capital: 100000,
      risk_profile: 'balanced',
      investment_horizon: 'long',
      investment_goal: '长期资产增值',
    })
    const advising = ref(false)
    const advisorResult = ref(null)

    const runAdvisor = async () => {
      advising.value = true
      try {
        const res = await api.post('/quant/advisor-context', {
          total_capital: advisorParams.total_capital,
          risk_profile: advisorParams.risk_profile,
          investment_horizon: advisorParams.investment_horizon,
          investment_goal: advisorParams.investment_goal,
        })
        advisorResult.value = res.data
      } catch (e) {
        alert('AI分析失败: ' + (e.response?.data?.error || e.message))
      } finally {
        advising.value = false
      }
    }

    // --- Helpers ---
    const scoreColor = (s) => {
      if (s >= 80) return '#22c55e'
      if (s >= 65) return '#3b82f6'
      if (s >= 50) return '#f59e0b'
      return '#ef4444'
    }

    const levelColor = (level) => {
      if (level === 'red') return '#ef4444'
      if (level === 'yellow') return '#f59e0b'
      return '#22c55e'
    }

    const opLabel = (op) => {
      const map = {
        buy_heavy: '🔥 重仓', buy: '✅ 买入', hold: '⏸️ 观望',
        reduce: '⬇️ 减仓', sell: '❌ 卖出'
      }
      return map[op] || op
    }

    const exitEmoji = (rec) => {
      const map = { '清仓': '🚨', '减仓': '⚠️', '观望': '👀', '持有': '✅' }
      return map[rec] || '❓'
    }

    const strategyLabel = (key) => {
      const map = {
        dca: '📅 普通定投', value_averaging: '🎯 价值平均（推荐）',
        grid: '📊 网格交易', adaptive: '🧠 自适应定投'
      }
      return map[key] || key
    }

    const formatMoney = (v) => {
      if (!v) return '0'
      if (v >= 10000) return (v / 10000).toFixed(1) + '万'
      return v.toFixed(0)
    }

    return {
      activeTab, tabs,
      scoreParams, scoring, scoreResult, runScoring,
      btParams, backtesting, backtestResult, runBacktest,
      exitCode, exitChecking, exitResult, checkExitSignals,
      advisorParams, advising, advisorResult, runAdvisor,
      scoreColor, levelColor, opLabel, exitEmoji, strategyLabel, formatMoney,
    }
  }
}
</script>

<style scoped>
.quant-advisor { max-width: 1200px; margin: 0 auto; padding: 1rem; }

.quant-tabs {
  display: flex; gap: 0.5rem; margin-bottom: 1.5rem;
  border-bottom: 2px solid var(--border-color, #e5e7eb); padding-bottom: 0;
}
.quant-tab {
  padding: 0.6rem 1.2rem; border: none; background: none;
  cursor: pointer; font-size: 0.95rem; color: #6b7280;
  border-bottom: 2px solid transparent; margin-bottom: -2px;
  transition: all 0.2s;
}
.quant-tab:hover { color: #374151; }
.quant-tab.active { color: #3b82f6; border-bottom-color: #3b82f6; font-weight: 600; }

.quant-panel { background: #fff; border-radius: 12px; padding: 1.5rem; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }
.panel-header { margin-bottom: 1.2rem; }
.panel-header h2 { margin: 0 0 0.3rem 0; font-size: 1.3rem; }
.panel-header p { margin: 0; color: #6b7280; font-size: 0.9rem; }

.score-controls {
  display: flex; gap: 0.8rem; flex-wrap: wrap; align-items: flex-end;
  margin-bottom: 1.5rem; padding: 1rem; background: #f9fafb; border-radius: 8px;
}
.control-group { display: flex; flex-direction: column; gap: 0.3rem; }
.control-group label { font-size: 0.8rem; color: #6b7280; font-weight: 500; }
.control-group input, .control-group select {
  padding: 0.45rem 0.7rem; border: 1px solid #d1d5db; border-radius: 6px;
  font-size: 0.9rem; min-width: 120px;
}
.btn-primary {
  padding: 0.5rem 1.5rem; background: #3b82f6; color: #fff; border: none;
  border-radius: 8px; cursor: pointer; font-size: 0.9rem; font-weight: 600;
  transition: background 0.2s;
}
.btn-primary:hover:not(:disabled) { background: #2563eb; }
.btn-primary:disabled { opacity: 0.6; cursor: not-allowed; }
.btn-large { padding: 0.8rem 2rem; font-size: 1.05rem; }

/* Score Table */
.score-table-wrapper { overflow-x: auto; margin-top: 1rem; }
.score-table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
.score-table th { background: #f9fafb; padding: 0.6rem 0.5rem; text-align: left; font-weight: 600; border-bottom: 2px solid #e5e7eb; white-space: nowrap; }
.score-table td { padding: 0.5rem; border-bottom: 1px solid #f3f4f6; }
.score-table tr.top-pick { background: #fefce8; }
.rank-badge {
  display: inline-block; width: 24px; height: 24px; line-height: 24px;
  text-align: center; border-radius: 50%; background: #3b82f6; color: #fff;
  font-size: 0.75rem; font-weight: 700;
}
tr.top-pick .rank-badge { background: #f59e0b; }
.fund-name-cell a { color: #3b82f6; text-decoration: none; font-weight: 600; }
.fund-name-cell a:hover { text-decoration: underline; }
.fund-code { display: block; font-size: 0.75rem; color: #9ca3af; }
.position-badge { font-weight: 700; color: #3b82f6; }
.op-badge {
  display: inline-block; padding: 0.15rem 0.5rem; border-radius: 10px;
  font-size: 0.78rem; font-weight: 600;
}
.op-buy_heavy { background: #dcfce7; color: #166534; }
.op-buy { background: #dbeafe; color: #1e40af; }
.op-hold { background: #fef3c7; color: #92400e; }
.op-reduce { background: #fee2e2; color: #991b1b; }
.op-sell { background: #fce7f3; color: #831843; }

/* Allocation */
.allocation-summary { margin-top: 1.5rem; padding: 1rem; background: #f9fafb; border-radius: 8px; }
.allocation-bars { display: flex; flex-direction: column; gap: 0.5rem; }
.alloc-bar-item { display: flex; align-items: center; gap: 0.5rem; }
.alloc-name { width: 80px; font-size: 0.8rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.alloc-bar-track { flex: 1; height: 16px; background: #e5e7eb; border-radius: 8px; overflow: hidden; }
.alloc-bar-fill { height: 100%; border-radius: 8px; transition: width 0.5s; min-width: 4px; }
.alloc-pct { width: 40px; font-size: 0.8rem; font-weight: 600; }

/* Strategy Cards */
.strategy-cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 1rem; margin-top: 1rem; }
.strategy-card { background: #f9fafb; border-radius: 10px; padding: 1rem; border: 1px solid #e5e7eb; }
.strategy-card h4 { margin: 0 0 0.8rem 0; }
.strategy-metrics { display: grid; grid-template-columns: 1fr 1fr; gap: 0.5rem; }
.metric { display: flex; flex-direction: column; }
.metric-label { font-size: 0.75rem; color: #6b7280; }
.metric-value { font-size: 1.1rem; font-weight: 700; }
.positive { color: #16a34a; }
.negative { color: #dc2626; }

/* Exit Signals */
.exit-result { margin-top: 1rem; }
.exit-summary { display: flex; align-items: center; gap: 1rem; padding: 1.2rem; border-radius: 10px; margin-bottom: 1rem; }
.exit-summary h3 { margin: 0; }
.exit-summary p { margin: 0.2rem 0 0 0; color: #6b7280; }
.exit-清仓 { background: #fee2e2; border: 1px solid #fecaca; }
.exit-减仓 { background: #fef3c7; border: 1px solid #fde68a; }
.exit-观望 { background: #f9fafb; border: 1px solid #e5e7eb; }
.exit-持有 { background: #dcfce7; border: 1px solid #bbf7d0; }
.exit-big-emoji { font-size: 2.5rem; }
.signal-list { display: flex; flex-direction: column; gap: 0.8rem; }
.signal-card { display: flex; gap: 1rem; padding: 1rem; border-radius: 8px; border: 1px solid #e5e7eb; }
.signal-card.sig-red { border-left: 4px solid #ef4444; }
.signal-card.sig-yellow { border-left: 4px solid #f59e0b; }
.signal-card.sig-green { border-left: 4px solid #22c55e; }
.dot { display: inline-block; width: 12px; height: 12px; border-radius: 50%; }
.dot.red { background: #ef4444; }
.dot.yellow { background: #f59e0b; }
.dot.green { background: #22c55e; }
.signal-content { flex: 1; }
.signal-content strong { display: block; margin-bottom: 0.3rem; }
.signal-content p { margin: 0.3rem 0 0 0; font-size: 0.85rem; color: #6b7280; }
.signal-score-bar { height: 6px; background: #e5e7eb; border-radius: 3px; margin: 0.3rem 0; overflow: hidden; }
.signal-score-fill { height: 100%; border-radius: 3px; }

/* Advisor */
.advisor-form { margin-bottom: 1.5rem; }
.form-row { display: flex; gap: 1rem; margin-bottom: 1rem; flex-wrap: wrap; }
.report-section { margin-bottom: 1.5rem; padding-bottom: 1rem; border-bottom: 1px solid #e5e7eb; }
.report-section h3 { margin-bottom: 0.5rem; }

/* Newbie Guide */
.newbie-guide { margin-top: 1.5rem; }
.newbie-guide details { background: #fffbeb; border: 1px solid #fde68a; border-radius: 8px; padding: 0.8rem 1rem; }
.newbie-guide summary { font-weight: 600; cursor: pointer; }
.guide-content { margin-top: 0.6rem; font-size: 0.9rem; color: #374151; }
.guide-content p { margin: 0.4rem 0; }
</style>
