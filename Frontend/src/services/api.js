import axios from 'axios'

// 使用相对路径，以便在开发环境中使用 Vite 代理，生产环境中使用 Nginx 代理
// 也可以通过环境变量 import.meta.env.VITE_API_BASE_URL 来配置
const API_BASE_URL = '/api'

const api = axios.create({
  baseURL: API_BASE_URL,
  timeout: 600000
})

const localBackendApi = axios.create({
  baseURL: 'http://localhost:5000/api',
  timeout: 600000
})

async function getWithLocalFallback(path, config = {}) {
  try {
    return await api.get(path, config)
  } catch (error) {
    if (error?.message === 'Network Error' && API_BASE_URL === '/api') {
      return localBackendApi.get(path, config)
    }
    throw error
  }
}

export const fundAPI = {
  // 搜索基金
  searchFunds(keyword) {
    return api.get(`/fund/search?q=${encodeURIComponent(keyword)}`)
  },
  
  // 获取搜索数据库状态
  getSearchStatus() {
    return api.get('/fund/search/status')
  },
  
  // 更新搜索数据库
  updateSearchDatabase() {
    return api.post('/fund/search/update')
  },
  
  // 获取基金详细信息
  getFundDetail(fundCode) {
    return api.get(`/fund/${fundCode}`)
  },

  getFundIndustryExposure(fundCode, refresh = false) {
    const params = refresh ? { refresh: true } : {}
    return api.get(`/fund/${fundCode}/industry-exposure`, { params })
  },
  
  // 获取基金基础信息
  getFundBasicInfo(fundCode) {
    return api.get(`/fund/${fundCode}/basic`)
  },
  
  // 获取基金走势数据
  getFundTrend(fundCode) {
    return api.get(`/fund/${fundCode}/trend`)
  },
  
  // 获取基金对比数据（带缓存，包含风险指标）
  getFundCompareData(fundCode, forceRefresh = false) {
    const params = forceRefresh ? '?refresh=true' : ''
    return api.get(`/fund/${fundCode}/compare-data${params}`)
  },
  
  // 获取每日市场行情
  getDailyMarket(forceRefresh = false) {
    const params = forceRefresh ? '?refresh=true' : ''
    return api.get(`/market/daily${params}`)
  },
  
  // AI分析基金
  analyzeFund(fundCode) {
    return api.get(`/fund/${fundCode}/analyze`)
  }
}

// ==================== 自选基金 API ====================
export const watchlistAPI = {
  // 获取自选列表（包含分组信息）
  getWatchlist() {
    return api.get('/watchlist')
  },
  
  // 检查基金是否在自选列表中
  checkInWatchlist(fundCode) {
    return api.get(`/watchlist/${fundCode}`)
  },
  
  // 添加基金到自选
  addToWatchlist(fundCode, fundName, fundType = '', groupId = null, estimate = null) {
    return api.post('/watchlist', {
      fund_code: fundCode,
      fund_name: fundName,
      fund_type: fundType,
      group_id: groupId,
      estimate
    })
  },
  
  // 从自选中移除
  removeFromWatchlist(fundCode) {
    return api.delete(`/watchlist/${fundCode}`)
  },
  
  // 批量删除
  batchDelete(fundCodes) {
    return api.post('/watchlist/batch-delete', {
      fund_codes: fundCodes
    })
  },
  
  // 更新排序 - 传入基金代码数组，顺序即为排序
  reorder(fundCodeOrder, groupId = null) {
    return api.put('/watchlist/reorder', {
      order: fundCodeOrder,
      group_id: groupId
    })
  },
  
  // 移动基金到分组
  moveFundToGroup(fundCode, groupId) {
    return api.put('/watchlist/move', {
      fund_code: fundCode,
      group_id: groupId
    })
  },
  
  // ==================== 分组 API ====================
  
  // 获取所有分组
  getGroups() {
    return api.get('/watchlist/groups')
  },
  
  // 创建分组
  createGroup(name) {
    return api.post('/watchlist/groups', { name })
  },
  
  // 重命名分组
  renameGroup(groupId, name) {
    return api.put(`/watchlist/groups/${groupId}`, { name })
  },
  
  // 删除分组
  deleteGroup(groupId) {
    return api.delete(`/watchlist/groups/${groupId}`)
  },
  
  // 分组排序
  reorderGroups(groupIdOrder) {
    return api.put('/watchlist/groups/reorder', {
      order: groupIdOrder
    })
  },
  
  // 刷新自选基金估值（实时估值更新）
  refreshEstimates() {
    return api.post('/watchlist/refresh-estimates')
  }
}

