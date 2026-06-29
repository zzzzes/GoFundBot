<template>
  <div class="quant-advisor">
    <!-- Tab Bar -->
    <div class="quant-tabs">
      <button v-for="tab in tabs" :key="tab.id" class="quant-tab" :class="{ active: activeTab === tab.id }" @click="activeTab = tab.id">
        {{ tab.icon }} {{ tab.name }}
      </button>
    </div>

    <!-- ============================ Tab 1: AI 决策 ============================ -->
    <div v-if="activeTab === 'decide'" class="panel">
      <div class="panel-hero">
        <h2>🤖 AI 投资决策引擎</h2>
        <p>AI 综合分析所有基金，给出完整买入/持仓/卖出方案。需要 DeepSeek Key。</p>
      </div>

      <div class="input-row">
        <div class="field"><label>💰 可用资金（元）</label><input v-model.number="decideParams.total_capital" type="number" min="1000" step="10000" /></div>
        <div class="field"><label>🎯 风险偏好</label>
          <select v-model="decideParams.risk_profile">
            <option value="conservative">🛡️ 稳健型</option>
            <option value="balanced" selected>⚖️ 平衡型</option>
            <option value="aggressive">🚀 进取型</option>
            <option value="momentum">📈 动量型</option>
          </select></div>
        <div class="field"><label>⏰ 投资期限</label>
          <select v-model="decideParams.investment_horizon">
            <option value="short">1 年以内</option><option value="medium">1–3 年</option><option value="long" selected>3 年以上</option>
          </select></div>
        <div class="field"><label>📝 投资目标</label><input v-model="decideParams.investment_goal" placeholder="攒首付 / 养老 / 子女教育" /></div>
        <button class="btn-go" @click="runDecide" :disabled="deciding">
          {{ deciding ? '🤔 AI 分析中...' : '🤖 让 AI 帮我决策' }}
        </button>
      </div>

      <div v-if="decideError" class="err"><button @click="decideError=''">×</button> {{ decideError }}</div>

      <div v-if="decideResult">
        <div v-if="!decideResult.llm_used" class="notice">
          ⚠️ {{ decideResult.message || 'AI 不可用。配置 DeepSeek Key 后自动启用。' }}
          <details style="margin-top:0.5rem">
            <summary>如何配置？</summary>
            <code>docker exec gofundbot bash -c 'cat &gt; /app/Backend/.env &lt;&lt; EOF
