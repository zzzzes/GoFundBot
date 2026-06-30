from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple


@dataclass(frozen=True)
class NavPoint:
    date: str
    nav: float


def run_strategy_backtest(
    nav_points: Iterable[Dict[str, Any]],
    strategy_type: str,
    capital: float,
    fee_rate: float = 0.0015,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    params = params or {}
    points = _normalize_nav_points(nav_points)
    if len(points) < 2:
        return {"error": "Insufficient net worth data for backtest"}

    strategy_type = strategy_type or "fixed_amount"
    if strategy_type not in {
        "fixed_amount",
        "double_down",
        "grid",
        "ma_timing",
        "trend_timing",
        "rocket_plan",
        "ai_plan",
        "dynamic_balance",
        "two_eight_rotation",
        "buy_hold",
        "kpi_analysis",
        "target_profit_plan",
    }:
        return {"error": f"Unsupported strategy_type: {strategy_type}"}

    if strategy_type == "target_profit_plan":
        return _run_target_profit_plan(points, capital, fee_rate, params)

    state = _new_state(capital)
    timeline: List[Dict[str, Any]] = []
    trades: List[Dict[str, Any]] = []
    signals: List[Dict[str, Any]] = []
    ctx: Dict[str, Any] = {
        "last_buy_nav": None,
        "last_period_key": None,
        "grid_base_nav": points[0].nav,
        "grid_level": 0,
        "consecutive_buy": 0,
        "consecutive_sell": 0,
    }

    ma_cache = _moving_averages(points, [10, 20, 60])

    for index, point in enumerate(points):
        date = point.date
        nav = point.nav
        before_trade_count = len(trades)

        if strategy_type == "fixed_amount":
            _strategy_fixed_amount(index, point, state, trades, signals, fee_rate, params, ctx)
        elif strategy_type == "double_down":
            _strategy_double_down(index, point, state, trades, signals, fee_rate, params, ctx)
        elif strategy_type == "grid":
            _strategy_grid(index, point, state, trades, signals, fee_rate, params, ctx)
        elif strategy_type == "ma_timing":
            _strategy_ma_timing(index, point, state, trades, signals, fee_rate, params, ctx, ma_cache)
        elif strategy_type == "trend_timing":
            _strategy_trend_timing(index, point, points, state, trades, signals, fee_rate, params, ctx, ma_cache)
        elif strategy_type == "rocket_plan":
            _strategy_rocket_plan(index, point, state, trades, signals, fee_rate, params, ctx)
        elif strategy_type == "ai_plan":
            _strategy_ai_plan(index, point, points, state, trades, signals, fee_rate, params, ctx, ma_cache)
        elif strategy_type == "dynamic_balance":
            _strategy_dynamic_balance(index, point, state, trades, signals, fee_rate, params, ctx)
        elif strategy_type == "two_eight_rotation":
            _strategy_two_eight_rotation(index, point, points, state, trades, signals, fee_rate, params, ctx, ma_cache)
        elif strategy_type == "buy_hold":
            _strategy_buy_hold(index, point, state, trades, signals, fee_rate, params)
        elif strategy_type == "kpi_analysis":
            _strategy_fixed_amount(index, point, state, trades, signals, fee_rate, params, ctx)

        trade_on_day = len(trades) > before_trade_count
        timeline.append(_snapshot(date, nav, state, trade_on_day))

    summary = _build_summary(points, timeline, trades, capital)
    metrics = {
        "holding_days": summary["holding_days"],
        "max_cost": summary["max_cost"],
        "cumulative_return": summary["total_return"],
        "average_invested": summary["average_invested"],
        "capital_return_rate": summary["return_rate"],
        "annual_return_rate": summary["annual_return"],
        "max_drawdown": summary["max_drawdown"],
        "average_capital_usage": summary["capital_usage_rate"],
        "buy_count": summary["buy_count"],
        "sell_count": summary["sell_count"],
    }

    return {
        "summary": summary,
        "timeline": timeline,
        "trades": trades,
        "signals": signals,
        "metrics": metrics,
        "strategy_config": {
            "strategy_type": strategy_type,
            "capital": round(capital, 2),
            "fee_rate": round(fee_rate * 100, 4),
            "params": params,
        },
    }


def _normalize_nav_points(raw_points: Iterable[Dict[str, Any]]) -> List[NavPoint]:
    points: List[NavPoint] = []
    for item in raw_points or []:
        date = item.get("date") or item.get("navDate") or item.get("trade_date")
        nav = item.get("net_worth")
        if nav is None:
            nav = item.get("nav")
        if not date or nav is None:
            continue
        try:
            date_text = str(date).split(" ")[0].replace("/", "-")
            if len(date_text) == 8 and date_text.isdigit():
                date_text = f"{date_text[:4]}-{date_text[4:6]}-{date_text[6:]}"
            points.append(NavPoint(date=date_text, nav=float(nav)))
        except (TypeError, ValueError):
            continue
    points.sort(key=lambda p: p.date)
    deduped: Dict[str, NavPoint] = {p.date: p for p in points if p.nav > 0}
    return [deduped[key] for key in sorted(deduped)]


def _new_state(capital: float) -> Dict[str, float]:
    capital = max(float(capital or 0), 0)
    return {
        "initial_capital": capital,
        "cash": capital,
        "shares": 0.0,
        "invested": 0.0,
        "fee": 0.0,
        "realized_cash": 0.0,
    }


def _buy(
    date: str,
    nav: float,
    amount: float,
    state: Dict[str, float],
    trades: List[Dict[str, Any]],
    signals: List[Dict[str, Any]],
    fee_rate: float,
    reason: str,
) -> bool:
    amount = min(float(amount or 0), state["cash"])
    if amount <= 0 or nav <= 0:
        return False
    fee = amount * fee_rate
    actual = amount - fee
    shares = actual / nav
    state["cash"] -= amount
    state["shares"] += shares
    state["invested"] += amount
    state["fee"] += fee
    _append_trade(date, "buy", amount, fee, shares, nav, state, trades, reason)
    signals.append({"date": date, "type": "buy", "nav": round(nav, 4), "reason": reason})
    return True


def _sell(
    date: str,
    nav: float,
    shares: float,
    state: Dict[str, float],
    trades: List[Dict[str, Any]],
    signals: List[Dict[str, Any]],
    fee_rate: float,
    reason: str,
) -> bool:
    shares = min(float(shares or 0), state["shares"])
    if shares <= 0 or nav <= 0:
        return False
    gross = shares * nav
    fee = gross * fee_rate
    amount = gross - fee
    state["shares"] -= shares
    state["cash"] += amount
    state["realized_cash"] += amount
    state["fee"] += fee
    _append_trade(date, "sell", amount, fee, -shares, nav, state, trades, reason)
    signals.append({"date": date, "type": "sell", "nav": round(nav, 4), "reason": reason})
    return True


def _append_trade(
    date: str,
    trade_type: str,
    amount: float,
    fee: float,
    share_delta: float,
    nav: float,
    state: Dict[str, float],
    trades: List[Dict[str, Any]],
    reason: str,
) -> None:
    market_value = state["shares"] * nav
    total_asset = state["cash"] + market_value
    total_return = total_asset - state.get("initial_capital", 0)
    return_rate = (total_return / state["invested"] * 100) if state["invested"] > 0 else 0
    trades.append({
        "index": len(trades) + 1,
        "date": date,
        "type": trade_type,
        "type_label": "买入" if trade_type == "buy" else "卖出",
        "amount": round(amount, 2),
        "fee": round(fee, 2),
        "share_delta": round(share_delta, 4),
        "holding_shares": round(state["shares"], 4),
        "cost": round(state["invested"], 2),
        "nav": round(nav, 4),
        "acc_nav": round(nav, 4),
        "value": round(market_value, 2),
        "cash": round(state["cash"], 2),
        "total_asset": round(total_asset, 2),
        "return": round(total_return, 2),
        "return_rate": round(return_rate, 2),
        "status": reason,
    })


def _snapshot(date: str, nav: float, state: Dict[str, float], trade_on_day: bool) -> Dict[str, Any]:
    value = state["shares"] * nav
    total_asset = state["cash"] + value
    total_return = total_asset - state["initial_capital"]
    invested = state["invested"]
    return_rate = (total_return / invested * 100) if invested > 0 else 0
    return {
        "date": date,
        "nav": round(nav, 4),
        "invested": round(invested, 2),
        "cash": round(state["cash"], 2),
        "shares": round(state["shares"], 4),
        "value": round(value, 2),
        "total_asset": round(total_asset, 2),
        "return": round(total_return, 2),
        "return_rate": round(return_rate, 2),
        "is_investment_day": trade_on_day,
        "status": "holding" if state["shares"] > 0 else "cash",
    }


def _strategy_fixed_amount(index, point, state, trades, signals, fee_rate, params, ctx):
    amount = _num(params, "amount", _num(params, "base_amount", 1000))
    if index == 0 and _num(params, "initial_amount", 0) > 0:
        _buy(point.date, point.nav, _num(params, "initial_amount", 0), state, trades, signals, fee_rate, "初始买入")

    frequency = params.get("frequency") or params.get("investment_type") or "monthly"
    if frequency == "lump_sum":
        if index == 0:
            _buy(point.date, point.nav, amount, state, trades, signals, fee_rate, "一次性买入")
        return

    if _period_due(point.date, frequency, params, ctx):
        _buy(point.date, point.nav, amount, state, trades, signals, fee_rate, "定额计划")


def _strategy_double_down(index, point, state, trades, signals, fee_rate, params, ctx):
    if "annual_plan_capital" in params:
        _strategy_double_down_plan(index, point, state, trades, signals, fee_rate, params, ctx)
        return

    base_amount = _num(params, "base_amount", _num(params, "amount", 1000))
    trigger_percent = _num(params, "drop_trigger_percent", 3)
    multiplier = max(_num(params, "multiplier", 2), 1)
    max_multiplier = max(_num(params, "max_multiplier", 4), 1)
    frequency = params.get("frequency") or "monthly"

    if index == 0:
        if params.get("start_condition", "immediate") == "immediate":
            if _buy(point.date, point.nav, base_amount, state, trades, signals, fee_rate, "首次买入"):
                ctx["last_buy_nav"] = point.nav
        return

    if not _period_due(point.date, frequency, params, ctx):
        return

    last_nav = ctx.get("last_buy_nav") or point.nav
    drop = (last_nav - point.nav) / last_nav * 100 if last_nav else 0
    if drop >= trigger_percent:
        steps = min(math.floor(drop / trigger_percent), max_multiplier)
        amount = base_amount * min(multiplier ** steps, max_multiplier)
        reason = f"下跌{round(drop, 2)}%翻倍买入"
    else:
        amount = base_amount
        reason = "常规定投"
    if _buy(point.date, point.nav, amount, state, trades, signals, fee_rate, reason):
        ctx["last_buy_nav"] = point.nav


def _strategy_double_down_plan(index, point, state, trades, signals, fee_rate, params, ctx):
    fee_rate = 0
    annual_capital = max(_num(params, "annual_plan_capital", 12000), 0)
    min_interval_days = max(int(_num(params, "min_trade_interval_days", 12)), 6)
    future_years = max(int(_num(params, "future_years", 2)), 0)
    multiplier = max(_num(params, "multiplier", 2), 1)
    max_multiplier = max(_num(params, "max_multiplier", 2), 1)
    drop_step_percent = max(_num(params, "drop_step_percent", 5), 0.1)
    opportunity_drawdown_percent = max(_num(params, "opportunity_drawdown_percent", 12), 0)
    initial_low_band_percent = max(_num(params, "initial_low_band_percent", 8), 0)
    lot_take_profit_percent = max(_num(params, "lot_take_profit_percent", 10), 0)
    restart_drop_percent = max(_num(params, "restart_after_sell_drop_percent", 3), 0)
    sell_drawdown_percent = max(_num(params, "sell_drawdown_percent", 0), 0)
    dynamic_drawdown = bool(params.get("dynamic_drawdown"))
    target_type = params.get("target_type") or "double"
    target_percent = _num(params, "target_percent", 20)

    start_date = ctx.setdefault("double_plan_start_date", point.date)
    start_nav = ctx.setdefault("double_plan_start_nav", point.nav)
    ctx["double_plan_seen_peak_nav"] = max(ctx.get("double_plan_seen_peak_nav") or point.nav, point.nav)
    base_amount = annual_capital / 12 if annual_capital > 0 else 0
    ctx.setdefault("double_plan_lots", [])

    if state["shares"] > 0:
        target_hit, target_metric = _double_plan_target_status(point, state, ctx, target_type, target_percent)
        if target_hit:
            ctx["double_plan_target_reached"] = True
            ctx["double_plan_peak_nav"] = max(ctx.get("double_plan_peak_nav") or point.nav, point.nav)
            ctx["double_plan_peak_metric"] = max(ctx.get("double_plan_peak_metric") or target_metric, target_metric)

        if ctx.get("double_plan_target_reached") and sell_drawdown_percent <= 0:
            _sell(point.date, point.nav, state["shares"], state, trades, signals, fee_rate, "目标达成，立即止盈")
            ctx["double_plan_lots"] = []
            state["invested"] = 0
            ctx["double_plan_target_reached"] = False
            ctx["double_plan_peak_nav"] = None
            ctx["double_plan_peak_metric"] = None
            ctx["double_plan_pause_buy_nav"] = None
            return

        peak_nav = ctx.get("double_plan_peak_nav")
        if ctx.get("double_plan_target_reached") and peak_nav:
            effective_drawdown = sell_drawdown_percent
            if dynamic_drawdown:
                peak_metric = ctx.get("double_plan_peak_metric") or target_metric
                effective_drawdown += max(0, peak_metric - _double_plan_target_value(target_type, target_percent)) * 0.25
            fallback = (peak_nav - point.nav) / peak_nav * 100 if peak_nav > 0 else 0
            if fallback >= effective_drawdown and target_hit:
                reason = f"目标达成后回落{round(fallback, 2)}%，止盈卖出"
                _sell(point.date, point.nav, state["shares"], state, trades, signals, fee_rate, reason)
                ctx["double_plan_lots"] = []
                state["invested"] = 0
                ctx["double_plan_target_reached"] = False
                ctx["double_plan_peak_nav"] = None
                ctx["double_plan_peak_metric"] = None
                ctx["double_plan_pause_buy_nav"] = None
                return

        pause_nav = ctx.get("double_plan_pause_buy_nav")
        if pause_nav:
            if point.nav <= pause_nav * (1 - restart_drop_percent / 100):
                ctx["double_plan_pause_buy_nav"] = None
            else:
                return

        lots = ctx.get("double_plan_lots") or []
        last_lot = lots[-1] if lots else None
        if last_lot and lot_take_profit_percent > 0:
            days_since_lot_buy = (datetime.strptime(point.date, "%Y-%m-%d") - datetime.strptime(last_lot["date"], "%Y-%m-%d")).days
            lot_return = (point.nav - last_lot["nav"]) / last_lot["nav"] * 100 if last_lot["nav"] > 0 else 0
            if days_since_lot_buy >= min_interval_days and lot_return >= lot_take_profit_percent:
                if _double_plan_sell_lot(point, state, trades, signals, fee_rate, last_lot, "牛市卖出"):
                    lots.pop()
                    ctx["double_plan_pause_buy_nav"] = point.nav
                    ctx["double_plan_last_buy_date"] = point.date
                    return

    last_trade_date = ctx.get("double_plan_last_buy_date")
    if last_trade_date:
        days_since = (datetime.strptime(point.date, "%Y-%m-%d") - datetime.strptime(last_trade_date, "%Y-%m-%d")).days
        if days_since < min_interval_days:
            return

    if base_amount <= 0:
        return

    elapsed_years = _elapsed_plan_years(start_date, point.date)
    available_budget = annual_capital * (elapsed_years + future_years)
    remaining_budget = max(available_budget - state["invested"], 0)
    if remaining_budget <= 0:
        return

    avg_cost_nav = _double_plan_average_cost_nav(ctx, state, point.nav)
    drop = (avg_cost_nav - point.nav) / avg_cost_nav * 100 if avg_cost_nav > 0 else 0
    seen_peak_nav = ctx.get("double_plan_seen_peak_nav") or point.nav
    peak_drawdown = (seen_peak_nav - point.nav) / seen_peak_nav * 100 if seen_peak_nav > 0 else 0

    has_position = state["shares"] > 0
    in_initial_low_zone = point.nav <= start_nav * (1 + initial_low_band_percent / 100)
    has_buy_opportunity = (
        not has_position
        or in_initial_low_zone
        or drop >= 0
        or peak_drawdown >= opportunity_drawdown_percent
    )
    if not has_buy_opportunity:
        return

    steps = max(math.floor(drop / drop_step_percent), 0)
    amount = base_amount * min(multiplier ** steps, max_multiplier)
    amount = min(amount, remaining_budget)

    reason = "计划定投" if steps <= 0 else f"低于持仓成本{round(drop, 2)}%，翻倍定投"
    if _buy(point.date, point.nav, amount, state, trades, signals, fee_rate, reason):
        trade = trades[-1]
        ctx["double_plan_lots"].append({
            "date": point.date,
            "cost": trade["amount"],
            "shares": abs(trade["share_delta"]),
            "nav": point.nav,
        })
        ctx["double_plan_last_buy_date"] = point.date
        ctx["double_plan_first_buy_date"] = ctx.get("double_plan_first_buy_date") or point.date
        ctx["double_plan_first_buy_nav"] = ctx.get("double_plan_first_buy_nav") or point.nav
        ctx["last_buy_nav"] = point.nav


def _double_plan_sell_lot(point, state, trades, signals, fee_rate, lot: Dict[str, Any], reason: str) -> bool:
    shares = min(lot.get("shares", 0), state["shares"])
    cost = min(lot.get("cost", 0), state["invested"])
    if shares <= 0:
        return False
    state["invested"] = max(state["invested"] - cost, 0)
    return _sell(point.date, point.nav, shares, state, trades, signals, fee_rate, reason)


def _double_plan_average_cost_nav(ctx: Dict[str, Any], state: Dict[str, float], fallback_nav: float) -> float:
    lots = ctx.get("double_plan_lots") or []
    total_shares = sum(lot.get("shares", 0) for lot in lots)
    total_cost = sum(lot.get("cost", 0) for lot in lots)
    if total_shares > 0:
        return total_cost / total_shares
    if state["shares"] > 0:
        return state["invested"] / state["shares"]
    return fallback_nav


def _elapsed_plan_years(start_date: str, current_date: str) -> int:
    days = (datetime.strptime(current_date, "%Y-%m-%d") - datetime.strptime(start_date, "%Y-%m-%d")).days
    return max(math.floor(days / 365.25) + 1, 1)


def _double_plan_target_value(target_type: str, target_percent: float) -> float:
    if target_type == "double":
        return 100
    return target_percent


def _double_plan_target_status(point, state, ctx, target_type: str, target_percent: float) -> Tuple[bool, float]:
    invested = state["invested"]
    value = state["shares"] * point.nav
    return_rate = (value - invested) / invested * 100 if invested > 0 else 0
    if target_type == "double":
        return return_rate >= 100, return_rate
    if target_type == "annual_return":
        lots = ctx.get("double_plan_lots") or []
        annual_returns = []
        for lot in lots:
            days = max((datetime.strptime(point.date, "%Y-%m-%d") - datetime.strptime(lot["date"], "%Y-%m-%d")).days, 1)
            years = days / 365.25
            lot_return = (lot["shares"] * point.nav - lot["cost"]) / lot["cost"] * 100 if lot["cost"] > 0 else 0
            annual = ((1 + lot_return / 100) ** (1 / years) - 1) * 100 if lot_return > -100 else -100
            annual_returns.append(annual)
        if annual_returns:
            metric = min(annual_returns)
            return metric >= target_percent, metric
        return False, 0
    if target_type == "absolute_return":
        return return_rate >= target_percent, return_rate
    return return_rate >= 100, return_rate


def _strategy_grid(index, point, state, trades, signals, fee_rate, params, ctx):
    amount = _num(params, "base_amount", 1000)
    grid_step = max(_num(params, "grid_step_percent", 3), 0.1) / 100
    sell_profit = max(_num(params, "sell_profit_percent", 5), 0.1) / 100
    max_buy = int(_num(params, "max_consecutive_buy", 99))
    max_sell = int(_num(params, "max_consecutive_sell", 99))
    base_nav = ctx["grid_base_nav"]

    if index == 0 and params.get("start_condition", "immediate") == "immediate":
        if _buy(point.date, point.nav, amount, state, trades, signals, fee_rate, "网格底仓"):
            ctx["last_buy_nav"] = point.nav
        return

    level = math.floor((point.nav - base_nav) / (base_nav * grid_step))
    if level < ctx["grid_level"] and ctx["consecutive_buy"] < max_buy:
        if _buy(point.date, point.nav, amount, state, trades, signals, fee_rate, "跌破网格买入"):
            ctx["grid_level"] = level
            ctx["consecutive_buy"] += 1
            ctx["consecutive_sell"] = 0
            ctx["last_buy_nav"] = point.nav
    elif state["shares"] > 0 and ctx.get("last_buy_nav") and point.nav >= ctx["last_buy_nav"] * (1 + sell_profit) and ctx["consecutive_sell"] < max_sell:
        sell_shares = min(state["shares"], amount / point.nav)
        if _sell(point.date, point.nav, sell_shares, state, trades, signals, fee_rate, "达到网格止盈"):
            ctx["grid_level"] = level
            ctx["consecutive_sell"] += 1
            ctx["consecutive_buy"] = 0


def _strategy_ma_timing(index, point, state, trades, signals, fee_rate, params, ctx, ma_cache):
    ma_days = int(_num(params, "ma_days", 20))
    base_amount = _num(params, "base_amount", 1000)
    buy_factor = _num(params, "below_ma_factor", 1.5)
    sell_percent = _num(params, "above_ma_sell_percent", 20) / 100
    frequency = params.get("frequency") or "weekly"
    ma = ma_cache.get(ma_days, [None] * (index + 1))[index]

    if ma is None:
        return
    if point.nav < ma and _period_due(point.date, frequency, params, ctx):
        _buy(point.date, point.nav, base_amount * buy_factor, state, trades, signals, fee_rate, f"低于MA{ma_days}加仓")
    elif point.nav > ma and sell_percent > 0 and state["shares"] > 0:
        _sell(point.date, point.nav, state["shares"] * sell_percent, state, trades, signals, fee_rate, f"高于MA{ma_days}减仓")


def _strategy_trend_timing(index, point, points, state, trades, signals, fee_rate, params, ctx, ma_cache):
    lookback = int(_num(params, "lookback_days", 20))
    base_amount = _num(params, "base_amount", 1000)
    weak_factor = _num(params, "weak_trend_factor", 0.5)
    sell_percent = _num(params, "downtrend_sell_percent", 15) / 100
    frequency = params.get("frequency") or "weekly"
    if index < lookback:
        return

    past_nav = points[index - lookback].nav
    trend_return = (point.nav - past_nav) / past_nav * 100 if past_nav else 0
    ma20 = ma_cache.get(20, [None] * (index + 1))[index]
    ma60 = ma_cache.get(60, [None] * (index + 1))[index]
    uptrend = trend_return >= _num(params, "trend_threshold_percent", 2) and (ma60 is None or ma20 is None or ma20 >= ma60)
    downtrend = trend_return <= -abs(_num(params, "downtrend_threshold_percent", 2))

    if uptrend and _period_due(point.date, frequency, params, ctx):
        _buy(point.date, point.nav, base_amount, state, trades, signals, fee_rate, "趋势向上买入")
    elif downtrend:
        if state["shares"] > 0 and sell_percent > 0:
            _sell(point.date, point.nav, state["shares"] * sell_percent, state, trades, signals, fee_rate, "趋势转弱减仓")
        elif _period_due(point.date, frequency, params, ctx):
            _buy(point.date, point.nav, base_amount * weak_factor, state, trades, signals, fee_rate, "弱趋势小额买入")


def _strategy_rocket_plan(index, point, state, trades, signals, fee_rate, params, ctx):
    base_amount = _num(params, "base_amount", 1000)
    trigger_percent = max(_num(params, "drop_trigger_percent", 5), 0.1)
    boost_factor = max(_num(params, "boost_factor", 1), 0)
    max_multiplier = max(_num(params, "max_multiplier", 5), 1)
    frequency = params.get("frequency") or "weekly"
    ctx["rocket_peak_nav"] = max(ctx.get("rocket_peak_nav") or point.nav, point.nav)

    if index == 0:
        if _buy(point.date, point.nav, base_amount, state, trades, signals, fee_rate, "火箭计划底仓"):
            ctx["last_buy_nav"] = point.nav
        return

    if not _period_due(point.date, frequency, params, ctx):
        return

    peak_nav = ctx.get("rocket_peak_nav") or point.nav
    drawdown = (peak_nav - point.nav) / peak_nav * 100 if peak_nav else 0
    if drawdown >= trigger_percent:
        stages = math.floor(drawdown / trigger_percent)
        multiplier = min(1 + stages * boost_factor, max_multiplier)
        reason = f"回撤{round(drawdown, 2)}%火箭加速"
    else:
        multiplier = 1
        reason = "火箭计划常规推进"
    if _buy(point.date, point.nav, base_amount * multiplier, state, trades, signals, fee_rate, reason):
        ctx["last_buy_nav"] = point.nav


def _strategy_ai_plan(index, point, points, state, trades, signals, fee_rate, params, ctx, ma_cache):
    base_amount = _num(params, "base_amount", 1000)
    frequency = params.get("frequency") or "weekly"
    dip_trigger = _num(params, "dip_trigger_percent", 4)
    dip_factor = _num(params, "dip_factor", 1.6)
    sell_percent = _num(params, "risk_off_sell_percent", 12) / 100
    lookback = int(_num(params, "lookback_days", 20))

    if index < 20:
        if index == 0:
            _buy(point.date, point.nav, base_amount, state, trades, signals, fee_rate, "智能计划底仓")
        return

    ma20 = ma_cache.get(20, [None] * (index + 1))[index]
    ma60 = ma_cache.get(60, [None] * (index + 1))[index]
    past_index = max(0, index - lookback)
    past_nav = points[past_index].nav
    trend_return = (point.nav - past_nav) / past_nav * 100 if past_nav else 0
    ctx["ai_peak_nav"] = max(ctx.get("ai_peak_nav") or point.nav, point.nav)
    peak_nav = ctx.get("ai_peak_nav") or point.nav
    drawdown = (peak_nav - point.nav) / peak_nav * 100 if peak_nav else 0

    if ma20 is not None and ma60 is not None and ma20 < ma60 and trend_return < 0:
        if state["shares"] > 0 and sell_percent > 0:
            _sell(point.date, point.nav, state["shares"] * sell_percent, state, trades, signals, fee_rate, "智能风控减仓")
        return

    if not _period_due(point.date, frequency, params, ctx):
        return

    if drawdown >= dip_trigger:
        _buy(point.date, point.nav, base_amount * dip_factor, state, trades, signals, fee_rate, "智能低位加仓")
    elif trend_return >= 0:
        _buy(point.date, point.nav, base_amount, state, trades, signals, fee_rate, "智能顺势买入")


def _strategy_dynamic_balance(index, point, state, trades, signals, fee_rate, params, ctx):
    target_percent = min(max(_num(params, "target_fund_percent", 60), 0), 100) / 100
    threshold = max(_num(params, "rebalance_threshold_percent", 5), 0.1) / 100
    frequency = params.get("frequency") or "monthly"
    total_asset = state["cash"] + state["shares"] * point.nav
    if total_asset <= 0:
        return

    if index == 0:
        _buy(point.date, point.nav, total_asset * target_percent, state, trades, signals, fee_rate, "动态平衡建仓")
        return

    if not _period_due(point.date, frequency, params, ctx):
        return

    fund_value = state["shares"] * point.nav
    current_percent = fund_value / total_asset
    target_value = total_asset * target_percent
    drift = current_percent - target_percent
    if drift > threshold:
        _sell(point.date, point.nav, (fund_value - target_value) / point.nav, state, trades, signals, fee_rate, "动态平衡卖出")
    elif drift < -threshold:
        _buy(point.date, point.nav, target_value - fund_value, state, trades, signals, fee_rate, "动态平衡买入")


def _strategy_two_eight_rotation(index, point, points, state, trades, signals, fee_rate, params, ctx, ma_cache):
    lookback = int(_num(params, "lookback_days", 20))
    strong_target = min(max(_num(params, "strong_target_percent", 80), 0), 100) / 100
    weak_target = min(max(_num(params, "weak_target_percent", 20), 0), 100) / 100
    switch_threshold = _num(params, "switch_threshold_percent", 1.5)
    frequency = params.get("frequency") or "weekly"
    if index < lookback:
        if index == 0:
            _buy(point.date, point.nav, state["cash"] * weak_target, state, trades, signals, fee_rate, "二八轮动防守底仓")
        return

    if not _period_due(point.date, frequency, params, ctx):
        return

    past_nav = points[index - lookback].nav
    momentum = (point.nav - past_nav) / past_nav * 100 if past_nav else 0
    ma20 = ma_cache.get(20, [None] * (index + 1))[index]
    ma60 = ma_cache.get(60, [None] * (index + 1))[index]
    target = strong_target if momentum >= switch_threshold and (ma60 is None or ma20 is None or ma20 >= ma60) else weak_target
    reason = "二八轮动进攻" if target == strong_target else "二八轮动防守"
    _rebalance_to_target(point.date, point.nav, target, state, trades, signals, fee_rate, reason)


def _strategy_buy_hold(index, point, state, trades, signals, fee_rate, params):
    if index != 0:
        return
    amount = _num(params, "amount", state["cash"])
    _buy(point.date, point.nav, amount, state, trades, signals, fee_rate, "买入持有基准")


def _run_target_profit_plan(points: List[NavPoint], capital: float, fee_rate: float, params: Dict[str, Any]) -> Dict[str, Any]:
    plan = _target_profit_defaults(capital, params)
    ma_cache = _moving_averages(points, [10, 60])
    cash = max(float(capital or 0), 0)
    lots: List[Dict[str, Any]] = []
    timeline: List[Dict[str, Any]] = []
    trades: List[Dict[str, Any]] = []
    signals: List[Dict[str, Any]] = []
    first_trade_date: Optional[str] = None
    last_trade_date: Optional[str] = None
    last_trade_nav: Optional[float] = None
    next_buy_amount = plan["buy_amount"]
    consecutive_buy_count = 0
    consecutive_sell_count = 0
    active = False
    completed_cycles = 0
    stopped = False
    pending_restart_after: Optional[str] = None
    cycle_start_cash = cash

    def current_shares() -> float:
        return sum(lot["shares"] for lot in lots)

    def current_cost() -> float:
        return sum(lot["cost"] for lot in lots)

    def current_value(nav: float) -> float:
        return current_shares() * nav

    def can_trade(date_text: str) -> bool:
        if not last_trade_date:
            return True
        days = (datetime.strptime(date_text, "%Y-%m-%d") - datetime.strptime(last_trade_date, "%Y-%m-%d")).days
        return days >= plan["min_trade_interval_days"]

    def append_trade(
        date: str,
        trade_type: str,
        amount: float,
        share_delta: float,
        nav: float,
        reason: str,
        fee: float = 0.0,
        cost_basis_override: Optional[float] = None,
    ) -> None:
        nonlocal first_trade_date, last_trade_date, last_trade_nav
        if first_trade_date is None:
            first_trade_date = date
        last_trade_date = date
        last_trade_nav = nav
        total_asset = cash + current_value(nav)
        cost = current_cost() if cost_basis_override is None else cost_basis_override
        total_return = total_asset - cycle_start_cash
        trades.append({
            "index": len(trades) + 1,
            "date": date,
            "type": trade_type,
            "type_label": "买入" if trade_type == "buy" else "卖出",
            "amount": round(amount, 2),
            "fee": round(fee, 2),
            "share_delta": round(share_delta, 4),
            "holding_shares": round(current_shares(), 4),
            "cost": round(cost, 2),
            "nav": round(nav, 4),
            "acc_nav": round(nav, 4),
            "value": round(current_value(nav), 2),
            "cash": round(cash, 2),
            "total_asset": round(total_asset, 2),
            "return": round(total_return, 2),
            "return_rate": round((total_return / cost * 100) if cost > 0 else 0, 2),
            "status": reason,
        })
        signals.append({"date": date, "type": trade_type, "nav": round(nav, 4), "reason": reason})

    def buy(date: str, nav: float, desired_amount: float, reason: str) -> bool:
        nonlocal cash
        amount = min(max(desired_amount, 0), cash)
        if amount <= 0 or nav <= 0:
            return False
        fee = 0.0
        shares = amount / nav
        cash -= amount
        lots.append({"date": date, "nav": nav, "shares": shares, "cost": amount})
        append_trade(date, "buy", amount, shares, nav, reason, fee)
        return True

    def sell_lot(date: str, nav: float, lot: Dict[str, Any], reason: str) -> bool:
        nonlocal cash
        if lot not in lots:
            return False
        gross = lot["shares"] * nav
        fee = 0.0
        amount = gross - fee
        cash += amount
        lots.remove(lot)
        append_trade(date, "sell", amount, -lot["shares"], nav, reason, fee)
        return True

    def sell_all(date: str, nav: float, reason: str) -> bool:
        nonlocal cash
        shares = current_shares()
        if shares <= 0:
            return False
        cost_before_sell = current_cost()
        gross = shares * nav
        fee = 0.0
        amount = gross - fee
        cash += amount
        lots.clear()
        append_trade(date, "sell", amount, -shares, nav, reason, fee, cost_before_sell)
        return True

    for index, point in enumerate(points):
        date = point.date
        nav = point.nav
        traded = False

        if pending_restart_after and date != pending_restart_after and not stopped:
            active = True
            cycle_start_cash = cash
            if buy(date, nav, plan["buy_amount"], "自动开启下一期定盈计划"):
                traded = True
                consecutive_buy_count = 1
                consecutive_sell_count = 0
                next_buy_amount = plan["buy_amount"] * (1 + plan["buy_increase_percent"] / 100)
                pending_restart_after = None

        if not active and not stopped and _target_profit_start_allowed(index, nav, ma_cache, plan):
            active = True
            traded = buy(date, nav, next_buy_amount, "定盈计划启动买入")
            consecutive_buy_count = 1 if traded else 0
            next_buy_amount = plan["buy_amount"] * (1 + plan["buy_increase_percent"] / 100)

        if active and not stopped and lots and can_trade(date):
            cost = current_cost()
            cycle_return = cash + current_value(nav) - cycle_start_cash
            holding_return_rate = cycle_return / cost * 100 if cost > 0 else 0

            if holding_return_rate >= plan["profit_target_percent"]:
                if sell_all(date, nav, "盈利目标达成，阶段完成"):
                    traded = True
                    completed_cycles += 1
                    consecutive_buy_count = 0
                    consecutive_sell_count = 0
                    next_buy_amount = plan["buy_amount"]
                    active = False
                    pending_restart_after = date
                    timeline.append(_target_profit_snapshot(date, nav, cash, lots, capital, traded))
                    continue

            last_lot = lots[-1] if lots else None
            if len(lots) > 1 and last_lot and consecutive_sell_count < plan["max_consecutive_sell"]:
                last_lot_return = (nav - last_lot["nav"]) / last_lot["nav"] * 100 if last_lot["nav"] else 0
                if last_lot_return >= plan["last_buy_rise_sell_percent"]:
                    if sell_lot(date, nav, last_lot, "最后一笔达到涨幅，卖出回笼"):
                        traded = True
                        consecutive_sell_count += 1
                        consecutive_buy_count = 0
                        next_buy_amount = plan["buy_amount"]

            if not traded and last_trade_nav:
                drop_from_last_trade = (last_trade_nav - nav) / last_trade_nav * 100 if last_trade_nav else 0
                if drop_from_last_trade >= plan["buy_drop_percent"] and current_cost() + next_buy_amount <= capital:
                    if buy(date, nav, next_buy_amount, "跌幅达标，继续买入"):
                        traded = True
                        consecutive_buy_count += 1
                        consecutive_sell_count = 0
                        next_buy_amount *= (1 + plan["buy_increase_percent"] / 100)

        timeline.append(_target_profit_snapshot(date, nav, cash, lots, capital, traded))

    summary = _build_target_profit_summary(points, timeline, trades, capital, first_trade_date, last_trade_date, completed_cycles)
    return {
        "summary": summary,
        "timeline": timeline,
        "trades": trades,
        "signals": signals,
        "metrics": {
            "holding_days": summary["holding_days"],
            "max_cost": summary["max_cost"],
            "cumulative_return": summary["total_return"],
            "average_invested": summary["average_invested"],
            "capital_return_rate": summary["return_rate"],
            "annual_return_rate": summary["annual_return"],
            "plan_return_rate": summary["plan_return_rate"],
            "plan_annual_return": summary["plan_annual_return"],
            "average_capital_usage": summary["capital_usage_rate"],
            "buy_count": summary["buy_count"],
            "sell_count": summary["sell_count"],
        },
        "strategy_config": {
            "strategy_type": "target_profit_plan",
            "capital": round(capital, 2),
            "fee_rate": round(fee_rate * 100, 4),
            "params": params,
            "resolved_params": plan,
        },
    }


def _target_profit_defaults(capital: float, params: Dict[str, Any]) -> Dict[str, Any]:
    plan_type = params.get("plan_type") or "manual"
    if plan_type == "auto_big":
        defaults = {
            "buy_amount": capital / 5,
            "profit_target_percent": 20,
            "buy_drop_percent": 5,
            "buy_increase_percent": 20,
            "last_buy_rise_sell_percent": 10,
            "max_consecutive_sell": 2,
        }
    elif plan_type == "auto_small":
        defaults = {
            "buy_amount": capital / 10,
            "profit_target_percent": 10,
            "buy_drop_percent": 3,
            "buy_increase_percent": 10,
            "last_buy_rise_sell_percent": 6,
            "max_consecutive_sell": 2,
        }
    else:
        defaults = {
            "buy_amount": capital / 10,
            "profit_target_percent": 10,
            "buy_drop_percent": 3,
            "buy_increase_percent": 10,
            "last_buy_rise_sell_percent": 6,
            "max_consecutive_sell": 2,
        }

    return {
        "plan_type": plan_type,
        "profit_target_percent": _num(params, "profit_target_percent", defaults["profit_target_percent"]),
        "buy_drop_percent": _num(params, "buy_drop_percent", defaults["buy_drop_percent"]),
        "buy_amount": _num(params, "buy_amount", defaults["buy_amount"]),
        "buy_increase_percent": _num(params, "buy_increase_percent", defaults["buy_increase_percent"]),
        "last_buy_rise_sell_percent": _num(params, "last_buy_rise_sell_percent", defaults["last_buy_rise_sell_percent"]),
        "max_consecutive_sell": int(_num(params, "max_consecutive_sell", defaults["max_consecutive_sell"])),
        "start_rule": params.get("start_rule") or "immediate",
        "start_price": _num(params, "start_price", 0),
        "min_trade_interval_days": int(_num(params, "min_trade_interval_days", 5)),
    }


def _target_profit_start_allowed(index: int, nav: float, ma_cache: Dict[int, List[Optional[float]]], plan: Dict[str, Any]) -> bool:
    rule = plan["start_rule"]
    if rule == "immediate":
        return index >= 0
    if rule == "ma10_above_ma60":
        ma10 = ma_cache.get(10, [None] * (index + 1))[index]
        ma60 = ma_cache.get(60, [None] * (index + 1))[index]
        return ma10 is not None and ma60 is not None and ma10 > ma60
    if rule == "price_gt":
        return plan["start_price"] > 0 and nav > plan["start_price"]
    if rule == "price_lt":
        return plan["start_price"] > 0 and nav < plan["start_price"]
    return False


def _target_profit_snapshot(date: str, nav: float, cash: float, lots: List[Dict[str, Any]], capital: float, traded: bool) -> Dict[str, Any]:
    shares = sum(lot["shares"] for lot in lots)
    cost = sum(lot["cost"] for lot in lots)
    value = shares * nav
    total_asset = cash + value
    total_return = total_asset - capital
    return {
        "date": date,
        "nav": round(nav, 4),
        "invested": round(cost, 2),
        "cash": round(cash, 2),
        "shares": round(shares, 4),
        "value": round(value, 2),
        "total_asset": round(total_asset, 2),
        "return": round(total_return, 2),
        "return_rate": round((value - cost) / cost * 100, 2) if cost > 0 else 0,
        "is_investment_day": traded,
        "status": "holding" if shares > 0 else "cash",
    }


def _build_target_profit_summary(
    points: List[NavPoint],
    timeline: List[Dict[str, Any]],
    trades: List[Dict[str, Any]],
    capital: float,
    first_trade_date: Optional[str],
    last_trade_date: Optional[str],
    completed_cycles: int,
) -> Dict[str, Any]:
    final = timeline[-1]
    if first_trade_date and last_trade_date:
        days = max((datetime.strptime(last_trade_date, "%Y-%m-%d") - datetime.strptime(first_trade_date, "%Y-%m-%d")).days, 0)
    else:
        days = max((datetime.strptime(points[-1].date, "%Y-%m-%d") - datetime.strptime(points[0].date, "%Y-%m-%d")).days, 0)
    active_timeline = [item for item in timeline if item["invested"] > 0]
    invested_values = [item["invested"] for item in active_timeline] or [0]
    average_invested = sum(invested_values) / len(invested_values) if invested_values else 0
    total_return = final["return"]
    input_return_rate = total_return / average_invested * 100 if average_invested > 0 else 0
    plan_return_rate = total_return / capital * 100 if capital > 0 else 0
    years = days / 365.25 if days > 0 else 0
    input_annual = input_return_rate / years if years > 0 else 0
    plan_annual = plan_return_rate / years if years > 0 else 0
    buy_count = sum(1 for trade in trades if trade["type"] == "buy")
    sell_count = sum(1 for trade in trades if trade["type"] == "sell")
    return {
        "total_capital": round(capital, 2),
        "total_invested": round(final["invested"], 2),
        "final_value": round(final["value"], 2),
        "cash": round(final["cash"], 2),
        "total_asset": round(final["total_asset"], 2),
        "total_return": round(total_return, 2),
        "return_rate": round(input_return_rate, 2),
        "annual_return": round(input_annual, 2),
        "plan_return_rate": round(plan_return_rate, 2),
        "plan_annual_return": round(plan_annual, 2),
        "max_drawdown": _max_drawdown([item["total_asset"] for item in timeline]),
        "holding_days": days,
        "max_cost": round(max(invested_values) if invested_values else 0, 2),
        "average_invested": round(average_invested, 2),
        "capital_usage_rate": round(average_invested / capital * 100, 2) if capital > 0 else 0,
        "buy_count": buy_count,
        "sell_count": sell_count,
        "investment_count": buy_count,
        "trade_count": len(trades),
        "completed_cycles": completed_cycles,
        "fee": round(sum(trade["fee"] for trade in trades), 2),
    }


def _max_drawdown(values: List[float]) -> float:
    if not values:
        return 0
    peak = values[0]
    drawdown = 0
    for value in values:
        peak = max(peak, value)
        if peak > 0:
            drawdown = min(drawdown, (value - peak) / peak * 100)
    return round(drawdown, 2)


def _rebalance_to_target(date, nav, target_percent, state, trades, signals, fee_rate, reason):
    total_asset = state["cash"] + state["shares"] * nav
    if total_asset <= 0:
        return False
    fund_value = state["shares"] * nav
    target_value = total_asset * target_percent
    diff = target_value - fund_value
    if abs(diff) < 1:
        return False
    if diff > 0:
        return _buy(date, nav, diff, state, trades, signals, fee_rate, reason)
    return _sell(date, nav, abs(diff) / nav, state, trades, signals, fee_rate, reason)


def _period_due(date_text: str, frequency: str, params: Dict[str, Any], ctx: Dict[str, Any]) -> bool:
    dt = datetime.strptime(date_text, "%Y-%m-%d")
    if frequency == "daily":
        return True
    if frequency == "weekly":
        target_weekday = int(_num(params, "weekday", _num(params, "investment_day", 0)))
        week_key = (dt.year, dt.isocalendar()[1])
        if dt.weekday() >= target_weekday and ctx.get("last_period_key") != ("w", week_key):
            ctx["last_period_key"] = ("w", week_key)
            return True
        return False
    month_key = (dt.year, dt.month)
    target_day = int(_num(params, "month_day", _num(params, "investment_day", 1)))
    if dt.day >= target_day and ctx.get("last_period_key") != ("m", month_key):
        ctx["last_period_key"] = ("m", month_key)
        return True
    return False


def _moving_averages(points: List[NavPoint], windows: List[int]) -> Dict[int, List[Optional[float]]]:
    result: Dict[int, List[Optional[float]]] = {}
    navs = [p.nav for p in points]
    for window in windows:
        values: List[Optional[float]] = []
        for idx in range(len(navs)):
            if idx + 1 < window:
                values.append(None)
            else:
                chunk = navs[idx + 1 - window:idx + 1]
                values.append(sum(chunk) / window)
        result[window] = values
    return result


def _build_summary(points: List[NavPoint], timeline: List[Dict[str, Any]], trades: List[Dict[str, Any]], capital: float) -> Dict[str, Any]:
    final = timeline[-1]
    start = datetime.strptime(points[0].date, "%Y-%m-%d")
    end = datetime.strptime(points[-1].date, "%Y-%m-%d")
    days = max((end - start).days, 0)
    invested_values = [item["invested"] for item in timeline]
    asset_values = [item["total_asset"] for item in timeline]
    average_invested = sum(invested_values) / len(invested_values) if invested_values else 0
    capital_usage = average_invested / capital * 100 if capital > 0 else 0
    return_rate = final["return_rate"]
    annual_return = 0
    if days > 0 and return_rate > -100:
        annual_return = ((1 + return_rate / 100) ** (365.25 / days) - 1) * 100

    peak = asset_values[0] if asset_values else 0
    max_drawdown = 0
    for value in asset_values:
        peak = max(peak, value)
        if peak > 0:
            max_drawdown = min(max_drawdown, (value - peak) / peak * 100)

    buy_count = sum(1 for trade in trades if trade["type"] == "buy")
    sell_count = sum(1 for trade in trades if trade["type"] == "sell")
    return {
        "total_capital": round(capital, 2),
        "total_invested": round(final["invested"], 2),
        "final_value": round(final["value"], 2),
        "cash": round(final["cash"], 2),
        "total_asset": round(final["total_asset"], 2),
        "total_return": round(final["return"], 2),
        "return_rate": round(return_rate, 2),
        "annual_return": round(annual_return, 2),
        "max_drawdown": round(max_drawdown, 2),
        "holding_days": days,
        "max_cost": round(max(invested_values) if invested_values else 0, 2),
        "average_invested": round(average_invested, 2),
        "capital_usage_rate": round(capital_usage, 2),
        "buy_count": buy_count,
        "sell_count": sell_count,
        "investment_count": buy_count,
        "trade_count": len(trades),
        "fee": round(sum(trade["fee"] for trade in trades), 2),
    }


def _num(params: Dict[str, Any], key: str, default: float) -> float:
    value = params.get(key, default)
    if value is None or value == "":
        return float(default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)
