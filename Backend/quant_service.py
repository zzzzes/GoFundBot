# -*- coding: utf-8 -*-
"""
量化分析服务模块 — QuantService v2

完全重构。AI 是决策核心，不是附加功能。

工作流程：
1. 拉取数据库中的基金数据
2. 计算标准化的量化指标（收益/风险/经理/稳定性，0-100 分）
3. 将所有数据发给 LLM，让 AI 做最终决策：
   - 哪只基金能买
   - 每只买多少
   - 用什么方式买
   - 什么时候卖
4. LLM 返回结构化 JSON，前端直接展示

这样用户看到的每一个买入/卖出建议都是 AI 基于数据做的判断，
而不是硬编码的 if-else 规则。
"""

from __future__ import annotations

import json
import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

import os
from pathlib import Path
from dotenv import load_dotenv

# 加载环境变量（让 LLM 配置生效）
_env_path = Path(__file__).parent / '.env'
if not _env_path.exists():
    _env_path = Path(__file__).parent.parent / '.env'
load_dotenv(dotenv_path=_env_path)


# ============================================================================
# 工具函数
# ============================================================================

def _safe_float(val: Any, default: float = 0.0) -> float:
    if val is None:
        return default
    try:
        return float(val)
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
    if val is None:
        return fallback
    try:
        return float(val)
    except (ValueError, TypeError):
        return fallback

def _normalize_in_place(funds: List[Dict], key: str, invert: bool = False):
    """对 fund dict[key] 做 min-max 归一化到 0-100。小样本时 clamp 防止极端值"""
    vals = [_safe_float(f.get(key)) for f in funds]
    valid = [v for v in vals if v != 0.0]
    n = len(valid)
    if n == 0:
        lo, hi = 0.0, 100.0
    elif n < 10:
        # 小样本夹紧：用 20/80 分位数代替 min/max，避免极端值
        sv = sorted(valid)
        lo = sv[int(n * 0.2)] if n >= 5 else sv[0]
        hi = sv[int(n * 0.8)] if n >= 5 else sv[-1]
        if abs(hi - lo) < 1e-8:
            lo, hi = 0.0, 100.0
    else:
        lo, hi = min(valid), max(valid)
        if abs(hi - lo) < 1e-8:
            lo, hi = 0.0, 100.0
    for f, v in zip(funds, vals):
        if v == 0.0:
            f[f"{key}_norm"] = 50.0
        else:
            score = max(0, min(100, (v - lo) / (hi - lo) * 100.0))
            f[f"{key}_norm"] = 100.0 - score if invert else score


# ============================================================================
# 数据类
# ============================================================================

@dataclass
class PositionParams:
    total_capital: float = 100000.0
    max_single_fund_pct: float = 20.0
    max_total_funds: int = 8
    risk_budget: float = 12.0

@dataclass
class BacktestAdvancedParams:
    fund_code: str = ""
    start_date: str = "2020-01-01"
    end_date: str = ""
    strategy: str = "dca"
    base_amount: float = 1000.0
    fee_rate: float = 0.15
    pe_series: Optional[List[Dict]] = None
    ma_period: int = 60
    grid_step: float = 5.0

@dataclass
class QuantScore:
    fund_code: str = ""
    fund_name: str = ""
    fund_type: str = ""
    return_score: float = 0.0
    risk_score: float = 0.0
    manager_score: float = 0.0
    macro_score: float = 0.0
    stability_score: float = 0.0
    composite_score: float = 0.0
    suggested_position_pct: float = 0.0
    suggested_amount: float = 0.0
    operation: str = "hold"
    summary: str = ""
    strengths: List[str] = field(default_factory=list)
    weaknesses: List[str] = field(default_factory=list)
    # AI 生成的额外字段
    ai_reason: str = ""           # AI 给出的买入/卖出理由
    ai_risk_warning: str = ""     # AI 给出的风险提示
    ai_strategy_tip: str = ""     # AI 给出的操作建议


# ============================================================================
# QuantService — 重构版
# ============================================================================