LLM_API_KEY=sk-你的Key
LLM_API_BASE=https://api.deepseek.com/v1
LLM_MODEL=deepseek-chat
EOF'</code>
          </details>
        </div>

        <div v-if="decideResult.ai_analysis" class="ai-plan">
          <div v-if="decideResult.ai_analysis.market_assessment" class="card">
            <h3>📈 市场研判</h3>
            <div class="sentiment-row">
              <span class="sentiment-badge" :style="{ background: sentimentColor(decideResult.ai_analysis.market_assessment.overall_sentiment) }">
                {{ decideResult.ai_analysis.market_assessment.overall_sentiment }}
              </span>
              <span class="score-num">{{ decideResult.ai_analysis.market_assessment.sentiment_score }} / 100</span>
            </div>
            <p>{{ decideResult.ai_analysis.market_assessment.summary }}</p>
          </div>

          <div v-if="decideResult.ai_analysis.portfolio_plan" class="card">
            <h3>📋 投资组合方案</h3>
            <p><strong>建议投入：</strong>{{ fmt(decideResult.ai_analysis.portfolio_plan.total_to_invest) }} 元
               &nbsp;|&nbsp; <strong>留现金：</strong>{{ fmt(decideResult.ai_analysis.portfolio_plan.cash_reserve) }} 元</p>
            <p v-if="decideResult.ai_analysis.portfolio_plan.cash_reserve_reason" class="hint">{{ decideResult.ai_analysis.portfolio_plan.cash_reserve_reason }}</p>
            <div class="table-wrap"><table class="st">
              <thead><tr><th>基金</th><th>操作</th><th>仓位</th><th>金额</th><th>买入方式</th><th>理由 / 风险 / 止损</th></tr></thead>
              <tbody><tr v-for="f in decideResult.ai_analysis.portfolio_plan.funds" :key="f.fund_code">
                <td><strong>{{ f.fund_name }}</strong><br><small>{{ f.fund_code }}</small></td>
                <td><span class="tag" :class="actionClass(f.action)">{{ f.action }}</span></td>
                <td>{{ f.allocation_pct }}%</td><td>{{ fmt(f.allocation_amount) }}</td>
                <td>{{ f.buy_method }}</td>
                <td class="reason">
                  <div v-if="f.buy_reason"><strong>理由:</strong> {{ f.buy_reason }}</div>
                  <div v-if="f.risk_warning" style="color:#dc2626"><strong>风险:</strong> {{ f.risk_warning }}</div>
                  <div v-if="f.stop_loss_condition" style="color:#d97706"><strong>止损:</strong> {{ f.stop_loss_condition }}</div>
                </td>
              </tr></tbody>
            </table></div>
          </div>

          <div v-if="decideResult.ai_analysis.execution_plan" class="card">
            <h3>⚡ 执行计划</h3>
            <div v-if="decideResult.ai_analysis.execution_plan.phase_1"><strong>📅 建仓期</strong><p>{{ decideResult.ai_analysis.execution_plan.phase_1 }}</p></div>
            <div v-if="decideResult.ai_analysis.execution_plan.phase_2"><strong>📅 持有期</strong><p>{{ decideResult.ai_analysis.execution_plan.phase_2 }}</p></div>
            <div v-if="decideResult.ai_analysis.execution_plan.rebalance_rule"><strong>🔄 调仓</strong><p>{{ decideResult.ai_analysis.execution_plan.rebalance_rule }}</p></div>
          </div>

          <div v-if="decideResult.ai_analysis.risk_management" class="card">
            <h3>🛡️ 风险管理</h3>
            <ul v-if="decideResult.ai_analysis.risk_management.blacklist_conditions"><li v-for="bc in decideResult.ai_analysis.risk_management.blacklist_conditions" :key="bc">{{ bc }}</li></ul>
          </div>
          <div v-if="decideResult.ai_analysis.newbie_guide" class="card newbie">
            <h3>📚 新手入门</h3>
            <p v-if="decideResult.ai_analysis.newbie_guide.key_metrics_explained">{{ decideResult.ai_analysis.newbie_guide.key_metrics_explained }}</p>
            <ul v-if="decideResult.ai_analysis.newbie_guide.common_mistakes"><li v-for="cm in decideResult.ai_analysis.newbie_guide.common_mistakes" :key="cm">{{ cm }}</li></ul>
          </div>
        </div>

        <div v-if="decideResult.funds_scored && decideResult.funds_scored.length" class="card" style="margin-top:1rem">
          <h3>📊 量化评分明细（标准化 0-100）</h3>
          <div class="table-wrap"><table class="st">
            <thead><tr><th>#</th><th>基金</th><th>综合</th><th>收益</th><th>风险</th><th>经理</th><th>稳定</th><th>仓位</th></tr></thead>
            <tbody><tr v-for="(s,i) in decideResult.funds_scored" :key="s.fund_code" :class="{ highlight: i<3 }">
              <td><span class="rank-num">{{ i+1 }}</span></td>
              <td>{{ s.fund_name }}<br><small>{{ s.fund_code }}</small></td>
              <td><strong>{{ s.composite_score }}</strong></td>
              <td>{{ s.return_score }}</td><td>{{ s.risk_score }}</td>
              <td>{{ s.manager_score }}</td><td>{{ s.stability_score }}</td>
              <td>{{ s.suggested_position_pct }}%</td>
            </tr></tbody>
          </table></div>
        </div>
      </div>
    </div>

    <!-- ============================ Tab 2: 单基金深度分析 ============================ -->
    <div v-if="activeTab === 'analyze'" class="panel">
      <div class="panel-hero"><h2>🔬 AI 单基金深度诊断</h2><p>AI 从收益/风险/经理/趋势四个维度分析买卖时机</p></div>
      <div class="input-row">
        <div class="field"><label>基金代码</label><input v-model="analyzeCode" placeholder="如 161725" @keyup.enter="runAnalyze" /></div>
        <button class="btn-go" @click="runAnalyze" :disabled="analyzing">{{ analyzing ? '分析中...' : '🔍 诊断' }}</button>
      </div>
      <div v-if="analyzeError" class="err"><button @click="analyzeError=''">×</button> {{ analyzeError }}</div>

      <div v-if="analyzeResult">
        <div v-if="!analyzeResult.llm_used && analyzeResult.message" class="notice">{{ analyzeResult.message }}</div>

        <div v-if="analyzeResult.ai_analysis" class="ai-plan">
          <div class="card">
            <div style="display:flex;align-items:center;gap:1rem;flex-wrap:wrap">
              <span class="tag" :class="actionClass(analyzeResult.ai_analysis.action)" style="font-size:1.05rem;padding:0.4rem 1rem">{{ analyzeResult.ai_analysis.action }}</span>
              <span><strong>置信度 {{ analyzeResult.ai_analysis.confidence }}/100</strong> | 评分 {{ analyzeResult.ai_analysis.score }}/100</span>
            </div>
            <p style="margin-top:0.5rem"><strong>{{ analyzeResult.ai_analysis.summary }}</strong></p>
          </div>
          <div class="card" v-if="analyzeResult.ai_analysis.bull_case?.length"><h4>🐂 看多理由</h4><ul><li v-for="r in analyzeResult.ai_analysis.bull_case" :key="r">{{ r }}</li></ul></div>
          <div class="card" v-if="analyzeResult.ai_analysis.bear_case?.length"><h4>🐻 看空理由</h4><ul><li v-for="r in analyzeResult.ai_analysis.bear_case" :key="r">{{ r }}</li></ul></div>
          <div class="card" v-if="analyzeResult.ai_analysis.key_metrics_analysis">
            <h4>📊 指标分析</h4>
            <div class="metrics-grid">
              <div v-for="(v,k) in analyzeResult.ai_analysis.key_metrics_analysis" :key="k" class="metric-card">
                <strong>{{ k }}:</strong> <span>{{ v }}</span>
              </div>
            </div>
          </div>
          <div class="card" v-if="analyzeResult.ai_analysis.suggested_entry"><h4>🎯 买入建议</h4><p><strong>{{ analyzeResult.ai_analysis.suggested_entry.method }}</strong></p><p>{{ analyzeResult.ai_analysis.suggested_entry.reason }}</p></div>
          <div class="card" v-if="analyzeResult.ai_analysis.suggested_exit"><h4>🛑 卖出纪律</h4><p>止损: {{ analyzeResult.ai_analysis.suggested_exit.stop_loss }}</p><p>止盈: {{ analyzeResult.ai_analysis.suggested_exit.take_profit }}</p></div>
        </div>
      </div>
    </div>

    <!-- ============================ Tab 3: 买入策略 ============================ -->
    <div v-if="activeTab === 'backtest'" class="panel">
      <div class="panel-hero"><h2>🎯 智能买入策略对比</h2><p>四种策略同时回测，自动加载净值数据，AI 解读结果</p></div>
      <div class="input-row">
        <div class="field"><label>基金代码</label><input v-model="bt.code" placeholder="如 161725" /></div>
        <div class="field"><label>起始日期</label><input v-model="bt.start" type="date" /></div>
        <div class="field"><label>每月金额</label><input v-model.number="bt.amount" type="number" min="100" step="500" /></div>
        <button class="btn-go" @click="runBacktest" :disabled="bting">{{ bting ? '回测中...' : '📈 对比回测' }}</button>
      </div>
      <div v-if="btError" class="err"><button @click="btError=''">×</button> {{ btError }}</div>
      <div v-if="btLoadingTip" class="notice">{{ btLoadingTip }}</div>
      <div v-if="btResult" class="grid-2col">
        <div v-for="(r,k) in btResult" :key="k" class="card">
          <h4>{{ strategyLabel(k) }}</h4>
          <div class="metrics">
            <div class="m"><span class="ml">收益率</span><span class="mv" :class="(r.final_return_rate||0)>0?'up':'down'">{{ (r.final_return_rate||0).toFixed(1) }}%</span></div>
            <div class="m"><span class="ml">年化</span><span class="mv">{{ (r.annualized_return||0).toFixed(1) }}%</span></div>
            <div class="m"><span class="ml">最大回撤</span><span class="mv down">{{ (r.max_drawdown||0).toFixed(1) }}%</span></div>
            <div class="m"><span class="ml">投入/市值</span><span class="mv">{{ fmt(r.total_invested) }} / {{ fmt(r.final_value) }}</span></div>
          </div>
          <div v-if="r.ai_interpretation" class="ai-tip">
            <strong>AI 点评:</strong> {{ r.ai_interpretation.explanation || r.ai_interpretation.verdict }}
            <div style="margin-top:0.3rem;font-size:0.78rem;color:#6b7280">
              适合: {{ r.ai_interpretation.best_for }}<br>
              ⚠️ {{ r.ai_interpretation.caveat }}
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- ============================ Tab 4: 卖出信号 ============================ -->
    <div v-if="activeTab === 'exit'" class="panel">
      <div class="panel-hero"><h2>🚨 动态卖出信号</h2><p>五维信号灯 + AI 复核（AI 不可用时自动用规则替代）</p></div>
      <div class="input-row">
        <div class="field"><label>基金代码</label><input v-model="exitCode" placeholder="如 161725" @keyup.enter="runExit" /></div>
        <button class="btn-go" @click="runExit" :disabled="exiting">{{ exiting ? '检测中...' : '🔍 检测' }}</button>
      </div>
      <div v-if="exitError" class="err"><button @click="exitError=''">×</button> {{ exitError }}</div>
      <div v-if="exitResult" class="ai-plan">
        <div class="exit-banner" :class="'exit-'+exitResult.recommendation">
          <span class="big-emoji">{{ exitEmoji(exitResult.recommendation) }}</span>
          <div><h3>{{ exitResult.recommendation }}</h3><p>综合评分 {{ exitResult.exit_score }}/100</p></div>
        </div>
        <div class="signal-cards">
          <div v-for="s in exitResult.signals" :key="s.name" class="sc" :class="'sc-'+s.level"><span class="dot" :class="s.level"></span><div><strong>{{ s.name }}</strong><p>{{ s.detail }}</p></div></div>
        </div>
        <div v-if="exitResult.ai_review" class="card">
          <h4>{{ exitResult.ai_review.action }}</h4>
          <p>{{ exitResult.ai_review.explanation }}</p>
          <small style="color:#6b7280">{{ exitResult.summary }}</small>
        </div>
      </div>
    </div>
  </div>
