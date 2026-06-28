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
        <p>AI 综合分析所有基金数据，给出完整买入/持仓/卖出方案</p>
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

      <div v-if="decideResult">
        <div v-if="!decideResult.llm_used" class="notice">
          ⚠️ {{ decideResult.message || '未配置 LLM Key。配置 DeepSeek Key 后 AI 将给出完整决策。' }}
        </div>

        <div v-if="decideResult.ai_analysis" class="ai-plan">
          <!-- 市场研判 -->
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
          <!-- 组合方案 -->
          <div v-if="decideResult.ai_analysis.portfolio_plan" class="card">
            <h3>📋 投资组合方案</h3>
            <p><strong>建议投入：</strong>{{ formatMoney(decideResult.ai_analysis.portfolio_plan.total_to_invest) }} 元
               &nbsp;|&nbsp; <strong>保留现金：</strong>{{ formatMoney(decideResult.ai_analysis.portfolio_plan.cash_reserve) }} 元</p>
            <p v-if="decideResult.ai_analysis.portfolio_plan.cash_reserve_reason" class="hint">{{ decideResult.ai_analysis.portfolio_plan.cash_reserve_reason }}</p>
            <div class="table-wrap"><table class="st">
              <thead><tr><th>基金</th><th>操作</th><th>仓位</th><th>金额</th><th>买入方式</th><th>理由</th></tr></thead>
              <tbody><tr v-for="f in decideResult.ai_analysis.portfolio_plan.funds" :key="f.fund_code">
                <td><strong>{{ f.fund_name }}</strong><br><small>{{ f.fund_code }}</small></td>
                <td><span class="tag" :class="actionClass(f.action)">{{ f.action }}</span></td>
                <td>{{ f.allocation_pct }}%</td><td>{{ formatMoney(f.allocation_amount) }}</td>
                <td>{{ f.buy_method }}</td><td class="reason">{{ f.buy_reason }}</td>
              </tr></tbody>
            </table></div>
          </div>
          <!-- 执行计划 -->
          <div v-if="decideResult.ai_analysis.execution_plan" class="card">
            <h3>⚡ 执行计划</h3>
            <div v-if="decideResult.ai_analysis.execution_plan.phase_1"><strong>📅 第一阶段（建仓）</strong><p>{{ decideResult.ai_analysis.execution_plan.phase_1 }}</p></div>
            <div v-if="decideResult.ai_analysis.execution_plan.phase_2"><strong>📅 第二阶段（持有）</strong><p>{{ decideResult.ai_analysis.execution_plan.phase_2 }}</p></div>
            <div v-if="decideResult.ai_analysis.execution_plan.rebalance_rule"><strong>🔄 调仓规则</strong><p>{{ decideResult.ai_analysis.execution_plan.rebalance_rule }}</p></div>
          </div>
          <!-- 风险管理 -->
          <div v-if="decideResult.ai_analysis.risk_management" class="card">
            <h3>🛡️ 风险管理</h3>
            <p v-if="decideResult.ai_analysis.risk_management.max_acceptable_drawdown">最大可接受回撤：<strong>{{ decideResult.ai_analysis.risk_management.max_acceptable_drawdown }}</strong></p>
            <ul v-if="decideResult.ai_analysis.risk_management.blacklist_conditions"><li v-for="bc in decideResult.ai_analysis.risk_management.blacklist_conditions" :key="bc">{{ bc }}</li></ul>
          </div>
          <!-- 新手指南 -->
          <div v-if="decideResult.ai_analysis.newbie_guide" class="card newbie">
            <h3>📚 新手入门</h3>
            <div v-if="decideResult.ai_analysis.newbie_guide.key_metrics_explained"><strong>关键指标</strong><p>{{ decideResult.ai_analysis.newbie_guide.key_metrics_explained }}</p></div>
            <div v-if="decideResult.ai_analysis.newbie_guide.common_mistakes"><strong>常见误区</strong><ul><li v-for="cm in decideResult.ai_analysis.newbie_guide.common_mistakes" :key="cm">{{ cm }}</li></ul></div>
          </div>
        </div>

        <!-- 量化评分表 -->
        <div v-if="decideResult.funds_scored && decideResult.funds_scored.length" class="card" style="margin-top:1rem">
          <h3>📊 量化评分明细</h3>
          <div class="table-wrap"><table class="st">
            <thead><tr><th>#</th><th>基金</th><th>综合</th><th>收益</th><th>风险</th><th>经理</th><th>稳定</th><th>仓位</th><th>评级</th></tr></thead>
            <tbody><tr v-for="(s,i) in decideResult.funds_scored" :key="s.fund_code" :class="{ highlight: i<3 }">
              <td><span class="rank-num">{{ i+1 }}</span></td>
              <td>{{ s.fund_name }}<br><small>{{ s.fund_code }}</small></td>
              <td><strong>{{ s.composite_score }}</strong></td>
              <td>{{ s.return_score }}</td><td>{{ s.risk_score }}</td>
              <td>{{ s.manager_score }}</td><td>{{ s.stability_score }}</td>
              <td>{{ s.suggested_position_pct }}%</td>
              <td><span class="tag" :class="actionClass(opLabel(s.operation))">{{ opLabel(s.operation) }}</span></td>
            </tr></tbody>
          </table></div>
        </div>
      </div>
    </div>

    <!-- ============================ Tab 2: 单基金深度分析 ============================ -->
    <div v-if="activeTab === 'analyze'" class="panel">
      <div class="panel-hero"><h2>🔬 AI 单基金深度诊断</h2><p>输入基金代码，AI 从收益/风险/经理/趋势四个维度深度分析</p></div>
      <div class="input-row">
        <div class="field"><label>基金代码</label><input v-model="analyzeCode" placeholder="如 161725" @keyup.enter="runAnalyze" /></div>
        <button class="btn-go" @click="runAnalyze" :disabled="analyzing">{{ analyzing ? '分析中...' : '🔍 分析' }}</button>
      </div>
      <div v-if="analyzeResult && analyzeResult.ai_analysis" class="ai-plan">
        <div class="card">
          <div style="display:flex;align-items:center;gap:1rem">
            <span class="tag" :class="actionClass(analyzeResult.ai_analysis.action)" style="font-size:1.1rem;padding:0.4rem 1rem">{{ analyzeResult.ai_analysis.action }}</span>
            <div><strong>置信度 {{ analyzeResult.ai_analysis.confidence }}/100</strong> &nbsp;|&nbsp; 评分 {{ analyzeResult.ai_analysis.score }}/100</div>
          </div>
          <p style="margin-top:0.5rem"><strong>{{ analyzeResult.ai_analysis.summary }}</strong></p>
        </div>
        <div class="card" v-if="analyzeResult.ai_analysis.bull_case"><h4>🐂 看多理由</h4><ul><li v-for="r in analyzeResult.ai_analysis.bull_case" :key="r">{{ r }}</li></ul></div>
        <div class="card" v-if="analyzeResult.ai_analysis.bear_case"><h4>🐻 看空理由</h4><ul><li v-for="r in analyzeResult.ai_analysis.bear_case" :key="r">{{ r }}</li></ul></div>
        <div class="card" v-if="analyzeResult.ai_analysis.key_metrics_analysis"><h4>指标分析</h4><div v-for="(v,k) in analyzeResult.ai_analysis.key_metrics_analysis" :key="k" style="margin:0.3rem 0"><strong>{{ k }}:</strong> {{ v }}</div></div>
        <div class="card" v-if="analyzeResult.ai_analysis.suggested_entry"><h4>买入建议</h4><p><strong>方式:</strong> {{ analyzeResult.ai_analysis.suggested_entry.method }}</p><p>{{ analyzeResult.ai_analysis.suggested_entry.reason }}</p></div>
        <div class="card" v-if="analyzeResult.ai_analysis.suggested_exit"><h4>卖出纪律</h4><p>止损: {{ analyzeResult.ai_analysis.suggested_exit.stop_loss_price_or_pct }}</p><p>止盈: {{ analyzeResult.ai_analysis.suggested_exit.take_profit_price_or_pct }}</p></div>
      </div>
    </div>

    <!-- ============================ Tab 3: 买入策略 ============================ -->
    <div v-if="activeTab === 'backtest'" class="panel">
      <div class="panel-hero"><h2>🎯 智能买入策略对比</h2><p>四种策略同时回测对比，AI 解读结果</p></div>
      <div class="input-row">
        <div class="field"><label>基金代码</label><input v-model="bt.code" placeholder="如 161725" /></div>
        <div class="field"><label>起始日期</label><input v-model="bt.start" type="date" /></div>
        <div class="field"><label>每月金额</label><input v-model.number="bt.amount" type="number" min="100" step="500" /></div>
        <button class="btn-go" @click="runBacktest" :disabled="bting">{{ bting ? '回测中...' : '📈 对比回测' }}</button>
      </div>
      <div v-if="btResult" class="grid-2col">
        <div v-for="(r,k) in btResult" :key="k" class="card">
          <h4>{{ strategyLabel(k) }}</h4>
          <div class="metrics">
            <div class="m"><span class="ml">收益率</span><span class="mv" :class="(r.final_return_rate||0)>0?'up':'down'">{{ (r.final_return_rate||0).toFixed(1) }}%</span></div>
            <div class="m"><span class="ml">年化</span><span class="mv">{{ (r.annualized_return||0).toFixed(1) }}%</span></div>
            <div class="m"><span class="ml">最大回撤</span><span class="mv down">{{ (r.max_drawdown||0).toFixed(1) }}%</span></div>
            <div class="m"><span class="ml">投入/市值</span><span class="mv">{{ formatMoney(r.total_invested) }} / {{ formatMoney(r.final_value) }}</span></div>
          </div>
          <div v-if="r.ai_interpretation" class="ai-tip">{{ r.ai_interpretation.explanation || r.ai_interpretation.verdict }}</div>
        </div>
      </div>
    </div>

    <!-- ============================ Tab 4: 卖出信号 ============================ -->
    <div v-if="activeTab === 'exit'" class="panel">
      <div class="panel-hero"><h2>🚨 动态卖出信号</h2><p>五维信号灯 + AI 复核，综合判断</p></div>
      <div class="input-row">
        <div class="field"><label>基金代码</label><input v-model="exitCode" placeholder="如 161725" @keyup.enter="runExit" /></div>
        <button class="btn-go" @click="runExit" :disabled="exiting">{{ exiting ? '检测中...' : '🔍 检测' }}</button>
      </div>
      <div v-if="exitResult" class="ai-plan">
        <div class="exit-banner" :class="'exit-'+exitResult.recommendation">
          <span class="big-emoji">{{ exitEmoji(exitResult.recommendation) }}</span>
          <div><h3>{{ exitResult.recommendation }}</h3><p>评分 {{ exitResult.exit_score }}/100 &nbsp;|&nbsp; {{ exitResult.summary }}</p></div>
        </div>
        <div class="signal-cards">
          <div v-for="s in exitResult.signals" :key="s.name" class="sc" :class="'sc-'+s.level"><span class="dot" :class="s.level"></span><div><strong>{{ s.name }}</strong><p>{{ s.detail }}</p></div></div>
        </div>
        <div v-if="exitResult.ai_review" class="card"><h4>🤖 AI 复核</h4><p><strong>{{ exitResult.ai_review.action }}</strong> — {{ exitResult.ai_review.explanation }}</p></div>
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

    // Tab 1
    const decideParams = reactive({ total_capital: 100000, risk_profile: 'balanced', investment_horizon: 'long', investment_goal: '长期资产增值' })
    const deciding = ref(false); const decideResult = ref(null)
    const runDecide = async () => { deciding.value = true; try { const r = await api.post('/quant/decide', { ...decideParams }); decideResult.value = r.data } catch(e) { alert('AI决策失败: '+(e.response?.data?.error||e.message)) } finally { deciding.value = false } }

    // Tab 2
    const analyzeCode = ref('161725'); const analyzing = ref(false); const analyzeResult = ref(null)
    const runAnalyze = async () => { analyzing.value = true; try { const r = await api.get('/quant/analyze/'+analyzeCode.value.trim()); analyzeResult.value = r.data } catch(e) { alert('分析失败: '+(e.response?.data?.error||e.message)) } finally { analyzing.value = false } }

    // Tab 3
    const bt = reactive({ code: '161725', start: '2020-01-01', amount: 1000 }); const bting = ref(false); const btResult = ref(null)
    const runBacktest = async () => { bting.value = true; btResult.value = null; const r = {}; for (const s of ['dca','value_averaging','grid','adaptive']) { try { const x = await api.post('/quant/backtest-advanced',{ fund_code:bt.code, start_date:bt.start, strategy:s, base_amount:bt.amount, fee_rate:0.15 }); r[s]=x.data } catch(e) { r[s]={ error:e.message } } } btResult.value = r; bting.value = false }

    // Tab 4
    const exitCode = ref('161725'); const exiting = ref(false); const exitResult = ref(null)
    const runExit = async () => { exiting.value = true; try { const r = await api.get('/quant/exit-signals/'+exitCode.value.trim()); exitResult.value = r.data } catch(e) { alert('检测失败: '+(e.response?.data?.error||e.message)) } finally { exiting.value = false } }

    const formatMoney = v => v ? (v>=10000 ? (v/10000).toFixed(1)+'万' : v.toFixed(0)) : '0'
    const opLabel = op => ({ buy_heavy:'重仓买入', buy:'建议买入', hold:'持有观望', reduce:'减仓', sell:'卖出' }[op]||op)
    const actionClass = a => ({ '重仓买入':'buy-h','强烈买入':'buy-h','买入':'buy','建议买入':'buy','少量配置':'hold','持有':'hold','观望':'hold','持有观望':'hold','减仓':'sell','清仓':'sell','卖出':'sell' }[a]||'hold')
    const sentimentColor = s => ({ '乐观':'#16a34a','中性偏多':'#65a30d','中性':'#6b7280','中性偏空':'#d97706','谨慎':'#dc2626' }[s]||'#6b7280')
    const strategyLabel = k => ({ dca:'📅 普通定投', value_averaging:'🎯 价值平均', grid:'📊 网格交易', adaptive:'🧠 自适应' }[k]||k)
    const exitEmoji = r => ({ '清仓':'🚨','减仓':'⚠️','观望':'👀','持有':'✅' }[r]||'❓')

    return { activeTab,tabs, decideParams,deciding,decideResult,runDecide, analyzeCode,analyzing,analyzeResult,runAnalyze, bt,bting,btResult,runBacktest, exitCode,exiting,exitResult,runExit, formatMoney,opLabel,actionClass,sentimentColor,strategyLabel,exitEmoji }
  }
}
</script>

