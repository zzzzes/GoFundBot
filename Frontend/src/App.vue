<!-- src/App.vue -->
<template>
  <div id="app">
    <header class="app-header">
      <div class="header-content">
        <div class="header-left">
          <h1>GoFundBot</h1>
          <p>智能基金分析 · 实时市场追踪</p>
        </div>
        <!-- 顶部搜索框 -->
        <div class="header-search">
          <FundSearch @fund-selected="handleHeaderSearch" :compact="true" />
        </div>
        <!-- 模式切换 -->
        <div class="header-right">
          <button
            v-if="canGoBack"
            class="back-btn"
            @click="goBack"
            title="返回上一步"
          >
            ← 返回
          </button>
          <div class="mode-switch">
            <button 
              class="mode-btn" 
              :class="{ active: viewMode === 'dashboard' }"
              @click="resetToDashboard"
            >
              🏠 市场大盘
            </button>
            <button 
              class="mode-btn" 
              :class="{ active: viewMode === 'screening' }"
              @click="navigateToMode('screening')"
            >
              🔍 基金筛选
            </button>
            <button 
              class="mode-btn" 
              :class="{ active: viewMode === 'backtest' }"
              @click="navigateToMode('backtest')"
            >
              💰 定投回测
            </button>
            <button 
              class="mode-btn" 
              :class="{ active: viewMode === 'portfolio' }"
              @click="navigateToMode('portfolio')"
            >
              📊 估值与持仓
            </button>
            <button 
              class="mode-btn" 
              :class="{ active: viewMode === 'research' }"
              @click="navigateToMode('research')"
            >
              投研看板
            </button>
            <button
              class="mode-btn mode-btn-ai"
              :class="{ active: viewMode === 'quant' }"
              @click="navigateToMode('quant')"
            >
              🤖 量化决策
            </button>
          </div>
        </div>
      </div>
    </header>
    
    <main class="app-main">
      <!-- 市场大盘模式 -->
      <div v-if="viewMode === 'dashboard'" class="dashboard-layout" :class="{ 'full-content': showFullContent }">
        <!-- 左侧：自选列表 -->
        <aside class="dashboard-sidebar">
          <FundWatchlist 
            @view-fund="handleDashboardFundView" 
            @add-to-compare="handleAddToCompare"
            :compareMode="compareMode"
            :compareFunds="compareFunds"
            :showCompareToggle="true"
            @toggle-compare="toggleCompareMode"
          />
        </aside>
        
        <!-- 中间：核心内容 -->
        <div class="dashboard-main">
          <!-- 基金对比页面（选中多只基金时显示） -->
          <FundComparison 
            v-if="compareMode && compareFunds.length >= 2" 
            :compareFunds="compareFunds"
            @remove-fund="handleRemoveFromCompare"
            @clear-funds="handleClearCompare"
          />
          <!-- 基金详情（选中时显示） -->
          <FundDetail v-else-if="selectedFundCode && !compareMode" :fundCode="selectedFundCode" @navigate-to-fund="handleFundSelected" />
          <!-- 市场指数 + 金价 -->
          <MarketOverview 
            v-else
            :showGoldHistory="true" 
            :showSSE30Min="true"
          />
        </div>
        
        <!-- 右侧：快讯 + 板块（显示详情/对比时隐藏） -->
        <aside class="dashboard-right" v-if="!showFullContent">
          <FlashNews :count="30" :refreshInterval="30000" />
          <SectorRank :limit="90" />
        </aside>
      </div>

      <!-- 其他模式 -->
      <div v-else class="main-layout">
        <!-- 左侧：自选列表 (筛选和估值持仓看板模式不显示) -->
        <aside class="sidebar-left" v-if="viewMode !== 'screening' && viewMode !== 'portfolio' && viewMode !== 'research'">
          <FundWatchlist 
            @view-fund="handleFundSelected" 
            @add-to-compare="handleAddToCompare"
            :compareMode="compareMode"
            :compareFunds="compareFunds"
            :showCompareToggle="true"
            @toggle-compare="toggleCompareMode"
          />
        </aside>
        
        <!-- 右侧：根据模式显示不同内容 -->
        <div class="content-area" :class="{ 'full-width': viewMode === 'screening' || viewMode === 'portfolio' || viewMode === 'research' }">
          <!-- 筛选模式 -->
          <template v-if="viewMode === 'screening'">
            <FundScreening 
              @view-fund="handleScreeningFundView"
              @add-to-compare="handleAddToCompare"
            />
          </template>

          <!-- 估值与持仓一体化看板 -->
          <template v-else-if="viewMode === 'portfolio'">
            <FundRealtime @view-detail="handleRealtimeFundView" />
          </template>

          <!-- 投研看板 -->
          <template v-else-if="viewMode === 'research'">
            <ResearchDashboard @view-fund="handleResearchFundView" />
          </template>

          <!-- 量化决策 -->
          <template v-else-if="viewMode === 'quant'">
            <QuantAdvisor @view-fund="handleFundSelected" />
          </template>
          
          <!-- 回测模式 -->
          <template v-else-if="viewMode === 'backtest'">
            <FundBacktest 
              :fundCode="selectedFundCode"
            />
          </template>

          <!-- 详情模式 -->
          <template v-else>
            <FundSearch @fund-selected="handleFundSelected" />
            <FundDetail v-if="selectedFundCode" :fundCode="selectedFundCode" @navigate-to-fund="handleFundSelected" />
            <div v-else class="welcome-container">
              <div class="welcome-icon">🔍</div>
              <h3>搜索基金开始分析</h3>
              <p>在上方搜索框输入基金代码或名称</p>
            </div>
          </template>
        </div>
      </div>
    </main>
    
    <footer class="app-footer">
      <p>数据来源：天天基金 / 东方财富 / 百度股市通 | 更新时间：{{ currentTime }}</p>
    </footer>
  </div>