</template>

<script>
import { ref, reactive } from 'vue'
import api from '../services/api.js'

export default {
  name: 'QuantAdvisor',
  setup() {
    const activeTab = ref('decide')
    const tabs = [
      { id: 'decide', name: 'AI 决策', icon: '🤖' },
      { id: 'analyze', name: '深度分析', icon: '🔬' },
      { id: 'backtest', name: '买入策略', icon: '🎯' },
      { id: 'exit', name: '卖出信号', icon: '🚨' },
    ]

    // ── Tab 1: AI 决策 ──
    const decideParams = reactive({ total_capital: 100000, risk_profile: 'balanced', investment_horizon: 'long', investment_goal: '长期资产增值' })
    const deciding = ref(false); const decideResult = ref(null); const decideError = ref('')
    const runDecide = async () => {
      deciding.value = true; decideError.value = ''; decideResult.value = null
      try { const r = await api.post('/quant/decide', { ...decideParams }); decideResult.value = r.data } catch (e) { decideError.value = e.response?.data?.error || e.message } finally { deciding.value = false }
    }

    // ── Tab 2: 单基金分析 ──
    const analyzeCode = ref('161725'); const analyzing = ref(false); const analyzeResult = ref(null); const analyzeError = ref('')
    const runAnalyze = async () => {
      analyzing.value = true; analyzeError.value = ''; analyzeResult.value = null
      try { const r = await api.get('/quant/analyze/' + analyzeCode.value.trim()); analyzeResult.value = r.data } catch (e) { analyzeError.value = e.response?.data?.error || e.message } finally { analyzing.value = false }
    }

    // ── Tab 3: 买入策略 ──
    const bt = reactive({ code: '161725', start: '2021-01-01', amount: 1000 }); const bting = ref(false); const btResult = ref(null); const btError = ref(''); const btLoadingTip = ref('')
    const runBacktest = async () => {
      bting.value = true; btResult.value = null; btError.value = ''; btLoadingTip.value = ''
      // 先确保基金有净值数据
      try {
        btLoadingTip.value = '正在加载净值数据...'
        await api.get('/fund/' + bt.code.trim() + '?refresh=true')
        btLoadingTip.value = ''
      } catch (e) { btLoadingTip.value = '' }
      const results = {}
      for (const s of ['dca', 'value_averaging', 'grid', 'adaptive']) {
        try {
          const x = await api.post('/quant/backtest-advanced', { fund_code: bt.code, start_date: bt.start, strategy: s, base_amount: bt.amount, fee_rate: 0.15 })
          results[s] = x.data
        } catch (e) { results[s] = { error: e.response?.data?.error || e.message } }
      }
      btResult.value = results; bting.value = false
    }

    // ── Tab 4: 卖出信号 ──
    const exitCode = ref('161725'); const exiting = ref(false); const exitResult = ref(null); const exitError = ref('')
    const runExit = async () => {
      exiting.value = true; exitError.value = ''; exitResult.value = null
      try { const r = await api.get('/quant/exit-signals/' + exitCode.value.trim()); exitResult.value = r.data } catch (e) { exitError.value = e.response?.data?.error || e.message } finally { exiting.value = false }
    }

    const fmt = v => v ? (v >= 10000 ? (v / 10000).toFixed(1) + '万' : Number(v).toFixed(0)) : '0'
    const actionClass = a => ({ '重仓买入': 'buy-h', '强烈买入': 'buy-h', '买入': 'buy', '建议买入': 'buy', '少量配置': 'hold', '持有': 'hold', '观望': 'hold', '减仓': 'sell', '清仓': 'sell', '卖出': 'sell' }[a] || 'hold')
    const sentimentColor = s => ({ '乐观': '#16a34a', '中性偏多': '#65a30d', '中性': '#6b7280', '中性偏空': '#d97706', '谨慎': '#dc2626' }[s] || '#6b7280')
    const strategyLabel = k => ({ dca: '📅 普通定投', value_averaging: '🎯 价值平均', grid: '📊 网格交易', adaptive: '🧠 自适应' }[k] || k)
    const exitEmoji = r => ({ '清仓': '🚨', '减仓': '⚠️', '观望': '👀', '持有': '✅' }[r] || '❓')

    return { activeTab, tabs, decideParams, deciding, decideResult, decideError, runDecide, analyzeCode, analyzing, analyzeResult, analyzeError, runAnalyze, bt, bting, btResult, btError, btLoadingTip, runBacktest, exitCode, exiting, exitResult, exitError, runExit, fmt, actionClass, sentimentColor, strategyLabel, exitEmoji }
  }
}
</script>

