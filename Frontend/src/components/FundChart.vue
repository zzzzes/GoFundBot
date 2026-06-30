<template>
  <div class="fund-chart-card">
    <div class="top-tabs">
      <div 
        class="tab-item" 
        :class="{ active: activeTab === 'performance' }"
        @click="switchTab('performance')"
      >
        业绩走势
      </div>
      <div 
        class="tab-item" 
        :class="{ active: activeTab === 'comparison' }"
        @click="switchTab('comparison')"
      >
        收益对比
      </div>
      <div 
        class="tab-item" 
        :class="{ active: activeTab === 'drawdown' }"
        @click="switchTab('drawdown')"
      >
        回撤修复
      </div>
    </div>

    <div class="summary-info" v-if="activeTab === 'performance'">
        <div class="info-group">
            <span class="legend-dot blue"></span>
            <span class="label">本基金</span>
            <br>
            <span class="value" :class="getColor(fundChange)">{{ fundChange > 0 ? '+' : ''}}{{ fundChange }}%</span>
        </div>
    </div>
    
    <div class="summary-info drawdown-info" v-else-if="activeTab === 'drawdown'">
        <div class="info-group">
            <div class="legend-dot-row">
                <span class="legend-line green"></span>
                <span class="label">最大回撤</span>
            </div>
            <div class="value-row">{{ maxDrawdownInfo.val }}%</div>
        </div>
        <div class="info-group">
             <div class="legend-dot-row">
                <span class="legend-box pink"></span>
                <span class="label">最大回撤修复天数</span>
             </div>
             <div class="value-row">{{ maxDrawdownInfo.days ? maxDrawdownInfo.days + '天' : '正在修复中...' }}</div>
        </div>
    </div>

    <div class="summary-info comparison-info" v-else-if="activeTab === 'comparison'">
        <div class="info-group" v-for="item in comparisonInfo" :key="item.name">
             <div class="legend-dot-row">
                <span class="legend-dot" :style="{ background: item.color, width: '12px', height: '3px' }"></span>
                <span class="label" style="margin-left: 4px;">{{ item.name }}</span>
             </div>
             <!-- Optional: Add value at end of period? -->
        </div>
    </div>

    <div class="chart-container">
      <div ref="chartEl" class="chart-el"></div>
    </div>

    <div class="time-ranges">
      <div 
        v-for="range in timeRanges" 
        :key="range.value" 
        class="range-item"
        :class="{ active: selectedRange === range.value }"
        @click="setTimeRange(range.value)"
      >
        {{ range.label }}
      </div>
    </div>
  </div>
</template>

<script>
import { ref, computed, onMounted, onUnmounted, watch, nextTick } from 'vue'
import * as echarts from 'echarts'