</template>

<script>
import { ref, computed, onMounted } from 'vue'
import FundSearch from './components/FundSearch.vue'
import FundDetail from './components/FundDetail.vue'
import FundWatchlist from './components/FundWatchlist.vue'
import FundComparison from './components/FundComparison.vue'
import FundScreening from './components/FundScreening.vue'
import FundBacktest from './components/FundBacktest.vue'
import FundRealtime from './components/FundRealtime.vue'
import ResearchDashboard from './components/ResearchDashboard.vue'
import QuantAdvisor from './components/QuantAdvisor.vue'
import MarketOverview from './components/MarketOverview.vue'
import FlashNews from './components/FlashNews.vue'
import SectorRank from './components/SectorRank.vue'

export default {
  name: 'App',
  components: {
    FundSearch,
    FundDetail,
    FundWatchlist,
    FundComparison,
    FundScreening,
    FundBacktest,
    FundRealtime,
    ResearchDashboard,
    MarketOverview,
    FlashNews,
    SectorRank,
    QuantAdvisor
  },
  setup() {
    const selectedFundCode = ref('')
    const currentTime = ref('')
    const viewMode = ref('dashboard') // 默认显示市场大盘
    const compareFunds = ref([]) // 用于对比的基金列表
    const compareMode = ref(false) // 是否处于对比模式
    const navStack = ref([])
    
    // 是否显示全宽内容（详情页或对比页时隐藏右侧栏）
    const showFullContent = computed(() => {
      return (compareMode.value && compareFunds.value.length >= 2) || 
             (selectedFundCode.value && !compareMode.value)
    })

    const canGoBack = computed(() => navStack.value.length > 0)

    const snapshotState = () => ({
      selectedFundCode: selectedFundCode.value,
      viewMode: viewMode.value,
      compareMode: compareMode.value,
      compareFunds: compareFunds.value.map(fund => ({ ...fund }))
    })

    const sameState = (a, b) => JSON.stringify(a) === JSON.stringify(b)

    const pushCurrentState = () => {
      const current = snapshotState()
      const last = navStack.value[navStack.value.length - 1]
      if (!last || !sameState(last, current)) {
        navStack.value.push(current)
        if (navStack.value.length > 30) navStack.value.shift()
      }
    }

    const restoreState = (state) => {
      selectedFundCode.value = state.selectedFundCode || ''
      viewMode.value = state.viewMode || 'dashboard'
      compareMode.value = !!state.compareMode
      compareFunds.value = Array.isArray(state.compareFunds)
        ? state.compareFunds.map(fund => ({ ...fund }))
        : []
    }

    const goBack = () => {
      const previous = navStack.value.pop()
      if (previous) restoreState(previous)
    }

    const normalizeFundCode = (fundOrCode) => {
      if (fundOrCode && typeof fundOrCode === 'object') {
        return fundOrCode.CODE || fundOrCode.fund_code || fundOrCode.code || ''
      }
      return fundOrCode || ''
    }

    const navigateToMode = (mode) => {
      if (viewMode.value === mode && !selectedFundCode.value && !compareMode.value) return
      pushCurrentState()
      viewMode.value = mode
      compareMode.value = false
      compareFunds.value = []
    }
    
    const handleFundSelected = (fundOrCode) => {
      if (compareMode.value) return // 对比模式下不切换基金
      const nextCode = normalizeFundCode(fundOrCode)
      if (nextCode && nextCode !== selectedFundCode.value) pushCurrentState()
      selectedFundCode.value = nextCode
    }
    
    // 顶部搜索框选中基金
    const handleHeaderSearch = (fundOrCode) => {
      pushCurrentState()
      compareMode.value = false // 退出对比模式
      viewMode.value = 'dashboard' // 切换到市场大盘
      selectedFundCode.value = normalizeFundCode(fundOrCode)
    }
    
    // 从仪表盘/自选点击基金
    const handleDashboardFundView = (fundOrCode) => {
      if (compareMode.value) return // 对比模式下不切换
      handleFundSelected(fundOrCode)
    }
    
    // 切换对比模式
    const toggleCompareMode = () => {
      pushCurrentState()
      compareMode.value = !compareMode.value
      if (!compareMode.value) {
        // 退出对比模式时清空对比列表
        compareFunds.value = []
      }
    }
    
    // 从筛选页面查看基金详情
    const handleScreeningFundView = (fundCode) => {
      pushCurrentState()
      selectedFundCode.value = fundCode
      viewMode.value = 'dashboard'
    }

    // 从估值卡片点击后跳转到基金详情（大盘页布局）
    const handleRealtimeFundView = (fundOrCode) => {
      pushCurrentState()
      compareMode.value = false
      viewMode.value = 'dashboard'
      selectedFundCode.value = normalizeFundCode(fundOrCode)
    }

    const handleResearchFundView = (fundOrCode) => {
      pushCurrentState()
      compareMode.value = false
      viewMode.value = 'dashboard'
      selectedFundCode.value = normalizeFundCode(fundOrCode)
    }
    
    // 添加基金到对比列表
    const handleAddToCompare = (fund) => {
      // 最多5只基金
      if (compareFunds.value.length >= 5) {
        alert('最多只能对比5只基金')
        return
      }
      // 检查是否已存在
      if (compareFunds.value.some(f => f.code === fund.code)) {
        // 如果已存在则移除
        compareFunds.value = compareFunds.value.filter(f => f.code !== fund.code)
        return
      }
      compareFunds.value.push({
        code: fund.code,
        name: fund.name
      })
    }
    
    // 从对比列表移除基金
    const handleRemoveFromCompare = (fundCode) => {
      compareFunds.value = compareFunds.value.filter(f => f.code !== fundCode)
    }
    
    // 清空对比列表
    const handleClearCompare = () => {
      compareFunds.value = []
    }
    
    // 重置到市场大盘（点击菜单栏"市场大盘"时）
    const resetToDashboard = () => {
      if (viewMode.value !== 'dashboard' || selectedFundCode.value || compareMode.value) {
        pushCurrentState()
      }
      viewMode.value = 'dashboard'
      selectedFundCode.value = ''
      // 如果处于对比模式，也退出
      if (compareMode.value) {
        compareMode.value = false
        compareFunds.value = []
      }
    }

    // 更新时间
    const updateTime = () => {
      const now = new Date()
      currentTime.value = now.toLocaleString('zh-CN')
    }
    
    onMounted(() => {
      updateTime()
      // 每分钟更新时间
      setInterval(updateTime, 60000)
    })
    
    return {
      selectedFundCode,
      currentTime,
      viewMode,
      compareFunds,
      compareMode,
      canGoBack,
      handleFundSelected,
      handleHeaderSearch,
      handleDashboardFundView,
      handleScreeningFundView,
      handleRealtimeFundView,
      handleResearchFundView,
      handleAddToCompare,
      handleRemoveFromCompare,
      handleClearCompare,
      toggleCompareMode,
      showFullContent,
      resetToDashboard,
      goBack,
      navigateToMode
    }
  }
}
</script>

