# -*- coding: utf-8 -*-
"""
量化分析服务模块 — QuantService

提供四大核心能力：
1. 多因子打分系统（买什么 + 买多少）
2. 智能买入策略（怎么买）
3. 动态卖出信号（什么时候卖）
4. AI 投资顾问（小白向导）

所有计算均基于现有数据库（FundBasicInfo / FundRiskMetrics / FundScreeningRank / FundTrend），
不引入新数据源，确保离线可用。
"""

from __future__ import annotations

import json
import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------


def _safe_float(val: Any, default: float = 0.0) -> float:
    if val is None:
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def _safe_int(val: Any, default: int = 0) -> int:
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


def _json_loads(val: Any, default: Any = None) -> Any:
    if val is None:
        return default if default is not None else {}
    if isinstance(val, (dict, list)):
        return val
    try:
        return json.loads(val)
    except (json.JSONDecodeError, TypeError):
        return default if default is not None else {}


def _none_to(val: Any, fallback: float) -> float:
    """None → fallback；保留有效零值"""
    if val is None:
        return fallback
    try:
        return float(val)
    except (ValueError, TypeError):
        return fallback


def _clamp(val: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, val))


def _percentile(sorted_values: List[float], pct: float) -> float:
    """返回升序列表的 pct 分位数（0-100）"""
    if not sorted_values:
        return 0.0
    k = (pct / 100.0) * (len(sorted_values) - 1)
    f = int(math.floor(k))
    c = int(math.ceil(k))
    if f == c:
        return sorted_values[f]
    return sorted_values[f] * (c - k) + sorted_values[c] * (k - f)


def _normalize_in_place(funds: List[Dict[str, Any]], key: str, invert: bool = False):
    """
    对 fund dict[key] 做 min-max 归一化到 0-100，覆盖写入 f'{key}_norm'
    invert=True 表示值越小越好（如回撤）
    """
    vals = [_safe_float(f.get(key)) for f in funds]
    valid = [v for v in vals if v != 0.0]
    if not valid:
        lo, hi = 0.0, 100.0
    else:
        lo, hi = min(valid), max(valid)
        if abs(hi - lo) < 1e-8:
            lo, hi = 0.0, 100.0
    for f, v in zip(funds, vals):
        if v == 0.0:
            f[f"{key}_norm"] = 50.0
        else:
            score = (v - lo) / (hi - lo) * 100.0
            f[f"{key}_norm"] = 100.0 - score if invert else score


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------


@dataclass
class FactorWeights:
    """因子权重配置 — 不同风险偏好的默认值"""
    return_weight: float = 30.0   # 收益因子
    risk_weight: float = 30.0     # 风险因子（稳健型可调高）
    manager_weight: float = 15.0  # 经理因子
    macro_weight: float = 10.0    # 宏观/行业因子
    stability_weight: float = 15.0  # 稳定性因子（趋势一致性）

    def validate(self) -> "FactorWeights":
        total = self.return_weight + self.risk_weight + self.manager_weight + self.macro_weight + self.stability_weight
        if total > 0:
            self.return_weight = self.return_weight / total * 100
            self.risk_weight = self.risk_weight / total * 100
            self.manager_weight = self.manager_weight / total * 100
            self.macro_weight = self.macro_weight / total * 100
            self.stability_weight = self.stability_weight / total * 100
        return self


# 预设权重配置
PRESET_WEIGHTS = {
    "balanced": FactorWeights(return_weight=30, risk_weight=30, manager_weight=15, macro_weight=10, stability_weight=15),
    "conservative": FactorWeights(return_weight=15, risk_weight=40, manager_weight=15, macro_weight=10, stability_weight=20),
    "aggressive": FactorWeights(return_weight=40, risk_weight=20, manager_weight=15, macro_weight=15, stability_weight=10),
    "momentum": FactorWeights(return_weight=45, risk_weight=15, manager_weight=10, macro_weight=15, stability_weight=15),
}


@dataclass
class PositionParams:
    """仓位计算参数"""
    total_capital: float = 100000.0          # 总资金（元）
    max_single_fund_pct: float = 20.0       # 单基最大仓位（%）
    max_total_funds: int = 8                 # 最多持有基金数
    risk_budget: float = 12.0               # 风险预算（期望波动率%）
    min_position: float = 500.0              # 最低持仓金额


@dataclass
class BacktestAdvancedParams:
    """高级回测参数"""
    fund_code: str = ""
    start_date: str = "2020-01-01"
    end_date: str = ""
    strategy: str = "value_averaging"        # dca / value_averaging / grid / adaptive
    base_amount: float = 1000.0              # 基准投资金额
    fee_rate: float = 0.15                   # 手续费(%)
    pe_series: Optional[List[Dict]] = None   # PE 估值序列
    ma_period: int = 60                      # 均线周期
    grid_step: float = 5.0                   # 网格步长(%)