<style scoped>
.quant-advisor { max-width: 1100px; margin: 0 auto; padding: 1rem; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; }
.quant-tabs { display: flex; gap: 0.3rem; margin-bottom: 1.2rem; border-bottom: 2px solid #e5e7eb; }
.quant-tab { padding: 0.55rem 1.2rem; border: none; background: none; cursor: pointer; font-size: 0.95rem; color: #6b7280; border-bottom: 2px solid transparent; margin-bottom: -2px; transition: all .2s; }
.quant-tab:hover { color: #374151; } .quant-tab.active { color: #2563eb; border-bottom-color: #2563eb; font-weight: 600; }
.panel { background: #fff; border-radius: 12px; padding: 1.5rem; box-shadow: 0 1px 3px rgba(0,0,0,.06); }
.panel-hero { margin-bottom: 1rem; } .panel-hero h2 { margin: 0 0 0.2rem; font-size: 1.3rem; } .panel-hero p { margin: 0; color: #6b7280; font-size: 0.9rem; }
.input-row { display: flex; gap: 0.7rem; flex-wrap: wrap; align-items: flex-end; margin-bottom: 1.2rem; padding: 0.8rem 1rem; background: #f9fafb; border-radius: 8px; }
.field { display: flex; flex-direction: column; gap: 0.2rem; } .field label { font-size: 0.75rem; color: #6b7280; font-weight: 500; }
.field input, .field select { padding: 0.4rem 0.6rem; border: 1px solid #d1d5db; border-radius: 6px; font-size: 0.88rem; min-width: 100px; }
.btn-go { padding: 0.45rem 1.4rem; background: #2563eb; color: #fff; border: none; border-radius: 8px; cursor: pointer; font-weight: 600; font-size: 0.9rem; white-space: nowrap; }
.btn-go:hover:not(:disabled) { background: #1d4ed8; } .btn-go:disabled { opacity: .6; cursor: not-allowed; }
.notice { padding: 1rem; background: #fef3c7; border-radius: 8px; margin-bottom: 1rem; font-size: 0.88rem; }
.notice code { background: #f3f4f6; padding: 0.15rem 0.4rem; border-radius: 4px; font-size: 0.78rem; white-space: pre-wrap; display: block; margin-top: 0.3rem; }
.err { padding: 0.5rem 0.8rem; background: #fee2e2; border-radius: 6px; margin-bottom: 0.8rem; color: #991b1b; font-size: 0.88rem; display: flex; align-items: center; gap: 0.5rem; }
.err button { background: none; border: none; cursor: pointer; font-size: 1.1rem; color: #991b1b; }
.ai-plan { display: flex; flex-direction: column; gap: 1rem; }
.card { padding: 1rem; background: #fff; border: 1px solid #e5e7eb; border-radius: 10px; } .card h3, .card h4 { margin: 0 0 0.5rem; } .card p { margin: 0.3rem 0; color: #374151; }
.hint { font-size: 0.82rem; color: #6b7280; }
.sentiment-row { display: flex; align-items: center; gap: 0.6rem; margin-bottom: 0.5rem; } .sentiment-badge { padding: 0.2rem 0.8rem; border-radius: 12px; color: #fff; font-weight: 600; font-size: 0.85rem; } .score-num { font-weight: 700; font-size: 1.1rem; }
.table-wrap { overflow-x: auto; }
.st { width: 100%; border-collapse: collapse; font-size: 0.82rem; } .st th { background: #f9fafb; padding: 0.5rem; text-align: left; border-bottom: 2px solid #e5e7eb; white-space: nowrap; } .st td { padding: 0.4rem 0.5rem; border-bottom: 1px solid #f3f4f6; vertical-align: top; } .st tr.highlight { background: #fefce8; }
.reason { max-width: 280px; font-size: 0.78rem; color: #374151; }
.reason div { margin: 0.2rem 0; }
.rank-num { display: inline-block; width: 22px; height: 22px; line-height: 22px; text-align: center; border-radius: 50%; background: #2563eb; color: #fff; font-size: 0.72rem; font-weight: 700; } tr.highlight .rank-num { background: #d97706; }
.tag { display: inline-block; padding: 0.12rem 0.5rem; border-radius: 10px; font-size: 0.75rem; font-weight: 600; white-space: nowrap; } .tag.buy-h { background: #dcfce7; color: #166534; } .tag.buy { background: #dbeafe; color: #1e40af; } .tag.hold { background: #fef3c7; color: #92400e; } .tag.sell { background: #fee2e2; color: #991b1b; }
.grid-2col { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 0.8rem; }
.metrics { display: grid; grid-template-columns: 1fr 1fr; gap: 0.4rem; } .m { display: flex; flex-direction: column; } .ml { font-size: 0.7rem; color: #6b7280; } .mv { font-size: 0.95rem; font-weight: 700; } .up { color: #16a34a; } .down { color: #dc2626; }
.ai-tip { margin-top: 0.6rem; font-size: 0.82rem; color: #2563eb; background: #eff6ff; padding: 0.5rem; border-radius: 6px; line-height: 1.4; }
.exit-banner { display: flex; align-items: center; gap: 1rem; padding: 1rem; border-radius: 10px; margin-bottom: 1rem; } .exit-banner h3 { margin: 0; } .exit-banner p { margin: 0.2rem 0 0; font-size: 0.85rem; } .exit-清仓 { background: #fee2e2; } .exit-减仓 { background: #fef3c7; } .exit-观望 { background: #f9fafb; border: 1px solid #e5e7eb; } .exit-持有 { background: #dcfce7; } .big-emoji { font-size: 2rem; }
.signal-cards { display: flex; flex-direction: column; gap: 0.5rem; } .sc { display: flex; gap: 0.8rem; padding: 0.7rem 1rem; border-radius: 8px; border: 1px solid #e5e7eb; } .sc-red { border-left: 4px solid #ef4444; } .sc-yellow { border-left: 4px solid #d97706; } .sc-green { border-left: 4px solid #16a34a; } .dot { width: 10px; height: 10px; border-radius: 50%; margin-top: 0.3rem; flex-shrink: 0; } .dot.red { background: #ef4444; } .dot.yellow { background: #d97706; } .dot.green { background: #16a34a; } .sc p { margin: 0.2rem 0 0; font-size: 0.82rem; color: #6b7280; }
.card.newbie { background: #fffbeb; border-color: #fde68a; }
.metrics-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 0.5rem; } .metric-card { padding: 0.3rem; }
</style>