<style>
:root {
  --primary-color: #1677ff;
  --primary-gradient: linear-gradient(135deg, #1677ff 0%, #0958d9 100%);
  --success-color: #52c41a;
  --danger-color: #ff4d4f;
  --warning-color: #faad14;
  --text-primary: #1f2937;
  --text-secondary: #6b7280;
  --text-tertiary: #9ca3af;
  --bg-primary: #f8fafc;
  --bg-card: #ffffff;
  --border-color: #e5e7eb;
  --shadow-sm: 0 1px 2px rgba(0, 0, 0, 0.05);
  --shadow-md: 0 4px 6px -1px rgba(0, 0, 0, 0.1);
  --shadow-lg: 0 10px 15px -3px rgba(0, 0, 0, 0.1);
  --radius-sm: 6px;
  --radius-md: 10px;
  --radius-lg: 16px;
}

* {
  margin: 0;
  padding: 0;
  box-sizing: border-box;
}

#app {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
  color: var(--text-primary);
  min-height: 100vh;
  display: flex;
  flex-direction: column;
  background: var(--bg-primary);
}

.app-header {
  background: var(--primary-gradient);
  color: white;
  padding: 12px 24px;
  box-shadow: var(--shadow-md);
  position: sticky;
  top: 0;
  z-index: 100;
}