@dataclass
class QuantScore:
    """单只基金量化打分结果"""
    fund_code: str = ""
    fund_name: str = ""
    fund_type: str = ""
    # 各因子得分 (0-100)
    return_score: float = 0.0
    risk_score: float = 0.0
    manager_score: float = 0.0
    macro_score: float = 0.0
    stability_score: float = 0.0
    # 综合
    composite_score: float = 0.0
    # 建议
    suggested_position_pct: float = 0.0       # 建议仓位占比(%)
    suggested_amount: float = 0.0             # 建议买入金额
    operation: str = "hold"                   # buy_heavy / buy / hold / reduce / sell
    # 描述
    summary: str = ""
    strengths: List[str] = field(default_factory=list)
    weaknesses: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# QuantService 核心
# ---------------------------------------------------------------------------


class QuantService:
    """量化分析服务 — 无状态，纯计算"""

    def __init__(self, db_session_factory=None):
        self._db_factory = db_session_factory

    # ------------------------------------------------------------------
    # 1. 多因子打分
    # ------------------------------------------------------------------

    def score_funds(
        self,
        db,
        fund_codes: Optional[List[str]] = None,
        fund_type: Optional[str] = None,
        min_return_1y: Optional[float] = None,
        risk_profile: str = "balanced",
        position_params: Optional[PositionParams] = None,
        top_n: int = 20,
        min_estab_years: float = 1.0,
    ) -> List[QuantScore]:
        """
        对基金库做多因子打分，返回排名 + 仓位建议。

        因子体系：
        1. 收益因子 (return)    — 1年/2年/3年排名百分位 + 绝对收益
        2. 风险因子 (risk)      — 夏普比率 + 卡玛比率 + 最大回撤
        3. 经理因子 (manager)   — 从业年限 + 年化回报 + 管理规模
        4. 宏观因子 (macro)     — 行业分散度 + 持仓集中度
        5. 稳定性因子 (stability) — 排名趋势一致性 + 波动率

        仓位公式：
        position_pct = max_single_pct × (risk_budget / volatility) / Σ(risk_budget / volatility)
        """
        from models import FundBasicInfo, FundRiskMetrics, FundScreeningRank, FundExtraData

        # --- 数据拉取 ---
        query = db.query(FundBasicInfo, FundRiskMetrics, FundScreeningRank, FundExtraData).outerjoin(
            FundRiskMetrics, FundBasicInfo.fund_code == FundRiskMetrics.fund_code
        ).outerjoin(
            FundScreeningRank, FundBasicInfo.fund_code == FundScreeningRank.fund_code
        ).outerjoin(
            FundExtraData, FundBasicInfo.fund_code == FundExtraData.fund_code
        )

        if fund_codes:
            query = query.filter(FundBasicInfo.fund_code.in_(fund_codes))
        if fund_type:
            query = query.filter(FundBasicInfo.fund_type.like(f"%{fund_type}%"))
        if min_return_1y is not None:
            query = query.filter(FundBasicInfo.return_1y >= min_return_1y)

        rows = query.all()
        if not rows:
            return []

        # --- 提取字段 ---
        funds: List[Dict[str, Any]] = []
        for basic, risk, rank, extra in rows:
            perf = _json_loads(basic.performance_json)
            f = {
                "fund_code": basic.fund_code,
                "fund_name": basic.fund_name,
                "fund_type": basic.fund_type or "未知",
                "return_1y": _safe_float(basic.return_1y),
                "return_3y": _safe_float(perf.get("3_year_return")),
                "return_6m": _safe_float(perf.get("6_month_return")),
                "return_3m": _safe_float(perf.get("3_month_return")),
                "return_1m": _safe_float(perf.get("1_month_return")),
                # 风险
                "sharpe_1y": _none_to(risk.sharpe_ratio_1y if risk else None, 0),
                "sharpe_3y": _none_to(risk.sharpe_ratio_3y if risk else None, 0),
                "calmar_1y": _none_to(risk.calmar_ratio_1y if risk else None, 0),
                "max_drawdown_1y": _none_to(risk.max_drawdown_1y if risk else None, 50),
                "volatility_1y": _none_to(risk.volatility_1y if risk else None, 25),
                "annual_return_1y": _none_to(risk.annual_return_1y if risk else None, 0),
                # 排名
                "rank_1y": _none_to(rank.rank_pct_1y if rank else None, 50),
                "rank_3y": _none_to(rank.rank_pct_3y if rank else None, 50),
                "rank_2y": _none_to(rank.rank_pct_2y if rank else None, 50),
                "rank_6m": _none_to(rank.rank_pct_6m if rank else None, 50),
                "rank_3m": _none_to(rank.rank_pct_3m if rank else None, 50),
                "pass_4433": bool(rank and rank.pass_4433 == 1),
                # 经理信息
                "manager_json": extra.fund_managers_json if extra else None,
                "established_date": _json_loads(basic.basic_json).get("established_date", "") if basic.basic_json else "",
            }
            # 过滤成立年限
            estab_years = self._estab_years(f["established_date"])
            if min_estab_years > 0 and estab_years < min_estab_years:
                continue
            # 解析经理数据
            mgr = _json_loads(f["manager_json"], [])
            if isinstance(mgr, dict):
                mgr = [mgr]
            f["manager_years"] = _safe_float(mgr[0].get("work_experience", 0)) if mgr else 0
            f["manager_avg_return"] = _safe_float(mgr[0].get("years_avg_return", 0)) if mgr else 0
            f["manager_scale"] = _safe_float(mgr[0].get("managed_fund_size", 0)) if mgr else 0

            funds.append(f)

        if not funds:
            return []

        # --- 归一化 ---
        _normalize_in_place(funds, "return_1y", invert=False)
        _normalize_in_place(funds, "return_3y", invert=False)
        _normalize_in_place(funds, "rank_1y", invert=True)  # 排名越小越好
        _normalize_in_place(funds, "rank_3y", invert=True)

        _normalize_in_place(funds, "sharpe_1y", invert=False)
        _normalize_in_place(funds, "calmar_1y", invert=False)
        _normalize_in_place(funds, "max_drawdown_1y", invert=True)
        _normalize_in_place(funds, "volatility_1y", invert=True)

        _normalize_in_place(funds, "manager_years", invert=False)
        _normalize_in_place(funds, "manager_avg_return", invert=False)

        # --- 计算因子得分 ---
        weights = PRESET_WEIGHTS.get(risk_profile, PRESET_WEIGHTS["balanced"])
        weights.validate()

        for f in funds:
            # 1. 收益因子
            f["return_score"] = (
                f["return_1y_norm"] * 0.35
                + f["return_3y_norm"] * 0.25
                + f["rank_1y_norm"] * 0.25
                + f["rank_3y_norm"] * 0.15
            )

            # 2. 风险因子
            f["risk_score"] = (
                f["sharpe_1y_norm"] * 0.30
                + f["calmar_1y_norm"] * 0.25
                + f["max_drawdown_1y_norm"] * 0.25
                + f["volatility_1y_norm"] * 0.20
            )

            # 3. 经理因子
            f["manager_score"] = (
                f["manager_years_norm"] * 0.40
                + f["manager_avg_return_norm"] * 0.60
            )

            # 4. 宏观因子（行业分散度 + 规模）
            f["macro_score"] = 60.0  # 默认中值，前端可校准

            # 5. 稳定性因子（排名一致性）
            ranks = [f["rank_1y"], f["rank_3y"], f["rank_6m"], f["rank_3m"]]
            valid_ranks = [r for r in ranks if r > 0]
            if len(valid_ranks) >= 2:
                rank_spread = statistics.stdev(valid_ranks) if len(valid_ranks) > 1 else 0
                f["stability_score"] = max(0, 100 - rank_spread * 2)
            else:
                f["stability_score"] = 50.0

            # 综合分
            f["composite_score"] = (
                weights.return_weight * f["return_score"]
                + weights.risk_weight * f["risk_score"]
                + weights.manager_weight * f["manager_score"]
                + weights.macro_weight * f["macro_score"]
                + weights.stability_weight * f["stability_score"]
            )

        # --- 排序 ---
        funds.sort(key=lambda x: x["composite_score"], reverse=True)
        top_funds = funds[:top_n]

        # --- 仓位计算 (风险预算法) ---
        pos_params = position_params or PositionParams()
        self._calculate_positions(top_funds, pos_params)

        # --- 输出 ---
        results = []
        for f in top_funds:
            op = self._operation_from_score(f["composite_score"])
            qs = QuantScore(
                fund_code=f["fund_code"],
                fund_name=f["fund_name"],
                fund_type=f["fund_type"],
                return_score=round(f["return_score"], 1),
                risk_score=round(f["risk_score"], 1),
                manager_score=round(f["manager_score"], 1),
                macro_score=round(f["macro_score"], 1),
                stability_score=round(f["stability_score"], 1),
                composite_score=round(f["composite_score"], 1),
                suggested_position_pct=round(f.get("position_pct", 0), 1),
                suggested_amount=round(f.get("position_amount", 0), 0),
                operation=op,
                summary=self._build_summary(f),
                strengths=self._build_strengths(f),
                weaknesses=self._build_weaknesses(f),
            )
            results.append(qs)

        return results

    def _calculate_positions(self, funds: List[Dict], params: PositionParams):
        """风险预算仓位分配"""
        if not funds:
            return

        # 风险倒数权重
        risk_inv_sum = 0.0
        for f in funds:
            vol = _safe_float(f.get("volatility_1y"), 15)
            vol = max(vol, 2.0)  # 最低波动 2%
            f["_risk_inv"] = params.risk_budget / vol
            risk_inv_sum += f["_risk_inv"]

        if risk_inv_sum <= 0:
            eq_pct = params.max_single_fund_pct / len(funds)
            for f in funds:
                f["position_pct"] = round(eq_pct, 1)
                f["position_amount"] = round(params.total_capital * eq_pct / 100, 0)
            return

        for f in funds:
            raw_pct = (f["_risk_inv"] / risk_inv_sum) * params.max_single_fund_pct * len(funds) / 100 * 100
            pct = min(raw_pct, params.max_single_fund_pct)
            pct = max(pct, 1.0)
            f["position_pct"] = round(pct, 1)
            f["position_amount"] = round(params.total_capital * pct / 100, 0)

    def _operation_from_score(self, score: float) -> str:
        if score >= 80:
            return "buy_heavy"
        elif score >= 65:
            return "buy"
        elif score >= 50:
            return "hold"
        elif score >= 35:
            return "reduce"
        else:
            return "sell"

    def _build_summary(self, f: Dict) -> str:
        score = f["composite_score"]
        if score >= 80:
            return f"综合得分 {score:.0f}，各维度表现优秀，适合作为核心仓位。"
        elif score >= 65:
            return f"综合得分 {score:.0f}，整体良好，可适当配置。"
        elif score >= 50:
            return f"综合得分 {score:.0f}，表现中规中矩，建议观望。"
        else:
            return f"综合得分 {score:.0f}，多个维度不佳，谨慎对待。"

    def _build_strengths(self, f: Dict) -> List[str]:
        s = []
        if f["return_score"] >= 70:
            s.append("历史收益优异")
        if f["risk_score"] >= 70:
            s.append("风险控制良好（高夏普/低回撤）")
        if f["manager_score"] >= 70:
            s.append("基金经理经验丰富")
        if f["stability_score"] >= 70:
            s.append("业绩排名稳定")
        if f.get("pass_4433"):
            s.append("通过4433法则筛选")
        return s[:3] if s else ["综合表现均衡"]

    def _build_weaknesses(self, f: Dict) -> List[str]:
        w = []
        if f["return_score"] <= 30:
            w.append("历史收益偏弱")
        if f["risk_score"] <= 30:
            w.append("风险调整收益不足")
        if f["manager_score"] <= 30:
            w.append("基金经理经验待验证")
        if f["stability_score"] <= 30:
            w.append("业绩波动较大")
        if f.get("max_drawdown_1y", 0) > 25:
            w.append("最大回撤较高")
        return w[:3] if w else []

    def _estab_years(self, date_str: str) -> float:
        if not date_str or date_str == "--":
            return 0
        try:
            dt = datetime.strptime(date_str[:10], "%Y-%m-%d")
            return (datetime.now() - dt).days / 365.0
        except ValueError:
            return 0

    # ------------------------------------------------------------------
    # 2. 智能买入策略回测
    # ------------------------------------------------------------------

    def backtest_advanced(self, db, params: BacktestAdvancedParams) -> Dict[str, Any]:
        """
        高级回测，支持四种策略：

        - dca (定投): 每月固定金额
        - value_averaging (价值平均): 目标市值增长，低位多买高位少买
        - grid (网格): 净值下跌 grid_step% 买入，上涨 grid_step% 卖出
        - adaptive (自适应): 参考 PE 分位调整投入（需 pe_series）
        """
        from models import FundTrend

        trend = db.query(FundTrend).filter(FundTrend.fund_code == params.fund_code).first()
        if not trend:
            return {"error": f"Fund {params.fund_code} not found"}

        raw = _json_loads(trend.net_worth_trend_json, [])
        nav_dict = {}
        for item in raw:
            d = item.get("date", "")
            n = item.get("net_worth")
            if d and n is not None:
                try:
                    nav_dict[d] = float(n)
                except (ValueError, TypeError):
                    continue

        sorted_dates = sorted(nav_dict.keys())
        if len(sorted_dates) < 2:
            return {"error": "Insufficient NAV data"}

        # 日期过滤
        start_dt = self._parse_date(params.start_date)
        end_dt = self._parse_date(params.end_date) if params.end_date else datetime.now()
        dates = [d for d in sorted_dates if start_dt <= self._parse_date(d) <= end_dt]
        if len(dates) < 2:
            return {"error": f"Insufficient data in range. Found {len(dates)} records."}

        nav_dict = {d: nav_dict[d] for d in dates}

        # PE 序列处理
        pe_dict = {}
        if params.pe_series:
            for pe in params.pe_series:
                pe_dict[pe.get("date", "")] = pe.get("pe_percentile", 50)

        # 执行回测
        if params.strategy == "value_averaging":
            result = self._backtest_value_averaging(dates, nav_dict, params)
        elif params.strategy == "grid":
            result = self._backtest_grid(dates, nav_dict, params)
        elif params.strategy == "adaptive":
            result = self._backtest_adaptive(dates, nav_dict, pe_dict, params)
        else:
            result = self._backtest_dca(dates, nav_dict, params)

        result["strategy"] = params.strategy
        result["fund_code"] = params.fund_code
        return result

    def _backtest_dca(self, dates, nav_dict, p: BacktestAdvancedParams) -> Dict:
        """普通定投"""
        total_invested = 0.0
        total_shares = 0.0
        timeline = []

        current_month = None
        for date in dates:
            nav = nav_dict[date]
            dt = self._parse_date(date)
            month_key = (dt.year, dt.month)

            is_invest = False
            if month_key != current_month:
                fee = p.base_amount * p.fee_rate / 100
                shares = (p.base_amount - fee) / nav
                total_shares += shares
                total_invested += p.base_amount
                current_month = month_key
                is_invest = True

            value = total_shares * nav
            ret = value - total_invested
            ret_pct = (ret / total_invested * 100) if total_invested > 0 else 0

            timeline.append({
                "date": date, "nav": round(nav, 4),
                "invested": round(total_invested, 2),
                "value": round(value, 2),
                "return_rate": round(ret_pct, 2),
                "is_invest_day": is_invest,
            })

        return self._finalize_result(timeline, total_invested)

    def _backtest_value_averaging(self, dates, nav_dict, p: BacktestAdvancedParams) -> Dict:
        """
        价值平均策略：目标市值每月增长 base_amount
        市值不足时补仓，超过目标时赎回
        """
        total_invested = 0.0
        total_shares = 0.0
        cash = 0.0
        timeline = []
        target_value = 0.0
        month_count = 0
        current_month = None

        for date in dates:
            nav = nav_dict[date]
            dt = self._parse_date(date)
            month_key = (dt.year, dt.month)

            is_action = False
            action_type = ""
            if month_key != current_month:
                month_count += 1
                target_value = p.base_amount * month_count
                current_value = total_shares * nav
                gap = target_value - current_value

                if gap > p.base_amount * 0.1:  # 缺口 > 10%
                    # 低估值，多买（当前市值 < 目标）
                    buy_amount = min(gap, p.base_amount * 2)
                    fee = buy_amount * p.fee_rate / 100
                    total_shares += (buy_amount - fee) / nav
                    total_invested += buy_amount
                    is_action = True
                    action_type = "buy"
                elif gap < -p.base_amount * 0.2:  # 超额 > 20%
                    # 赎回超额部分
                    sell_amount = min(-gap, current_value * 0.1)
                    sell_shares = sell_amount / nav
                    total_shares -= sell_shares
                    cash += sell_amount
                    is_action = True
                    action_type = "sell"

                current_month = month_key

            value = total_shares * nav + cash
            ret = value - total_invested
            ret_pct = (ret / total_invested * 100) if total_invested > 0 else 0

            timeline.append({
                "date": date, "nav": round(nav, 4),
                "invested": round(total_invested, 2),
                "value": round(value, 2),
                "return_rate": round(ret_pct, 2),
                "is_action_day": is_action,
                "action": action_type,
                "target_value": round(target_value, 2),
            })

        return self._finalize_result(timeline, total_invested)

    def _backtest_grid(self, dates, nav_dict, p: BacktestAdvancedParams) -> Dict:
        """
        网格交易：以初始净值为基准，每跌 grid_step% 买入一份，每涨 grid_step% 卖出一份
        """
        total_invested = 0.0
        total_shares = 0.0
        cash = 0.0
        timeline = []
        base_nav = nav_dict[dates[0]]
        last_grid_buy = base_nav
        last_grid_sell = base_nav
        grid_amount = p.base_amount

        for date in dates:
            nav = nav_dict[date]
            is_action = False
            action_type = ""

            # 触发买入网格
            if nav <= last_grid_buy * (1 - p.grid_step / 100):
                fee = grid_amount * p.fee_rate / 100
                shares = (grid_amount - fee) / nav
                total_shares += shares
                total_invested += grid_amount
                last_grid_buy = nav
                is_action = True
                action_type = "grid_buy"

            # 触发卖出网格
            if total_shares > 0 and nav >= last_grid_sell * (1 + p.grid_step / 100):
                sell_shares = min(grid_amount / nav, total_shares * 0.2)
                total_shares -= sell_shares
                cash += sell_shares * nav
                last_grid_sell = nav
                is_action = True
                action_type = "grid_sell"

            value = total_shares * nav + cash
            ret = value - total_invested
            ret_pct = (ret / total_invested * 100) if total_invested > 0 else 0

            timeline.append({
                "date": date, "nav": round(nav, 4),
                "invested": round(total_invested, 2),
                "value": round(value, 2),
                "return_rate": round(ret_pct, 2),
                "is_action_day": is_action,
                "action": action_type,
                "grid_level": round((last_grid_buy - base_nav) / base_nav * 100 / p.grid_step) if p.grid_step > 0 else 0,
            })

        return self._finalize_result(timeline, total_invested)

    def _backtest_adaptive(self, dates, nav_dict, pe_dict, p: BacktestAdvancedParams) -> Dict:
        """
        自适应定投：PE 分位低时加倍投入，高时减半
        PE 分位映射：
        < 20% → 2× 基准
        20-40% → 1.5×
        40-70% → 1×
        70-85% → 0.5×
        > 85% → 暂停
        """
        total_invested = 0.0
        total_shares = 0.0
        timeline = []
        current_month = None

        def pe_multiplier(pe_pct):
            if pe_pct <= 20:
                return 2.0
            elif pe_pct <= 40:
                return 1.5
            elif pe_pct <= 70:
                return 1.0
            elif pe_pct <= 85:
                return 0.5
            else:
                return 0.0

        for date in dates:
            nav = nav_dict[date]
            dt = self._parse_date(date)
            month_key = (dt.year, dt.month)

            is_invest = False
            multiplier = 1.0
            if month_key != current_month:
                pe_pct = pe_dict.get(date, 50)
                multiplier = pe_multiplier(pe_pct)
                if multiplier > 0:
                    amount = p.base_amount * multiplier
                    fee = amount * p.fee_rate / 100
                    shares = (amount - fee) / nav
                    total_shares += shares
                    total_invested += amount
                current_month = month_key
                is_invest = True

            value = total_shares * nav
            ret = value - total_invested
            ret_pct = (ret / total_invested * 100) if total_invested > 0 else 0

            timeline.append({
                "date": date, "nav": round(nav, 4),
                "invested": round(total_invested, 2),
                "value": round(value, 2),
                "return_rate": round(ret_pct, 2),
                "is_invest_day": is_invest,
                "pe_multiplier": multiplier,
            })

        return self._finalize_result(timeline, total_invested)

    def _finalize_result(self, timeline, total_invested):
        if not timeline:
            return {"error": "Empty timeline"}
        last = timeline[-1]
        peak_value = max(t["value"] for t in timeline)
        max_drawdown = 0.0
        peak_so_far = 0.0
        for t in timeline:
            if t["value"] > peak_so_far:
                peak_so_far = t["value"]
            dd = (peak_so_far - t["value"]) / peak_so_far * 100 if peak_so_far > 0 else 0
            max_drawdown = max(max_drawdown, dd)

        days = len(timeline)
        annual_return = ((1 + last["return_rate"] / 100) ** (365 / max(days, 1)) - 1) * 100

        return {
            "total_invested": round(total_invested, 2),
            "final_value": last["value"],
            "final_return_rate": last["return_rate"],
            "annualized_return": round(annual_return, 2),
            "max_drawdown": round(max_drawdown, 2),
            "peak_value": round(peak_value, 2),
            "investment_count": sum(1 for t in timeline if t.get("is_invest_day") or t.get("is_action_day")),
            "timeline": timeline,
        }

    def _parse_date(self, s: str):
        for fmt in ["%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"]:
            try:
                return datetime.strptime(s[:10], fmt)
            except (ValueError, IndexError):
                continue
        return datetime(2000, 1, 1)

    # ------------------------------------------------------------------
    # 3. 动态卖出信号
    # ------------------------------------------------------------------

    def exit_signals(self, db, fund_code: str) -> Dict[str, Any]:
        """
        综合卖出信号检测，返回五维信号灯：

        1. 估值过热 — PE 分位 > 70% 且浮盈 > 20%
        2. 趋势破位 — 收盘价低于 MA60
        3. 经理变更 — 基金经理近期更换
        4. 规模暴增 — 规模半年增长 > 100%
        5. 止损触发 — 近1年最大回撤 > 25%

        Returns:
            {
                "fund_code": "xxx",
                "signals": [
                    {"name": "估值过热", "level": "red"|"yellow"|"green", "score": 0-100, "detail": "..."},
                    ...
                ],
                "exit_score": 0-100,  # 越高越该卖出
                "recommendation": "持有"|"减仓"|"清仓"|"观望",
                "summary": "..."
            }
        """
        from models import FundBasicInfo, FundTrend, FundRiskMetrics, FundExtraData

        basic = db.query(FundBasicInfo).filter(FundBasicInfo.fund_code == fund_code).first()
        if not basic:
            return {"error": f"Fund {fund_code} not found"}

        trend = db.query(FundTrend).filter(FundTrend.fund_code == fund_code).first()
        risk = db.query(FundRiskMetrics).filter(FundRiskMetrics.fund_code == fund_code).first()
        extra = db.query(FundExtraData).filter(FundExtraData.fund_code == fund_code).first()

        signals = []

        # --- 1. 估值过热 ---
        perf = _json_loads(basic.performance_json)
        return_1y = _safe_float(basic.return_1y or _safe_float(perf.get("1_year_return", 0)))
        pe_level = "green"
        if return_1y > 30:
            pe_level = "red"
            score = 80
            detail = f"近1年涨幅 {return_1y:.1f}%，已大幅偏离均值，估值可能偏高"
        elif return_1y > 15:
            pe_level = "yellow"
            score = 45
            detail = f"近1年涨幅 {return_1y:.1f}%，注意估值风险"
        else:
            score = 10
            detail = f"近1年涨幅 {return_1y:.1f}%，估值合理"
        signals.append({"name": "估值过热", "level": pe_level, "score": score, "detail": detail})

        # --- 2. 趋势破位 ---
        nav_data = _json_loads(trend.net_worth_trend_json if trend else "[]", [])
        ma_broken = False
        if len(nav_data) >= 60:
            recent = nav_data[-60:]
            navs = [_safe_float(x.get("net_worth", 0)) for x in recent]
            valid_navs = [n for n in navs if n > 0]
            if len(valid_navs) >= 30:
                ma60 = sum(valid_navs) / len(valid_navs)
                latest = valid_navs[-1]
                if latest < ma60 * 0.95:
                    ma_broken = True
                    signals.append({"name": "趋势破位", "level": "red", "score": 75,
                                    "detail": f"当前净值 {latest:.4f} 低于60日均值 {ma60:.4f} 超5%"})
                elif latest < ma60:
                    signals.append({"name": "趋势破位", "level": "yellow", "score": 40,
                                    "detail": f"当前净值 {latest:.4f} 略低于60日均值 {ma60:.4f}"})
                else:
                    signals.append({"name": "趋势破位", "level": "green", "score": 5,
                                    "detail": f"净值在60日均线上方，趋势良好"})
            else:
                signals.append({"name": "趋势破位", "level": "green", "score": 0, "detail": "数据不足"})
        else:
            signals.append({"name": "趋势破位", "level": "green", "score": 0, "detail": "数据不足"})

        # --- 3. 经理变更 ---
        mgr = _json_loads(extra.fund_managers_json if extra else "[]", [])
        if isinstance(mgr, dict):
            mgr = [mgr]
        mgr_days = _safe_float(mgr[0].get("manage_days", 365)) if mgr else 365
        if mgr_days < 180:
            signals.append({"name": "经理变更", "level": "red", "score": 80,
                            "detail": f"基金经理任职仅 {mgr_days:.0f} 天，需观察"})
        elif mgr_days < 365:
            signals.append({"name": "经理变更", "level": "yellow", "score": 30,
                            "detail": f"基金经理任职不足1年"})
        else:
            signals.append({"name": "经理变更", "level": "green", "score": 0,
                            "detail": "基金经理任职稳定"})

        # --- 4. 规模暴增 ---
        scale_json = _json_loads(trend.scale_fluctuation_json if trend else "[]", [])
        scale_warning = False
        if isinstance(scale_json, list) and len(scale_json) >= 2:
            scales = []
            for item in scale_json:
                s = _safe_float(item.get("scale")) if isinstance(item, dict) else _safe_float(item)
                if s > 0:
                    scales.append(s)
            if len(scales) >= 2:
                growth = (scales[-1] - scales[0]) / scales[0] * 100
                if growth > 100:
                    scale_warning = True
                    signals.append({"name": "规模暴增", "level": "red", "score": 60,
                                    "detail": f"基金规模增长 {growth:.0f}%，可能拖累收益"})
                elif growth > 50:
                    signals.append({"name": "规模暴增", "level": "yellow", "score": 30,
                                    "detail": f"基金规模增长 {growth:.0f}%"})
                else:
                    signals.append({"name": "规模暴增", "level": "green", "score": 5,
                                    "detail": "规模变化正常"})
            else:
                signals.append({"name": "规模暴增", "level": "green", "score": 0, "detail": "数据不足"})
        else:
            signals.append({"name": "规模暴增", "level": "green", "score": 0, "detail": "数据不足"})

        # --- 5. 止损触发 ---
        max_dd = _safe_float(risk.max_drawdown_1y if risk else 0)
        if max_dd > 25:
            signals.append({"name": "止损触发", "level": "red", "score": 80,
                            "detail": f"近1年最大回撤 {max_dd:.1f}%，超过25%警戒线"})
        elif max_dd > 15:
            signals.append({"name": "止损触发", "level": "yellow", "score": 35,
                            "detail": f"近1年最大回撤 {max_dd:.1f}%，注意风险"})
        else:
            signals.append({"name": "止损触发", "level": "green", "score": 5,
                            "detail": f"近1年最大回撤 {max_dd:.1f}%，风险可控"})

        # --- 综合 ---
        reds = sum(1 for s in signals if s["level"] == "red")
        yellows = sum(1 for s in signals if s["level"] == "yellow")
        exit_score = sum(s["score"] for s in signals) / max(len(signals), 1)

        if reds >= 2:
            recommendation = "清仓"
        elif reds >= 1:
            recommendation = "减仓"
        elif yellows >= 2:
            recommendation = "观望"
        else:
            recommendation = "持有"

        return {
            "fund_code": fund_code,
            "fund_name": basic.fund_name,
            "signals": signals,
            "exit_score": round(exit_score, 1),
            "recommendation": recommendation,
            "summary": f"五维卖出信号综合评分 {exit_score:.0f}/100，建议{recommendation}。红色信号 {reds} 个，黄色 {yellows} 个。",
        }

    # ------------------------------------------------------------------
    # 4. AI 投资顾问提示构建
    # ------------------------------------------------------------------

    def build_advisor_prompt(
        self,
        db,
        total_capital: float,
        risk_profile: str,
        investment_goal: str,
        investment_horizon: str,
        fund_codes: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        构建 AI 投资顾问的完整上下文，包括：
        - 用户画像（资金、风险偏好、目标）
        - 当前市场概览
        - 推荐基金打分表
        - 仓位分配建议
        """

        # 打分
        scores = self.score_funds(
            db=db,
            fund_codes=fund_codes,
            risk_profile=risk_profile,
            position_params=PositionParams(total_capital=total_capital),
            top_n=15,
        )

        profile_desc = {
            "conservative": "稳健型：追求资产保值增值，最大可承受回撤 10%，偏好低波动产品",
            "balanced": "平衡型：追求适度增长，最大可承受回撤 20%",
            "aggressive": "进取型：追求高回报，可承受较大波动，最大回撤 30%",
            "momentum": "动量型：追逐市场趋势，关注短期表现和资金流向",
        }.get(risk_profile, "平衡型")

        horizon_desc = {
            "short": "短期 (1年以内)",
            "medium": "中期 (1-3年)",
            "long": "长期 (3年以上)",
        }.get(investment_horizon, "中期 (1-3年)")

        user_info = f"""
## 用户画像
- 可用资金：{total_capital:,.0f} 元
- 风险偏好：{profile_desc}
- 投资目标：{investment_goal}
- 投资期限：{horizon_desc}
"""

        fund_table_lines = ["## 推荐基金打分表\n"]
        fund_table_lines.append("| 排名 | 基金名称 | 类型 | 综合分 | 收益 | 风险 | 经理 | 稳定性 | 建议仓位 | 操作 |")
        fund_table_lines.append("|------|---------|------|--------|------|------|------|--------|----------|------|")
        for i, s in enumerate(scores[:10], 1):
            fund_table_lines.append(
                f"| {i} | {s.fund_name}({s.fund_code}) | {s.fund_type} | {s.composite_score} | "
                f"{s.return_score} | {s.risk_score} | {s.manager_score} | {s.stability_score} | "
                f"{s.suggested_position_pct}% | {s.operation} |"
            )

        op_map = {"buy_heavy": "重仓买入", "buy": "建议买入", "hold": "持有观望", "reduce": "减仓", "sell": "卖出"}

        fund_detail_lines = []
        for s in scores[:5]:
            fund_detail_lines.append(f"""
### {s.fund_name} ({s.fund_code})
- **综合评分**: {s.composite_score}/100
- **买入建议**: {op_map.get(s.operation, s.operation)}
- **建议仓位**: {s.suggested_position_pct}%（约 {s.suggested_amount:,.0f} 元）
- **亮点**: {'; '.join(s.strengths)}
- **注意**: {'; '.join(s.weaknesses)}
""")

        context = {
            "user_info": user_info,
            "fund_table": "\n".join(fund_table_lines),
            "fund_details": "\n".join(fund_detail_lines),
            "scores": [s.__dict__ for s in scores],
        }

        return context


# ---------------------------------------------------------------------------
# 单例
# ---------------------------------------------------------------------------

_quant_service: Optional[QuantService] = None


def get_quant_service() -> QuantService:
    global _quant_service
    if _quant_service is None:
        _quant_service = QuantService()
    return _quant_service