// ==================== 基金筛选 API ====================
export const screeningAPI = {
  // 获取筛选数据库状态
  getStatus() {
    return api.get('/screening/status')
  },

  // 获取更新进度（仅内存，极快）
  getProgress() {
    return api.get('/screening/progress')
  },
  
  // 启动数据更新（直接获取完整数据）
  startUpdate(options = {}) {
    return api.post('/screening/update', options)
  },
  
  // 停止更新
  stopUpdate() {
    return api.post('/screening/stop')
  },
  
  // 查询筛选基金
  query(params) {
    return api.post('/screening/query', params)
  },
  
  // 获取预设策略列表
  getStrategies() {
    return api.get('/screening/strategies')
  },
  
  // 获取可用的基金类型（根据当前筛选条件）
  getAvailableTypes(params) {
    return api.post('/screening/available-types', params)
  },

  getIndustryTags() {
    return api.get('/screening/industry-tags')
  },

  getStockIndustryStatus() {
    return api.get('/screening/stock-industry/status')
  },

  warmupStockIndustry(params = {}) {
    return api.post('/screening/stock-industry/warmup', params)
  },
  
  // 获取单只基金筛选详情
  getFundDetail(fundCode) {
    return api.get(`/screening/fund/${fundCode}`)
  },
  
  // 补全风险指标
  fillRiskMetrics() {
    return api.post('/screening/fill-risk')
  },

  // 更新单只基金筛选数据
  updateSingleFund(fundCode) {
    return api.post(`/screening/update-single/${fundCode}`)
  },
  
  // 重新计算同类型排名
  recalculateRankings() {
    return api.post('/screening/recalculate-rankings')
  }
}

// ==================== 基金回测 API ====================
export const backtestAPI = {
  // 定投回测
  runStrategy(data) {
    return api.post('/backtest/strategy', data)
  },

  fixedInvestment(data) {
    return api.post('/backtest/fixed-investment', data)
  }
}

// ==================== 市场数据 API ====================
export const marketAPI = {
  // 获取市场概览（汇总数据）
  getOverview() {
    return api.get('/market/overview')
  },
  
  // 获取7x24快讯
  getFlashNews(count = 30, page = 1) {
    return api.get(`/market/news?count=${count}&page=${page}`)
  },
  
  // 获取行业板块排行
  getSectorRank(limit = 90) {
    return api.get(`/market/sectors?limit=${limit}`)
  },
  
  // 获取市场指数
  getMarketIndex() {
    return api.get('/market/index')
  },
  
  // 获取实时金价
  getGoldRealtime() {
    return api.get('/market/gold/realtime')
  },
  
  // 获取历史金价
  getGoldHistory(days = 10) {
    return api.get(`/market/gold/history?days=${days}`)
  },
  
  // 获取近7日成交量
  getVolumeWeekly() {
    return api.get('/market/volume')
  },
  
  // 获取近30分钟上证指数
  getSSE30min() {
    return api.get('/market/sse')
  },

  // 获取多指数分时数据
  getIndicesIntraday() {
    return api.get('/market/indices/intraday')
  },

  // 获取个股实时行情
  getStockQuote(code) {
    return api.get(`/stock/${code}/quote`)
  },

  // 获取个股历史K线数据
  getStockKline(code, params = {}) {
    return api.get(`/stock/${code}/kline`, { params })
  }
}

// ==================== 投研看板 API ====================
export const researchAPI = {
  getDashboard(params = {}) {
    return getWithLocalFallback('/research/dashboard', { params })
  },

  getMarketStats() {
    return getWithLocalFallback('/research/market-stats')
  },

  getFundDashboard(limit = 5) {
    return getWithLocalFallback('/research/fund-dashboard', { params: { limit } })
  },

  getEtfTracking(limit = 80, refresh = false) {
    return getWithLocalFallback('/research/etf-tracking', { params: { limit, refresh } })
  },

  getIndustryPerformance() {
    return getWithLocalFallback('/research/industry-performance')
  },

  rebuildIndustryPerformance() {
    return api.post('/research/rebuild-industry-performance')
  },

  getSectorSummary(limit = 50) {
    return getWithLocalFallback('/research/sector-summary', { params: { limit } })
  }
}

export default api