.header-content {
  max-width: 1920px;
  margin: 0 auto;
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.header-left h1 {
  font-size: 1.6rem;
  font-weight: 700;
  margin-bottom: 2px;
  letter-spacing: -0.5px;
}

.header-left p {
  opacity: 0.85;
  font-size: 0.85rem;
}

/* 顶部搜索框 */
.header-search {
  flex: 1;
  max-width: 600px;
  margin: 0 40px;
}

.header-search :deep(.fund-search) {
  margin-bottom: 0;
  background: transparent;
  padding: 0;
  box-shadow: none;
}

.header-search :deep(.search-header) {
  gap: 8px;
}

.header-search :deep(.search-box) {
  min-width: 300px;
  flex: 1;
}

.header-search :deep(.search-input) {
  background: rgba(255, 255, 255, 0.95);
  border: 2px solid transparent;
  height: 40px;
  flex: 1;
}

.header-search :deep(.search-input:focus) {
  border-color: rgba(255, 255, 255, 0.5);
  box-shadow: 0 0 0 3px rgba(255, 255, 255, 0.2);
}

.header-search :deep(.search-btn) {
  background: linear-gradient(135deg, #ff9f43 0%, #f39c12 100%);
  height: 40px;
  padding: 0 24px;
  color: white;
  font-weight: 600;
  border: none;
  box-shadow: 0 2px 8px rgba(243, 156, 18, 0.35);
}

.header-search :deep(.search-btn:hover) {
  background: linear-gradient(135deg, #ffb366 0%, #f5a623 100%);
  box-shadow: 0 4px 12px rgba(243, 156, 18, 0.45);
  transform: translateY(-1px);
}

.header-search :deep(.refresh-btn) {
  background: rgba(255, 255, 255, 0.2);
  color: white;
  width: 40px;
  height: 40px;
  border-radius: 8px;
}

.header-search :deep(.refresh-btn:hover:not(:disabled)) {
  background: rgba(255, 255, 255, 0.3);
}

.header-search :deep(.search-results) {
  position: absolute;
  top: 100%;
  left: 0;
  right: 0;
  z-index: 1000;
  margin-top: 4px;
  max-height: 300px;
}

.header-right {
  display: flex;
  align-items: center;
  gap: 8px;
}

.back-btn {
  padding: 8px 12px;
  border: 1px solid rgba(255, 255, 255, 0.35);
  border-radius: var(--radius-sm);
  background: rgba(255, 255, 255, 0.16);
  color: #fff;
  cursor: pointer;
  font-size: 13px;
  font-weight: 600;
  white-space: nowrap;
  transition: all 0.2s ease;
}

.back-btn:hover {
  background: rgba(255, 255, 255, 0.28);
}

.mode-switch {
  display: flex;
  gap: 6px;
  background: rgba(255, 255, 255, 0.12);
  padding: 4px;
  border-radius: var(--radius-md);
}

.mode-btn {
  padding: 8px 14px;
  border: none;
  border-radius: var(--radius-sm);
  cursor: pointer;
  font-size: 13px;
  font-weight: 500;
  background: transparent;
  color: rgba(255, 255, 255, 0.75);
  transition: all 0.2s ease;
  white-space: nowrap;
}

.mode-btn:hover {
  background: rgba(255, 255, 255, 0.15);
  color: white;
}

.mode-btn.active {
  background: white;
  color: var(--primary-color);
  box-shadow: var(--shadow-sm);
}

.app-main {
  flex: 1;
  max-width: 1920px;
  width: 100%;
  margin: 0 auto;
  padding: 20px;
}

/* ==================== 仪表盘布局 ==================== */
.dashboard-layout {
  display: grid;
  grid-template-columns: 380px 1fr 380px;
  gap: 20px;
  min-height: calc(100vh - 140px);
  transition: grid-template-columns 0.3s ease;
}

.dashboard-layout.full-content {
  grid-template-columns: 380px 1fr;
}

.dashboard-sidebar {
  position: sticky;
  top: 80px;
  height: fit-content;
  max-height: calc(100vh - 100px);
  overflow-y: auto;
}

.dashboard-main {
  min-width: 0;
}

.dashboard-right {
  display: flex;
  flex-direction: column;
  gap: 12px;
  position: sticky;
  top: 80px;
  height: calc(100vh - 100px);
  min-height: 0;
  overflow: hidden;
}

.dashboard-right .flash-news-container,
.dashboard-right .sector-rank-container {
  flex: 1 1 0;
  min-height: 0;
  max-height: none;
}

.dashboard-right .sector-rank-container {
  display: flex;
  flex-direction: column;
  overflow: hidden;
  padding: 12px;
}

.dashboard-right .sector-content {
  display: flex;
  flex: 1 1 auto;
  flex-direction: column;
  min-height: 0;
  overflow: hidden;
}

.dashboard-right .sector-list {
  flex: 1 1 auto;
  min-height: 0;
  max-height: none;
  overflow-y: auto;
  gap: 6px;
}

.dashboard-right .sector-item {
  padding: 7px 10px;
}

.dashboard-right .filter-panel,
.dashboard-right .overview-bar {
  flex-shrink: 0;
  margin-bottom: 8px;
}

.dashboard-right .update-time {
  flex-shrink: 0;
}

/* ==================== 原有布局 ==================== */
.main-layout {
  display: flex;
  gap: 20px;
  min-height: calc(100vh - 160px);
}

.sidebar-left {
  width: 400px;
  flex-shrink: 0;
}

.content-area {
  flex: 1;
  min-width: 0;
}

.content-area.full-width {
  width: 100%;
}

/* ==================== 对比面板 ==================== */
.compare-panel {
  margin-bottom: 20px;
  border-radius: var(--radius-lg);
  background: var(--bg-card);
  box-shadow: var(--shadow-md);
  overflow: hidden;
}

/* ==================== 欢迎页面 ==================== */
.welcome-container {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 80px 40px;
  background: var(--bg-card);
  border-radius: var(--radius-lg);
  margin-top: 20px;
  box-shadow: var(--shadow-md);
  text-align: center;
}

.welcome-icon {
  font-size: 48px;
  margin-bottom: 16px;
  background: linear-gradient(135deg, #1677ff20 0%, #0958d920 100%);
  width: 80px;
  height: 80px;
  line-height: 80px;
  border-radius: 50%;
}

.welcome-container h3 {
  font-size: 1.4rem;
  color: var(--text-primary);
  margin-bottom: 8px;
}

.welcome-container p {
  color: var(--text-secondary);
  font-size: 0.95rem;
}

/* ==================== 页脚 ==================== */
.app-footer {
  background: var(--bg-card);
  text-align: center;
  padding: 12px;
  border-top: 1px solid var(--border-color);
  font-size: 0.8rem;
  color: var(--text-tertiary);
}

/* ==================== 响应式设计 ==================== */
@media (max-width: 1600px) {
  .header-search {
    max-width: 400px;
    margin: 0 20px;
  }
}

@media (max-width: 1400px) {
  .dashboard-layout {
    grid-template-columns: 340px 1fr 340px;
  }
  
  .sidebar-left {
    width: 360px;
  }
  
  .header-search {
    max-width: 350px;
  }
}

@media (max-width: 1200px) {
  .dashboard-layout {
    grid-template-columns: 1fr 340px;
  }
  
  .dashboard-sidebar {
    display: none;
  }
  
  .sidebar-left {
    width: 320px;
  }
  
  .header-search {
    max-width: 280px;
    margin: 0 15px;
  }
}

@media (max-width: 1024px) {
  .main-layout {
    flex-direction: column;
  }
  
  .sidebar-left {
    width: 100%;
  }
  
  .dashboard-layout {
    grid-template-columns: 1fr;
  }
  
  .dashboard-right {
    position: static;
    max-height: none;
  }
  
  .mode-switch {
    flex-wrap: wrap;
    justify-content: center;
  }
  
  .header-search {
    order: 3;
    width: 100%;
    max-width: 100%;
    margin: 10px 0 0 0;
  }
}

@media (max-width: 768px) {
  .app-header {
    padding: 10px 16px;
  }
  
  .header-content {
    flex-direction: column;
    gap: 12px;
  }
  
  .header-left {
    text-align: center;
  }
  
  .mode-btn {
    padding: 6px 10px;
    font-size: 12px;
  }
  
  .app-main {
    padding: 12px;
  }
  
  .header-search :deep(.db-status) {
    display: none;
  }
}

/* ==================== 滚动条美化 ==================== */
::-webkit-scrollbar {
  width: 6px;
  height: 6px;
}

::-webkit-scrollbar-track {
  background: transparent;
}

::-webkit-scrollbar-thumb {
  background: #d1d5db;
  border-radius: 3px;
}

::-webkit-scrollbar-thumb:hover {
  background: #9ca3af;
}
</style>