export default {
  name: 'FundChart',
  props: {
    netWorthTrend: {
      type: Array,
      default: () => []
    },
    acWorthTrend: {
      type: Array,
      default: () => []
    },
    grandTotal: {
      type: Array,
      default: () => []
    }
  },
  setup(props) {
    const chartEl = ref(null)
    const activeTab = ref('performance')
    const selectedRange = ref('1y')
    let chartInstance = null

    const timeRanges = [
      { label: '近3月', value: '3m' },
      { label: '近6月', value: '6m' },
      { label: '近1年', value: '1y' },
      { label: '近3年', value: '3y' },
      { label: '全部', value: 'all' }
    ]

    const setTimeRange = (range) => {
      selectedRange.value = range
      updateChart()
    }
    
    const switchTab = (tab) => {
        activeTab.value = tab
        nextTick(() => {
            if (chartInstance) {
                chartInstance.dispose();
                chartInstance = null;
            }
            initChart();
        })
    }

    const getColor = (val) => {
        if (!val) return ''
        return val >= 0 ? 'text-red' : 'text-green'
    }

    // Computed properties for summary
    const fundChange = ref('0.00')
    const maxDrawdownInfo = ref({ val: '0.00', days: 0 })
    const comparisonInfo = ref([])

    const filterByDate = (data, range) => {
        if (!data || data.length === 0) return []
        const now = new Date()
        let startDate = new Date(0)

        // Compute start date without mutating `now`
        const nowTs = now.getTime()
        if (range === '3m') {
            const d = new Date(nowTs)
            d.setMonth(d.getMonth() - 3)
            startDate = d
        } else if (range === '6m') {
            const d = new Date(nowTs)
            d.setMonth(d.getMonth() - 6)
            startDate = d
        } else if (range === '1y') {
            const d = new Date(nowTs)
            d.setFullYear(d.getFullYear() - 1)
            startDate = d
        } else if (range === '3y') {
            const d = new Date(nowTs)
            d.setFullYear(d.getFullYear() - 3)
            startDate = d
        }

        const startTs = startDate.getTime()

        // 支持新格式: [{date: '2024-01-01', value: 1.23}]
        if (data.length > 0 && data[0] && data[0].date !== undefined && typeof data[0].date === 'string') {
            return data
                .map(item => {
                    const ts = new Date(item.date).getTime()
                    if (isNaN(ts)) return null
                    return [ts, item.value]
                })
                .filter(item => item !== null && item[0] >= startTs)
        }
        // 旧格式: [[timestamp, value]]
        return data.filter(item => {
            if (!item || item.length < 2) return false
            const ts = item[0]
            if (typeof ts !== 'number' || isNaN(ts)) return false
            return ts >= startTs
        })
    }

    const roundTo = (value, precision = 6) => {
        const factor = Math.pow(10, precision)
        return Math.round(value * factor) / factor
    }

    const getDecimalPlaces = (value) => {
        if (!isFinite(value)) return 0
        const text = String(value)
        if (text.includes('e-')) {
            return Number(text.split('e-')[1])
        }
        return (text.split('.')[1] || '').length
    }

    const getNiceStep = (rawStep) => {
        if (!isFinite(rawStep) || rawStep <= 0) return 1

        const exponent = Math.floor(Math.log10(rawStep))
        const magnitude = Math.pow(10, exponent)
        const fraction = rawStep / magnitude

        let niceFraction = 10
        if (fraction <= 1) {
            niceFraction = 1
        } else if (fraction <= 2) {
            niceFraction = 2
        } else if (fraction <= 2.5) {
            niceFraction = 2.5
        } else if (fraction <= 5) {
            niceFraction = 5
        }

        return roundTo(niceFraction * magnitude)
    }

    const getNiceYAxis = (values) => {
        const numericValues = values.filter(value => typeof value === 'number' && isFinite(value))
        if (numericValues.length === 0) return {}

        let min = Math.min(...numericValues)
        let max = Math.max(...numericValues)

        if (min === max) {
            const padding = Math.max(Math.abs(min) * 0.1, 1)
            min -= padding
            max += padding
        }

        const padding = Math.max((max - min) * 0.08, 0.05)
        const paddedMin = min - padding
        const paddedMax = max + padding
        const interval = getNiceStep((paddedMax - paddedMin) / 6)
        const precision = Math.max(getDecimalPlaces(interval), 0)

        return {
            min: roundTo(Math.floor(paddedMin / interval) * interval, precision),
            max: roundTo(Math.ceil(paddedMax / interval) * interval, precision),
            interval
        }
    }

    const formatAxisLabel = (value, unit = '%') => {
        const num = Number(value)
        if (!isFinite(num)) return ''
        const abs = Math.abs(num)
        const decimals = abs >= 10 ? 0 : abs >= 1 ? 1 : 2
        return `${num.toFixed(decimals).replace(/\.0+$/, '').replace(/(\.\d*?)0+$/, '$1')}${unit}`
    }

    const applyNiceYAxis = (option, unit = '%') => {
        const values = (option.series || [])
            .flatMap(series => series.data || [])
            .map(point => Array.isArray(point) ? Number(point[1]) : Number(point))

        Object.assign(option.yAxis, getNiceYAxis(values), {
            axisLabel: {
                formatter: value => formatAxisLabel(value, unit)
            }
        })
    }

    const processData = () => {
        const rawData = (props.netWorthTrend || [])
            .filter(item => item && typeof item.x === 'number' && !isNaN(item.x) && typeof item.y === 'number' && !isNaN(item.y))
            .map(item => [item.x, item.y]);
        const filtered = filterByDate(rawData, selectedRange.value).slice().sort((a, b) => a[0] - b[0]);

        if (filtered.length === 0) return { chartData: [], drawdownInfo: null, useRawValues: false }

        const startVal = filtered[0][1]
        const endVal = filtered[filtered.length - 1][1]
        fundChange.value = startVal !== 0 ? ((endVal - startVal) / startVal * 100).toFixed(2) : '0.00'

        // 转为百分比（保留4位小数，避免货币基金等低波动品种全部四舍五入为0）
        const toPercent = (val) => startVal !== 0 ? parseFloat(((val - startVal) / startVal * 100).toFixed(4)) : 0
        const percentTrend = filtered.map(item => [item[0], toPercent(item[1])])

        // 全零检测：如果百分比数据完全无变化（货币基金净值恒为1.0000），
        // 直接用原始净值绘图，让 ECharts 按数值自动缩放
        const pctValues = percentTrend.map(p => p[1])
        const pctRange = Math.max(...pctValues) - Math.min(...pctValues)
        const useRawValues = pctRange < 0.0001

        const chartData = useRawValues
          ? filtered.map(item => [item[0], item[1]])           // 原始净值
          : percentTrend                                        // 百分比

        // Calculate Max Drawdown & Recovery
        let curMaxdd = 0;
        let globalPeakIndex = 0;
        let globalValleyIndex = 0;
        
        let runningPeakValue = -Infinity;
        let runningPeakIndex = 0;
        
        for (let i = 0; i < filtered.length; i++) {
            const val = filtered[i][1];
            if (val > runningPeakValue) {
                runningPeakValue = val;
                runningPeakIndex = i;
            }
            
            const dd = (runningPeakValue - val) / runningPeakValue;
            if (dd > curMaxdd) {
                curMaxdd = dd;
                globalPeakIndex = runningPeakIndex;
                globalValleyIndex = i;
            }
        }
        
        // Check Recovery
        let recoveryIndex = -1;
        const peakValRaw = filtered[globalPeakIndex][1];
        
        for (let i = globalPeakIndex + 1; i < filtered.length; i++) {
            if (filtered[i][1] >= peakValRaw) {
                recoveryIndex = i;
                break;
            }
        }
        
        const peakDate = filtered[globalPeakIndex][0];
        const valleyDate = filtered[globalValleyIndex][0];
        const recoveryDate = recoveryIndex !== -1 ? filtered[recoveryIndex][0] : null;
        
        const days = recoveryDate ? Math.ceil((recoveryDate - peakDate) / (1000 * 3600 * 24)) : null;

        const ddInfo = {
            val: (curMaxdd * 100).toFixed(2),
            peakDate,
            valleyDate,
            recoveryDate,
            days,
            peakValue: toPercent(peakValRaw),
            valleyValue: toPercent(filtered[globalValleyIndex][1]),
            recoveryValue: recoveryIndex !== -1 ? toPercent(filtered[recoveryIndex][1]) : null
        }

        
        maxDrawdownInfo.value = ddInfo
        
        // Also process Comparison Data just in case we need to filter for Comparison Tab?
        // Usually comparison tab shows "All" or follows the range selector if enabled. 
        // User requirements usually imply comparison follows standard range or all. 
        // But the range selector is hidden for comparison in template: v-if="activeTab !== 'comparison'"
        
        return {
            chartData,
            drawdownInfo: ddInfo,
            useRawValues
        }
    }

    const initChart = () => {
      if (!chartEl.value) return
      if (!chartInstance) {
          chartInstance = echarts.init(chartEl.value)
      }
      updateChart()
    }

    const updateChart = () => {
      if (!chartInstance) return

      chartInstance.clear(); 

      const option = {
        grid: { left: '3%', right: '5%', bottom: '10%', top: '15%', containLabel: true },
        tooltip: { 
            trigger: 'axis',
            formatter: function (params) {
                let res = '<div>' + echarts.format.formatTime('yyyy-MM-dd', params[0].value[0]) + '</div>'
                params.forEach(item => {
                    let val = item.value[1];
                    // If comparison, values are usually percents.
                    // If net worth, values are currency.
                    res += `<div>${item.marker} ${item.seriesName}: ${val}${activeTab.value === 'comparison' ? '%' : ''}</div>`
                })
                return res;
            }
        },
        xAxis: { type: 'time', boundaryGap: false, axisLine: { show: false }, axisTick: { show: false } },
        yAxis: {
            type: 'value',
            scale: true,
            splitLine: { lineStyle: { type: 'dashed' } },
            axisLabel: { formatter: value => formatAxisLabel(value, '%') }
        },
        series: []
      }

      if (activeTab.value === 'performance') {
          const { chartData, useRawValues } = processData()
          const unit = useRawValues ? '' : '%'
          option.series.push({
              name: '本基金',
              type: 'line',
              data: chartData,
              smooth: true,
              symbol: 'none',
              lineStyle: { width: 2, color: '#007bff' },
              areaStyle: {
                  color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
                      { offset: 0, color: 'rgba(0, 123, 255, 0.2)' },
                      { offset: 1, color: 'rgba(0, 123, 255, 0.0)' }
                  ])
              }
          })

          applyNiceYAxis(option, useRawValues ? '' : '%')

          option.tooltip.formatter = function (params) {
              let res = '<div>' + echarts.format.formatTime('yyyy-MM-dd', params[0].value[0]) + '</div>'
              params.forEach(item => {
                  const val = Number(item.value[1])
                  res += `<div>${item.marker} ${item.seriesName}: ${isFinite(val) ? val.toFixed(useRawValues ? 4 : 2) : item.value[1]}${unit}</div>`
              })
              return res;
          }
      } else if (activeTab.value === 'comparison') {
          const comparisonData = props.grandTotal || []
          
          if (comparisonData.length > 0) {
              const colors = ['#007bff', '#91cc75', '#fac858', '#ee6666', '#5470c6'];
              
              // Update Legend Info
              comparisonInfo.value = comparisonData.map((item, index) => ({
                  name: item.name,
                  color: colors[index % colors.length]
              }))

              const series = comparisonData.map((item, index) => {
                  const rawData = item.data || [];
                  const filteredData = filterByDate(rawData, selectedRange.value);
                  
                  return {
                    name: item.name,
                    type: 'line',
                    data: filteredData, 
                    smooth: true,
                    symbol: 'none',
                    lineStyle: { 
                        width: item.name.includes('本基金') ? 3 : 1.5
                    },
                    itemStyle: {
                        color: colors[index % colors.length]
                    },
                    z: item.name.includes('本基金') ? 3 : 2
                 }
              });
              
              option.series = series
              option.legend = { show: false } // Hide internal legend
              applyNiceYAxis(option, '%')
              
              // Adjust tooltip for comparison to show %
              option.tooltip.formatter = function (params) {
                  let res = '<div>' + echarts.format.formatTime('yyyy-MM-dd', params[0].value[0]) + '</div>'
                  params.forEach(item => {
                      const val = Number(item.value[1])
                      res += `<div>
                        <span style="display:inline-block;margin-right:5px;border-radius:50%;width:10px;height:10px;background-color:${item.color};"></span>
                        ${item.seriesName}: ${isFinite(val) ? val.toFixed(2) : item.value[1]}%
                      </div>`
                  })
                  return res;
              }
          }
      } else if (activeTab.value === 'drawdown') {
          const { chartData, drawdownInfo } = processData()

          if (chartData.length > 0) {
              const seriesData = {
                  name: '本基金',
                  type: 'line',
                  data: chartData,
                  smooth: true,
                  symbol: 'none',
                  lineStyle: { width: 2, color: '#88aaff' },
                  markArea: {
                      itemStyle: { color: 'rgba(255, 230, 230, 0.6)' },
                      data: []
                  },
                  markPoint: {
                      symbol: 'circle',
                      symbolSize: 8,
                      label: {
                          show: true,
                          color: '#fff',
                          padding: [4, 8],
                          borderRadius: 4
                      },
                      data: []
                  }
              }
              
              if (drawdownInfo && drawdownInfo.peakDate) {
                  const endDate = drawdownInfo.recoveryDate || chartData[chartData.length - 1][0];
                  
                  seriesData.markArea.data.push([
                      { xAxis: drawdownInfo.peakDate },
                      { xAxis: endDate }
                  ]);
                  
                  const points = [];
                  points.push({
                      coord: [drawdownInfo.peakDate, drawdownInfo.peakValue],
                      itemStyle: { color: '#ff9800' }, 
                      label: { show: false } 
                  });
                   
                  points.push({
                       coord: [drawdownInfo.valleyDate, drawdownInfo.valleyValue],
                       itemStyle: { color: '#00bfa5' },
                       label: {
                           offset: [0, 15],
                           formatter: `最大回撤${drawdownInfo.val}%`,
                           backgroundColor: 'rgba(0, 191, 165, 0.7)',
                           position: 'top'
                       }
                  });
                   
                  if (drawdownInfo.recoveryDate) {
                       points.push({
                           coord: [drawdownInfo.recoveryDate, drawdownInfo.recoveryValue],
                           itemStyle: { color: '#ff5252' },
                           label: {
                               offset: [0, -15],
                               formatter: `${drawdownInfo.days}天修复`,
                               backgroundColor: 'rgba(255, 82, 82, 0.7)',
                               position: 'bottom'
                           }
                       });
                  }
                   
                  seriesData.markPoint.data = points;
              }
              option.series.push(seriesData)
              applyNiceYAxis(option, '%')
          }
      }

      chartInstance.setOption(option, true)  // notMerge=true for clean state
      chartInstance.resize()                // ensure proper sizing after data update
    }

    onMounted(() => {
      initChart()
      window.addEventListener('resize', () => chartInstance?.resize())
    })

    onUnmounted(() => {
      if (chartInstance) {
        chartInstance.dispose()
      }
      window.removeEventListener('resize', () => chartInstance?.resize())
    })

    watch([() => props.netWorthTrend, () => props.grandTotal], () => {
      nextTick(() => updateChart()) 
    }, { deep: true })
    
    return {
      chartEl,
      timeRanges,
      selectedRange,
      setTimeRange,
      activeTab,
      switchTab,
      fundChange,
      maxDrawdownInfo,
      comparisonInfo,
      getColor
    }
  }
}
</script>