class QuantService:

    def __init__(self):
        self._openai_client = None
        self._api_key = os.getenv("LLM_API_KEY", "")
        self._api_base = os.getenv("LLM_API_BASE", "https://api.deepseek.com/v1")
        self._model = os.getenv("LLM_MODEL", "deepseek-chat")

    @property
    def ai_available(self) -> bool:
        return bool(self._api_key)

    def _get_llm_client(self):
        """懒加载 OpenAI 兼容客户端"""
        if self._openai_client is None and self._api_key:
            try:
                from openai import OpenAI
                self._openai_client = OpenAI(
                    api_key=self._api_key,
                    base_url=self._api_base,
                )
            except ImportError:
                print("[QuantService] 请安装 openai: pip install openai")
            except Exception as e:
                print(f"[QuantService] LLM 初始化失败: {e}")
        return self._openai_client

    def _call_llm(self, system_prompt: str, user_prompt: str, max_tokens: int = 4096) -> Optional[str]:
        """调用 LLM，返回文本"""
        client = self._get_llm_client()
        if not client:
            return None
        try:
            resp = client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.3,
                max_tokens=max_tokens,
            )
            return resp.choices[0].message.content
        except Exception as e:
            print(f"[QuantService] LLM 调用失败: {e}")
            return None

    def _parse_json_from_llm(self, text: str) -> Dict:
        """从 LLM 返回中提取 JSON"""
        import re
        # 尝试 ```json ``` 代码块
        m = re.search(r'```json\s*([\s\S]*?)\s*```', text)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                pass
        # 尝试直接解析
        try:
            return json.loads(text.strip())
        except json.JSONDecodeError:
            pass
        # 尝试找到 { } 范围
        start = text.find('{')
        end = text.rfind('}')
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end+1])
            except json.JSONDecodeError:
                pass
        return {}

    # ==================================================================
    # 核心：AI 驱动的买入/卖出决策
    # ==================================================================

    def ai_decide(
        self,
        db,
        total_capital: float = 100000,
        risk_profile: str = "balanced",
        investment_goal: str = "长期资产增值",
        investment_horizon: str = "long",
        fund_codes: Optional[List[str]] = None,
        fund_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        AI 驱动的投资决策引擎。

        这个方法是整个量化系统的核心：收集所有基金的量化数据，
        构建一个完整的分析提示，然后让 LLM 做最终决策。

        Returns:
            {
                "ai_analysis": { ... LLM 返回的完整决策 ... },
                "funds_scored": [ ... 量化打分列表（供前端表格展示）... ],
                "llm_used": true/false,
                "model": "deepseek-chat"
            }
        """
        # Step 1: 量化打分（标准化到 0-100）
        scores = self._score_funds_internal(db, fund_codes, fund_type, risk_profile, total_capital, top_n=20)

        if not scores:
            return {"error": "没有找到符合条件的基金数据，请先同步数据", "funds_scored": [], "llm_used": False}

        # Step 2: 如果没有 AI，返回纯量化结果
        if not self.ai_available:
            return {
                "funds_scored": [s.__dict__ for s in scores],
                "llm_used": False,
                "ai_analysis": None,
                "message": "⚠️ 未配置 LLM API Key，显示的是纯量化打分结果。配置 DeepSeek Key 后可获得 AI 决策建议。",
            }

        # Step 3: 构建 AI 提示
        prompt = self._build_decision_prompt(scores, total_capital, risk_profile, investment_goal, investment_horizon)

        system_prompt = """你是一位拥有 15 年经验的基金投资顾问，也是量化投资专家。你的客户是基金投资新手。"""

        # Step 4: 调用 LLM
        llm_text = self._call_llm(system_prompt, prompt, max_tokens=4096)
        ai_analysis = self._parse_json_from_llm(llm_text) if llm_text else None

        return {
            "funds_scored": [s.__dict__ for s in scores],
            "llm_used": True,
            "model": self._model,
            "ai_analysis": ai_analysis,
        }

    # ==================================================================
    # 构建 AI 决策提示
    # ==================================================================

    def _build_decision_prompt(self, scores, total_capital, risk_profile, investment_goal, investment_horizon):
        """构建发送给 LLM 的完整决策提示"""
        profile_desc = {
            "conservative": "稳健型：追求资产保值增值，最大可承受回撤 10%，偏好低波动产品",
            "balanced": "平衡型：追求适度增长，最大可承受回撤 20%",
            "aggressive": "进取型：追求高回报，可承受较大波动",
            "momentum": "动量型：追逐市场趋势",
        }.get(risk_profile, "平衡型")

        horizon_desc = {"short": "短期 (1年以内)", "medium": "中期 (1-3年)", "long": "长期 (3年以上)"}.get(investment_horizon, "中期")

        lines = [f"""## 用户画像
- 可用资金：{total_capital:,.0f} 元
- 风险偏好：{profile_desc}
- 投资目标：{investment_goal}
- 投资期限：{horizon_desc}

## 基金量化评分数据（按综合分降序排列）

| # | 基金名称 | 代码 | 类型 | 综合 | 收益 | 风险 | 经理 | 稳定 | 仓位 |
|---|---------|------|------|------|------|------|------|------|------|"""]

        for i, s in enumerate(scores[:15], 1):
            lines.append(f"| {i} | {s.fund_name} | {s.fund_code} | {s.fund_type} | {s.composite_score} | {s.return_score} | {s.risk_score} | {s.manager_score} | {s.stability_score} | {s.suggested_position_pct}% |")

        lines.append(f"""
## 各基金详情
""")
        for s in scores[:8]:
            lines.append(f"""
### {s.fund_name} ({s.fund_code})
- 综合: {s.composite_score}/100 | 收益: {s.return_score}/100 | 风险: {s.risk_score}/100 | 经理: {s.manager_score}/100 | 稳定性: {s.stability_score}/100
- 建议仓位: {s.suggested_position_pct}% (≈{s.suggested_amount:,.0f}元)
- 亮点: {'; '.join(s.strengths)}
- 风险点: {'; '.join(s.weaknesses)}
""")

        lines.append(f"""
请基于以上数据，为这位新手投资者制定完整的投资方案。用中文输出 JSON（不要 markdown 代码块）：
{{
    "market_assessment": {{"overall_sentiment": "乐观/中性偏多/中性/中性偏空/谨慎", "sentiment_score": 0-100, "summary": "市场判断"}},
    "portfolio_plan": {{
        "total_to_invest": 数字,
        "cash_reserve": 数字,
        "cash_reserve_reason": "保留现金原因",
        "funds": [{{"fund_code": "xxx", "fund_name": "xxx", "action": "重仓买入/买入/少量配置/观望/减仓", "allocation_pct": 数字, "allocation_amount": 数字, "buy_method": "一次性买入/分3批/每月定投", "buy_reason": "买入理由", "risk_warning": "风险", "stop_loss_condition": "止损条件", "target_return": "预期年化"}}]
    }},
    "execution_plan": {{"phase_1": "建仓期操作", "phase_2": "持有期操作", "rebalance_rule": "调仓规则"}},
    "risk_management": {{"max_acceptable_drawdown": "最大回撤", "blacklist_conditions": ["清仓条件"]}},
    "newbie_guide": {{"key_metrics_explained": "指标解释", "common_mistakes": ["错误1", "错误2"], "next_steps": ["步骤1", "步骤2"]}}
}}""")

        return "\n".join(lines)

    # ============================================================
    # AI 深度分析单只基金（Tab 2 "单基金深度诊断"）
    # ============================================================

    def ai_analyze_single_fund(self, db, fund_code: str) -> Dict[str, Any]:
        """AI 深度分析单只基金：从 DB 拉数据 → 格式化为 prompt → LLM 诊断"""
        from models import FundBasicInfo, FundTrend, FundRiskMetrics, FundScreeningRank, FundExtraData

        rows = db.query(FundBasicInfo, FundRiskMetrics, FundScreeningRank, FundExtraData, FundTrend).outerjoin(
            FundRiskMetrics, FundBasicInfo.fund_code == FundRiskMetrics.fund_code
        ).outerjoin(
            FundScreeningRank, FundBasicInfo.fund_code == FundScreeningRank.fund_code
        ).outerjoin(
            FundExtraData, FundBasicInfo.fund_code == FundExtraData.fund_code
        ).outerjoin(
            FundTrend, FundBasicInfo.fund_code == FundTrend.fund_code
        ).filter(FundBasicInfo.fund_code == fund_code).first()

        if not rows:
            return {"success": False, "error": f"未找到基金 {fund_code}"}

        basic, risk, rank, extra, trend = rows
        perf = _json_loads(basic.performance_json)

        # 构建数据摘要
        fd = {
            "fund_code": basic.fund_code, "fund_name": basic.fund_name,
            "fund_type": basic.fund_type or "未知",
            "returns": {
                "1m": _safe_float(perf.get("1_month_return")),
                "3m": _safe_float(perf.get("3_month_return")),
                "6m": _safe_float(perf.get("6_month_return")),
                "1y": _safe_float(basic.return_1y or perf.get("1_year_return")),
                "2y": _safe_float(perf.get("2_year_return")),
                "3y": _safe_float(perf.get("3_year_return")),
            },
            "risk": {
                "sharpe": _none_to(risk.sharpe_ratio_1y if risk else None, 0),
                "calmar": _none_to(risk.calmar_ratio_1y if risk else None, 0),
                "max_dd": _none_to(risk.max_drawdown_1y if risk else None, 0),
                "volatility": _none_to(risk.volatility_1y if risk else None, 0),
            },
            "rankings": {
                "1y": _none_to(rank.rank_pct_1y if rank else None, 50),
                "3y": _none_to(rank.rank_pct_3y if rank else None, 50),
            },
            "pass_4433": bool(rank and rank.pass_4433 == 1),
            "manager": {},
        }

        mgr_json = _json_loads(extra.fund_managers_json if extra else "[]", [])
        if isinstance(mgr_json, dict):
            mgr_json = [mgr_json]
        if mgr_json:
            m = mgr_json[0]
            fd["manager"] = {"name": m.get("name", "?"), "experience": m.get("work_experience", "?"),
                             "scale": m.get("managed_fund_size", "?"), "star": m.get("star_rating", "?")}

        nw = _json_loads(trend.net_worth_trend_json if trend else "[]", [])
        recent = []
        for item in nw[-90:]:
            nav = _safe_float(item.get("net_worth", 0)) if isinstance(item, dict) else _safe_float(item)
            if nav > 0: recent.append(nav)
        if len(recent) >= 20:
            fd["trend"] = {"latest": recent[-1], "ma20": round(sum(recent[-20:]) / 20, 4),
                           "ma60": round(sum(recent[-min(60, len(recent)):]) / min(60, len(recent)), 4)}

        if not self.ai_available:
            return {"success": True, "fund_data": fd, "llm_used": False,
                    "ai_analysis": None, "message": "⚠️ 未配置 LLM API Key"}

        prompt = json.dumps(fd, ensure_ascii=False, indent=2)
        system = """你是资深基金分析师。基于数据分析给出买卖建议。输出 JSON：
```json
{
    "action": "强烈买入/买入/持有/减仓/卖出",
    "confidence": 0-100,
    "score": 0-100,
    "summary": "一句话总结",
    "bull_case": ["看多理由"],
    "bear_case": ["看空理由"],
    "key_metrics_analysis": {"return": "收益分析", "risk": "风险分析", "manager": "经理分析", "trend": "趋势分析"},
    "suggested_entry": {"method": "一次性/分3批/定投/等回调", "reason": "理由"},
    "suggested_exit": {"stop_loss": "止损条件", "take_profit": "止盈条件"}
}
```"""
        llm_text = self._call_llm(system, prompt, max_tokens=2048)
        return {"success": True, "fund_data": fd, "llm_used": True, "model": self._model,
                "ai_analysis": self._parse_json_from_llm(llm_text) if llm_text else None}

    # ==================================================================
    # 内部：标准化量化打分（给 AI 提供结构化输入）
    # ==================================================================

    def _score_funds_internal(
        self, db, fund_codes, fund_type, risk_profile, total_capital, top_n=20
    ) -> List[QuantScore]:
        """内部打分方法，返回标准化 0-100 分数"""
        from models import FundBasicInfo, FundRiskMetrics, FundScreeningRank, FundExtraData

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

        rows = query.all()
        if not rows:
            return []

        funds = []
        for basic, risk, rank, extra in rows:
            perf = _json_loads(basic.performance_json)
            f = {
                "fund_code": basic.fund_code,
                "fund_name": basic.fund_name,
                "fund_type": basic.fund_type or "未知",
                "return_1y": _safe_float(basic.return_1y or perf.get("1_year_return")),
                "return_3y": _safe_float(perf.get("3_year_return")),
                "return_6m": _safe_float(perf.get("6_month_return")),
                "return_3m": _safe_float(perf.get("3_month_return")),
                "sharpe_1y": _none_to(risk.sharpe_ratio_1y if risk else None, 0),
                "sharpe_3y": _none_to(risk.sharpe_ratio_3y if risk else None, 0),
                "calmar_1y": _none_to(risk.calmar_ratio_1y if risk else None, 0),
                "max_drawdown_1y": _none_to(risk.max_drawdown_1y if risk else None, 50),
                "volatility_1y": _none_to(risk.volatility_1y if risk else None, 25),
                "annual_return_1y": _none_to(risk.annual_return_1y if risk else None, 0),
                "rank_1y": _none_to(rank.rank_pct_1y if rank else None, 50),
                "rank_3y": _none_to(rank.rank_pct_3y if rank else None, 50),
                "rank_6m": _none_to(rank.rank_pct_6m if rank else None, 50),
                "rank_3m": _none_to(rank.rank_pct_3m if rank else None, 50),
                "pass_4433": bool(rank and rank.pass_4433 == 1),
                "manager_json": extra.fund_managers_json if extra else None,
            }
            mgr = _json_loads(f["manager_json"], [])
            if isinstance(mgr, dict):
                mgr = [mgr]
            f["manager_years"] = _safe_float(mgr[0].get("work_experience", "5年") if mgr else 5)
            f["manager_avg_return"] = _safe_float(mgr[0].get("years_avg_return", 0) if mgr else 0)
            funds.append(f)

        if not funds:
            return []

        # 归一化各维度到 0-100
        _normalize_in_place(funds, "return_1y", invert=False)
        _normalize_in_place(funds, "return_3y", invert=False)
        _normalize_in_place(funds, "rank_1y", invert=True)
        _normalize_in_place(funds, "rank_3y", invert=True)
        _normalize_in_place(funds, "sharpe_1y", invert=False)
        _normalize_in_place(funds, "calmar_1y", invert=False)
        _normalize_in_place(funds, "max_drawdown_1y", invert=True)
        _normalize_in_place(funds, "volatility_1y", invert=True)
        _normalize_in_place(funds, "manager_years", invert=False)
        _normalize_in_place(funds, "manager_avg_return", invert=False)

        # 风险偏好权重
        weights = {
            "conservative": {"ret": 15, "risk": 40, "mgr": 15, "macro": 10, "stab": 20},
            "balanced":     {"ret": 30, "risk": 30, "mgr": 15, "macro": 10, "stab": 15},
            "aggressive":   {"ret": 40, "risk": 20, "mgr": 15, "macro": 15, "stab": 10},
            "momentum":     {"ret": 45, "risk": 15, "mgr": 10, "macro": 15, "stab": 15},
        }.get(risk_profile, {"ret": 30, "risk": 30, "mgr": 15, "macro": 10, "stab": 15})

        total_w = sum(weights.values()) or 1

        for f in funds:
            f["return_score"] = (
                f["return_1y_norm"] * 0.35 + f["return_3y_norm"] * 0.25
                + f["rank_1y_norm"] * 0.25 + f["rank_3y_norm"] * 0.15
            )
            f["risk_score"] = (
                f["sharpe_1y_norm"] * 0.30 + f["calmar_1y_norm"] * 0.25
                + f["max_drawdown_1y_norm"] * 0.25 + f["volatility_1y_norm"] * 0.20
            )
            f["manager_score"] = f["manager_years_norm"] * 0.40 + f["manager_avg_return_norm"] * 0.60
            f["macro_score"] = 60.0

            ranks = [f["rank_1y"], f["rank_3y"], f["rank_6m"], f["rank_3m"]]
            valid = [r for r in ranks if r > 0]
            f["stability_score"] = max(0, 100 - statistics.stdev(valid) * 2) if len(valid) >= 2 else 50.0

            # 综合分 = 加权求和 / total_w，确保在 0-100 范围内
            raw = (
                weights["ret"] * f["return_score"]
                + weights["risk"] * f["risk_score"]
                + weights["mgr"] * f["manager_score"]
                + weights["macro"] * f["macro_score"]
                + weights["stab"] * f["stability_score"]
            )
            f["composite_score"] = raw / total_w

        # 排序
        funds.sort(key=lambda x: x["composite_score"], reverse=True)
        top = funds[:top_n]

        # 仓位
        pos = PositionParams(total_capital=total_capital)
        risk_inv_sum = 0.0
        for f in top:
            vol = max(_safe_float(f.get("volatility_1y"), 15), 2.0)
            f["_risk_inv"] = pos.risk_budget / vol
            risk_inv_sum += f["_risk_inv"]
        for f in top:
            if risk_inv_sum > 0:
                pct = (f["_risk_inv"] / risk_inv_sum) * pos.max_single_fund_pct * len(top) / 100 * 100
            else:
                pct = pos.max_single_fund_pct / len(top)
            pct = max(1.0, min(pct, pos.max_single_fund_pct))
            f["position_pct"] = round(pct, 1)
            f["position_amount"] = round(pos.total_capital * pct / 100, 0)

        # 构建输出
        results = []
        for f in top:
            cs = f["composite_score"]
            if cs >= 75:
                op = "buy_heavy"
            elif cs >= 60:
                op = "buy"
            elif cs >= 45:
                op = "hold"
            elif cs >= 30:
                op = "reduce"
            else:
                op = "sell"

            qs = QuantScore(
                fund_code=f["fund_code"], fund_name=f["fund_name"], fund_type=f["fund_type"],
                return_score=round(f["return_score"], 1), risk_score=round(f["risk_score"], 1),
                manager_score=round(f["manager_score"], 1), macro_score=round(f["macro_score"], 1),
                stability_score=round(f["stability_score"], 1), composite_score=round(cs, 1),
                suggested_position_pct=f["position_pct"], suggested_amount=f["position_amount"],
                operation=op,
                summary=self._summary(cs),
                strengths=self._strengths(f),
                weaknesses=self._weaknesses(f),
            )
            results.append(qs)
        return results

    def _summary(self, score):
        if score >= 75: return f"综合得分 {score:.0f}/100，各项指标优秀，建议作为核心配置"
        if score >= 60: return f"综合得分 {score:.0f}/100，整体良好，适合适量配置"
        if score >= 45: return f"综合得分 {score:.0f}/100，表现中规中矩，建议观望"
        return f"综合得分 {score:.0f}/100，多项指标不佳，暂不建议买入"

    def _strengths(self, f):
        s = []
        if f["return_score"] >= 70: s.append("历史收益优异")
        if f["risk_score"] >= 70: s.append("风险控制优秀")
        if f["manager_score"] >= 70: s.append("基金经理经验丰富")
        if f["stability_score"] >= 70: s.append("业绩稳定性好")
        if f.get("pass_4433"): s.append("通过4433经典筛选")
        return s[:3] if s else ["各维度表现均衡"]

    def _weaknesses(self, f):
        w = []
        if f["return_score"] <= 30: w.append("历史收益偏弱")
        if f["risk_score"] <= 30: w.append("风险调整后收益不足")
        if f["manager_score"] <= 30: w.append("基金经理需进一步观察")
        if f["stability_score"] <= 30: w.append("排名波动较大")
        return w[:3] if w else []

    # ==================================================================
    # 智能买入策略回测（保留原逻辑，增加 AI 结果解读）
    # ==================================================================

    def backtest_advanced(self, db, params: BacktestAdvancedParams) -> Dict[str, Any]:
        from models import FundTrend
        trend = db.query(FundTrend).filter(FundTrend.fund_code == params.fund_code).first()
        if not trend:
            return {"error": f"Fund {params.fund_code} not found", "success": False}

        raw = _json_loads(trend.net_worth_trend_json, [])
        nav_dict = {}
        for item in raw:
            d = item.get("date", "")
            n = item.get("net_worth")
            if d and n is not None:
                try: nav_dict[d] = float(n)
                except (ValueError, TypeError): continue

        sorted_dates = sorted(nav_dict.keys())
        if len(sorted_dates) < 2:
            return {"error": "Insufficient NAV data", "success": False}

        start_dt = self._parse_date(params.start_date)
        end_dt = self._parse_date(params.end_date) if params.end_date else datetime.now()
        dates = [d for d in sorted_dates if start_dt <= self._parse_date(d) <= end_dt]
        if len(dates) < 2:
            return {"error": f"只找到 {len(dates)} 条净值记录", "success": False}

        nav_dict = {d: nav_dict[d] for d in dates}
        pe_dict = {}
        if params.pe_series:
            for pe in params.pe_series:
                pe_dict[pe.get("date", "")] = pe.get("pe_percentile", 50)

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
        result["success"] = True

        # 用 AI 解读回测结果
        if self.ai_available and len(result.get("timeline", [])) > 10:
            ai_text = self._call_llm(
                system_prompt="你是一位量化回测分析师。请解读回测结果，用通俗语言解释给投资新手。输出 JSON：{\"verdict\": \"策略优秀/良好/一般/不推荐\", \"explanation\": \"通俗解释（100字内）\", \"best_for\": \"适合什么样的投资者\", \"caveat\": \"需要注意的风险\"}",
                user_prompt=f"策略: {params.strategy}, 最终收益率: {result.get('final_return_rate', 0):.1f}%, 年化: {result.get('annualized_return', 0):.1f}%, 最大回撤: {result.get('max_drawdown', 0):.1f}%, 总投入: {result.get('total_invested', 0):.0f}, 最终市值: {result.get('final_value', 0):.0f}",
                max_tokens=512,
            )
            result["ai_interpretation"] = self._parse_json_from_llm(ai_text) if ai_text else None

        return result

    def _backtest_dca(self, dates, nav_dict, p):
        total_invested = 0.0; total_shares = 0.0; timeline = []; current_month = None
        for date in dates:
            nav = nav_dict[date]; dt = self._parse_date(date); month_key = (dt.year, dt.month)
            is_invest = False
            if month_key != current_month:
                fee = p.base_amount * p.fee_rate / 100
                total_shares += (p.base_amount - fee) / nav
                total_invested += p.base_amount; current_month = month_key; is_invest = True
            value = total_shares * nav; ret_pct = (value - total_invested) / total_invested * 100 if total_invested > 0 else 0
            timeline.append({"date": date, "nav": round(nav, 4), "invested": round(total_invested, 2), "value": round(value, 2), "return_rate": round(ret_pct, 2), "is_invest_day": is_invest})
        return self._finalize(timeline, total_invested)

    def _backtest_value_averaging(self, dates, nav_dict, p):
        total_invested = 0.0; total_shares = 0.0; cash = 0.0; timeline = []; target_value = 0.0; month_count = 0; current_month = None
        for date in dates:
            nav = nav_dict[date]; dt = self._parse_date(date); month_key = (dt.year, dt.month)
            is_action = False; action_type = ""
            if month_key != current_month:
                month_count += 1; target_value = p.base_amount * month_count
                gap = target_value - total_shares * nav
                if gap > p.base_amount * 0.1:
                    buy_amount = min(gap, p.base_amount * 2)
                    total_shares += (buy_amount * (1 - p.fee_rate / 100)) / nav
                    total_invested += buy_amount; is_action = True; action_type = "buy"
                elif gap < -p.base_amount * 0.2:
                    sell_amount = min(-gap, total_shares * nav * 0.1)
                    total_shares -= sell_amount / nav; cash += sell_amount; is_action = True; action_type = "sell"
                current_month = month_key
            value = total_shares * nav + cash; ret_pct = (value - total_invested) / total_invested * 100 if total_invested > 0 else 0
            timeline.append({"date": date, "nav": round(nav, 4), "invested": round(total_invested, 2), "value": round(value, 2), "return_rate": round(ret_pct, 2), "is_action_day": is_action, "action": action_type, "target_value": round(target_value, 2)})
        return self._finalize(timeline, total_invested)

    def _backtest_grid(self, dates, nav_dict, p):
        total_invested = 0.0; total_shares = 0.0; cash = 0.0; timeline = []
        base_nav = nav_dict[dates[0]]; last_buy = base_nav; last_sell = base_nav; grid_amt = p.base_amount
        for date in dates:
            nav = nav_dict[date]; is_action = False; action_type = ""
            if nav <= last_buy * (1 - p.grid_step / 100):
                total_shares += (grid_amt * (1 - p.fee_rate / 100)) / nav
                total_invested += grid_amt; last_buy = nav; is_action = True; action_type = "grid_buy"
            if total_shares > 0 and nav >= last_sell * (1 + p.grid_step / 100):
                sell_shares = min(grid_amt / nav, total_shares * 0.2)
                total_shares -= sell_shares; cash += sell_shares * nav; last_sell = nav; is_action = True; action_type = "grid_sell"
            value = total_shares * nav + cash; ret_pct = (value - total_invested) / total_invested * 100 if total_invested > 0 else 0
            timeline.append({"date": date, "nav": round(nav, 4), "invested": round(total_invested, 2), "value": round(value, 2), "return_rate": round(ret_pct, 2), "is_action_day": is_action, "action": action_type})
        return self._finalize(timeline, total_invested)

    def _backtest_adaptive(self, dates, nav_dict, pe_dict, p):
        total_invested = 0.0; total_shares = 0.0; timeline = []; current_month = None
        def mult(x):
            if x <= 20: return 2.0
            elif x <= 40: return 1.5
            elif x <= 70: return 1.0
            elif x <= 85: return 0.5
            else: return 0.0
        for date in dates:
            nav = nav_dict[date]; dt = self._parse_date(date); month_key = (dt.year, dt.month)
            is_invest = False; m = 1.0
            if month_key != current_month:
                pe_pct = pe_dict.get(date, 50); m = mult(pe_pct)
                if m > 0:
                    amt = p.base_amount * m
                    total_shares += (amt * (1 - p.fee_rate / 100)) / nav
                    total_invested += amt
                current_month = month_key; is_invest = True
            value = total_shares * nav; ret_pct = (value - total_invested) / total_invested * 100 if total_invested > 0 else 0
            timeline.append({"date": date, "nav": round(nav, 4), "invested": round(total_invested, 2), "value": round(value, 2), "return_rate": round(ret_pct, 2), "is_invest_day": is_invest, "pe_multiplier": m})
        return self._finalize(timeline, total_invested)

    def _finalize(self, timeline, total_invested):
        if not timeline: return {"error": "Empty timeline"}
        last = timeline[-1]
        peak_val = max(t["value"] for t in timeline)
        dd = 0.0; peak = 0.0
        for t in timeline:
            peak = max(peak, t["value"])
            if peak > 0:
                dd = max(dd, (peak - t["value"]) / peak * 100)
        days = len(timeline)
        ann = ((1 + last["return_rate"] / 100) ** (365 / max(days, 1)) - 1) * 100
        return {
            "total_invested": round(total_invested, 2), "final_value": last["value"],
            "final_return_rate": last["return_rate"], "annualized_return": round(ann, 2),
            "max_drawdown": round(dd, 2), "peak_value": round(peak_val, 2),
            "investment_count": sum(1 for t in timeline if t.get("is_invest_day") or t.get("is_action_day")),
            "timeline": timeline,
        }

    def _parse_date(self, s):
        for fmt in ["%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"]:
            try: return datetime.strptime(s[:10], fmt)
            except (ValueError, IndexError): continue
        return datetime(2000, 1, 1)

    # ==================================================================
    # 动态卖出信号
    # ==================================================================

    def exit_signals(self, db, fund_code: str) -> Dict[str, Any]:
        from models import FundBasicInfo, FundTrend, FundRiskMetrics, FundExtraData
        basic = db.query(FundBasicInfo).filter(FundBasicInfo.fund_code == fund_code).first()
        if not basic: return {"error": f"Fund {fund_code} not found"}
        trend = db.query(FundTrend).filter(FundTrend.fund_code == fund_code).first()
        risk = db.query(FundRiskMetrics).filter(FundRiskMetrics.fund_code == fund_code).first()
        extra = db.query(FundExtraData).filter(FundExtraData.fund_code == fund_code).first()

        perf = _json_loads(basic.performance_json)
        return_1y = _safe_float(basic.return_1y or perf.get("1_year_return", 0))
        signals = []

        # 1. 估值
        if return_1y > 30: sig = ("red", 80, f"近1年涨幅 {return_1y:.1f}% 过高")
        elif return_1y > 15: sig = ("yellow", 45, f"近1年涨幅 {return_1y:.1f}% 偏高")
        else: sig = ("green", 10, f"近1年涨幅 {return_1y:.1f}% 合理")
        signals.append({"name": "估值过热", "level": sig[0], "score": sig[1], "detail": sig[2]})

        # 2. 趋势
        nav_data = _json_loads(trend.net_worth_trend_json if trend else "[]", [])
        if len(nav_data) >= 60:
            recent = nav_data[-60:]
            navs = [_safe_float(x.get("net_worth", 0)) for x in recent]
            valid = [n for n in navs if n > 0]
            if len(valid) >= 30:
                ma60 = sum(valid) / len(valid); latest = valid[-1]
                if latest < ma60 * 0.95: sig = ("red", 75, f"净值 {latest:.4f} 远低于60日均线 {ma60:.4f}")
                elif latest < ma60: sig = ("yellow", 40, f"净值 {latest:.4f} 略低于60日均线")
                else: sig = ("green", 5, "净值在60日均线上方")
            else: sig = ("green", 0, "数据不足")
        else: sig = ("green", 0, "数据不足")
        signals.append({"name": "趋势破位", "level": sig[0], "score": sig[1], "detail": sig[2]})

        # 3. 经理
        mgr = _json_loads(extra.fund_managers_json if extra else "[]", [])
        if isinstance(mgr, dict): mgr = [mgr]
        mgr_days = _safe_float(mgr[0].get("manage_days", 365)) if mgr else 365
        if mgr_days < 180: sig = ("red", 80, f"基金经理任职仅 {mgr_days:.0f} 天")
        elif mgr_days < 365: sig = ("yellow", 30, "基金经理任职不足1年")
        else: sig = ("green", 0, "基金经理任职稳定")
        signals.append({"name": "经理变更", "level": sig[0], "score": sig[1], "detail": sig[2]})

        # 4. 规模
        sig = ("green", 0, "规模数据不足")
        scale_json = _json_loads(trend.scale_fluctuation_json if trend else "[]", [])
        if isinstance(scale_json, list) and len(scale_json) >= 2:
            scales = [_safe_float(x.get("scale")) if isinstance(x, dict) else _safe_float(x) for x in scale_json]
            scales = [s for s in scales if s > 0]
            if len(scales) >= 2:
                growth = (scales[-1] - scales[0]) / scales[0] * 100
                if growth > 100: sig = ("red", 60, f"规模暴增 {growth:.0f}%")
                elif growth > 50: sig = ("yellow", 30, f"规模增长 {growth:.0f}%")
                else: sig = ("green", 5, f"规模变化 {growth:.0f}% 正常")
        signals.append({"name": "规模暴增", "level": sig[0], "score": sig[1], "detail": sig[2]})

        # 5. 止损
        max_dd = _safe_float(risk.max_drawdown_1y if risk else 0)
        if max_dd > 25: sig = ("red", 80, f"近1年最大回撤 {max_dd:.1f}% 超警戒")
        elif max_dd > 15: sig = ("yellow", 35, f"近1年最大回撤 {max_dd:.1f}%")
        else: sig = ("green", 5, f"近1年最大回撤 {max_dd:.1f}% 可控")
        signals.append({"name": "止损触发", "level": sig[0], "score": sig[1], "detail": sig[2]})

        reds = sum(1 for s in signals if s["level"] == "red")
        yellows = sum(1 for s in signals if s["level"] == "yellow")
        exit_score = sum(s["score"] for s in signals) / max(len(signals), 1)

        if reds >= 2: recommendation = "清仓"
        elif reds >= 1: recommendation = "减仓"
        elif yellows >= 2: recommendation = "观望"
        else: recommendation = "持有"

        # AI 复核（LLM 不可用时用规则自动生成）
        ai_review = None
        if self.ai_available:
            ai_text = self._call_llm(
                system_prompt="你是风控专家。基于卖出信号判断是否该卖出。输出 JSON：{\"agree\": true/false, \"action\": \"立即清仓/逐步减仓/暂时持有/逢高卖出\", \"explanation\": \"判断理由\"}",
                user_prompt=f"基金: {basic.fund_name}({fund_code}), 评分: {exit_score:.0f}/100, 信号: " + ", ".join(f"{s['name']}={s['level']}" for s in signals),
                max_tokens=300,
            )
            ai_review = self._parse_json_from_llm(ai_text) if ai_text else None

        # 如果 LLM 没返回，用规则生成 fallback
        if not ai_review:
            if reds >= 2:
                ai_review = {"agree": True, "action": "立即清仓", "explanation": f"触发{reds}个红色卖出信号，建议立即清仓离场。"}
            elif reds >= 1:
                ai_review = {"agree": True, "action": "逐步减仓", "explanation": f"出现{reds}个红色信号，建议分批减仓控制风险。"}
            elif yellows >= 2:
                ai_review = {"agree": True, "action": "暂时持有", "explanation": f"有{yellows}个黄色预警信号，暂时持有密切观察。"}
            else:
                ai_review = {"agree": False, "action": "继续持有", "explanation": "各维度信号正常，暂无卖出必要。"}

        return {
            "fund_code": fund_code, "fund_name": basic.fund_name,
            "signals": signals, "exit_score": round(exit_score, 1),
            "recommendation": recommendation,
            "summary": f"五维信号 {exit_score:.0f}/100，{recommendation}（🔴{reds} 🟡{yellows}）",
            "ai_review": ai_review,
        }

    # ==================================================================
    # AI 投资顾问（给前端对话用的）
    # ==================================================================

    def chat_advisor(
        self, db, total_capital: float, risk_profile: str,
        investment_goal: str, investment_horizon: str, fund_codes=None
    ) -> Dict:
        """
        等同于原来的 build_advisor_prompt，但现在 AI 真正参与决策。
        这是给「AI 投资顾问」Tab 用的。
        """
        return self.ai_decide(
            db=db, total_capital=total_capital, risk_profile=risk_profile,
            investment_goal=investment_goal, investment_horizon=investment_horizon,
            fund_codes=fund_codes,
        )


# ============================================================================
# 单例
# ============================================================================

_quant_service: Optional[QuantService] = None

def get_quant_service() -> QuantService:
    global _quant_service
    if _quant_service is None:
        _quant_service = QuantService()
    return _quant_service