<style scoped>
.quant-advisor { max-width: 1100px; margin: 0 auto; padding: 1rem; font-family: -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif; }
.quant-tabs { display: flex; gap: 0.3rem; margin-bottom: 1.2rem; border-bottom: 2px solid #e5e7eb; }
.quant-tab { padding: 0.55rem 1.2rem; border: none; background: none; cursor: pointer; font-size: 0.95rem; color: #6b7280; border-bottom: 2px solid transparent; margin-bottom: -2px; transition: all .2s; }
.quant-tab:hover { color: #374151; } .quant-tab.active { color: #3b82f6; border-bottom-color: #3b82f6; font-weight: 600; }
.panel { background: #fff; border-radius: 12px; padding: 1.5rem; box-shadow: 0 1px 3px rgba(0,0,0,.06); }
.panel-hero { margin-bottom: 1rem; } .panel-hero h2 { margin: 0 0 0.2rem; font-size: 1.3rem; } .panel-hero p { margin: 0; color: #6b7280; font-size: 0.9rem; }
.input-row { display: flex; gap: 0.7rem; flex-wrap: wrap; align-items: flex-end; margin-bottom: 1.2rem; padding: 0.8rem 1rem; background: #f9fafb; border-radius: 8px; }
.field { display: flex; flex-direction: column; gap: 0.2rem; } .field label { font-size: 0.75rem; color: #6b7280; font-weight: 500; }
.field input, .field select { padding: 0.4rem 0.6rem; border: 1px solid #d1d5db; border-radius: 6px; font-size: 0.88rem; min-width: 100px; }
.btn-go { padding: 0.45rem 1.4rem; background: #3b82f6; color: #fff; border: none; border-radius: 8px; cursor: pointer; font-weight: 600; font-size: 0.9rem; white-space: nowrap; }
.btn-go:hover:not(:disabled) { background: #2563eb; } .btn-go:disabled { opacity: .6; cursor: not-allowed; }
.notice { padding: 1rem; background: #fef3c7; border-radius: 8px; margin-bottom: 1rem; font-size: 0.9rem; }
.ai-plan { display: flex; flex-direction: column; gap: 1rem; }
.card { padding: 1rem; background: #fff; border: 1px solid #e5e7eb; border-radius: 10px; } .card h3, .card h4 { margin: 0 0 0.5rem; } .card p { margin: 0.3rem 0; color: #374151; }
.hint { font-size: 0.82rem; color: #6b7280; }
.sentiment-row { display: flex; align-items: center; gap: 0.6rem; margin-bottom: 0.5rem; } .sentiment-badge { padding: 0.2rem 0.8rem; border-radius: 12px; color: #fff; font-weight: 600; font-size: 0.85rem; } .score-num { font-weight: 700; font-size: 1.1rem; }
.table-wrap { overflow-x: auto; }
.st { width: 100%; border-collapse: collapse; font-size: 0.82rem; } .st th { background: #f9fafb; padding: 0.5rem; text-align: left; border-bottom: 2px solid #e5e7eb; white-space: nowrap; } .st td { padding: 0.4rem 0.5rem; border-bottom: 1px solid #f3f4f6; } .st tr.highlight { background: #fefce8; }
.reason { max-width: 200px; font-size: 0.78rem; color: #6b7280; }
.rank-num { display: inline-block; width: 22px; height: 22px; line-height: 22px; text-align: center; border-radius: 50%; background: #3b82f6; color: #fff; font-size: 0.72rem; font-weight: 700; } tr.highlight .rank-num { background: #f59e0b; }
.tag { display: inline-block; padding: 0.12rem 0.5rem; border-radius: 10px; font-size: 0.75rem; font-weight: 600; } .tag.buy-h { background: #dcfce7; color: #166534; } .tag.buy { background: #dbeafe; color: #1e40af; } .tag.hold { background: #fef3c7; color: #92400e; } .tag.sell { background: #fee2e2; color: #991b1b; }
.grid-2col { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 0.8rem; }
.metrics { display: grid; grid-template-columns: 1fr 1fr; gap: 0.4rem; } .m { display: flex; flex-direction: column; } .ml { font-size: 0.7rem; color: #6b7280; } .mv { font-size: 0.95rem; font-weight: 700; } .up { color: #16a34a; } .down { color: #dc2626; }
.ai-tip { margin-top: 0.6rem; font-size: 0.82rem; color: #3b82f6; background: #eff6ff; padding: 0.5rem; border-radius: 6px; }
.exit-banner { display: flex; align-items: center; gap: 1rem; padding: 1rem; border-radius: 10px; margin-bottom: 1rem; } .exit-banner h3 { margin: 0; } .exit-banner p { margin: 0.2rem 0 0; font-size: 0.85rem; } .exit-清仓 { background: #fee2e2; } .exit-减仓 { background: #fef3c7; } .exit-观望 { background: #f9fafb; border: 1px solid #e5e7eb; } .exit-持有 { background: #dcfce7; } .big-emoji { font-size: 2rem; }
.signal-cards { display: flex; flex-direction: column; gap: 0.5rem; } .sc { display: flex; gap: 0.8rem; padding: 0.7rem 1rem; border-radius: 8px; border: 1px solid #e5e7eb; } .sc-red { border-left: 4px solid #ef4444; } .sc-yellow { border-left: 4px solid #f59e0b; } .sc-green { border-left: 4px solid #22c55e; } .dot { width: 10px; height: 10px; border-radius: 50%; margin-top: 0.3rem; flex-shrink: 0; } .dot.red { background: #ef4444; } .dot.yellow { background: #f59e0b; } .dot.green { background: #22c55e; } .sc p { margin: 0.2rem 0 0; font-size: 0.82rem; color: #6b7280; }
.card.newbie { background: #fffbeb; border-color: #fde68a; }
</style>