<style scoped>
.fund-chart-card {
  height: 100%;
  display: flex;
  flex-direction: column;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  overflow: hidden;
}

.top-tabs {
  display: flex;
  border-bottom: 1px solid #f0f0f0;
}

.tab-item {
  flex: 1;
  text-align: center;
  padding: 15px 0;
  font-size: 16px;
  color: #666;
  cursor: pointer;
  position: relative;
  font-weight: 500;
}

.tab-item.active {
  color: #333;
  font-weight: bold;
}

.tab-item.active::after {
  content: '';
  position: absolute;
  bottom: 0;
  left: 50%;
  transform: translateX(-50%);
  width: 20px;
  height: 3px;
  background: #333;
  border-radius: 2px;
}

.summary-info {
  display: flex;
  justify-content: space-around;
  padding: 15px 20px 5px;
}

.info-group {
    text-align: center;
}

.legend-dot {
    display: inline-block;
    width: 8px;
    height: 3px;
    vertical-align: middle;
    margin-right: 5px;
    background: #007bff;
    border-radius: 2px;
}

.legend-dot-row {
    display: flex;
    align-items: center;
    justify-content: center;
    margin-bottom: 4px;
}

.legend-line.green {
    width: 12px;
    height: 3px;
    background: #00bfa5;
    margin-right: 5px;
}

.legend-box.pink {
    width: 12px;
    height: 12px;
    background: rgba(255, 230, 230, 1);
    margin-right: 5px;
}

.label {
    font-size: 13px;
    color: #999;
}

.value {
    font-size: 18px;
    font-weight: bold;
    display: block;
    margin-top: 4px;
}

.value-row {
    font-size: 18px;
    font-weight: bold;
    color: #333;
}

.text-red { color: #f5222d; }
.text-green { color: #52c41a; }

.chart-container {
  padding: 0 10px;
  flex: 1;
  min-height: 0;
}

.chart-el {
  width: 100%;
  height: 100%;
  min-height: 280px;
}

.time-ranges {
  display: flex;
  justify-content: space-between;
  padding: 10px 20px;
}

.range-item {
  padding: 4px 12px;
  color: #999;
  cursor: pointer;
  font-size: 13px;
  border-radius: 12px;
}

.range-item.active {
  background: #e6f7ff;
  color: #007bff;
  font-weight: 500;
}
</style>
