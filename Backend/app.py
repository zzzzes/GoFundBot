from flask import Flask, request, jsonify, g
from flask_cors import CORS
from database import init_db, SessionLocal
from models import (FundBasicInfo, FundTrend, FundEstimate, FundPortfolio, 
                    FundExtraData, FundWatchlist, FundWatchlistGroup, 
                    FundRiskMetrics, FundScreeningRank, DataFetchTask, FundNavHistory,
                    StockIndustry, FundIndustryTag, FundIndustryPerformance, FundEtfTracking)
from fund_api import FundAPI
from fund_list_cache import get_fund_list_cache
from ai_service import get_ai_service
from fund_master_routes import fund_master_bp
from data_service_routes import data_service_bp
from services.data_service_client import DataServiceClient, DataServiceError, get_data_service_client
from services.data_service_legacy_mapper import map_data_service_detail_to_legacy
from quant_service import get_quant_service, BacktestAdvancedParams, PositionParams
from services.backtest_engine import run_strategy_backtest
from sqlalchemy.orm import Session
from sqlalchemy import desc, asc, and_, or_, func, cast, Float
from datetime import datetime, timedelta
import json
import math
import os
import sys
import time
import threading
import time
import re
import requests

app = Flask(__name__, static_folder='static', static_url_path='')
CORS(app)  # 允许跨域请求

# 注册市场数据 Blueprint
app.register_blueprint(fund_master_bp)
app.register_blueprint(data_service_bp)

# ---------------------------------------------------------------------------
# DataService detail helpers (gray-release)
# ---------------------------------------------------------------------------

def _validate_data_service_fund_quality(mapped_data: dict):
    """Quality gate for DataService-mapped fund detail.

    Returns (passed: bool, issues: list[str]).
    Checks that critical fields exist and have meaningful data.
    """
    issues = []

    # 1. basic_info
    bi = mapped_data.get('basic_info', {}) or {}
    if not bi.get('fund_code'):
        issues.append('basic_info.fund_code missing')
    if not bi.get('fund_name'):
        issues.append('basic_info.fund_name missing')

    # 2. realtime_estimate
    est = mapped_data.get('realtime_estimate', {}) or {}
    if not est or est.get('missing'):
        issues.append('realtime_estimate missing')

    # 3. net_worth_trend non-empty
    nw = mapped_data.get('net_worth_trend', [])
    if not isinstance(nw, list) or len(nw) == 0:
        issues.append('net_worth_trend empty')

    # 4. portfolio stock_codes non-empty
    pf = mapped_data.get('portfolio', {}) or {}
    sc = pf.get('stock_codes', []) if isinstance(pf, dict) else []
    if not isinstance(sc, list) or len(sc) == 0:
        issues.append('portfolio.stock_codes empty')

    # 5. risk_metrics has at least some values
    rm = mapped_data.get('risk_metrics', {}) or {}
    rm_values = sum(1 for v in rm.values() if v is not None) if isinstance(rm, dict) else 0
    if rm_values == 0:
        issues.append('risk_metrics has no valid values')

    # 6. performance has core return fields
    perf = mapped_data.get('performance', {}) or {}
    has_perf = any(
        perf.get(k) is not None
        for k in ('1_year_return', '1_month_return', '3_month_return', '6_month_return')
    )
    if not has_perf:
        issues.append('performance missing core return fields')

    # 7. asset_allocation present
    aa = mapped_data.get('asset_allocation', {}) or {}
    if not aa or aa.get('missing'):
        issues.append('asset_allocation missing')

    passed = len(issues) == 0
    return passed, issues


def _get_fund_detail_from_data_service(fund_code: str) -> dict:
    """Fetch fund detail from DataService and map to legacy structure."""
    client = get_data_service_client()
    ds_payload = client.get_fund_detail(fund_code)
    result = map_data_service_detail_to_legacy(ds_payload)
    result['_data_source'] = {
        "mode": "data_service",
        "mapped": True,
        "completeness_score": 70,
        "replacement_tier": "tier2_gray_validation"
    }
    return result


def _try_data_service_fund_detail(fund_code: str):
    """Try DataService detail; return (mapped_data, quality_passed, issues) or None."""
    try:
        mapped = _get_fund_detail_from_data_service(fund_code)
        passed, issues = _validate_data_service_fund_quality(mapped)
        return mapped, passed, issues
    except DataServiceError as e:
        print(f"auto mode: DataService failed for {fund_code}, fallback to legacy: {e}")
        return None
    except Exception as e:
        print(f"auto mode: unexpected error for {fund_code}, fallback to legacy: {e}")
        return None


def get_db():
    if 'db' not in g:
        g.db = SessionLocal()
    return g.db

@app.teardown_appcontext
def teardown_db(exception):
    db = g.pop('db', None)
    if db is not None:
        db.close()

# 初始化数据库
init_db()
fund_api = FundAPI()
fund_list_cache = get_fund_list_cache()

def _json_dumps(data):
    return json.dumps(data, ensure_ascii=False) if data is not None else None

def _json_loads(data, default):
    if not data:
        return default
    try:
        return json.loads(data)
    except Exception:
        return default

def _normalize_stock_code(code):
    text = str(code or '').strip()
    text = re.sub(r'^(sh|sz|hk)', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\.(SH|SZ|HK)$', '', text, flags=re.IGNORECASE)
    if re.match(r'^[A-Za-z]{1,6}([.-][A-Za-z]{1,3})?$', text):
        return text.upper()
    return text if re.match(r'^\d{5,6}$', text) else text

def _is_us_stock_code(code):
    text = str(code or '').strip().upper()
    if not text or re.match(r'^\d{5,6}$', text):
        return False
    return bool(re.match(r'^[A-Z]{1,6}([.-][A-Z]{1,3})?$', text))

# 常见交易所后缀 → 市场映射
_EXCHANGE_SUFFIX_MAP = {
    '.NS': ('印度', 'IN'),   # 印度国家交易所
    '.BO': ('印度', 'IN'),   # 印度孟买交易所
    '.T':  ('日本', 'JP'),   # 东京交易所
    '.DE': ('德国', 'DE'),   # 德国 Xetra
    '.PA': ('法国', 'FR'),   # 法国 Euronext
    '.L':  ('英国', 'GB'),   # 伦敦交易所
    '.HK': ('香港', 'HK'),   # 港交所（冗余保护）
    '.MC': ('西班牙', 'ES'),  # 马德里
    '.MI': ('意大利', 'IT'),  # 米兰
    '.SW': ('瑞士', 'CH'),   # 瑞士
    '.AS': ('荷兰', 'NL'),   # 阿姆斯特丹
    '.BR': ('巴西', 'BR'),   # 巴西
    '.TO': ('加拿大', 'CA'),  # 多伦多
    '.V':  ('加拿大', 'CA'),  # 多伦多创业板
    '.AX': ('澳大利亚', 'AU'), # 澳大利亚
    '.KS': ('韩国', 'KR'),   # 韩国
    '.TW': ('台湾', 'TW'),   # 台湾
    '.SS': ('中国', 'CN'),   # 上海（A股）
    '.SZ': ('中国', 'CN'),   # 深圳（A股）
}

def _detect_market_from_code(code):
    """从股票代码后缀推断市场，返回 (市场中文名, 地区码) 或 None"""
    text = str(code or '').strip().upper()
    # 检查已知后缀
    for suffix, (name, region) in _EXCHANGE_SUFFIX_MAP.items():
        if text.endswith(suffix):
            return (name, region)
    # 纯字母、无后缀、不确定市场 → 不猜测，留给上层判断
    return None

def _market_hint_from_holdings(holdings, code):
    """从原始持仓数据中提取股票的市场信息作为兜底"""
    items = _portfolio_holding_items(holdings)
    for item in items:
        item_code = _normalize_stock_code(item.get('code', ''))
        if item_code != code:
            continue
        # 优先用持仓数据自带的 market/region 字段
        market = str(item.get('market') or item.get('region') or '').strip()
        name = str(item.get('name') or '').strip()
        # 常见中文市场名映射
        MARKET_NAME_MAP = {
            '印度': ('印度', 'IN'), '日本': ('日本', 'JP'),
            '德国': ('德国', 'DE'), '法国': ('法国', 'FR'),
            '英国': ('英国', 'GB'), '美国': ('美股', 'US'),
            '美股': ('美股', 'US'), '港股': ('港股', 'HK'),
            '香港': ('香港', 'HK'), '韩国': ('韩国', 'KR'),
            '台湾': ('台湾', 'TW'), '越南': ('越南', 'VN'),
            '新加坡': ('新加坡', 'SG'), '澳大利亚': ('澳洲', 'AU'),
        }
        for key, (industry, region) in MARKET_NAME_MAP.items():
            if key in market or key in name:
                return {'industry': industry, 'region': region,
                        'name': name or code, 'source': 'holding_market_field'}
        # 从股票名称末尾的交易所后缀推断
        suffix_hint = _detect_market_from_code(name or item.get('original_code', code))
        if suffix_hint:
            return {'industry': suffix_hint[0], 'region': suffix_hint[1],
                    'name': name or code, 'source': 'holding_name_suffix'}
    # 从代码自身推断
    hint = _detect_market_from_code(code)
    if hint:
        return {'industry': hint[0], 'region': hint[1],
                'name': code, 'source': 'code_suffix_detect'}
    return {'industry': '海外', 'region': 'WW',
            'name': code, 'source': 'unknown_ticker'}

def _is_a_share_stock_code(code):
    return bool(re.match(r'^\d{6}$', str(code or '').strip()))

def _safe_ratio(value):
    if value is None:
        return 0.0
    text = str(value).strip().replace('%', '')
    try:
        return float(text)
    except Exception:
        return 0.0

def _portfolio_holding_items(raw_holdings):
    if not isinstance(raw_holdings, list):
        return []

    if raw_holdings and isinstance(raw_holdings[0], dict):
        items = []
        for item in raw_holdings:
            code = _normalize_stock_code(item.get('code') or item.get('stock_code') or item.get('gpdm'))
            if not code:
                continue
            ratio_value = item.get('ratio') or item.get('position') or item.get('hold_ratio')
            items.append({
                **item,
                'code': code,
                'name': item.get('name') or item.get('stock_name') or item.get('gpmc') or '',
                'ratio': _safe_ratio(ratio_value),
                'ratio_available': ratio_value not in (None, '', '-', '--'),
            })
        return items

    items = []
    for index in range(0, len(raw_holdings), 3):
        if index + 2 >= len(raw_holdings):
            break
        code = _normalize_stock_code(raw_holdings[index])
        if not code:
            continue
        ratio_value = raw_holdings[index + 2]
        items.append({
            'code': code,
            'name': raw_holdings[index + 1],
            'ratio': _safe_ratio(ratio_value),
            'ratio_available': ratio_value not in (None, '', '-', '--'),
        })
    return items

def _fund_name_or_type_text(portfolio: dict):
    if not isinstance(portfolio, dict):
        return ''
    parts = [
        portfolio.get('fund_name'),
        portfolio.get('name'),
        portfolio.get('fund_type'),
        portfolio.get('type'),
        portfolio.get('index_name'),
        portfolio.get('tracking_index'),
    ]
    return ' '.join(str(part) for part in parts if part)

FUND_TYPE_RULES = [
    ('货币型', ['货币', '现金', '货币市场']),
    ('债券型', ['债券', '纯债', '短债', '中短债', '可转债', '债券指数']),
]

BROAD_INDEX_KEYWORDS = [
    '沪深300', '中证500', '中证800', '中证1000', '中证2000',
    '上证50', '上证180', '深证100', '创业板指', '创业板50',
    '科创50', '科创100', 'A500', 'MSCI中国A50', '央企50',
    '宽基', '综合指数', '综指',
    'CSI 300', 'CSI300', 'CSI 500', 'CSI500', 'SSE 50',
]

ETF_FEEDER_KEYWORDS = [
    'ETF联接', 'ETF连接', 'ETF链接', '联接基金', '联接A', '联接C', 'ETF FEEDER',
]

MARKET_TOPIC_RULES = [
    ('美股科技', ['纳斯达克', 'NASDAQ', '纳指']),
    ('美股', ['标普', 'S&P', 'SP500', 'S&P500', '道琼斯', 'DOW JONES', '美国', '美股']),
    ('港股科技', ['恒生科技', '港股科技']),
    ('港股', ['恒生', '港股', '香港', 'HANG SENG', '国企指数']),
    ('全球市场', ['全球', '海外', '中国海外', '全球精选', '全球配置', '环球']),
    ('印度市场', ['印度', 'INDIA']),
    ('越南市场', ['越南', 'VIETNAM']),
    ('日本市场', ['日本', '日经', 'NIKKEI']),
    ('德国市场', ['德国', 'DAX']),
    ('法国市场', ['法国', 'CAC', 'CAC40']),
    ('英国市场', ['英国', '富时', 'FTSE']),
    ('韩国市场', ['韩国', 'KOSPI']),
    ('东南亚市场', ['东南亚', '东盟', 'ASEAN']),
    ('新兴市场', ['新兴市场', '新兴经济体']),
]

TOPIC_RULES = [
    ('半导体', ['半导体', '芯片', '集成电路', 'CHIP', 'SEMICONDUCTOR', '光模块', '光芯片', 'CPO', '先进封装']),
    ('证券', ['证券', '券商', '证券公司', '证券保险']),
    ('银行', ['银行', 'BANK']),
    ('保险', ['保险']),
    ('医药', ['医药', '医疗', '生物医药', '创新药', '医疗器械', 'HEALTH', 'PHARMA', '中药', '制药']),
    ('消费', ['消费', '食品饮料', '主要消费', '可选消费', '酒', '白酒', '家电', '零售', '电商']),
    ('新能源', ['新能源', '新能源汽车', '新能源车', '光伏', '太阳能', '储能', '电池', '锂电', '风电', '碳中和']),
    ('军工', ['军工', '国防', '航天', '航空', '军舰', '武器装备']),
    ('人工智能', ['人工智能', 'AI', '机器人', '智能', '算力', '大模型', '自动驾驶']),
    ('计算机', ['计算机', '软件', '云计算', '大数据', '互联网', 'SAAS', '信创', '数字经济']),
    ('通信', ['通信', '5G', '通讯', '6G', '卫星通信', '光通信']),
    ('电子', ['电子', '消费电子', '元件', 'PCB']),
    ('传媒', ['传媒', '游戏', '动漫', '影视', '短视频']),
    ('房地产', ['房地产', '地产']),
    ('黄金', ['黄金', '贵金属', 'GOLD']),
    ('有色金属', ['有色', '有色金属', '稀土', '矿业', '矿产']),
    ('煤炭', ['煤炭']),
    ('钢铁', ['钢铁']),
    ('化工', ['化工', '化学', '新材料', '高分子', '聚氨酯']),
    ('农业', ['农业', '农牧', '畜牧', '养殖', '种业', '渔业']),
    ('汽车', ['汽车', '整车', '零部件', '汽车电子']),
    ('电力', ['电力', '发电', '电网', '水电', '核电', '特高压']),
    ('交通运输', ['交通运输', '物流', '航运', '港口', '铁路']),
    ('环保', ['环保', '节能', '环境', '水处理']),
    ('建筑', ['建筑', '基建', '工程', '建材']),
    ('红利', ['红利', '股息', '高息', '高分红']),
    ('量化', ['量化', '多因子', '对冲', '绝对收益']),
    ('灵活配置', ['灵活配置', '灵活策略', '弹性配置']),
    ('行业轮动', ['行业轮动', '主题轮动', '景气轮动']),
]

def _match_keyword_rule(text, rules):
    text = str(text or '').upper()
    for topic, keywords in rules:
        for keyword in keywords:
            if keyword.upper() in text:
                return topic, keyword
    return None, None

def _is_broad_index_fund_text(text):
    text = str(text or '').upper()
    return any(keyword.upper() in text for keyword in BROAD_INDEX_KEYWORDS)

def _is_etf_feeder_text(text):
    text = str(text or '').upper()
    return any(keyword.upper() in text for keyword in ETF_FEEDER_KEYWORDS)

def _topic_from_fund_text(text):
    topic, _ = _match_keyword_rule(text, TOPIC_RULES)
    return topic

def _is_hk_holding_item(item):
    if not isinstance(item, dict):
        return False
    market = str(item.get('market') or item.get('region') or '').upper()
    code = str(item.get('code') or item.get('stock_code') or item.get('original_code') or '').strip()
    return market in ('HK', 'HKG', '香港', '港股') or bool(re.match(r'^\d{5}$', code))

def _fund_text_industry_fallback(text):
    text = str(text or '')
    fund_type, type_keyword = _match_keyword_rule(text, FUND_TYPE_RULES)
    if fund_type:
        return {
            'name': fund_type,
            'ratio': 0.0,
            'count': 0,
            'basis': 'fund_type',
            'source': 'fund_name_rule',
            'matched_keyword': type_keyword,
        }

    market_topic, market_keyword = _match_keyword_rule(text, MARKET_TOPIC_RULES)
    if market_topic:
        return {
            'name': market_topic,
            'ratio': 0.0,
            'count': 0,
            'basis': 'market_region',
            'source': 'fund_name_rule',
            'matched_keyword': market_keyword,
        }

    if _is_broad_index_fund_text(text):
        return {
            'name': '宽基指数',
            'ratio': 0.0,
            'count': 0,
            'basis': 'broad_index_name',
            'source': 'fund_name_rule',
        }

    topic, topic_keyword = _match_keyword_rule(text, TOPIC_RULES)
    if topic:
        return {
            'name': topic,
            'ratio': 0.0,
            'count': 0,
            'basis': 'index_topic' if ('指数' in text or 'ETF' in text.upper()) else 'fund_name_topic',
            'source': 'fund_name_rule',
            'matched_keyword': topic_keyword,
        }

    if _is_etf_feeder_text(text):
        return {
            'name': '指数联接',
            'ratio': 0.0,
            'count': 0,
            'basis': 'index_topic',
            'source': 'fund_name_rule',
        }
    return None

def _upsert_stock_industry(db: Session, stock_data: dict):
    code = _normalize_stock_code(stock_data.get('code'))
    if not code:
        return None

    record = db.query(StockIndustry).filter(StockIndustry.stock_code == code).first()
    if not record:
        record = StockIndustry(stock_code=code)
        db.add(record)

    concepts = stock_data.get('concepts')
    if not isinstance(concepts, list):
        concepts = []

    record.stock_name = stock_data.get('name') or record.stock_name
    record.industry = stock_data.get('industry') or record.industry
    record.region = stock_data.get('region') or record.region
    record.concepts_json = _json_dumps(concepts)
    record.source = stock_data.get('source') or 'data_service'
    record.updated_time = datetime.now()
    return record

def _stock_industry_payload(record):
    return {
        'stock_code': record.stock_code,
        'stock_name': record.stock_name,
        'industry': record.industry,
        'region': record.region,
        'concepts': _json_loads(record.concepts_json, []),
        'source': record.source,
        'updated_time': record.updated_time.isoformat() if record.updated_time else None,
    }

def _fetch_stock_industry_batch(db: Session, codes, force_refresh=False, timeout=None):
    normalized_codes = []
    for code in codes or []:
        normalized = _normalize_stock_code(code)
        if normalized and re.match(r'^\d{5,6}$', normalized) and normalized not in normalized_codes:
            normalized_codes.append(normalized)

    if not normalized_codes:
        return {}, []

    industry_map = {}
    failed_codes = []
    chunk_size = 100
    client = DataServiceClient(timeout=timeout if timeout is not None else (8.0 if force_refresh else 4.0))
    for index in range(0, len(normalized_codes), chunk_size):
        chunk = normalized_codes[index:index + chunk_size]
        try:
            payload = client.get_stock_references(chunk)
            ds_data = payload.get('data', {}) if isinstance(payload, dict) else {}
            ds_items = ds_data.get('items', []) if isinstance(ds_data, dict) else []
            success_codes = set()
            for result in ds_items:
                if not isinstance(result, dict) or not result.get('success'):
                    continue
                stock_data = result.get('data') if isinstance(result.get('data'), dict) else {}
                record = _upsert_stock_industry(db, stock_data)
                if record:
                    industry_map[record.stock_code] = _stock_industry_payload(record)
                    success_codes.add(record.stock_code)
            failed_codes.extend(code for code in chunk if code not in success_codes)
        except DataServiceError as exc:
            print(f"stock industry dictionary: DataService unavailable for {len(chunk)} codes: {exc}")
            failed_codes.extend(chunk)
        except Exception as exc:
            print(f"stock industry dictionary: unexpected error for {len(chunk)} codes: {exc}")
            failed_codes.extend(chunk)

    return industry_map, failed_codes

def _resolve_stock_industries(db: Session, holdings, force_refresh=False, allow_network=False):
    items = _portfolio_holding_items(holdings)
    codes = []
    for item in items:
        code = _normalize_stock_code(item.get('code'))
        if code and code not in codes:
            codes.append(code)

    if not codes:
        return {}, []

    records = db.query(StockIndustry).filter(StockIndustry.stock_code.in_(codes)).all()
    industry_map = {record.stock_code: _stock_industry_payload(record) for record in records}

    # 收集需要网络查询的代码：本地没有行业信息的 A 股和字母代码
    need_network = []
    for code in codes:
        if industry_map.get(code, {}).get('industry'):
            continue
        # A 股（6 位数字）或字母代码都尝试网络查询
        if _is_a_share_stock_code(code) or _is_us_stock_code(code):
            need_network.append(code)

    if allow_network and need_network:
        fetched_map, _ = _fetch_stock_industry_batch(
            db, need_network, force_refresh=force_refresh,
        )
        industry_map.update(fetched_map)

    # 网络查询后仍未解析的字母代码 → 兜底推断
    for code in codes:
        if industry_map.get(code, {}).get('industry'):
            continue
        if not _is_us_stock_code(code):
            continue
        market_hint = _market_hint_from_holdings(holdings, code)
        record = db.query(StockIndustry).filter(StockIndustry.stock_code == code).first()
        if not record:
            record = StockIndustry(stock_code=code)
            db.add(record)
        record.stock_name = record.stock_name or market_hint.get('name') or code
        record.industry = market_hint.get('industry') or '海外'
        record.region = market_hint.get('region') or 'WW'
        record.source = market_hint.get('source', 'rule.unknown_ticker')
        record.updated_time = datetime.now()
        industry_map[code] = _stock_industry_payload(record)

    unresolved = [code for code in codes if not industry_map.get(code, {}).get('industry')]
    return industry_map, unresolved

def _enhance_portfolio_industries(db: Session, portfolio: dict, force_refresh=False):
    if not isinstance(portfolio, dict):
        return portfolio

    target_keys = ['stock_codes_new', 'stock_codes']
    all_holdings = []
    for key in target_keys:
        all_holdings.extend(_portfolio_holding_items(portfolio.get(key)))

    industry_map, unresolved = _resolve_stock_industries(
        db,
        all_holdings,
        force_refresh=force_refresh,
        allow_network=force_refresh,
    )

    for key in target_keys:
        items = _portfolio_holding_items(portfolio.get(key))
        if not items:
            continue
        enhanced = []
        for item in items:
            info = industry_map.get(item.get('code'), {})
            enhanced.append({
                **item,
                'industry': info.get('industry'),
                'region': info.get('region'),
                'concepts': info.get('concepts') or [],
            })
        portfolio[key] = enhanced

    portfolio['industry_unresolved_codes'] = unresolved
    portfolio['industry_tag'] = _build_portfolio_industry_tag(portfolio)
    return portfolio

def _build_portfolio_industry_tag(portfolio: dict):
    if not isinstance(portfolio, dict):
        return None

    fund_text = _fund_name_or_type_text(portfolio)
    fallback_tag = _fund_text_industry_fallback(fund_text)
    holdings = _portfolio_holding_items(portfolio.get('stock_codes_new')) or _portfolio_holding_items(portfolio.get('stock_codes'))

    if not holdings:
        return fallback_tag or {
            'name': '混合型',
            'ratio': 0.0,
            'count': 0,
            'basis': 'mixed',
            'source': 'top_stock_holdings',
            'reason': 'missing_holdings',
        }

    buckets = {}
    total_ratio = 0.0
    total_ratio_count = 0
    for item in holdings:
        industry = (
            item.get('industry')
            or item.get('industry_name')
            or item.get('industryName')
            or item.get('sector')
            or item.get('sector_name')
        )
        if not industry and _is_us_stock_code(item.get('code')):
            # 不再直接假定为美股，优先用持仓自带的 industry，其次从代码后缀推断
            item_industry = item.get('industry') or item.get('industry_name')
            if item_industry and item_industry not in ('美股',):
                industry = item_industry
            else:
                hint = _detect_market_from_code(item.get('code') or '')
                industry = hint[0] if hint else '海外'
        if not industry and _is_hk_holding_item(item):
            industry = '港股'
        if not industry:
            continue

        ratio = _safe_ratio(item.get('ratio'))
        ratio_available = bool(item.get('ratio_available')) and ratio > 0
        if ratio_available:
            total_ratio += ratio
            total_ratio_count += 1
        bucket = buckets.setdefault(industry, {
            'name': industry,
            'ratio': 0.0,
            'ratio_count': 0,
            'count': 0,
            'stocks': [],
        })
        if ratio_available:
            bucket['ratio'] += ratio
            bucket['ratio_count'] += 1
        bucket['count'] += 1
        bucket['stocks'].append({
            'code': item.get('code'),
            'name': item.get('name'),
            'ratio': ratio if ratio_available else None,
        })

    if not buckets:
        return fallback_tag or {
            'name': '混合型',
            'ratio': 0.0,
            'count': 0,
            'basis': 'mixed',
            'source': 'top_stock_holdings',
            'reason': 'unresolved_holdings',
        }

    top = sorted(
        buckets.values(),
        key=lambda item: (item['count'], item['ratio']),
        reverse=True,
    )[0]
    top_by_ratio = sorted(
        buckets.values(),
        key=lambda item: (item['ratio'], item['count']),
        reverse=True,
    )[0]

    valid_count = sum(item['count'] for item in buckets.values())
    top_share = (
        (top_by_ratio['ratio'] / total_ratio * 100)
        if total_ratio > 0
        else 0.0
    )
    count_share = top['count'] / valid_count * 100 if valid_count else 0.0
    market_dominant = top['name'] in ('美股', '港股', '海外', '印度', '日本', '德国', '法国', '英国', '越南', '韩国') and top['count'] >= 3

    # 如果投票结果是「海外」，尝试从基金名称中提取更精确的市场
    result_name = top['name']
    if result_name == '海外' and fallback_tag and fallback_tag.get('basis') == 'market_region':
        result_name = fallback_tag['name']

    if top['count'] >= 4 or market_dominant:
        return {
            'name': result_name,
            'ratio': round(top['ratio'], 2),
            'count': top['count'],
            'basis': 'market_region' if top['name'] in ('美股', '港股', '海外', '印度', '日本', '德国', '法国', '英国', '越南', '韩国') else 'holding_count',
            'source': 'top_stock_holdings',
            'top_share': round(top_share, 2),
            'count_share': round(count_share, 2),
            'valid_count': valid_count,
            'has_weight': total_ratio_count > 0,
        }

    if total_ratio > 0 and (top_by_ratio['ratio'] / total_ratio * 100) >= 35:
        return {
            'name': top_by_ratio['name'],
            'ratio': round(top_by_ratio['ratio'], 2),
            'count': top_by_ratio['count'],
            'basis': 'holding_weight',
            'source': 'top_stock_holdings',
            'top_share': round(top_share, 2),
            'count_share': round(top_by_ratio['count'] / valid_count * 100, 2) if valid_count else 0.0,
            'valid_count': valid_count,
            'has_weight': True,
        }

    if total_ratio > 0 and top['count'] >= 3 and top_share >= 45:
        return {
            'name': top['name'],
            'ratio': round(top['ratio'], 2),
            'count': top['count'],
            'basis': 'holding_weight',
            'source': 'top_stock_holdings',
            'top_share': round(top_share, 2),
            'count_share': round(count_share, 2),
            'valid_count': valid_count,
            'has_weight': True,
        }

    if fallback_tag:
        fallback = dict(fallback_tag)
        fallback.update({
            'ratio': round(top_by_ratio['ratio'], 2),
            'count': top_by_ratio['count'],
            'top_share': round(top_share, 2),
            'count_share': round(top_by_ratio['count'] / valid_count * 100, 2) if valid_count else 0.0,
            'valid_count': valid_count,
            'has_weight': total_ratio_count > 0,
            'holding_top_industry': top_by_ratio['name'],
        })
        return fallback

    return {
        'name': '混合型',
        'ratio': round(top_by_ratio['ratio'], 2),
        'count': top_by_ratio['count'],
        'basis': 'mixed',
        'source': 'top_stock_holdings',
        'top_share': round(top_share, 2),
        'count_share': round(top_by_ratio['count'] / valid_count * 100, 2) if valid_count else 0.0,
        'valid_count': valid_count,
        'has_weight': total_ratio_count > 0,
    }

def _build_industry_tag_from_cached_holdings(raw_holdings, industry_lookup, fund_name=None, fund_type=None):
    holdings = _portfolio_holding_items(raw_holdings)
    enhanced = []
    for item in holdings:
        info = industry_lookup.get(item.get('code'), {})
        enhanced.append({
            **item,
            'industry': info.get('industry'),
        })
    return _build_portfolio_industry_tag({
        'stock_codes_new': enhanced,
        'fund_name': fund_name,
        'fund_type': fund_type,
    })

def _upsert_fund_industry_tag(db: Session, fund_code: str, tag: dict, detail=None, unresolved_count=0):
    fund_code = _normalize_fund_code(fund_code)
    if not fund_code or not tag:
        return None

    record = db.query(FundIndustryTag).filter(FundIndustryTag.fund_code == fund_code).first()
    if not record:
        record = FundIndustryTag(fund_code=fund_code)
        db.add(record)

    record.industry_tag = tag.get('name') or '混合型'
    record.industry_count = int(tag.get('count') or 0)
    record.industry_ratio = _safe_ratio(tag.get('ratio'))
    record.basis = tag.get('basis') or 'mixed'
    record.source = tag.get('source') or 'top_stock_holdings'
    evidence = dict(detail or {})
    evidence['classification'] = {
        key: value
        for key, value in tag.items()
        if key not in ('name', 'ratio', 'count')
    }
    record.detail_json = _json_dumps(evidence)
    record.unresolved_count = int(unresolved_count or 0)
    record.updated_time = datetime.now()
    return record

def _save_portfolio_from_holdings_payload(db: Session, fund_code: str, payload: dict):
    data = payload.get('data', {}) if isinstance(payload, dict) else {}
    items = data.get('items', []) if isinstance(data, dict) else []
    if not isinstance(items, list) or not items:
        return None

    stock_items = []
    for item in items:
        if not isinstance(item, dict):
            continue
        code = _normalize_stock_code(item.get('stockCode') or item.get('code'))
        if not code:
            continue
        ratio = item.get('ratio')
        if isinstance(ratio, (int, float)) and 0 < ratio <= 1:
            ratio = round(ratio * 100, 4)
        stock_items.append({
            'code': code,
            'name': item.get('stockName') or item.get('name') or code,
            'market': item.get('market'),
            'ratio': ratio,
        })

    if not stock_items:
        return None

    fund_code = _normalize_fund_code(fund_code)
    record = db.query(FundPortfolio).filter(FundPortfolio.fund_code == fund_code).first()
    if not record:
        record = FundPortfolio(fund_code=fund_code)
        db.add(record)

    record.stock_codes_json = _json_dumps(stock_items)
    record.stock_codes_new_json = _json_dumps(stock_items)
    record.bond_codes_json = _json_dumps(data.get('bondCodes', []) if isinstance(data, dict) else [])
    record.bond_codes_new_json = _json_dumps(data.get('bondCodesNew', []) if isinstance(data, dict) else [])
    record.updated_time = datetime.now()
    return record

def _refresh_fund_industry_tag(db: Session, fund_code: str, fetch_holdings_if_missing=False, force_stock_refresh=False, allow_stock_network=False):
    fund_code = _normalize_fund_code(fund_code)
    if not fund_code:
        return None

    portfolio = db.query(FundPortfolio).filter(FundPortfolio.fund_code == fund_code).first()
    if not portfolio and fetch_holdings_if_missing:
        try:
            payload = get_data_service_client().get_fund_holdings(fund_code)
            portfolio = _save_portfolio_from_holdings_payload(db, fund_code, payload)
        except Exception as exc:
            print(f"fund industry tag: holdings fetch failed for {fund_code}: {exc}")

    basic = db.query(FundBasicInfo).filter(FundBasicInfo.fund_code == fund_code).first()
    if not portfolio:
        tag = _build_portfolio_industry_tag({
            'fund_name': basic.fund_name if basic else None,
            'fund_type': basic.fund_type if basic else None,
        })
        return _upsert_fund_industry_tag(db, fund_code, tag, detail={'reason': 'missing_holdings'}, unresolved_count=0)

    raw_holdings = _json_loads(portfolio.stock_codes_new_json, []) or _json_loads(portfolio.stock_codes_json, [])
    holding_items = _portfolio_holding_items(raw_holdings)
    industry_map, unresolved = _resolve_stock_industries(
        db,
        holding_items,
        force_refresh=force_stock_refresh,
        allow_network=allow_stock_network or force_stock_refresh,
    )

    enhanced = []
    for item in holding_items:
        info = industry_map.get(item.get('code'), {})
        enhanced.append({
            **item,
            'industry': info.get('industry'),
            'region': info.get('region'),
            'concepts': info.get('concepts') or [],
        })

    tag = _build_portfolio_industry_tag({
        'stock_codes_new': enhanced,
        'fund_name': basic.fund_name if basic else None,
        'fund_type': basic.fund_type if basic else None,
    })
    industry_counts = {}
    for item in enhanced:
        industry = item.get('industry')
        if not industry:
            continue
        bucket = industry_counts.setdefault(industry, {'count': 0, 'ratio': 0.0})
        bucket['count'] += 1
        bucket['ratio'] += _safe_ratio(item.get('ratio'))

    return _upsert_fund_industry_tag(
        db,
        fund_code,
        tag,
        detail={'industries': industry_counts},
        unresolved_count=len(unresolved),
    )

def _stats_numbers(values):
    nums = []
    for value in values:
        num = _to_float(value)
        if num is not None:
            nums.append(num)
    nums.sort()
    if not nums:
        return {
            'avg': None,
            'median': None,
            'positive_rate': None,
            'count': 0,
        }
    mid = len(nums) // 2
    median = nums[mid] if len(nums) % 2 else (nums[mid - 1] + nums[mid]) / 2
    return {
        'avg': round(sum(nums) / len(nums), 2),
        'median': round(median, 2),
        'positive_rate': round(sum(1 for num in nums if num > 0) / len(nums) * 100, 2),
        'count': len(nums),
    }

def rebuild_industry_performance_stats(db: Session):
    rows = db.query(FundIndustryTag, FundBasicInfo).join(
        FundBasicInfo, FundIndustryTag.fund_code == FundBasicInfo.fund_code
    ).all()

    grouped = {}
    for tag, basic in rows:
        industry = tag.industry_tag or '混合型'
        perf = _json_loads(basic.performance_json, {}) if basic else {}
        bucket = grouped.setdefault(industry, {
            'funds': [],
            'return_3m': [],
            'return_6m': [],
            'return_1y': [],
            'return_3y': [],
        })
        bucket['funds'].append({
            'fund_code': basic.fund_code,
            'fund_name': basic.fund_name,
            'fund_type': basic.fund_type,
        })
        bucket['return_3m'].append(perf.get('3_month_return'))
        bucket['return_6m'].append(perf.get('6_month_return'))
        bucket['return_1y'].append(perf.get('1_year_return'))
        bucket['return_3y'].append(perf.get('3_year_return'))

    existing = {
        row.industry_tag: row
        for row in db.query(FundIndustryPerformance).all()
    }

    touched = set()
    for industry, bucket in grouped.items():
        stat_3m = _stats_numbers(bucket['return_3m'])
        stat_6m = _stats_numbers(bucket['return_6m'])
        stat_1y = _stats_numbers(bucket['return_1y'])
        stat_3y = _stats_numbers(bucket['return_3y'])

        record = existing.get(industry)
        if not record:
            record = FundIndustryPerformance(industry_tag=industry)
            db.add(record)

        record.fund_count = len(bucket['funds'])
        record.return_3m_avg = stat_3m['avg']
        record.return_3m_median = stat_3m['median']
        record.return_6m_avg = stat_6m['avg']
        record.return_6m_median = stat_6m['median']
        record.return_1y_avg = stat_1y['avg']
        record.return_1y_median = stat_1y['median']
        record.return_3y_avg = stat_3y['avg']
        record.return_3y_median = stat_3y['median']
        record.positive_3m_rate = stat_3m['positive_rate']
        record.positive_6m_rate = stat_6m['positive_rate']
        record.positive_1y_rate = stat_1y['positive_rate']
        record.positive_3y_rate = stat_3y['positive_rate']
        record.detail_json = _json_dumps({
            'period_counts': {
                '3m': stat_3m['count'],
                '6m': stat_6m['count'],
                '1y': stat_1y['count'],
                '3y': stat_3y['count'],
            },
            'funds': bucket['funds'][:50],
        })
        record.updated_time = datetime.now()
        touched.add(industry)

    for industry, record in existing.items():
        if industry not in touched:
            db.delete(record)

    return len(touched)

def _industry_performance_payload(db: Session):
    rows = db.query(FundIndustryPerformance).order_by(
        desc(FundIndustryPerformance.return_3m_median)
    ).all()
    items = []
    for row in rows:
        detail = _json_loads(row.detail_json, {})
        items.append({
            'industry': row.industry_tag,
            'fund_count': row.fund_count,
            'return_3m_avg': row.return_3m_avg,
            'return_3m_median': row.return_3m_median,
            'return_6m_avg': row.return_6m_avg,
            'return_6m_median': row.return_6m_median,
            'return_1y_avg': row.return_1y_avg,
            'return_1y_median': row.return_1y_median,
            'return_3y_avg': row.return_3y_avg,
            'return_3y_median': row.return_3y_median,
            'positive_3m_rate': row.positive_3m_rate,
            'positive_6m_rate': row.positive_6m_rate,
            'positive_1y_rate': row.positive_1y_rate,
            'positive_3y_rate': row.positive_3y_rate,
            'period_counts': detail.get('period_counts', {}) if isinstance(detail, dict) else {},
            'updated_time': row.updated_time.isoformat() if row.updated_time else None,
        })

    return {
        'items': items,
        'summary': {
            'total': len(items),
            'fund_count': sum(item.get('fund_count') or 0 for item in items),
            'updated_time': max((item.get('updated_time') for item in items if item.get('updated_time')), default=None),
        },
        'top_3m': sorted(items, key=lambda item: item.get('return_3m_median') if item.get('return_3m_median') is not None else -9999, reverse=True)[:8],
        'top_1y': sorted(items, key=lambda item: item.get('return_1y_median') if item.get('return_1y_median') is not None else -9999, reverse=True)[:8],
        'weak_3m': sorted(items, key=lambda item: item.get('return_3m_median') if item.get('return_3m_median') is not None else 9999)[:8],
    }

def _latest_research_industry_task(db):
    return db.query(DataFetchTask).filter(
        DataFetchTask.task_type == 'research_industry_performance'
    ).order_by(desc(DataFetchTask.started_time), desc(DataFetchTask.id)).first()

def _task_to_research_status(task):
    if not task:
        return {'running': False, 'status': 'idle', 'message': ''}
    return {
        'task_id': task.id,
        'running': task.status == 'running',
        'status': task.status,
        'progress': task.current_count or 0,
        'total': task.target_count or 0,
        'success_count': task.success_count or 0,
        'fail_count': task.fail_count or 0,
        'message': task.message or '',
        'started_time': task.started_time.isoformat() if task.started_time else None,
        'finished_time': task.finished_time.isoformat() if task.finished_time else None,
    }

def _run_research_industry_performance_rebuild(task_id):
    db = SessionLocal()
    try:
        task = db.query(DataFetchTask).filter(DataFetchTask.id == task_id).first()
        if task:
            total = db.query(FundIndustryTag).count()
            task.status = 'running'
            task.message = '后台汇总板块行情...'
            task.target_count = total
            task.current_count = 0
            task.updated_time = datetime.now()
            db.commit()

        rebuilt_total = rebuild_industry_performance_stats(db)
        task = db.query(DataFetchTask).filter(DataFetchTask.id == task_id).first()
        if task:
            task.status = 'finished'
            task.message = f'板块行情汇总完成，共 {rebuilt_total} 个板块'
            task.current_count = task.target_count or rebuilt_total
            task.success_count = rebuilt_total
            task.fail_count = 0
            task.finished_time = datetime.now()
            task.updated_time = datetime.now()
        db.commit()
    except Exception as exc:
        db.rollback()
        task = db.query(DataFetchTask).filter(DataFetchTask.id == task_id).first()
        if task:
            task.status = 'failed'
            task.message = f'板块行情汇总失败: {exc}'
            task.error_message = str(exc)
            task.finished_time = datetime.now()
            task.updated_time = datetime.now()
            db.commit()
    finally:
        db.close()

def _screening_industry_context(db: Session, fund_codes):
    codes = [str(code) for code in fund_codes if code]
    if not codes:
        return {}

    persisted = db.query(FundIndustryTag).filter(FundIndustryTag.fund_code.in_(codes)).all()
    tag_map = {
        item.fund_code: {
            'name': item.industry_tag,
            'ratio': item.industry_ratio,
            'count': item.industry_count,
            'basis': item.basis,
            'source': item.source,
        }
        for item in persisted
    }
    missing_codes = [code for code in codes if code not in tag_map]
    if not missing_codes:
        return tag_map

    portfolios = db.query(FundPortfolio).filter(FundPortfolio.fund_code.in_(missing_codes)).all()
    portfolio_map = {item.fund_code: item for item in portfolios}

    stock_codes = set()
    for portfolio in portfolios:
        raw_holdings = _json_loads(portfolio.stock_codes_new_json, []) or _json_loads(portfolio.stock_codes_json, [])
        for item in _portfolio_holding_items(raw_holdings):
            code = item.get('code')
            if code:
                stock_codes.add(code)

    industry_lookup = {}
    if stock_codes:
        records = db.query(StockIndustry).filter(StockIndustry.stock_code.in_(stock_codes)).all()
        industry_lookup = {
            record.stock_code: {
                'industry': record.industry,
                'stock_name': record.stock_name,
            }
            for record in records
        }

    for fund_code, portfolio in portfolio_map.items():
        raw_holdings = _json_loads(portfolio.stock_codes_new_json, []) or _json_loads(portfolio.stock_codes_json, [])
        basic = db.query(FundBasicInfo).filter(FundBasicInfo.fund_code == fund_code).first()
        tag = _build_industry_tag_from_cached_holdings(
            raw_holdings,
            industry_lookup,
            fund_name=basic.fund_name if basic else None,
            fund_type=basic.fund_type if basic else None,
        )
        tag_map[fund_code] = tag
        _upsert_fund_industry_tag(db, fund_code, tag, detail={'source': 'screening_cache_backfill'})
    return tag_map

def _build_fund_industry_exposure(db: Session, fund_code: str, force_refresh=False):
    fund_code = _normalize_fund_code(fund_code)
    portfolio = db.query(FundPortfolio).filter(FundPortfolio.fund_code == fund_code).first()
    if not portfolio:
        return None

    holdings = _json_loads(portfolio.stock_codes_new_json, []) or _json_loads(portfolio.stock_codes_json, [])
    holding_items = _portfolio_holding_items(holdings)
    industry_map, unresolved = _resolve_stock_industries(
        db,
        holding_items,
        force_refresh=force_refresh,
        allow_network=force_refresh,
    )

    exposure = {}
    enriched_holdings = []
    for item in holding_items:
        code = item.get('code')
        info = industry_map.get(code, {})
        industry = info.get('industry') or '未识别'
        ratio = _safe_ratio(item.get('ratio'))
        enriched_item = {
            **item,
            'industry': info.get('industry'),
            'region': info.get('region'),
            'concepts': info.get('concepts') or [],
        }
        enriched_holdings.append(enriched_item)
        if not info.get('industry'):
            industry = '未识别'

        bucket = exposure.setdefault(industry, {
            'industry': industry,
            'ratio': 0.0,
            'count': 0,
            'stocks': [],
        })
        bucket['ratio'] += ratio
        bucket['count'] += 1
        bucket['stocks'].append({
            'code': code,
            'name': item.get('name'),
            'ratio': ratio,
        })

    exposure_items = sorted(exposure.values(), key=lambda item: (item['ratio'], item['count']), reverse=True)
    for item in exposure_items:
        item['ratio'] = round(item['ratio'], 2)

    basic = db.query(FundBasicInfo).filter(FundBasicInfo.fund_code == fund_code).first()
    result = {
        'fund_code': fund_code,
        'holdings': enriched_holdings,
        'industries': exposure_items,
        'industry_tag': _build_portfolio_industry_tag({
            'stock_codes_new': enriched_holdings,
            'fund_name': basic.fund_name if basic else fund_code,
            'fund_type': basic.fund_type if basic else None,
        }),
        'unresolved_codes': unresolved,
        'updated_time': datetime.now().isoformat(),
    }
    _upsert_fund_industry_tag(
        db,
        fund_code,
        result['industry_tag'],
        detail={'industries': exposure_items},
        unresolved_count=len(unresolved),
    )
    return result

def _normalize_fund_code(code):
    code = str(code or '').strip()
    return code.zfill(6) if re.match(r'^\d{1,6}$', code) else code

def _build_cached_response(db: Session, fund_code: str):
    basic = db.query(FundBasicInfo).filter(FundBasicInfo.fund_code == fund_code).first()
    trend = db.query(FundTrend).filter(FundTrend.fund_code == fund_code).first()
    estimate = db.query(FundEstimate).filter(FundEstimate.fund_code == fund_code).first()
    portfolio = db.query(FundPortfolio).filter(FundPortfolio.fund_code == fund_code).first()
    extra = db.query(FundExtraData).filter(FundExtraData.fund_code == fund_code).first()

    if not any([basic, trend, estimate, portfolio, extra]):
        return None

    data = {}

    if basic:
        data['basic_info'] = _json_loads(basic.basic_json, {})
        data['performance'] = _json_loads(basic.performance_json, {})

    if trend:
        data['net_worth_trend'] = _json_loads(trend.net_worth_trend_json, [])
        data['accumulated_net_worth'] = _json_loads(trend.accumulated_net_worth_json, [])
        data['position_trend'] = _json_loads(trend.position_trend_json, [])
        data['total_return_trend'] = _json_loads(trend.total_return_trend_json, [])
        data['ranking_trend'] = _json_loads(trend.ranking_trend_json, [])
        data['ranking_percentage'] = _json_loads(trend.ranking_percentage_json, [])
        data['scale_fluctuation'] = _json_loads(trend.scale_fluctuation_json, {})

    if estimate:
        display_estimate_change = estimate.estimate_change
        if _estimate_is_after_nav(estimate.estimate_time, estimate.net_worth_date):
            try:
                estimate_nav = float(estimate.estimate_value)
                official_nav = float(estimate.net_worth)
                if official_nav:
                    display_estimate_change = round((estimate_nav - official_nav) / official_nav * 100, 4)
            except Exception:
                display_estimate_change = estimate.estimate_change

        data['realtime_estimate'] = {
            'name': estimate.name,
            'fund_code': fund_code,
            'net_worth': estimate.net_worth,
            'net_worth_date': estimate.net_worth_date,
            'estimate_value': estimate.estimate_value,
            'estimate_change': _value_to_string(display_estimate_change),
            'estimate_time': estimate.estimate_time
        }

    if portfolio:
        data['portfolio'] = _enhance_portfolio_industries(db, {
            'stock_codes': _json_loads(portfolio.stock_codes_json, []),
            'bond_codes': _json_loads(portfolio.bond_codes_json, []),
            'stock_codes_new': _json_loads(portfolio.stock_codes_new_json, []),
            'bond_codes_new': _json_loads(portfolio.bond_codes_new_json, [])
        })
        data['fund_industry_tag'] = data['portfolio'].get('industry_tag')
        _upsert_fund_industry_tag(
            db,
            fund_code,
            data['fund_industry_tag'],
            detail={'source': 'cached_response'},
            unresolved_count=len(data['portfolio'].get('industry_unresolved_codes') or []),
        )

    if extra:
        data['holder_structure'] = _json_loads(extra.holder_structure_json, {})
        data['asset_allocation'] = _json_loads(extra.asset_allocation_json, {})
        data['performance_evaluation'] = _json_loads(extra.performance_evaluation_json, {})
        data['fund_managers'] = _json_loads(extra.fund_managers_json, [])
        data['subscription_redemption'] = _json_loads(extra.subscription_redemption_json, {})
        data['same_type_funds'] = _json_loads(extra.same_type_funds_json, [])

    return data

def _sync_fund_industry_response(db: Session, fund_code: str, data: dict, source='fund_detail_response'):
    if not isinstance(data, dict):
        return data

    basic_info = data.get('basic_info', {}) if isinstance(data.get('basic_info'), dict) else {}
    portfolio = data.get('portfolio') if isinstance(data.get('portfolio'), dict) else None
    if portfolio:
        enriched_portfolio = dict(portfolio)
        enriched_portfolio['fund_name'] = basic_info.get('fund_name') or enriched_portfolio.get('fund_name')
        enriched_portfolio['fund_type'] = basic_info.get('fund_type') or enriched_portfolio.get('fund_type')
        enriched_portfolio = _enhance_portfolio_industries(db, enriched_portfolio)
        data['portfolio'] = enriched_portfolio
        tag = enriched_portfolio.get('industry_tag')
        if tag:
            _upsert_fund_industry_tag(
                db,
                fund_code,
                tag,
                detail={'source': source},
                unresolved_count=len(enriched_portfolio.get('industry_unresolved_codes') or []),
            )
            data['fund_industry_tag'] = tag

    persisted_tag = db.query(FundIndustryTag).filter(FundIndustryTag.fund_code == fund_code).first()
    if persisted_tag:
        data['fund_industry_tag'] = {
            'name': persisted_tag.industry_tag,
            'ratio': persisted_tag.industry_ratio,
            'count': persisted_tag.industry_count,
            'basis': persisted_tag.basis,
            'source': persisted_tag.source,
        }

    return data

@app.route('/api/eastmoney/<path:subpath>', methods=['GET'])
def proxy_eastmoney(subpath):
    """
    代理东方财富行情 API 请求

    用于生产环境：前端通过本后端转发请求到东方财富，
    避免跨域和 JSONP 依赖问题。
    开发环境中由 Vite 代理处理，此端点作为兜底。
    """
    import requests as req
    import urllib3
    urllib3.disable_warnings()

    target_url = f"https://push2.eastmoney.com/{subpath}"
    query_string = request.query_string.decode('utf-8')
    if query_string:
        target_url = f"{target_url}?{query_string}"

    base_headers = {
        "Referer": "https://quote.eastmoney.com/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }

    errors = []

    # 方案1：使用系统代理（与浏览器行为一致）
    try:
        s1 = req.Session()
        s1.trust_env = True
        resp = s1.get(target_url, headers=base_headers, timeout=10, verify=False)
        if resp.status_code == 200:
            return jsonify(resp.json())
    except Exception as exc:
        errors.append(f"env-proxy: {str(exc)[:80]}")

    # 方案2：不使用系统代理（直连）
    try:
        s2 = req.Session()
        s2.trust_env = False
        resp = s2.get(target_url, headers=base_headers, timeout=10, verify=False)
        if resp.status_code == 200:
            return jsonify(resp.json())
    except Exception as exc:
        errors.append(f"direct: {str(exc)[:80]}")

    # 方案3：curl_cffi 模拟 Chrome（绕过 TLS 指纹检测）
    try:
        from curl_cffi import requests as curl_requests
        last_imp_error = None
        for impersonate_target in ("chrome124", "chrome120", "chrome110", "chrome101", "edge101"):
            try:
                resp = curl_requests.get(
                    target_url,
                    headers=base_headers,
                    timeout=10,
                    verify=False,
                    impersonate=impersonate_target
                )
                if resp.status_code == 200:
                    return jsonify(resp.json())
            except Exception as imp_exc:
                last_imp_error = str(imp_exc)[:60]
                continue
        errors.append(f"curl_cffi(all): {last_imp_error or 'unknown'}")
    except Exception as exc:
        errors.append(f"curl_cffi: {str(exc)[:80]}")

    # 方案4：普通 requests（默认配置，verify=True）
    try:
        resp = req.get(target_url, headers=base_headers, timeout=10)
        if resp.status_code == 200:
            return jsonify(resp.json())
    except Exception as exc:
        errors.append(f"default: {str(exc)[:80]}")

    import logging
    logging.getLogger(__name__).warning(f"东方财富代理全部失败: {'; '.join(errors)}")
    return jsonify({"error": "东方财富代理请求失败", "details": errors}), 502


@app.route('/api/health')
def hello():
    """测试接口是否可用"""
    return jsonify({"message": "Fund Analysis API is running!"})

@app.route('/api/fund/search', methods=['GET'])
def search_funds():
    """Search funds by keyword – DataService first, local cache fallback."""
    keyword = request.args.get('q', '')
    if not keyword:
        return jsonify({"error": "Keyword is required"}), 400

    # 1) Try DataService first
    try:
        ds_payload = get_data_service_client().search_funds(keyword)
        ds_data = ds_payload.get('data', {}) if isinstance(ds_payload, dict) else {}
        ds_items = ds_data.get('items', []) if isinstance(ds_data, dict) else []

        if ds_items:
            # Map DataService DTO back to Frontend-expected format
            funds = []
            for item in ds_items:
                if not isinstance(item, dict):
                    continue
                funds.append({
                    'CODE': item.get('code', ''),
                    'NAME': item.get('name', ''),
                    'TYPE': item.get('type', ''),
                    'PINYIN': item.get('pinyin', ''),
                })
            print(f"fund search: DataService success, {len(funds)} results for '{keyword}'")
            return jsonify({"data": funds})
    except DataServiceError as e:
        print(f"fund search: DataService unavailable, fallback to local cache: {e}")

    # 2) Fallback to local cache
    funds = fund_list_cache.search(keyword, limit=20)
    return jsonify({"data": funds})

@app.route('/api/fund/search/status', methods=['GET'])
def get_search_status():
    """获取搜索数据库状态"""
    status = fund_list_cache.get_status()
    return jsonify(status)

@app.route('/api/fund/search/update', methods=['POST'])
def update_search_database():
    """更新本地基金搜索数据库"""
    result = fund_list_cache.update_from_api()
    if result['success']:
        return jsonify(result)
    else:
        return jsonify(result), 500

@app.route('/api/fund/<fund_code>', methods=['GET'])
def get_fund_detail(fund_code):
    """
    获取基金详细信息
    
    数据一致性策略：
    - 每次访问都从API获取最新数据
    - 同时更新所有相关表（FundBasicInfo, FundTrend, FundRiskMetrics等）
    - 确保详情、对比、筛选三个模块的数据源统一
    """
    fund_code = _normalize_fund_code(fund_code)
    if not fund_code:
        return jsonify({"error": "Fund code is required"}), 400

    # --- Gray-release source parameter ---
    # Priority: URL param > FUND_DEFAULT_SOURCE env > 'legacy'
    source = request.args.get('source', '').strip().lower()
    if not source:
        source = os.environ.get('FUND_DEFAULT_SOURCE', 'legacy').strip().lower()

    if source not in ('legacy', 'data_service', 'auto'):
        return jsonify({
            "success": False,
            "error": "Invalid source. Use legacy, data_service, or auto."
        }), 400

    # source=data_service: DataService only, no fallback
    if source == 'data_service':
        db = get_db()
        result = _get_fund_detail_from_data_service(fund_code)
        result = _sync_fund_industry_response(db, fund_code, result, source='data_service_detail')
        try:
            db.commit()
        except Exception as exc:
            print(f"data_service fund industry commit failed: {exc}")
            db.rollback()
        return jsonify(result)

    # source=auto: DataService first with quality gate, fallback to legacy
    auto_fallback = False
    if source == 'auto':
        ds_try = _try_data_service_fund_detail(fund_code)
        if ds_try is not None:
            ds_result, quality_passed, quality_issues = ds_try
            if quality_passed:
                db = get_db()
                ds_result = _sync_fund_industry_response(db, fund_code, ds_result, source='auto_data_service_detail')
                try:
                    db.commit()
                except Exception as exc:
                    print(f"auto data_service fund industry commit failed: {exc}")
                    db.rollback()
                ds_result['_data_source'] = {
                    "mode": "auto",
                    "used": "data_service",
                    "fallback": False,
                    "quality_passed": True,
                    "quality_issues": []
                }
                return jsonify(ds_result)
            else:
                # Quality gate failed – fallback to legacy
                print(f"auto mode: quality gate failed for {fund_code}: {quality_issues}")
                auto_fallback = True
        else:
            # DataService request/mapping failed
            auto_fallback = True

    db = get_db() # 获取数据库会话

    # 使用新的 get_fund_data 方法获取清洗后的完整数据
    fund_data = fund_api.get_fund_data(fund_code)
    
    if fund_data:
        basic_info = fund_data.get('basic_info', {})
        performance = fund_data.get('performance', {})
        trend = {
            'net_worth_trend': fund_data.get('net_worth_trend', []),
            'accumulated_net_worth': fund_data.get('accumulated_net_worth', []),
            'position_trend': fund_data.get('position_trend', []),
            'total_return_trend': fund_data.get('total_return_trend', []),
            'ranking_trend': fund_data.get('ranking_trend', []),
            'ranking_percentage': fund_data.get('ranking_percentage', []),
            'scale_fluctuation': fund_data.get('scale_fluctuation', {})
        }
        estimate = fund_data.get('realtime_estimate', {})
        portfolio = fund_data.get('portfolio', {})
        fund_data = _sync_fund_industry_response(db, fund_code, fund_data, source='fund_detail')
        portfolio = fund_data.get('portfolio', {})
        extra = {
            'holder_structure': fund_data.get('holder_structure', {}),
            'asset_allocation': fund_data.get('asset_allocation', {}),
            'performance_evaluation': fund_data.get('performance_evaluation', {}),
            'fund_managers': fund_data.get('fund_managers', []),
            'subscription_redemption': fund_data.get('subscription_redemption', {}),
            'same_type_funds': fund_data.get('same_type_funds', [])
        }

        basic_record = db.query(FundBasicInfo).filter(FundBasicInfo.fund_code == fund_code).first()
        if basic_record:
            basic_record.fund_name = basic_info.get('fund_name')
            basic_record.fund_type = basic_info.get('fund_type')
            basic_record.original_rate = basic_info.get('original_rate')
            basic_record.current_rate = basic_info.get('current_rate')
            basic_record.min_subscription_amount = basic_info.get('min_subscription_amount')
            basic_record.is_hb = basic_info.get('is_hb')
            basic_record.basic_json = _json_dumps(basic_info)
            basic_record.performance_json = _json_dumps(performance)
            # 设置可排序的收益率字段
            try:
                basic_record.return_1y = float(performance.get('1_year_return')) if performance.get('1_year_return') else None
            except (ValueError, TypeError):
                basic_record.return_1y = None
        else:
            # 解析收益率用于排序
            return_1y_val = None
            try:
                return_1y_val = float(performance.get('1_year_return')) if performance.get('1_year_return') else None
            except (ValueError, TypeError):
                pass
            basic_record = FundBasicInfo(
                fund_code=fund_code,
                fund_name=basic_info.get('fund_name') or fund_code,
                fund_type=basic_info.get('fund_type'),
                original_rate=basic_info.get('original_rate'),
                current_rate=basic_info.get('current_rate'),
                min_subscription_amount=basic_info.get('min_subscription_amount'),
                is_hb=basic_info.get('is_hb'),
                return_1y=return_1y_val,
                basic_json=_json_dumps(basic_info),
                performance_json=_json_dumps(performance)
            )
            db.add(basic_record)

        trend_record = db.query(FundTrend).filter(FundTrend.fund_code == fund_code).first()
        # Only overwrite cached net_worth_trend if fresh data is non-empty
        # Prevents API glitches from wiping valid cached data
        fresh_nwt = trend['net_worth_trend']
        fresh_acw = trend['accumulated_net_worth']
        fresh_pos = trend['position_trend']
        fresh_total_ret = trend['total_return_trend']
        fresh_rank = trend['ranking_trend']
        fresh_rank_pct = trend['ranking_percentage']
        fresh_scale = trend['scale_fluctuation']

        if trend_record:
            if fresh_nwt:
                trend_record.net_worth_trend_json = _json_dumps(fresh_nwt)
            if fresh_acw:
                trend_record.accumulated_net_worth_json = _json_dumps(fresh_acw)
            if fresh_pos:
                trend_record.position_trend_json = _json_dumps(fresh_pos)
            if fresh_total_ret:
                trend_record.total_return_trend_json = _json_dumps(fresh_total_ret)
            if fresh_rank:
                trend_record.ranking_trend_json = _json_dumps(fresh_rank)
            if fresh_rank_pct:
                trend_record.ranking_percentage_json = _json_dumps(fresh_rank_pct)
            if fresh_scale:
                trend_record.scale_fluctuation_json = _json_dumps(fresh_scale)
        else:
            trend_record = FundTrend(
                fund_code=fund_code,
                net_worth_trend_json=_json_dumps(fresh_nwt),
                accumulated_net_worth_json=_json_dumps(fresh_acw),
                position_trend_json=_json_dumps(fresh_pos),
                total_return_trend_json=_json_dumps(fresh_total_ret),
                ranking_trend_json=_json_dumps(fresh_rank),
                ranking_percentage_json=_json_dumps(fresh_rank_pct),
                scale_fluctuation_json=_json_dumps(fresh_scale)
            )
            db.add(trend_record)

        estimate_result = _upsert_fund_estimate(
            db,
            fund_code,
            name=estimate.get('name'),
            net_worth=estimate.get('net_worth'),
            net_worth_date=estimate.get('net_worth_date'),
            estimate_value=estimate.get('estimate_value'),
            estimate_change=estimate.get('estimate_change'),
            estimate_time=estimate.get('estimate_time')
        )
        estimate_result = _sync_latest_official_nav(db, fund_code, estimate_result)
        if isinstance(estimate_result, dict):
            fund_data['realtime_estimate'] = {
                **estimate,
                'net_worth': estimate_result.get('net_worth'),
                'net_worth_date': estimate_result.get('net_worth_date'),
                'estimate_value': estimate_result.get('estimate_value'),
                'estimate_change': estimate_result.get('estimate_change'),
                'estimate_time': estimate_result.get('estimate_time'),
            }

        portfolio_record = db.query(FundPortfolio).filter(FundPortfolio.fund_code == fund_code).first()
        if portfolio_record:
            portfolio_record.stock_codes_json = _json_dumps(portfolio.get('stock_codes', []))
            portfolio_record.bond_codes_json = _json_dumps(portfolio.get('bond_codes', []))
            portfolio_record.stock_codes_new_json = _json_dumps(portfolio.get('stock_codes_new', []))
            portfolio_record.bond_codes_new_json = _json_dumps(portfolio.get('bond_codes_new', []))
        else:
            portfolio_record = FundPortfolio(
                fund_code=fund_code,
                stock_codes_json=_json_dumps(portfolio.get('stock_codes', [])),
                bond_codes_json=_json_dumps(portfolio.get('bond_codes', [])),
                stock_codes_new_json=_json_dumps(portfolio.get('stock_codes_new', [])),
                bond_codes_new_json=_json_dumps(portfolio.get('bond_codes_new', []))
            )
            db.add(portfolio_record)

        extra_record = db.query(FundExtraData).filter(FundExtraData.fund_code == fund_code).first()
        if extra_record:
            extra_record.holder_structure_json = _json_dumps(extra['holder_structure'])
            extra_record.asset_allocation_json = _json_dumps(extra['asset_allocation'])
            extra_record.performance_evaluation_json = _json_dumps(extra['performance_evaluation'])
            extra_record.fund_managers_json = _json_dumps(extra['fund_managers'])
            extra_record.subscription_redemption_json = _json_dumps(extra['subscription_redemption'])
            extra_record.same_type_funds_json = _json_dumps(extra['same_type_funds'])
        else:
            extra_record = FundExtraData(
                fund_code=fund_code,
                holder_structure_json=_json_dumps(extra['holder_structure']),
                asset_allocation_json=_json_dumps(extra['asset_allocation']),
                performance_evaluation_json=_json_dumps(extra['performance_evaluation']),
                fund_managers_json=_json_dumps(extra['fund_managers']),
                subscription_redemption_json=_json_dumps(extra['subscription_redemption']),
                same_type_funds_json=_json_dumps(extra['same_type_funds'])
            )
            db.add(extra_record)

        # 【数据一致性】同时更新风险指标，确保详情/对比/筛选数据统一
        net_worth_trend = fund_data.get('net_worth_trend', [])
        if net_worth_trend and len(net_worth_trend) >= 30:
            risk_metrics = calculate_risk_metrics(net_worth_trend)
            if risk_metrics:
                _save_risk_metrics(db, fund_code, risk_metrics)
                # 将风险指标也附加到返回数据中
                fund_data['risk_metrics'] = risk_metrics

        try:
            db.commit()
        except Exception as e:
            print(f"Error saving to database: {e}")
            db.rollback()

        # Add debug meta for auto-fallback mode
        if auto_fallback:
            fund_data["_data_source"] = {
                "mode": "auto",
                "used": "legacy",
                "fallback": True,
                "quality_passed": False,
                "quality_issues": ["quality gate not passed — using legacy fallback"]
            }

        return jsonify(fund_data)
    
    # 如果API获取失败，尝试从数据库获取缓存数据作为兜底
    cached_data = _build_cached_response(db, fund_code)
    if cached_data:
        cached_data = _sync_fund_industry_response(db, fund_code, cached_data, source='cached_response')
        try:
            db.commit()
        except Exception as exc:
            print(f"cached fund industry commit failed: {exc}")
            db.rollback()
        return jsonify(cached_data)

    return jsonify({"error": "Fund not found"}), 404

@app.route('/api/fund/<fund_code>/industry-exposure', methods=['GET'])
def get_fund_industry_exposure(fund_code):
    """Return industry exposure inferred from a fund's top stock holdings."""
    db = get_db()
    force_refresh = request.args.get('refresh', '').lower() in ('1', 'true', 'yes')
    result = _build_fund_industry_exposure(db, fund_code, force_refresh=force_refresh)
    if result is None:
        return jsonify({
            "success": False,
            "error": "No portfolio data found for this fund. Open the fund detail once to fetch holdings first.",
        }), 404

    try:
        db.commit()
    except Exception as exc:
        print(f"industry exposure commit failed: {exc}")
        db.rollback()

    return jsonify({
        "success": True,
        "data": result,
    })

@app.route('/api/stock/<code>/quote', methods=['GET'])
def get_stock_quote(code):
    """
    获取个股实时行情数据

    调用腾讯财经接口获取实时行情，包括：
    - 基础：名称、代码、交易所
    - 行情：当前价、涨跌额、涨跌幅、今开、昨收、最高、最低
    - 交易：成交量、成交额、换手率、振幅
    - 估值：市盈率、总市值

    Returns:
        - 成功: {success: true, data: {...}}
        - 失败: {success: false, error: "..."}
    """
    from services.market_data import get_market_data_service as get_mds
    try:
        mds = get_mds()
        quote = mds.get_realtime_quote(code, use_cache=True)
        if not quote:
            return jsonify({"success": False, "error": f"未找到股票 {code} 的行情数据"}), 404
        return jsonify({"success": True, "data": quote})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/stock/<code>/kline', methods=['GET'])
def get_stock_kline(code):
    """
    获取个股历史 K 线数据

    数据来源：东方财富 A 股 K 线接口（前复权）

    Query params:
        - period: daily / weekly / monthly（默认 daily）
        - adjust: qfq（前复权）/ hfq（后复权）/ none（默认 qfq）
        - startDate: 起始日期 YYYYMMDD（可选）
        - endDate: 结束日期 YYYYMMDD（可选）

    Returns:
        - 成功: {success: true, data: [{date, open, close, high, low, volume, amount, ...}], total_count: N}
        - 失败: {success: false, error: "..."}
    """
    from services.market_data import get_market_data_service as get_mds
    period = request.args.get('period', 'daily')
    adjust = request.args.get('adjust', 'qfq')
    start_date = request.args.get('startDate', '')
    end_date = request.args.get('endDate', '')
    try:
        # 规范化股票代码：兼容 sh600519 / 600519.SH / 600519 等格式
        normalized_code = re.sub(r'^(sh|sz|bj|SH|SZ|BJ)|\.(SH|SZ|BJ)$', '', code.strip())
        mds = get_mds()
        result = mds.get_a_stock_kline(
            normalized_code,
            klt=period,
            fqt=adjust,
            start_date=start_date,
            end_date=end_date,
        )
        if not result.get('success'):
            return jsonify(result), 404
        return jsonify(result)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/market/daily', methods=['GET'])
def get_daily_market():
    """
    获取每日市场行情摘要（AI 驱动）
    
    优化后的接口特性：
    1. 使用硅基流动 API 进行 AI 分析
    2. 整合市场数据后生成摘要
    3. 支持 ?refresh=true 强制刷新
    
    Returns:
        - 成功: {market_sentiment, summary, key_points, ...}
        - 错误: {error: "..."}
    """
    try:
        ai_service = get_ai_service()
        if not ai_service.is_available():
            return jsonify({"error": "AI service not configured. Please set LLM_API_KEY in .env"}), 503
        
        # 从 fund_master_service 获取市场数据
        from fund_master_service import FundMasterService
        from services.market_data import get_market_data_service as get_mds

        service = FundMasterService()
        mds = get_mds()

        market_data = {
            'indices': service.get_market_overview(),
            'sectors': mds.get_industry_boards(page_size=500),
            'news': service.get_flash_news()[:10]
        }
        
        result = ai_service.generate_market_summary(market_data)
        return jsonify(result)
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/fund/<fund_code>/analyze', methods=['GET'])
def analyze_fund(fund_code):
    """
    使用 AI 分析基金（基于硅基流动 API）
    
    分析内容包括：
    - 基金业绩评价
    - 基金经理能力
    - 持仓结构分析
    - 后市展望
    - 亮点与风险提示
    """
    fund_code = _normalize_fund_code(fund_code)
    if not fund_code:
        return jsonify({"error": "Fund code is required"}), 400
    
    # 1. 获取基金数据
    fund_data = fund_api.get_fund_data(fund_code)
    if not fund_data:
        return jsonify({"error": "Fund data not found"}), 404
        
    # 2. 调用 AI 服务进行分析
    try:
        ai_service = get_ai_service()
        if not ai_service.is_available():
            return jsonify({
                "error": "AI service not configured. Please set LLM_API_KEY in .env file."
            }), 503
            
        result = ai_service.analyze_fund(fund_data)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/fund/<fund_code>/basic', methods=['GET'])
def get_fund_basic(fund_code):
    """获取基金基础信息 实时调用API"""
    fund_code = _normalize_fund_code(fund_code)
    if not fund_code:
        return jsonify({"error": "Fund code is required"}), 400
    fund_data = fund_api.get_fund_data(fund_code)
    if fund_data and fund_data.get('basic_info'):
        result = {
            **fund_data.get('basic_info', {}),
            **fund_data.get('performance', {})
        }
        return jsonify(result)

    db = get_db()
    basic = db.query(FundBasicInfo).filter(FundBasicInfo.fund_code == fund_code).first()
    if basic:
        basic_info = _json_loads(basic.basic_json, {})
        performance = _json_loads(basic.performance_json, {})
        return jsonify({**basic_info, **performance})

    return jsonify({"error": "Fund basic info not found"}), 404

@app.route('/api/fund/<fund_code>/trend', methods=['GET'])
def get_fund_trend(fund_code):
    """获取基金走势数据 实时调用API"""
    fund_code = _normalize_fund_code(fund_code)
    if not fund_code:
        return jsonify({"error": "Fund code is required"}), 400
    
    fund_data = fund_api.get_fund_data(fund_code)
    if fund_data and 'net_worth_trend' in fund_data:
        return jsonify({
            "net_worth_trend": fund_data['net_worth_trend'],
            "accumulated_net_worth": fund_data.get('accumulated_net_worth', [])
        })

    db = get_db()
    trend = db.query(FundTrend).filter(FundTrend.fund_code == fund_code).first()
    if trend:
        return jsonify({
            "net_worth_trend": _json_loads(trend.net_worth_trend_json, []),
            "accumulated_net_worth": _json_loads(trend.accumulated_net_worth_json, [])
        })

    return jsonify({"error": "Fund trend data not found"}), 404


# ==================== 自选基金 API ====================

@app.route('/api/watchlist', methods=['GET'])
def get_watchlist():
    """获取自选基金列表（按分组和排序顺序）"""
    db = get_db()
    
    # 获取所有分组
    groups = db.query(FundWatchlistGroup).order_by(FundWatchlistGroup.sort_order).all()
    
    # 获取所有基金
    watchlist = db.query(FundWatchlist).order_by(FundWatchlist.sort_order).all()
    
    # 构建分组数据
    groups_data = []
    for group in groups:
        groups_data.append({
            'id': group.id,
            'name': group.name,
            'sort_order': group.sort_order
        })
    
    # 构建基金数据
    funds_data = []
    for item in watchlist:
        estimate = db.query(FundEstimate).filter(FundEstimate.fund_code == item.fund_code).first()

        net_worth = estimate.net_worth if estimate else None
        net_worth_date = estimate.net_worth_date if estimate else None
        display_change = estimate.estimate_change if estimate else None

        # 兜底：FundTrend 存储 pingzhongdata 的净值走势 JSON，
        # 解析取最新净值，如果比 FundEstimate (fundgz) 更新就覆盖
        try:
            trend = db.query(FundTrend).filter(FundTrend.fund_code == item.fund_code).first()
            if trend and trend.net_worth_trend_json:
                trend_data = json.loads(trend.net_worth_trend_json)
                if isinstance(trend_data, list) and trend_data:
                    last = trend_data[-1]
                    if isinstance(last, dict):
                        t_nw = last.get('net_worth')
                        t_date = last.get('date')
                        if t_nw is not None and t_date is not None:
                            trend_change = _trend_daily_return(trend_data)
                            if not net_worth_date or _normalize_date(t_date) >= _normalize_date(net_worth_date):
                                net_worth = str(t_nw)
                                net_worth_date = str(t_date)
                                if trend_change is not None and not _estimate_is_after_nav(
                                    estimate.estimate_time if estimate else None,
                                    t_date
                                ):
                                    display_change = _value_to_string(trend_change)
        except Exception:
            pass

        fund_data = {
            'fund_code': item.fund_code,
            'fund_name': item.fund_name,
            'fund_type': item.fund_type,
            'group_id': item.group_id,
            'sort_order': item.sort_order,
            'created_time': item.created_time.isoformat() if item.created_time else None,
            'net_worth': net_worth,
            'net_worth_date': net_worth_date,
            'estimate_value': estimate.estimate_value if estimate else None,
            'estimate_change': display_change,
            'estimate_time': estimate.estimate_time if estimate else None
        }
        funds_data.append(fund_data)
    
    return jsonify({
        'groups': groups_data,
        'data': funds_data
    })


@app.route('/api/watchlist/<fund_code>', methods=['GET'])
def check_watchlist(fund_code):
    fund_code = _normalize_fund_code(fund_code)
    """检查基金是否在自选列表中"""
    db = get_db()
    exists = db.query(FundWatchlist).filter(FundWatchlist.fund_code == fund_code).first() is not None
    return jsonify({'in_watchlist': exists})


@app.route('/api/watchlist', methods=['POST'])
def add_to_watchlist():
    """添加基金到自选列表"""
    data = request.get_json()
    fund_code = _normalize_fund_code(data.get('fund_code'))
    fund_name = data.get('fund_name', '')
    fund_type = data.get('fund_type', '')
    group_id = data.get('group_id')  # 可选的分组ID
    estimate_payload = data.get('estimate') if isinstance(data.get('estimate'), dict) else None
    
    if not fund_code:
        return jsonify({'error': 'Fund code is required'}), 400
    
    db = get_db()
    
    # 检查是否已存在
    existing = db.query(FundWatchlist).filter(FundWatchlist.fund_code == fund_code).first()
    if existing:
        return jsonify({'error': 'Fund already in watchlist', 'fund_code': fund_code}), 409
    
    # 获取当前最大排序值（在同一分组内）
    query = db.query(FundWatchlist)
    if group_id:
        query = query.filter(FundWatchlist.group_id == group_id)
    max_order = query.order_by(FundWatchlist.sort_order.desc()).first()
    new_order = (max_order.sort_order + 1) if max_order else 0
    
    # 创建新记录
    new_item = FundWatchlist(
        fund_code=fund_code,
        fund_name=fund_name,
        fund_type=fund_type,
        group_id=group_id,
        sort_order=new_order
    )
    
    try:
        db.add(new_item)
        estimate_result = None
        if estimate_payload:
            estimate_result = _upsert_fund_estimate(
                db,
                fund_code,
                name=estimate_payload.get('name') or fund_name,
                net_worth=_value_to_string(estimate_payload.get('net_worth')),
                net_worth_date=estimate_payload.get('net_worth_date'),
                estimate_value=_value_to_string(estimate_payload.get('estimate_value')),
                estimate_change=_value_to_string(estimate_payload.get('estimate_change')),
                estimate_time=estimate_payload.get('estimate_time')
            )
        db.commit()
        if not estimate_result:
            try:
                estimate_db = SessionLocal()
                try:
                    estimate_result = _refresh_single_fund_estimate(estimate_db, fund_code)
                    estimate_db.commit()
                finally:
                    estimate_db.close()
            except Exception as estimate_exc:
                print(f"add watchlist: initial estimate refresh failed for {fund_code}: {estimate_exc}")
        return jsonify({
            'message': 'Fund added to watchlist',
            'fund_code': fund_code,
            'sort_order': new_order,
            'estimate': estimate_result
        }), 201
    except Exception as e:
        db.rollback()
        return jsonify({'error': str(e)}), 500


@app.route('/api/watchlist/<fund_code>', methods=['DELETE'])
def remove_from_watchlist(fund_code):
    fund_code = _normalize_fund_code(fund_code)
    """从自选列表移除基金"""
    db = get_db()
    
    item = db.query(FundWatchlist).filter(FundWatchlist.fund_code == fund_code).first()
    if not item:
        return jsonify({'error': 'Fund not in watchlist'}), 404
    
    try:
        db.delete(item)
        db.commit()
        return jsonify({'message': 'Fund removed from watchlist', 'fund_code': fund_code})
    except Exception as e:
        db.rollback()
        return jsonify({'error': str(e)}), 500


@app.route('/api/watchlist/batch-delete', methods=['POST'])
def batch_delete_from_watchlist():
    """批量删除自选基金"""
    data = request.get_json()
    fund_codes = [_normalize_fund_code(code) for code in data.get('fund_codes', [])]
    
    if not fund_codes:
        return jsonify({'error': 'Fund codes are required'}), 400
    
    db = get_db()
    
    try:
        deleted_count = db.query(FundWatchlist).filter(
            FundWatchlist.fund_code.in_(fund_codes)
        ).delete(synchronize_session=False)
        db.commit()
        return jsonify({
            'message': f'Deleted {deleted_count} funds from watchlist',
            'deleted_count': deleted_count
        })
    except Exception as e:
        db.rollback()
        return jsonify({'error': str(e)}), 500


@app.route('/api/watchlist/reorder', methods=['PUT'])
def reorder_watchlist():
    """
    更新自选基金排序
    请求体格式: { "order": ["000001", "000002", "000003"], "group_id": 1 }
    数组顺序即为排序顺序，索引值作为 sort_order
    group_id 可选，用于同时更新基金的分组
    """
    data = request.get_json()
    order = data.get('order', [])
    group_id = data.get('group_id')  # 可选，移动到某个分组
    
    if not order:
        return jsonify({'error': 'Order array is required'}), 400
    
    db = get_db()
    
    try:
        for index, fund_code in enumerate(order):
            fund_code = _normalize_fund_code(fund_code)
            update_data = {'sort_order': index}
            if group_id is not None:
                update_data['group_id'] = group_id if group_id > 0 else None
            db.query(FundWatchlist).filter(
                FundWatchlist.fund_code == fund_code
            ).update(update_data)
        db.commit()
        return jsonify({'message': 'Watchlist reordered successfully'})
    except Exception as e:
        db.rollback()
        return jsonify({'error': str(e)}), 500


# ==================== 分组管理 API ====================

@app.route('/api/watchlist/groups', methods=['GET'])
def get_groups():
    """获取所有分组"""
    db = get_db()
    groups = db.query(FundWatchlistGroup).order_by(FundWatchlistGroup.sort_order).all()
    
    result = [{
        'id': g.id,
        'name': g.name,
        'sort_order': g.sort_order
    } for g in groups]
    
    return jsonify({'data': result})


@app.route('/api/watchlist/groups', methods=['POST'])
def create_group():
    """创建新分组"""
    data = request.get_json()
    name = data.get('name', '').strip()
    
    if not name:
        return jsonify({'error': 'Group name is required'}), 400
    
    db = get_db()
    
    # 获取最大排序值
    max_order = db.query(FundWatchlistGroup).order_by(FundWatchlistGroup.sort_order.desc()).first()
    new_order = (max_order.sort_order + 1) if max_order else 0
    
    new_group = FundWatchlistGroup(name=name, sort_order=new_order)
    
    try:
        db.add(new_group)
        db.commit()
        return jsonify({
            'message': 'Group created',
            'group': {
                'id': new_group.id,
                'name': new_group.name,
                'sort_order': new_group.sort_order
            }
        }), 201
    except Exception as e:
        db.rollback()
        return jsonify({'error': str(e)}), 500


@app.route('/api/watchlist/groups/<int:group_id>', methods=['PUT'])
def update_group(group_id):
    """更新分组（重命名）"""
    data = request.get_json()
    name = data.get('name', '').strip()
    
    if not name:
        return jsonify({'error': 'Group name is required'}), 400
    
    db = get_db()
    group = db.query(FundWatchlistGroup).filter(FundWatchlistGroup.id == group_id).first()
    
    if not group:
        return jsonify({'error': 'Group not found'}), 404
    
    try:
        group.name = name
        db.commit()
        return jsonify({'message': 'Group updated', 'group': {'id': group.id, 'name': group.name}})
    except Exception as e:
        db.rollback()
        return jsonify({'error': str(e)}), 500


@app.route('/api/watchlist/groups/<int:group_id>', methods=['DELETE'])
def delete_group(group_id):
    """删除分组（分组内的基金会变为未分组）"""
    db = get_db()
    group = db.query(FundWatchlistGroup).filter(FundWatchlistGroup.id == group_id).first()
    
    if not group:
        return jsonify({'error': 'Group not found'}), 404
    
    try:
        # 将该分组的基金设为未分组
        db.query(FundWatchlist).filter(FundWatchlist.group_id == group_id).update({'group_id': None})
        db.delete(group)
        db.commit()
        return jsonify({'message': 'Group deleted'})
    except Exception as e:
        db.rollback()
        return jsonify({'error': str(e)}), 500


@app.route('/api/watchlist/groups/reorder', methods=['PUT'])
def reorder_groups():
    """更新分组排序"""
    data = request.get_json()
    order = data.get('order', [])  # [group_id1, group_id2, ...]
    
    if not order:
        return jsonify({'error': 'Order array is required'}), 400
    
    db = get_db()
    
    try:
        for index, group_id in enumerate(order):
            db.query(FundWatchlistGroup).filter(
                FundWatchlistGroup.id == group_id
            ).update({'sort_order': index})
        db.commit()
        return jsonify({'message': 'Groups reordered successfully'})
    except Exception as e:
        db.rollback()
        return jsonify({'error': str(e)}), 500


@app.route('/api/watchlist/move', methods=['PUT'])
def move_fund_to_group():
    """移动基金到指定分组"""
    data = request.get_json()
    fund_code = _normalize_fund_code(data.get('fund_code'))
    group_id = data.get('group_id')  # None 或 0 表示移到未分组
    
    if not fund_code:
        return jsonify({'error': 'Fund code is required'}), 400
    
    db = get_db()
    fund = db.query(FundWatchlist).filter(FundWatchlist.fund_code == fund_code).first()
    
    if not fund:
        return jsonify({'error': 'Fund not in watchlist'}), 404
    
    try:
        fund.group_id = group_id if group_id and group_id > 0 else None
        db.commit()
        return jsonify({'message': 'Fund moved successfully'})
    except Exception as e:
        db.rollback()
        return jsonify({'error': str(e)}), 500


def _value_to_string(value):
    if value is None:
        return None
    return str(value)


def _normalize_date(value):
    """Normalize a date to YYYY-MM-DD for safe string comparison.

    Handles zero-padding differences (e.g. "2026-6-21" → "2026-06-21")
    and slash formats (e.g. "2026/06/21" → "2026-06-21").
    """
    if not value:
        return ''
    text = str(value).strip()
    match = re.search(r'(\d{4})[-/](\d{1,2})[-/](\d{1,2})', text)
    if match:
        return '{}-{:02d}-{:02d}'.format(
            match.group(1),
            int(match.group(2)),
            int(match.group(3)),
        )
    return text


def _extract_date_text(value):
    if not value:
        return ''
    match = re.search(r'\d{4}[-/]\d{1,2}[-/]\d{1,2}', str(value))
    return match.group(0).replace('/', '-') if match else ''


def _estimate_is_after_nav(estimate_time, nav_date):
    estimate_date = _normalize_date(estimate_time)
    official_date = _normalize_date(nav_date)
    return bool(estimate_date and official_date and estimate_date > official_date)


def _apply_estimate_fields(rec, name, net_worth, net_worth_date,
                          estimate_value, estimate_change, estimate_time):
    """将字段应用到已有的 FundEstimate 记录（仅覆盖非 None 值）"""
    if name is not None:
        rec.name = name
    if net_worth_date is not None and (
        not rec.net_worth_date
        or _normalize_date(net_worth_date) >= _normalize_date(rec.net_worth_date)
    ):
        if net_worth is not None:
            rec.net_worth = net_worth
        rec.net_worth_date = net_worth_date
    if estimate_value is not None:
        rec.estimate_value = estimate_value
    if estimate_change is not None:
        rec.estimate_change = estimate_change
    if estimate_time is not None:
        rec.estimate_time = estimate_time


def _upsert_fund_estimate(
    db,
    fund_code,
    name=None,
    net_worth=None,
    net_worth_date=None,
    estimate_value=None,
    estimate_change=None,
    estimate_time=None,
):
    estimate_record = db.query(FundEstimate).filter(
        FundEstimate.fund_code == fund_code
    ).first()

    if estimate_record:
        # 更新已有记录
        _apply_estimate_fields(estimate_record, name, net_worth, net_worth_date,
                               estimate_value, estimate_change, estimate_time)
    else:
        estimate_record = FundEstimate(
            fund_code=fund_code,
            name=name,
            net_worth=net_worth,
            net_worth_date=net_worth_date,
            estimate_value=estimate_value,
            estimate_change=estimate_change,
            estimate_time=estimate_time
        )
        db.add(estimate_record)
        try:
            db.flush()
        except Exception:
            # 并发竞态：另一个请求先插入了同一 fund_code
            db.rollback()
            existing = db.query(FundEstimate).filter(
                FundEstimate.fund_code == fund_code
            ).first()
            if existing:
                _apply_estimate_fields(existing, name, net_worth, net_worth_date,
                                       estimate_value, estimate_change, estimate_time)
                estimate_record = existing
            else:
                # 极罕见情况，直接重新 add
                db.add(estimate_record)

    return {
        'fund_code': fund_code,
        'estimate_value': estimate_value,
        'estimate_change': estimate_change,
        'estimate_time': estimate_time,
        'net_worth': net_worth,
        'net_worth_date': net_worth_date
    }


def _upsert_data_service_estimate(db, fund_code, estimate_data):
    return _upsert_fund_estimate(
        db,
        fund_code,
        name=estimate_data.get('name'),
        net_worth=_value_to_string(estimate_data.get('nav')),
        net_worth_date=estimate_data.get('navDate'),
        estimate_value=_value_to_string(estimate_data.get('estimatedNav')),
        estimate_change=_value_to_string(estimate_data.get('estimatedChangePercent')),
        estimate_time=estimate_data.get('estimateTime')
    )


def _latest_nav_from_data_service(fund_code):
    payload = get_data_service_client().get_fund_nav_history(fund_code)
    items = _nav_history_payload_items(payload)
    latest = None
    for item in items:
        if not isinstance(item, dict):
            continue
        date = item.get('date')
        nav = item.get('nav')
        if not date or nav is None:
            continue
        if latest is None or _normalize_date(date) > _normalize_date(latest.get('date')):
            latest = item
    if not latest:
        return None
    return {
        'date': str(latest.get('date')),
        'nav': latest.get('nav'),
        'daily_return': latest.get('dailyReturn'),
    }


def _first_present(mapping, keys):
    for key in keys:
        if isinstance(mapping, dict) and key in mapping and mapping.get(key) is not None:
            return mapping.get(key)
    return None


def _trend_daily_return(rows):
    if not isinstance(rows, list) or not rows:
        return None

    normalized = [
        row for row in rows
        if isinstance(row, dict) and row.get('date') is not None and row.get('net_worth') is not None
    ]
    if not normalized:
        return None

    normalized.sort(key=lambda item: _normalize_date(item.get('date')))
    latest = normalized[-1]
    daily_return = _first_present(latest, ('dailyReturn', 'equityReturn', 'growth_rate'))
    if daily_return is not None:
        return daily_return

    if len(normalized) >= 2:
        try:
            prev_nav = float(normalized[-2].get('net_worth'))
            curr_nav = float(latest.get('net_worth'))
            if prev_nav:
                return round((curr_nav - prev_nav) / prev_nav * 100, 4)
        except Exception:
            return None
    return None


def _latest_nav_from_fund_trend(db, fund_code):
    trend = db.query(FundTrend).filter(FundTrend.fund_code == fund_code).first()
    rows = _json_loads(trend.net_worth_trend_json, []) if trend else []
    if not isinstance(rows, list) or not rows:
        return None

    normalized = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        date = row.get('date')
        nav = row.get('net_worth')
        if date is None or nav is None:
            continue
        normalized.append(row)
    if not normalized:
        return None

    normalized.sort(key=lambda item: _normalize_date(item.get('date')))
    latest = normalized[-1]
    daily_return = _trend_daily_return(normalized)

    return {
        'date': str(latest.get('date')),
        'nav': latest.get('net_worth'),
        'daily_return': daily_return,
    }


def _refresh_fund_detail_estimate_snapshot(db, fund_code, result=None):
    """Refresh lightweight NAV/estimate fields from the fund detail source.

    Opening the detail page writes pingzhongdata's latest net-worth trend into
    FundTrend. The watchlist refresh should do the same small sync so users do
    not need to open each fund before seeing the latest official NAV.
    """
    try:
        raw_data = fund_api._fetch_raw_data(fund_code)
    except Exception as exc:
        print(f"refresh detail snapshot: failed for {fund_code}: {exc}")
        return result

    if not raw_data:
        return result

    trend_rows = fund_api.cleaner.clean_array_data(
        raw_data.get('Data_netWorthTrend'), 'net_worth'
    )
    if trend_rows:
        trend_record = db.query(FundTrend).filter(FundTrend.fund_code == fund_code).first()
        if trend_record:
            trend_record.net_worth_trend_json = _json_dumps(trend_rows)
            trend_record.updated_time = datetime.now()
        else:
            db.add(FundTrend(
                fund_code=fund_code,
                net_worth_trend_json=_json_dumps(trend_rows),
                accumulated_net_worth_json=_json_dumps([]),
                position_trend_json=_json_dumps([]),
                total_return_trend_json=_json_dumps([]),
                ranking_trend_json=_json_dumps([]),
                ranking_percentage_json=_json_dumps([]),
                scale_fluctuation_json=_json_dumps({}),
            ))

    latest_nav = trend_rows[-1] if trend_rows else {}
    fundgz_nav = raw_data.get('dwjz')
    fundgz_date = raw_data.get('jzrq')
    net_worth = fundgz_nav
    net_worth_date = fundgz_date
    if latest_nav:
        trend_nav = latest_nav.get('net_worth')
        trend_date = latest_nav.get('date')
        if trend_nav is not None and trend_date is not None:
            if not fundgz_date or _normalize_date(trend_date) > _normalize_date(fundgz_date):
                net_worth = trend_nav
                net_worth_date = trend_date

    result = _upsert_fund_estimate(
        db,
        fund_code,
        name=raw_data.get('name') or raw_data.get('fS_name'),
        net_worth=_value_to_string(net_worth),
        net_worth_date=net_worth_date,
        estimate_value=_value_to_string(raw_data.get('gsz')),
        estimate_change=_value_to_string(raw_data.get('gszzl')),
        estimate_time=raw_data.get('gztime'),
    )

    return _sync_latest_official_nav(db, fund_code, result)


def _sync_latest_official_nav(db, fund_code, result=None):
    candidates = []
    try:
        latest_ds = _latest_nav_from_data_service(fund_code)
        if latest_ds:
            latest_ds['source'] = 'data_service_nav'
            candidates.append(latest_ds)
    except Exception as exc:
        print(f"sync official nav: DataService unavailable for {fund_code}: {exc}")

    latest_trend = _latest_nav_from_fund_trend(db, fund_code)
    if latest_trend:
        latest_trend['source'] = 'fund_trend'
        candidates.append(latest_trend)

    latest = None
    for item in candidates:
        if not item.get('date'):
            continue
        if latest is None or _normalize_date(item.get('date')) > _normalize_date(latest.get('date')):
            latest = item
    if not latest:
        return result

    estimate = db.query(FundEstimate).filter(FundEstimate.fund_code == fund_code).first()
    if not estimate:
        estimate = FundEstimate(fund_code=fund_code)
        db.add(estimate)

    latest_date = latest.get('date')
    current_date = estimate.net_worth_date
    if latest_date and (not current_date or _normalize_date(latest_date) >= _normalize_date(current_date)):
        latest_nav = _value_to_string(latest.get('nav'))
        if latest_nav is not None:
            estimate.net_worth = latest_nav
        estimate.net_worth_date = latest_date
        has_newer_intraday_estimate = _estimate_is_after_nav(estimate.estimate_time, latest_date)
        if latest.get('daily_return') is not None and not has_newer_intraday_estimate:
            estimate.estimate_change = _value_to_string(latest.get('daily_return'))

        if isinstance(result, dict):
            result['net_worth'] = estimate.net_worth
            result['net_worth_date'] = estimate.net_worth_date
            if not has_newer_intraday_estimate:
                result['estimate_change'] = estimate.estimate_change
            result['official_nav_synced'] = True

    return result


def _refresh_fundgz_estimate(db, fund_code):
    real_time_url = f"http://fundgz.1234567.com.cn/js/{fund_code}.js"
    response = requests.get(real_time_url, headers={
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    }, timeout=1.5)

    if response.status_code != 200:
        return None

    match = re.search(r"jsonpgz\((.*?)\);", response.text)
    if not match:
        return None

    rt_data = json.loads(match.group(1))
    if not rt_data:
        return None

    net_worth = rt_data.get('dwjz')
    net_worth_date = rt_data.get('jzrq')

    # 兜底：如果 fundgz CDN 延迟，从 FundTrend JSON 取最新的净值
    # FundTrend 在查看详情页时由 pingzhongdata 写入，更新更及时
    try:
        trend = db.query(FundTrend).filter(FundTrend.fund_code == fund_code).first()
        if trend and trend.net_worth_trend_json:
            trend_data = json.loads(trend.net_worth_trend_json)
            if isinstance(trend_data, list) and trend_data:
                last = trend_data[-1]
                if isinstance(last, dict):
                    t_nw = last.get('net_worth')
                    t_date = last.get('date')
                    if t_nw is not None and t_date is not None:
                        if not net_worth_date or _normalize_date(t_date) > _normalize_date(net_worth_date):
                            net_worth = str(t_nw)
                            net_worth_date = str(t_date)
    except Exception:
        pass  # FundTrend 可能没有数据，不影响

    return _upsert_fund_estimate(
        db,
        fund_code,
        name=rt_data.get('name'),
        net_worth=net_worth,
        net_worth_date=net_worth_date,
        estimate_value=rt_data.get('gsz'),
        estimate_change=rt_data.get('gszzl'),
        estimate_time=rt_data.get('gztime')
    )


def _refresh_single_fund_estimate(db, fund_code):
    result = None
    try:
        payload = DataServiceClient(timeout=1.5).get_fund_estimates([fund_code])
        ds_data = payload.get('data', {}) if isinstance(payload, dict) else {}
        for item in ds_data.get('items', []) if isinstance(ds_data, dict) else []:
            if isinstance(item, dict) and item.get('code') == fund_code and isinstance(item.get('data'), dict):
                result = _upsert_data_service_estimate(db, fund_code, item['data'])
                break
    except Exception as exc:
        print(f"refresh single estimate: DataService unavailable for {fund_code}: {exc}")

    detail_result = _refresh_fund_detail_estimate_snapshot(db, fund_code, result)
    if detail_result:
        return detail_result

    try:
        result = _refresh_fundgz_estimate(db, fund_code)
        if result:
            return _sync_latest_official_nav(db, fund_code, result)
    except Exception as exc:
        print(f"refresh single estimate: fundgz failed for {fund_code}: {exc}")

    latest = _latest_nav_from_fund_trend(db, fund_code)
    if latest:
        return _sync_latest_official_nav(db, fund_code, {
            'fund_code': fund_code,
            'estimate_value': None,
            'estimate_change': None,
            'estimate_time': None,
            'net_worth': _value_to_string(latest.get('nav')),
            'net_worth_date': latest.get('date'),
        })

    # 兜底：尝试复用 FundEstimate 中已有的缓存数据（例如之前查看详情页时写入的）
    existing = db.query(FundEstimate).filter(FundEstimate.fund_code == fund_code).first()
    if existing:
        print(f"refresh single estimate: reusing cached estimate for {fund_code}")
        return {
            'fund_code': fund_code,
            'estimate_value': existing.estimate_value,
            'estimate_change': existing.estimate_change,
            'estimate_time': existing.estimate_time,
            'net_worth': existing.net_worth,
            'net_worth_date': existing.net_worth_date,
        }
    return None


@app.route('/api/watchlist/refresh-estimates', methods=['GET', 'POST'])
def refresh_watchlist_estimates():
    """
    批量刷新自选基金的实时估值数据
    此接口专门用于快速获取实时估值，不涉及完整基金数据更新
    """
    db = get_db()
    
    # 获取所有自选基金代码
    watchlist = db.query(FundWatchlist).all()
    if not watchlist:
        return jsonify({'message': 'Watchlist is empty', 'updated': 0})
    
    fund_codes = [item.fund_code for item in watchlist]
    updated_count = 0
    results = []
    failed_count = 0

    for fund_code in fund_codes:
        try:
            result = _refresh_single_fund_estimate(db, fund_code)
            if result:
                updated_count += 1
                results.append(result)
            else:
                failed_count += 1
        except Exception as e:
            # 单个基金失败不影响其他
            print(f"刷新 {fund_code} 估值失败: {e}")
            failed_count += 1

    print(
        "refresh_watchlist_estimates: "
        f"updated={updated_count}, failed={failed_count}, total={len(fund_codes)}"
    )

    try:
        db.commit()
    except Exception as e:
        db.rollback()
        return jsonify({'error': str(e)}), 500
    
    return jsonify({
        'message': f'Updated {updated_count} funds',
        'updated': updated_count,
        'total': len(fund_codes),
        'failed': failed_count,
        'data': results
    })


# ==================== 风险指标计算 ====================

def calculate_risk_metrics(net_worth_trend):
    """
    计算基金风险指标：最大回撤、夏普比率、年化波动率、年化收益率
    net_worth_trend: [{'date': '2024-01-01', 'net_worth': 1.0}, ...]
    """
    if not net_worth_trend or len(net_worth_trend) < 30:
        return None
    
    # 按日期排序
    sorted_data = sorted(net_worth_trend, key=lambda x: x.get('date', ''))
    
    # 转换为净值数组和日期数组
    dates = []
    values = []
    for item in sorted_data:
        if item.get('net_worth') is not None:
            dates.append(item.get('date'))
            values.append(float(item.get('net_worth')))
    
    # 过滤首日异常数据（如面值1.0与实际净值100+差异巨大）
    # 这种情况会导致波动率和回撤计算极其离谱
    if len(values) >= 2:
        v0 = values[0]
        v1 = values[1]
        if v0 > 0 and abs((v1 - v0) / v0) > 0.5:
            values.pop(0)
            dates.pop(0)
    
    if len(values) < 30:
        return None
    
    now = datetime.now()
    
    def get_period_data(months):
        """获取指定时间段的数据"""
        if months == 'all':
            return values, dates
        
        cutoff_date = (now - timedelta(days=months * 30)).strftime('%Y-%m-%d')
        period_values = []
        period_dates = []
        for i, d in enumerate(dates):
            if d >= cutoff_date:
                period_values.append(values[i])
                period_dates.append(d)
        return period_values, period_dates
    
    def calc_max_drawdown(period_values):
        """计算最大回撤"""
        if len(period_values) < 2:
            return None
        
        peak = period_values[0]
        max_dd = 0
        
        for value in period_values:
            if value > peak:
                peak = value
            drawdown = (peak - value) / peak * 100
            if drawdown > max_dd:
                max_dd = drawdown
        
        return round(max_dd, 2)
    
    def calc_daily_returns(period_values):
        """计算日收益率序列"""
        if len(period_values) < 2:
            return []
        returns = []
        for i in range(1, len(period_values)):
            if period_values[i-1] != 0:
                ret = (period_values[i] - period_values[i-1]) / period_values[i-1]
                returns.append(ret)
        return returns
    
    def calc_annual_return(period_values, trading_days):
        """计算年化收益率"""
        if len(period_values) < 2 or period_values[0] == 0:
            return None
        total_return = (period_values[-1] - period_values[0]) / period_values[0]
        if trading_days <= 0:
            return None
        annual_return = ((1 + total_return) ** (252 / trading_days) - 1) * 100
        return round(annual_return, 2)
    
    def calc_volatility(daily_returns):
        """计算年化波动率"""
        if len(daily_returns) < 10:
            return None
        mean_return = sum(daily_returns) / len(daily_returns)
        variance = sum((r - mean_return) ** 2 for r in daily_returns) / len(daily_returns)
        daily_vol = math.sqrt(variance)
        annual_vol = daily_vol * math.sqrt(252) * 100
        return round(annual_vol, 2)
    
    def calc_sharpe_ratio(annual_return, volatility, risk_free_rate=2.0):
        """计算夏普比率，假设无风险利率为2%"""
        if volatility is None or volatility == 0 or annual_return is None:
            return None
        sharpe = (annual_return - risk_free_rate) / volatility
        return round(sharpe, 2)
    
    result = {}
    
    # 计算不同时间段的最大回撤
    for period, months in [('3m', 3), ('6m', 6), ('1y', 12), ('3y', 36), ('all', 'all')]:
        period_values, _ = get_period_data(months)
        result[f'max_drawdown_{period}'] = calc_max_drawdown(period_values)
    
    # 计算1年和3年的年化收益率、波动率、夏普比率
    # 重要：对于数据不足的周期，不计算指标（返回None），避免年化放大产生误导性数据
    min_trading_days = {
        '1y': 200,  # 至少200个交易日才计算1年期指标（约10个月）
        '3y': 600,  # 至少600个交易日才计算3年期指标（约2.5年）
    }
    
    for period, months in [('1y', 12), ('3y', 36)]:
        period_values, period_dates = get_period_data(months)
        trading_days = len(period_values)
        
        # 检查数据是否充足
        min_days = min_trading_days.get(period, 30)
        if trading_days < min_days:
            # 数据不足，不计算该周期的指标
            result[f'annual_return_{period}'] = None
            result[f'volatility_{period}'] = None
            result[f'sharpe_ratio_{period}'] = None
            result[f'calmar_ratio_{period}'] = None
            continue
        
        daily_returns = calc_daily_returns(period_values)
        annual_return = calc_annual_return(period_values, trading_days)
        volatility = calc_volatility(daily_returns)
        sharpe = calc_sharpe_ratio(annual_return, volatility)
        
        # 额外检查：如果波动率异常大（>500%），说明数据有问题，放弃该计算结果
        if volatility is not None and volatility > 500:
            result[f'annual_return_{period}'] = None
            result[f'volatility_{period}'] = None
            result[f'sharpe_ratio_{period}'] = None
            result[f'calmar_ratio_{period}'] = None
            continue
        
        result[f'annual_return_{period}'] = annual_return
        result[f'volatility_{period}'] = volatility
        result[f'sharpe_ratio_{period}'] = sharpe
        
        # 计算卡玛比率
        max_dd = result.get(f'max_drawdown_{period}')
        if annual_return is not None and max_dd is not None and max_dd > 0:
            result[f'calmar_ratio_{period}'] = round(annual_return / max_dd, 2)
        else:
            result[f'calmar_ratio_{period}'] = None
    
    return result


def is_data_fresh(updated_time, days=7):
    """检查数据是否在指定天数内"""
    if not updated_time:
        return False
    return (datetime.now() - updated_time).days < days


@app.route('/api/fund/<fund_code>/compare-data', methods=['GET'])
def get_fund_compare_data(fund_code):
    fund_code = _normalize_fund_code(fund_code)
    """
    获取基金对比数据，优先使用数据库缓存（1周内）
    返回完整的基金详情数据和风险指标
    
    数据来源（统一）：
    - 基本信息、业绩: FundBasicInfo
    - 走势数据: FundTrend
    - 扩展数据: FundExtraData
    - 风险指标: FundRiskMetrics
    """
    db = get_db()
    force_refresh = request.args.get('refresh', 'false').lower() == 'true'
    
    try:
        # 检查缓存数据是否新鲜（1周内）
        trend_record = db.query(FundTrend).filter(FundTrend.fund_code == fund_code).first()
        risk_record = db.query(FundRiskMetrics).filter(FundRiskMetrics.fund_code == fund_code).first()
        
        use_cache = (
            not force_refresh and 
            trend_record and 
            is_data_fresh(trend_record.updated_time, days=7)
        )
        
        if use_cache:
            # 使用缓存数据
            data = _build_cached_response(db, fund_code)
            if data:
                # 检查风险指标是否存在且新鲜
                risk_data_valid = (
                    risk_record and 
                    is_data_fresh(risk_record.updated_time, days=7) and
                    risk_record.sharpe_ratio_1y is not None
                )

                # 额外检查：如果波动率异常大（>1000%），说明之前计算时受到了脏数据影响，需要重算
                if risk_data_valid and risk_record.volatility_1y and risk_record.volatility_1y > 1000:
                    risk_data_valid = False
                
                if risk_data_valid:
                    data['risk_metrics'] = {
                        'max_drawdown_3m': risk_record.max_drawdown_3m,
                        'max_drawdown_6m': risk_record.max_drawdown_6m,
                        'max_drawdown_1y': risk_record.max_drawdown_1y,
                        'max_drawdown_3y': risk_record.max_drawdown_3y,
                        'max_drawdown_all': risk_record.max_drawdown_all,
                        'sharpe_ratio_1y': risk_record.sharpe_ratio_1y,
                        'sharpe_ratio_3y': risk_record.sharpe_ratio_3y,
                        'volatility_1y': risk_record.volatility_1y,
                        'volatility_3y': risk_record.volatility_3y,
                        'annual_return_1y': risk_record.annual_return_1y,
                        'annual_return_3y': risk_record.annual_return_3y,
                        'calmar_ratio_1y': risk_record.calmar_ratio_1y,
                        'calmar_ratio_3y': risk_record.calmar_ratio_3y,
                    }
                else:
                    # 风险指标缺失，从缓存的净值数据计算
                    net_worth_trend = data.get('net_worth_trend', [])
                    risk_metrics = calculate_risk_metrics(net_worth_trend)
                    
                    if risk_metrics:
                        # 保存到 FundRiskMetrics
                        _save_risk_metrics(db, fund_code, risk_metrics)
                        db.commit()
                        data['risk_metrics'] = risk_metrics
                    else:
                        data['risk_metrics'] = {}
                
                data['data_source'] = 'cache'
                data['cache_time'] = trend_record.updated_time.isoformat() if trend_record.updated_time else None
                return jsonify(data)
        
        # 从API获取新数据
        api_data = fund_api.get_fund_data(fund_code)
        if not api_data:
            # 如果API失败，尝试返回缓存数据
            if trend_record:
                data = _build_cached_response(db, fund_code)
                if data:
                    if risk_record and risk_record.sharpe_ratio_1y is not None:
                        data['risk_metrics'] = {
                            'max_drawdown_3m': risk_record.max_drawdown_3m,
                            'max_drawdown_6m': risk_record.max_drawdown_6m,
                            'max_drawdown_1y': risk_record.max_drawdown_1y,
                            'max_drawdown_3y': risk_record.max_drawdown_3y,
                            'max_drawdown_all': risk_record.max_drawdown_all,
                            'sharpe_ratio_1y': risk_record.sharpe_ratio_1y,
                            'sharpe_ratio_3y': risk_record.sharpe_ratio_3y,
                            'volatility_1y': risk_record.volatility_1y,
                            'volatility_3y': risk_record.volatility_3y,
                            'annual_return_1y': risk_record.annual_return_1y,
                            'annual_return_3y': risk_record.annual_return_3y,
                            'calmar_ratio_1y': risk_record.calmar_ratio_1y,
                            'calmar_ratio_3y': risk_record.calmar_ratio_3y,
                        }
                    else:
                        net_worth_trend = data.get('net_worth_trend', [])
                        risk_metrics = calculate_risk_metrics(net_worth_trend)
                        if risk_metrics:
                            _save_risk_metrics(db, fund_code, risk_metrics)
                            db.commit()
                        data['risk_metrics'] = risk_metrics or {}
                    data['data_source'] = 'stale_cache'
                    return jsonify(data)
            return jsonify({'error': 'Failed to fetch fund data'}), 500
        
        # 计算风险指标
        net_worth_trend = api_data.get('net_worth_trend', [])
        risk_metrics = calculate_risk_metrics(net_worth_trend)
        
        # 保存到数据库（所有相关表）
        _save_fund_data_to_db(db, fund_code, api_data)
        if risk_metrics:
            _save_risk_metrics(db, fund_code, risk_metrics)
        db.commit()
        
        # 返回数据
        api_data['risk_metrics'] = risk_metrics or {}
        api_data['data_source'] = 'api'
        return jsonify(api_data)
        
    except Exception as e:
        print(f"Error fetching fund compare data: {e}")
        db.rollback()
        return jsonify({'error': str(e)}), 500


def _save_risk_metrics(db: Session, fund_code: str, risk_metrics: dict):
    """保存风险指标到 FundRiskMetrics 表"""
    if not risk_metrics:
        return
    
    risk_record = db.query(FundRiskMetrics).filter(FundRiskMetrics.fund_code == fund_code).first()
    
    if risk_record:
        for key, value in risk_metrics.items():
            if hasattr(risk_record, key):
                setattr(risk_record, key, value)
        risk_record.updated_time = datetime.now()
    else:
        risk_record = FundRiskMetrics(
            fund_code=fund_code,
            **{k: v for k, v in risk_metrics.items() if hasattr(FundRiskMetrics, k)}
        )
        db.add(risk_record)


def _save_fund_data_to_db(db: Session, fund_code: str, data: dict):
    """保存基金数据到数据库"""
    try:
        # 保存基本信息
        basic_info = data.get('basic_info', {})
        performance = data.get('performance', {})
        # 解析收益率用于排序
        return_1y_val = None
        try:
            return_1y_val = float(performance.get('1_year_return')) if performance.get('1_year_return') else None
        except (ValueError, TypeError):
            pass
        
        basic_record = db.query(FundBasicInfo).filter(FundBasicInfo.fund_code == fund_code).first()
        if basic_record:
            basic_record.fund_name = basic_info.get('fund_name', '')
            basic_record.fund_type = basic_info.get('fund_type', '')
            basic_record.return_1y = return_1y_val
            basic_record.basic_json = _json_dumps(basic_info)
            basic_record.performance_json = _json_dumps(performance)
            basic_record.updated_time = datetime.now()
        else:
            basic_record = FundBasicInfo(
                fund_code=fund_code,
                fund_name=basic_info.get('fund_name', ''),
                fund_type=basic_info.get('fund_type', ''),
                return_1y=return_1y_val,
                basic_json=_json_dumps(basic_info),
                performance_json=_json_dumps(performance)
            )
            db.add(basic_record)
        
        # 保存走势数据
        trend_record = db.query(FundTrend).filter(FundTrend.fund_code == fund_code).first()
        if trend_record:
            trend_record.net_worth_trend_json = _json_dumps(data.get('net_worth_trend', []))
            trend_record.accumulated_net_worth_json = _json_dumps(data.get('accumulated_net_worth', []))
            trend_record.position_trend_json = _json_dumps(data.get('position_trend', []))
            trend_record.total_return_trend_json = _json_dumps(data.get('total_return_trend', []))
            trend_record.ranking_trend_json = _json_dumps(data.get('ranking_trend', []))
            trend_record.ranking_percentage_json = _json_dumps(data.get('ranking_percentage', []))
            trend_record.scale_fluctuation_json = _json_dumps(data.get('scale_fluctuation', {}))
            trend_record.updated_time = datetime.now()
        else:
            trend_record = FundTrend(
                fund_code=fund_code,
                net_worth_trend_json=_json_dumps(data.get('net_worth_trend', [])),
                accumulated_net_worth_json=_json_dumps(data.get('accumulated_net_worth', [])),
                position_trend_json=_json_dumps(data.get('position_trend', [])),
                total_return_trend_json=_json_dumps(data.get('total_return_trend', [])),
                ranking_trend_json=_json_dumps(data.get('ranking_trend', [])),
                ranking_percentage_json=_json_dumps(data.get('ranking_percentage', [])),
                scale_fluctuation_json=_json_dumps(data.get('scale_fluctuation', {}))
            )
            db.add(trend_record)
        
        # 保存额外数据
        extra_record = db.query(FundExtraData).filter(FundExtraData.fund_code == fund_code).first()
        if extra_record:
            extra_record.holder_structure_json = _json_dumps(data.get('holder_structure', {}))
            extra_record.asset_allocation_json = _json_dumps(data.get('asset_allocation', {}))
            extra_record.performance_evaluation_json = _json_dumps(data.get('performance_evaluation', {}))
            extra_record.fund_managers_json = _json_dumps(data.get('fund_managers', []))
            extra_record.subscription_redemption_json = _json_dumps(data.get('subscription_redemption', {}))
            extra_record.same_type_funds_json = _json_dumps(data.get('same_type_funds', []))
            extra_record.updated_time = datetime.now()
        else:
            extra_record = FundExtraData(
                fund_code=fund_code,
                holder_structure_json=_json_dumps(data.get('holder_structure', {})),
                asset_allocation_json=_json_dumps(data.get('asset_allocation', {})),
                performance_evaluation_json=_json_dumps(data.get('performance_evaluation', {})),
                fund_managers_json=_json_dumps(data.get('fund_managers', [])),
                subscription_redemption_json=_json_dumps(data.get('subscription_redemption', {})),
                same_type_funds_json=_json_dumps(data.get('same_type_funds', []))
            )
            db.add(extra_record)
        
        db.commit()
    except Exception as e:
        db.rollback()
        print(f"Error saving fund data to db: {e}")


# ==================== 基金筛选功能 ====================

# 全局变量：批量更新状态
SCREENING_SNAPSHOT_TYPES = ['gp', 'hh', 'zq', 'zs', 'qdii', 'fof']


def _fund_type_lookup():
    lookup = {}
    for item in fund_list_cache.fund_list:
        code = _normalize_fund_code(item.get('CODE', ''))
        if code:
            lookup[code] = {
                'name': item.get('NAME') or '',
                'type': item.get('TYPE') or '',
            }
    return lookup


def _matches_fund_type(fund_type, selected_types):
    if not selected_types:
        return True
    text = str(fund_type or '')
    return any(str(selected or '') in text for selected in selected_types)


def _snapshot_performance(item):
    return {
        '1_month_return': item.get('return1m'),
        '3_month_return': item.get('return3m'),
        '6_month_return': item.get('return6m'),
        '1_year_return': item.get('return1y'),
        '2_year_return': item.get('return2y'),
        '3_year_return': item.get('return3y'),
        'ytd_return': item.get('ytd'),
        'since_inception_return': item.get('sinceInception'),
    }


def _save_screening_snapshot_item(db, item, type_lookup):
    code = _normalize_fund_code(item.get('code', ''))
    if not code:
        return False

    cached = type_lookup.get(code, {})
    fund_name = item.get('name') or cached.get('name') or code
    fund_type = item.get('type') or cached.get('type') or ''
    performance = _snapshot_performance(item)
    basic_info = {
        'fund_code': code,
        'fund_name': fund_name,
        'fund_type': fund_type,
        'net_worth': item.get('nav'),
        'net_worth_date': item.get('navDate'),
        'fee': item.get('fee'),
        'source': item.get('source'),
    }

    record = db.query(FundBasicInfo).filter(FundBasicInfo.fund_code == code).first()
    if record:
        # 跳过 7 天内已更新的数据，不再全量覆写
        if record.updated_time and (datetime.now() - record.updated_time) < timedelta(days=7):
            return True  # 数据新鲜，跳过
        record.fund_name = fund_name
        record.fund_type = fund_type
        record.return_1y = item.get('return1y')
        record.basic_json = _json_dumps(basic_info)
        record.performance_json = _json_dumps(performance)
        record.updated_time = datetime.now()
    else:
        db.add(FundBasicInfo(
            fund_code=code,
            fund_name=fund_name,
            fund_type=fund_type,
            return_1y=item.get('return1y'),
            basic_json=_json_dumps(basic_info),
            performance_json=_json_dumps(performance),
        ))

    _refresh_fund_industry_tag(db, code, fetch_holdings_if_missing=False)
    return True


def _fetch_screening_snapshot_items(limit=None, db=None, task_id=None):
    items = []
    max_count = int(limit) if limit else None
    client = get_data_service_client()
    total_types = len(SCREENING_SNAPSHOT_TYPES)
    t0 = time.time()

    print(f"[筛查更新] 开始获取批量排行 (共{total_types}类)...", flush=True)

    for idx, type_code in enumerate(SCREENING_SNAPSHOT_TYPES, 1):
        if max_count and len(items) >= max_count:
            break

        t1 = time.time()
        _set_screening_progress(
            db, task_id,
            message=f"获取排行 ({idx}/{total_types}): {type_code}",
            current_count=len(items),
        )

        try:
            payload = client.get_fund_screening_snapshot([type_code], page_size=500, sort='1nzf')
            elapsed = time.time() - t1
        except DataServiceError as e:
            print(f"[筛查更新] {type_code}: DataService错误({time.time()-t1:.1f}s): {e}", flush=True)
            _set_screening_progress(
                db, task_id,
                message=f"获取排行 ({idx}/{total_types}): {type_code} 失败, 跳过",
                current_count=len(items),
            )
            continue
        except Exception as e:
            print(f"[筛查更新] {type_code}: 未知错误({time.time()-t1:.1f}s): {e}", flush=True)
            _set_screening_progress(
                db, task_id,
                message=f"获取排行 ({idx}/{total_types}): {type_code} 异常, 跳过",
                current_count=len(items),
            )
            continue

        data = payload.get('data', {}) if isinstance(payload, dict) else {}
        page_items = data.get('items', []) if isinstance(data, dict) else []
        failed_pages = data.get('failedPages', []) if isinstance(data, dict) else []
        if failed_pages:
            print(f"[筛查更新] {type_code}: {len(failed_pages)}个失败页: {failed_pages[:3]}", flush=True)

        remaining = None if max_count is None else max_count - len(items)
        new_items = page_items if remaining is None else page_items[:remaining]
        items.extend(new_items)

        print(f"[筛查更新] {type_code}: +{len(new_items)}只 ({elapsed:.1f}s), 累计{len(items)}只", flush=True)

        _set_screening_progress(
            db, task_id,
            message=f"获取排行 ({idx}/{total_types}): {type_code} ✓ (+{len(new_items)}, 累计 {len(items)})",
            current_count=len(items),
        )

    print(f"[筛查更新] 批量排行获取完成: {len(items)}只, 耗时{time.time()-t0:.1f}s", flush=True)
    return items


def _nav_history_to_risk_input(payload):
    data = payload.get('data', {}) if isinstance(payload, dict) else {}
    items = data.get('items', []) if isinstance(data, dict) else []
    trend = []
    for item in items:
        if not isinstance(item, dict):
            continue
        nav = item.get('nav')
        date = item.get('date')
        if nav is not None and date:
            trend.append({'date': str(date), 'net_worth': nav})
    return trend


def _nav_history_payload_items(payload):
    data = payload.get('data', {}) if isinstance(payload, dict) else {}
    items = data.get('items', []) if isinstance(data, dict) else []
    return items if isinstance(items, list) else []


def _latest_cached_nav_date(db, fund_code):
    row = db.query(FundNavHistory.trade_date).filter(
        FundNavHistory.fund_code == fund_code
    ).order_by(desc(FundNavHistory.trade_date)).first()
    return row[0] if row else None


def _load_nav_history_rows(db, fund_code):
    rows = db.query(FundNavHistory).filter(
        FundNavHistory.fund_code == fund_code,
        FundNavHistory.nav.isnot(None),
    ).order_by(FundNavHistory.trade_date.asc()).all()
    return [
        {
            'date': row.trade_date,
            'net_worth': row.nav,
        }
        for row in rows
    ]


def _upsert_nav_history_rows(db, fund_code, payload):
    # Historical NAV is now used only transiently for risk calculation.
    # Keep this compatibility helper non-persistent to prevent DB growth.
    return len(_nav_history_payload_items(payload))


def _snapshot_nav_date(db, fund_code):
    basic = db.query(FundBasicInfo).filter(FundBasicInfo.fund_code == fund_code).first()
    if not basic:
        return None
    basic_info = _json_loads(basic.basic_json, {})
    return basic_info.get('net_worth_date') or basic_info.get('navDate')


def _nav_cache_is_fresh(db, fund_code):
    latest_cached = _latest_cached_nav_date(db, fund_code)
    latest_snapshot = _snapshot_nav_date(db, fund_code)
    if not latest_cached:
        return False
    if latest_snapshot and latest_cached < str(latest_snapshot):
        return False
    return True


def _sync_nav_history_ifund_style(db, fund_code, force=False):
    fund_code = _normalize_fund_code(fund_code)
    if not fund_code:
        return 0
    if not force and _nav_cache_is_fresh(db, fund_code):
        return 0
    latest_cached = _latest_cached_nav_date(db, fund_code)
    # force=True 时从头全量拉取（用于初始填充），否则增量拉取
    if force:
        start_date = None
    elif latest_cached:
        try:
            start_date = (datetime.strptime(latest_cached, '%Y-%m-%d') + timedelta(days=1)).strftime('%Y-%m-%d')
        except ValueError:
            start_date = None
    else:
        start_date = None
    payload = get_data_service_client().get_fund_nav_history(fund_code, start_date=start_date)
    inserted = _upsert_nav_history_rows(db, fund_code, payload)
    db.commit()
    return inserted


def update_single_fund_risk_metrics(fund_code, db):
    fund_code = _normalize_fund_code(fund_code)
    try:
        payload = get_data_service_client().get_fund_nav_history(fund_code)
        net_worth_trend = _nav_history_to_risk_input(payload)
        if len(net_worth_trend) < 30:
            print(f"[补充风险] 跳过 {fund_code}: 净值不足({len(net_worth_trend)}条)", flush=True)
            return False
        risk_metrics = calculate_risk_metrics(net_worth_trend)
        if not risk_metrics:
            print(f"[补充风险] 跳过 {fund_code}: 风险计算失败(可能数据异常或基金太新)", flush=True)
            return False
        _save_risk_metrics(db, fund_code, risk_metrics)
        _refresh_fund_industry_tag(db, fund_code, fetch_holdings_if_missing=True)
        db.commit()
        return True
    except Exception as e:
        db.rollback()
        print(f"[补充风险] 异常 {fund_code}: {e}", flush=True)
        return False


def _select_nav_candidates(db):
    """选择需要更新风险指标的基金：缺失风险数据或数据过期（超过7天）。"""
    cutoff_date = datetime.now() - timedelta(days=7)

    basic_rows = db.query(
        FundBasicInfo.fund_code, FundBasicInfo.fund_name, FundBasicInfo.fund_type
    ).all()

    risk_rows = db.query(
        FundRiskMetrics.fund_code, FundRiskMetrics.sharpe_ratio_1y, FundRiskMetrics.updated_time
    ).all()
    risk_map = {r.fund_code: (r.sharpe_ratio_1y, r.updated_time) for r in risk_rows}

    candidates = []
    for basic in basic_rows:
        code = basic.fund_code
        if code not in risk_map:
            # 无风险指标记录
            candidates.append({
                'code': code,
                'name': basic.fund_name,
                'type': basic.fund_type or '',
            })
        else:
            sharpe, updated = risk_map[code]
            if sharpe is None:
                # 有记录但缺失关键指标（夏普为空）
                candidates.append({
                    'code': code,
                    'name': basic.fund_name,
                    'type': basic.fund_type or '',
                })
            elif updated is None or updated < cutoff_date:
                # 数据过期（超过7天未更新）
                candidates.append({
                    'code': code,
                    'name': basic.fund_name,
                    'type': basic.fund_type or '',
                })

    candidates.sort(key=lambda item: item['code'])
    print(f"[筛查更新] 风险指标候选: {len(candidates)}只 (缺风险或过期>7天)", flush=True)
    return candidates


def _fund_codes_needing_industry_refresh(db, candidate_codes=None, force=False):
    query = db.query(FundBasicInfo.fund_code)
    if candidate_codes is not None:
        codes = [_normalize_fund_code(code) for code in candidate_codes if _normalize_fund_code(code)]
        if not codes:
            return []
        query = query.filter(FundBasicInfo.fund_code.in_(codes))

    basic_codes = [row[0] for row in query.all() if row[0]]
    if force:
        return basic_codes

    tag_rows = db.query(FundIndustryTag).filter(
        FundIndustryTag.fund_code.in_(basic_codes)
    ).all() if basic_codes else []
    tag_map = {row.fund_code: row for row in tag_rows}

    result = []
    for code in basic_codes:
        tag = tag_map.get(code)
        if not tag:
            result.append(code)
            continue
        if not tag.industry_tag:
            result.append(code)
            continue
        if tag.basis == 'mixed' and tag.source == 'top_stock_holdings' and (tag.industry_count or 0) <= 0:
            result.append(code)
    return result


def _collect_stock_codes_from_portfolios(db, fund_codes=None):
    query = db.query(FundPortfolio)
    if fund_codes is not None:
        codes = [_normalize_fund_code(code) for code in fund_codes if _normalize_fund_code(code)]
        if not codes:
            return []
        query = query.filter(FundPortfolio.fund_code.in_(codes))

    stock_codes = []
    for portfolio in query.all():
        raw_holdings = _json_loads(portfolio.stock_codes_new_json, []) or _json_loads(portfolio.stock_codes_json, [])
        for item in _portfolio_holding_items(raw_holdings):
            code = _normalize_stock_code(item.get('code'))
            if code and code not in stock_codes:
                stock_codes.append(code)
    return stock_codes


def warm_stock_industry_dictionary(db, fund_codes=None, force=False, limit=None):
    stock_codes = _collect_stock_codes_from_portfolios(db, fund_codes)
    if limit is not None:
        try:
            limit = max(1, int(limit))
            stock_codes = stock_codes[:limit]
        except (TypeError, ValueError):
            pass

    if not stock_codes:
        return {'total': 0, 'cached': 0, 'fetched': 0, 'failed': 0}

    records = db.query(StockIndustry).filter(StockIndustry.stock_code.in_(stock_codes)).all()
    cached_codes = {
        record.stock_code
        for record in records
        if record.industry and not force
    }
    missing_codes = [code for code in stock_codes if force or code not in cached_codes]
    if not missing_codes:
        return {'total': len(stock_codes), 'cached': len(cached_codes), 'fetched': 0, 'failed': 0}

    fetched_map, failed_codes = _fetch_stock_industry_batch(
        db,
        missing_codes,
        force_refresh=force,
        timeout=8.0 if force else 5.0,
    )
    db.commit()
    return {
        'total': len(stock_codes),
        'cached': len(cached_codes),
        'fetched': len(fetched_map),
        'failed': len(failed_codes),
    }


def _akshare_row_value(row, fallback_index, *names):
    for name in names:
        try:
            value = row.get(name)
            if value is not None:
                return value
        except Exception:
            pass
    try:
        return row.iloc[fallback_index]
    except Exception:
        return None


def build_stock_industry_dictionary_from_akshare(db, force=False, board_limit=None, stock_limit=None):
    try:
        import akshare as ak
    except Exception as exc:
        raise RuntimeError(f"akshare not available: {exc}")

    try:
        board_limit = int(board_limit) if board_limit else None
    except (TypeError, ValueError):
        board_limit = None
    try:
        stock_limit = int(stock_limit) if stock_limit else None
    except (TypeError, ValueError):
        stock_limit = None

    hk_result = build_hk_stock_industry_dictionary_from_eastmoney(db, force=force)

    try:
        result = _build_stock_industry_dictionary_from_sina(
            db,
            ak,
            force=force,
            board_limit=board_limit,
            stock_limit=stock_limit,
        )
        result['source'] = 'akshare.sina.sector'
        result['hk'] = hk_result
        return result
    except Exception as exc:
        print(f"akshare sina industry dictionary failed, fallback to eastmoney: {exc}", flush=True)
        board_df = ak.stock_board_industry_name_em()
        result = _build_stock_industry_dictionary_from_em_boards(
            db,
            ak,
            board_df,
            force=force,
            board_limit=board_limit,
            stock_limit=stock_limit,
        )
        result['source'] = 'akshare.eastmoney.industry_board'
        result['hk'] = hk_result
        return result


def build_hk_stock_industry_dictionary_from_eastmoney(db, force=False, page_size=500):
    url = 'https://datacenter.eastmoney.com/securities/api/data/v1/get'
    columns = (
        'SECUCODE,SECURITY_CODE,ORG_NAME,ORG_EN_ABBR,BELONG_INDUSTRY,REG_PLACE'
    )
    updated = 0
    skipped = 0
    fetched = 0
    failed_pages = 0
    page = 1
    total_pages = None

    while total_pages is None or page <= total_pages:
        params = {
            'reportName': 'RPT_HKF10_INFO_ORGPROFILE',
            'columns': columns,
            'quoteColumns': '',
            'pageNumber': str(page),
            'pageSize': str(page_size),
            'sortTypes': '',
            'sortColumns': '',
            'source': 'F10',
            'client': 'PC',
            'v': str(int(time.time() * 1000)),
        }
        try:
            response = requests.get(url, params=params, timeout=12)
            response.raise_for_status()
            payload = response.json()
            result = payload.get('result') if isinstance(payload, dict) else {}
            rows = result.get('data') if isinstance(result, dict) else []
            if total_pages is None:
                total_pages = int(result.get('pages') or 0) if isinstance(result, dict) else 0
            if not rows:
                break
        except Exception as exc:
            failed_pages += 1
            print(f"eastmoney hk industry dictionary: failed page {page}: {exc}", flush=True)
            break

        for row in rows:
            if not isinstance(row, dict):
                continue
            stock_code = _normalize_stock_code(row.get('SECURITY_CODE'))
            if not stock_code or not re.match(r'^\d{5}$', stock_code):
                continue
            industry = str(row.get('BELONG_INDUSTRY') or '').strip()
            if not industry:
                continue
            fetched += 1

            record = db.query(StockIndustry).filter(StockIndustry.stock_code == stock_code).first()
            if not record:
                record = StockIndustry(stock_code=stock_code)
                db.add(record)
            elif record.industry and not force:
                skipped += 1
                continue

            record.stock_name = str(row.get('ORG_NAME') or record.stock_name or '')
            record.industry = industry
            record.region = str(row.get('REG_PLACE') or record.region or '')
            concepts = _json_loads(record.concepts_json, [])
            if industry not in concepts:
                concepts.append(industry)
            record.concepts_json = _json_dumps(concepts[:20])
            record.source = 'eastmoney.hk.company_profile'
            record.updated_time = datetime.now()
            updated += 1

        if page % 5 == 0:
            db.commit()
        page += 1

    db.commit()
    return {
        'fetched': fetched,
        'created_or_updated': updated,
        'skipped': skipped,
        'failed_pages': failed_pages,
        'pages': total_pages or 0,
    }


def _build_stock_industry_dictionary_from_em_boards(db, ak, board_df, force=False, board_limit=None, stock_limit=None):
    if board_df is None or getattr(board_df, 'empty', True):
        return {'boards': 0, 'stocks': 0, 'created_or_updated': 0, 'skipped': 0, 'failed_boards': 0}

    boards = []
    for _, row in board_df.iterrows():
        name = _akshare_row_value(row, 1, '板块名称', '行业名称', '名称', 'name')
        code = _akshare_row_value(row, 0, '板块代码', '代码', 'code')
        if name:
            boards.append({'name': str(name), 'code': str(code or '')})
    if board_limit:
        boards = boards[:board_limit]

    updated = 0
    skipped = 0
    seen_stocks = set()
    failed_boards = 0
    for index, board in enumerate(boards, 1):
        if stock_limit and len(seen_stocks) >= stock_limit:
            break
        try:
            cons_df = ak.stock_board_industry_cons_em(symbol=board['name'])
        except Exception as exc:
            failed_boards += 1
            print(f"akshare stock industry dictionary: failed board {board['name']}: {exc}", flush=True)
            continue
        if cons_df is None or getattr(cons_df, 'empty', True):
            continue

        for _, stock_row in cons_df.iterrows():
            if stock_limit and len(seen_stocks) >= stock_limit:
                break
            stock_code = _normalize_stock_code(_akshare_row_value(stock_row, 1, '代码', '股票代码', 'code'))
            if not stock_code or not re.match(r'^\d{6}$', stock_code):
                continue
            stock_name = _akshare_row_value(stock_row, 2, '名称', '股票名称', 'name')
            if stock_code in seen_stocks:
                continue
            seen_stocks.add(stock_code)

            record = db.query(StockIndustry).filter(StockIndustry.stock_code == stock_code).first()
            if not record:
                record = StockIndustry(stock_code=stock_code)
                db.add(record)
            elif record.industry and not force:
                skipped += 1
                continue

            record.stock_name = str(stock_name or record.stock_name or '')
            record.industry = board['name']
            record.region = record.region or ''
            concepts = _json_loads(record.concepts_json, [])
            if board['name'] not in concepts:
                concepts.append(board['name'])
            record.concepts_json = _json_dumps(concepts[:20])
            record.source = 'akshare.eastmoney.industry_board'
            record.updated_time = datetime.now()
            updated += 1

        if index % 10 == 0:
            db.commit()

    db.commit()
    return {
        'boards': len(boards),
        'stocks': len(seen_stocks),
        'created_or_updated': updated,
        'skipped': skipped,
        'failed_boards': failed_boards,
    }


def _build_stock_industry_dictionary_from_sina(db, ak, force=False, board_limit=None, stock_limit=None):
    board_df = ak.stock_sector_spot()
    if board_df is None or getattr(board_df, 'empty', True):
        return {'boards': 0, 'stocks': 0, 'created_or_updated': 0, 'skipped': 0, 'failed_boards': 0}

    boards = []
    for _, row in board_df.iterrows():
        label = row.get('label') if hasattr(row, 'get') else None
        name = _akshare_row_value(row, 1, '行业', '板块', 'name')
        if label and name:
            boards.append({'label': str(label), 'name': str(name)})
    if board_limit:
        boards = boards[:board_limit]

    updated = 0
    skipped = 0
    seen_stocks = set()
    failed_boards = 0
    for index, board in enumerate(boards, 1):
        if stock_limit and len(seen_stocks) >= stock_limit:
            break
        try:
            cons_df = ak.stock_sector_detail(sector=board['label'])
        except Exception as exc:
            failed_boards += 1
            print(f"akshare sina industry dictionary: failed board {board['name']}: {exc}", flush=True)
            continue
        if cons_df is None or getattr(cons_df, 'empty', True):
            continue

        for _, stock_row in cons_df.iterrows():
            if stock_limit and len(seen_stocks) >= stock_limit:
                break
            stock_code = _normalize_stock_code(stock_row.get('code') if hasattr(stock_row, 'get') else None)
            if not stock_code or not re.match(r'^\d{6}$', stock_code):
                continue
            stock_name = stock_row.get('name') if hasattr(stock_row, 'get') else None
            if stock_code in seen_stocks:
                continue
            seen_stocks.add(stock_code)

            record = db.query(StockIndustry).filter(StockIndustry.stock_code == stock_code).first()
            if not record:
                record = StockIndustry(stock_code=stock_code)
                db.add(record)
            elif record.industry and not force:
                skipped += 1
                continue

            record.stock_name = str(stock_name or record.stock_name or '')
            record.industry = board['name']
            record.region = record.region or ''
            concepts = _json_loads(record.concepts_json, [])
            if board['name'] not in concepts:
                concepts.append(board['name'])
            record.concepts_json = _json_dumps(concepts[:20])
            record.source = 'akshare.sina.sector'
            record.updated_time = datetime.now()
            updated += 1

        if index % 10 == 0:
            db.commit()

    db.commit()
    return {
        'boards': len(boards),
        'stocks': len(seen_stocks),
        'created_or_updated': updated,
        'skipped': skipped,
        'failed_boards': failed_boards,
    }


def batch_refresh_fund_industry_tags(
    db,
    fund_codes=None,
    task_id=None,
    force=False,
    limit=None,
    build_full_dictionary=False,
    allow_missing_stock_network=False,
):
    codes = _fund_codes_needing_industry_refresh(db, fund_codes, force=force)
    if limit is not None:
        try:
            limit = max(1, int(limit))
            codes = codes[:limit]
        except (TypeError, ValueError):
            pass

    total = len(codes)
    if total == 0:
        return {'success_count': 0, 'fail_count': 0, 'total': 0}

    full_dictionary_result = None
    full_dictionary_ok = False
    if build_full_dictionary:
        _set_screening_progress(
            db,
            task_id,
            message='构建全市场股票行业字典...',
            target_count=total,
            current_count=0,
            success_count=0,
            fail_count=0,
            current_item='',
        )
        try:
            full_dictionary_result = build_stock_industry_dictionary_from_akshare(db)
            full_dictionary_ok = True
            print(f"[stock industry dictionary] akshare full build: {full_dictionary_result}", flush=True)
        except Exception as exc:
            print(f"[stock industry dictionary] akshare full build failed: {exc}", flush=True)

    _set_screening_progress(
        db,
        task_id,
        message='读取本地股票行业字典...',
        target_count=total,
        current_count=0,
        success_count=0,
        fail_count=0,
        current_item='',
    )
    dictionary_result = {
        'portfolio_stock_total': len(_collect_stock_codes_from_portfolios(db, codes)),
        'mapped_stock_total': db.query(StockIndustry).filter(
            StockIndustry.industry.isnot(None),
            StockIndustry.industry != '',
        ).count(),
    }
    print(f"[industry dictionary] local lookup: {dictionary_result}", flush=True)

    if allow_missing_stock_network and (not build_full_dictionary or full_dictionary_ok):
        portfolio_stock_codes = _collect_stock_codes_from_portfolios(db, codes)
        mapped_rows = db.query(StockIndustry).filter(
            StockIndustry.stock_code.in_(portfolio_stock_codes),
            StockIndustry.industry.isnot(None),
            StockIndustry.industry != '',
        ).all() if portfolio_stock_codes else []
        mapped_codes = {row.stock_code for row in mapped_rows}
        missing_stock_codes = [code for code in portfolio_stock_codes if code not in mapped_codes]
        missing_a_share_codes = [code for code in missing_stock_codes if _is_a_share_stock_code(code)]
        skipped_non_a_share = len(missing_stock_codes) - len(missing_a_share_codes)
        if missing_a_share_codes:
            _set_screening_progress(
                db,
                task_id,
                message=f'批量补充缺失A股行业({len(missing_a_share_codes)}只)...',
                target_count=total,
                current_count=0,
                success_count=0,
                fail_count=0,
                current_item='',
            )
            fetched_map, failed_codes = _fetch_stock_industry_batch(
                db,
                missing_a_share_codes,
                force_refresh=False,
                timeout=5.0,
            )
            db.commit()
            print(
                f"[industry dictionary] missing A-share stocks fetched: "
                f"{len(fetched_map)}/{len(missing_a_share_codes)} "
                f"(failed {len(failed_codes)}, skipped non-A-share {skipped_non_a_share})",
                flush=True,
            )
        elif missing_stock_codes:
            print(
                f"[industry dictionary] skipped {skipped_non_a_share} non-A-share/unrecognized holding codes "
                "for stock industry lookup",
                flush=True,
            )
    elif allow_missing_stock_network and build_full_dictionary and not full_dictionary_ok:
        print(
            "[industry dictionary] skip missing stock network fetch because akshare full build failed; "
            "using local stock_industry only",
            flush=True,
        )

    success = 0
    fail = 0
    _set_screening_progress(
        db,
        task_id,
        message='刷新基金行业标签...',
        target_count=total,
        current_count=0,
        success_count=0,
        fail_count=0,
        current_item='',
    )

    for index, code in enumerate(codes, 1):
        if screening_stop_flag:
            break

        _set_screening_progress(
            db,
            task_id,
            current_count=index,
            current_item=code,
        )
        try:
            tag = _refresh_fund_industry_tag(
                db,
                code,
                fetch_holdings_if_missing=True,
                force_stock_refresh=False,
                allow_stock_network=False,
            )
            if tag:
                success += 1
            else:
                fail += 1
        except Exception as exc:
            print(f"batch industry tag refresh failed for {code}: {exc}", flush=True)
            fail += 1

        if index % 20 == 0:
            db.commit()
            _set_screening_progress(
                db,
                task_id,
                success_count=success,
                fail_count=fail,
            )

    db.commit()
    _set_screening_progress(
        db,
        task_id,
        success_count=success,
        fail_count=fail,
        current_item='',
    )
    return {'success_count': success, 'fail_count': fail, 'total': total}


def _create_data_fetch_task(db, task_type, options=None, message=''):
    task = DataFetchTask(
        task_type=task_type,
        status='running',
        target_count=0,
        current_count=0,
        success_count=0,
        fail_count=0,
        current_item='',
        message=message,
        options_json=_json_dumps(options or {}),
        started_time=datetime.now(),
        updated_time=datetime.now(),
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


def _latest_active_task(db):
    """返回最新的运行中或最近完成的任务（不限类型）"""
    return db.query(DataFetchTask).filter(
        DataFetchTask.task_type.in_(['screening_update', 'fill_risk'])
    ).order_by(desc(DataFetchTask.started_time), desc(DataFetchTask.id)).first()


def _is_active_task(task):
    """检查任务是否仍在活跃运行（超过1小时未更新视为僵死）"""
    if not task or task.status != 'running':
        return False
    reference_time = task.updated_time or task.started_time
    if reference_time and datetime.now() - reference_time > timedelta(hours=1):
        task.status = 'failed'
        task.error_message = '任务超过 6 小时未更新，已标记为陈旧任务'
        task.message = task.error_message
        task.finished_time = datetime.now()
        task.updated_time = datetime.now()
        return False
    return True


def _task_to_update_status(task):
    if not task:
        return {
            'running': screening_update_status.get('running', False),
            'progress': screening_update_status.get('progress', 0),
            'total': screening_update_status.get('total', 0),
            'current_fund': screening_update_status.get('current_fund', ''),
            'success_count': screening_update_status.get('success_count', 0),
            'fail_count': screening_update_status.get('fail_count', 0),
            'message': screening_update_status.get('message', ''),
        }
    result = {
        'task_id': task.id,
        'running': task.status == 'running',
        'progress': task.current_count or 0,
        'total': task.target_count or 0,
        'current_fund': task.current_item or '',
        'success_count': task.success_count or 0,
        'fail_count': task.fail_count or 0,
        'message': task.message or '',
        'status': task.status,
        'started_time': task.started_time.isoformat() if task.started_time else None,
        'finished_time': task.finished_time.isoformat() if task.finished_time else None,
    }
    # 对于运行中的任务，用内存状态补充 DB 中可能滞后的字段
    if task.status == 'running':
        mem_mapping = [
            ('progress', 'progress'),
            ('total', 'total'),
            ('current_fund', 'current_fund'),
            ('success_count', 'success_count'),
            ('fail_count', 'fail_count'),
            ('message', 'message'),
        ]
        for mem_key, result_key in mem_mapping:
            mem_val = screening_update_status.get(mem_key)
            if mem_val is not None and mem_val != '':
                result[result_key] = mem_val
    return result


def _set_screening_progress(db=None, task_id=None, **fields):
    key_map = {
        'current_count': 'progress',
        'target_count': 'total',
        'current_item': 'current_fund',
    }
    for key, value in fields.items():
        memory_key = key_map.get(key, key)
        if memory_key in screening_update_status:
            screening_update_status[memory_key] = value

    if not db or not task_id:
        return

    task = db.query(DataFetchTask).filter(DataFetchTask.id == task_id).first()
    if not task:
        return

    for key, value in fields.items():
        if hasattr(task, key):
            setattr(task, key, value)
    if fields.get('status') in ('finished', 'failed', 'stopped'):
        task.finished_time = datetime.now()
    task.updated_time = datetime.now()
    db.commit()


screening_update_status = {
    'running': False,
    'progress': 0,
    'total': 0,
    'current_fund': '',
    'success_count': 0,
    'fail_count': 0,
    'start_time': None,
    'message': ''
}


def calculate_calmar_ratio(annual_return, max_drawdown):
    """计算卡玛比率"""
    if max_drawdown is None or max_drawdown == 0 or annual_return is None:
        return None
    return round(annual_return / max_drawdown, 2)


def check_4433_rule(rank_1y, rank_2y, rank_3y, rank_5y, rank_6m, rank_3m):
    """
    检查是否符合4433法则
    4433法则：
    - 近1年、2年、3年、5年排名在同类前1/4 (25%)
    - 近6个月、3个月排名在前1/3 (33.33%)
    注意：这里的排名应该是同类型基金中的排名百分位
    """
    # 长期排名：1年必须有，2年3年至少有一个
    if rank_1y is None or rank_1y > 25:
        return False
    
    # 检查2年和3年排名（如果有数据）
    long_term_available = [r for r in [rank_2y, rank_3y] if r is not None]
    if long_term_available:
        for rank in long_term_available:
            if rank > 25:
                return False
    
    # 如果有5年数据，也需要在前25%
    if rank_5y is not None and rank_5y > 25:
        return False
    
    # 短期排名：6个月和3个月都必须在前33.33%
    if rank_6m is None or rank_6m > 33.33:
        return False
    if rank_3m is None or rank_3m > 33.33:
        return False
    
    return True


# ==================== 基金筛选功能 ====================

def check_4433_rule(rank_1y, rank_2y, rank_3y, rank_5y, rank_6m, rank_3m):
    """
    检查是否符合4433法则
    4433法则：
    - 近1年、2年、3年排名在同类前1/4 (25%)
    - 近6个月、3个月排名在前1/3 (33.33%)
    """
    # 长期排名：1年必须有，2年3年至少有一个
    if rank_1y is None or rank_1y > 25:
        return False
    
    # 检查2年和3年排名（如果有数据）
    long_term_available = [r for r in [rank_2y, rank_3y] if r is not None]
    if long_term_available:
        for rank in long_term_available:
            if rank > 25:
                return False
    
    # 如果有5年数据，也需要在前25%
    if rank_5y is not None and rank_5y > 25:
        return False
    
    # 短期排名：6个月和3个月都必须在前33.33%
    if rank_6m is None or rank_6m > 33.33:
        return False
    if rank_3m is None or rank_3m > 33.33:
        return False
    
    return True


def calculate_same_type_rankings(db):
    """
    计算同类型基金的排名百分位
    基于 FundBasicInfo 中的收益数据计算排名，保存到 FundScreeningRank
    """
    # 获取所有有效的基金类型
    fund_types = db.query(FundBasicInfo.fund_type).filter(
        FundBasicInfo.fund_type.isnot(None),
        FundBasicInfo.fund_type != ''
    ).distinct().all()
    
    fund_types = [ft[0] for ft in fund_types]
    print(f"[同类排名] 发现 {len(fund_types)} 种基金类型")
    
    for fund_type in fund_types:
        # 获取该类型的所有基金（包含业绩数据）
        funds = db.query(FundBasicInfo).filter(
            FundBasicInfo.fund_type == fund_type,
            FundBasicInfo.performance_json.isnot(None)
        ).all()
        
        if len(funds) < 2:
            continue
        
        print(f"[同类排名] 处理 {fund_type}: {len(funds)} 只基金")
        
        # 解析业绩数据
        fund_performances = []
        for fund in funds:
            perf = _json_loads(fund.performance_json, {})
            fund_performances.append({
                'fund_code': fund.fund_code,
                'return_1m': perf.get('1_month_return'),
                'return_3m': perf.get('3_month_return'),
                'return_6m': perf.get('6_month_return'),
                'return_1y': perf.get('1_year_return'),
                'return_2y': perf.get('2_year_return'),
                'return_3y': perf.get('3_year_return'),
            })
        
        # 为每个时间段计算排名
        periods = [
            ('return_1m', 'rank_pct_1m'),
            ('return_3m', 'rank_pct_3m'),
            ('return_6m', 'rank_pct_6m'),
            ('return_1y', 'rank_pct_1y'),
            ('return_2y', 'rank_pct_2y'),
            ('return_3y', 'rank_pct_3y'),
        ]
        
        # 为每只基金创建或更新排名记录
        fund_ranks = {}  # fund_code -> rank data
        for fp in fund_performances:
            fund_ranks[fp['fund_code']] = {}
        
        for return_field, rank_field in periods:
            # 获取有该时段收益数据的基金
            # 重要：排除收益率为 0、"0.00"、None 的基金
            # 这些通常是成立时间不足导致的缺失数据，而非真实的0%收益
            def is_valid_return(val):
                if val is None:
                    return False
                try:
                    num_val = float(val)
                    # 真实0%收益极其罕见，0值通常表示数据缺失
                    # 允许一个很小的误差范围（如 -0.01% ~ 0.01%）视为可能的真实数据
                    if abs(num_val) < 0.01:
                        return False
                    return True
                except (ValueError, TypeError):
                    return False
            
            funds_with_data = [(fp['fund_code'], float(fp[return_field])) for fp in fund_performances 
                               if is_valid_return(fp[return_field])]
            
            if len(funds_with_data) < 2:
                continue
            
            # 按收益率降序排序（收益高排名靠前）
            funds_with_data.sort(key=lambda x: x[1], reverse=True)
            total = len(funds_with_data)
            
            # 计算每只基金的排名百分位
            for rank_idx, (fund_code, _) in enumerate(funds_with_data, 1):
                rank_pct = round((rank_idx / total) * 100, 2)
                fund_ranks[fund_code][rank_field] = rank_pct
        
        # 更新数据库
        for fund_code, ranks in fund_ranks.items():
            rank_record = db.query(FundScreeningRank).filter(
                FundScreeningRank.fund_code == fund_code
            ).first()
            
            if not rank_record:
                rank_record = FundScreeningRank(fund_code=fund_code)
                db.add(rank_record)
            
            for field, value in ranks.items():
                setattr(rank_record, field, value)
            
            # 计算4433法则
            pass_4433 = check_4433_rule(
                ranks.get('rank_pct_1y'),
                ranks.get('rank_pct_2y'),
                ranks.get('rank_pct_3y'),
                None,
                ranks.get('rank_pct_6m'),
                ranks.get('rank_pct_3m')
            )
            rank_record.pass_4433 = 1 if pass_4433 else 0
            rank_record.updated_time = datetime.now()
    
    db.commit()
    print("[同类排名] 同类型排名计算完成")


# 全局变量：批量更新状态
screening_update_status = {
    'running': False,
    'progress': 0,
    'total': 0,
    'current_fund': '',
    'success_count': 0,
    'fail_count': 0,
    'start_time': None,
    'message': ''
}
screening_stop_flag = False


def batch_update_fund_data(
    fund_types=None,
    limit=None,
    mode='sync_nav',
    task_id=None,
    industry_limit=None,
    tasks=None,
    build_industry_dictionary=True,
):
    """Run the ifund-style screening refresh: local snapshot first, optional candidate NAV sync."""
    global screening_update_status, screening_stop_flag

    tasks = tasks or {}
    update_basic = bool(tasks.get('basic', True))
    calculate_rankings_task = bool(tasks.get('rankings', update_basic))
    calculate_risk_task = bool(tasks.get('risk', mode != 'sync_only'))
    refresh_industry_task = bool(tasks.get('industry', True))
    rebuild_industry_performance_task = bool(tasks.get('industry_performance', refresh_industry_task))

    db = SessionLocal()
    t0 = time.time()
    print(f"[筛查更新] ========== 开始批量更新 ({mode}) ==========", flush=True)
    try:
        if not task_id:
            task = _create_data_fetch_task(
                db,
                'screening_update',
                {
                    'fund_types': fund_types or [],
                    'limit': limit,
                    'mode': mode,
                    'industry_limit': industry_limit,
                    'build_industry_dictionary': build_industry_dictionary,
                    'tasks': tasks,
                },
                message='获取批量排行...',
            )
            task_id = task.id

        if mode == 'sync_only':
            mode = 'sync_only'
        else:
            mode = 'sync_nav'
        screening_stop_flag = False
        screening_update_status.update({
            'running': True,
            'progress': 0,
            'total': 0,
            'current_fund': '',
            'success_count': 0,
            'fail_count': 0,
            'start_time': datetime.now(),
            'message': '获取批量排行...',
        })
        _set_screening_progress(db, task_id, status='running', message='获取批量排行...')

        if update_basic:
            fund_list = _fetch_screening_snapshot_items(limit=limit, db=db, task_id=task_id)
        else:
            query = db.query(FundBasicInfo)
            if limit:
                try:
                    query = query.limit(max(1, int(limit)))
                except (TypeError, ValueError):
                    pass
            fund_list = [
                {'code': item.fund_code, 'name': item.fund_name, 'type': item.fund_type}
                for item in query.all()
            ]
        t_lookup_start = time.time()
        type_lookup = _fund_type_lookup()
        print(f"[筛查更新] 类型查找表构建: {len(type_lookup)}条 ({time.time()-t_lookup_start:.1f}s)", flush=True)

        if fund_types:
            before_filter = len(fund_list)
            fund_list = [
                item for item in fund_list
                if _matches_fund_type(
                    item.get('type') or type_lookup.get(_normalize_fund_code(item.get('code')), {}).get('type'),
                    fund_types,
                )
            ]
            print(f"[筛查更新] 类型筛选: {before_filter} → {len(fund_list)}只 (筛选条件: {fund_types})", flush=True)

        success_count = 0
        fail_count = 0
        total_to_save = len(fund_list)
        if update_basic:
            print(f"[筛查更新] 开始写入基础数据: {total_to_save}只...", flush=True)
            _set_screening_progress(
                db,
                task_id,
                message='写入基础数据...',
                target_count=total_to_save,
                current_count=0,
                success_count=0,
                fail_count=0,
                current_item='',
            )

            for index, fund in enumerate(fund_list, 1):
                if screening_stop_flag:
                    _set_screening_progress(
                        db,
                        task_id,
                        status='stopped',
                        message=f"已手动停止。成功: {success_count}, 失败: {fail_count}",
                        current_count=index - 1,
                        success_count=success_count,
                        fail_count=fail_count,
                    )
                    return {'success': False, 'stopped': True}

                fund_code = _normalize_fund_code(fund.get('code', ''))
                if _save_screening_snapshot_item(db, fund, type_lookup):
                    success_count += 1
                else:
                    fail_count += 1

                screening_update_status.update({
                    'progress': index,
                    'current_fund': f"{fund_code} - {fund.get('name', '')}",
                    'success_count': success_count,
                    'fail_count': fail_count,
                })

                db.commit()
                _set_screening_progress(
                    db,
                    task_id,
                    current_count=index,
                    current_item=f"{fund_code} - {fund.get('name', '')}",
                    success_count=success_count,
                    fail_count=fail_count,
                )

                if index % 500 == 0:
                    print(f"[筛查更新] 写入进度: {index}/{total_to_save} (成功{success_count}, 失败{fail_count})", flush=True)

            db.commit()
            print(f"[筛查更新] 基础数据写入完成: 成功{success_count}, 失败{fail_count}, 耗时{time.time()-t0:.1f}s", flush=True)
        else:
            _set_screening_progress(db, task_id, message='跳过基础数据更新...', target_count=total_to_save, current_count=0)

        if not screening_stop_flag and calculate_rankings_task:
            print(f"[筛查更新] 开始计算同类排名...", flush=True)
            _set_screening_progress(db, task_id, message='计算同类排名...', current_item='')
            calculate_same_type_rankings(db)
            print(f"[筛查更新] 同类排名计算完成", flush=True)

        if not screening_stop_flag and calculate_risk_task:
            candidates = _select_nav_candidates(db)
            nav_success = 0
            nav_fail = 0
            _set_screening_progress(
                db,
                task_id,
                message='补齐候选净值并计算风险指标...',
                target_count=len(candidates),
                current_count=0,
                success_count=0,
                fail_count=0,
                current_item='',
            )

            for index, fund in enumerate(candidates, 1):
                if screening_stop_flag:
                    _set_screening_progress(
                        db,
                        task_id,
                        status='stopped',
                        message=f"已手动停止。净值同步成功: {nav_success}, 失败: {nav_fail}",
                        current_count=index - 1,
                        success_count=nav_success,
                        fail_count=nav_fail,
                    )
                    return {'success': False, 'stopped': True}

                current_item = f"{fund['code']} - {fund.get('name', '')}"
                _set_screening_progress(
                    db,
                    task_id,
                    current_count=index,
                    current_item=current_item,
                )
                if update_single_fund_risk_metrics(fund['code'], db):
                    nav_success += 1
                else:
                    nav_fail += 1
                _set_screening_progress(
                    db,
                    task_id,
                    success_count=nav_success,
                    fail_count=nav_fail,
                )

            success_count = nav_success
            fail_count = nav_fail

        if not screening_stop_flag and refresh_industry_task:
            industry_codes = [
                _normalize_fund_code(item.get('code'))
                for item in fund_list
                if _normalize_fund_code(item.get('code'))
            ]
            industry_result = batch_refresh_fund_industry_tags(
                db,
                fund_codes=industry_codes,
                task_id=task_id,
                force=True,
                limit=industry_limit,
                build_full_dictionary=build_industry_dictionary,
                allow_missing_stock_network=True,
            )
            print(
                f"[screening update] industry tags refreshed: "
                f"{industry_result['success_count']}/{industry_result['total']} "
                f"(fail {industry_result['fail_count']})",
                flush=True,
            )
            success_count = industry_result['success_count']
            fail_count = industry_result['fail_count']
            _set_screening_progress(
                db,
                task_id,
                target_count=industry_result['total'],
                current_count=industry_result['total'],
                success_count=success_count,
                fail_count=fail_count,
                current_item='',
            )

        if not screening_stop_flag and rebuild_industry_performance_task:
            _set_screening_progress(db, task_id, message='汇总行业表现...', current_item='')
            rebuild_industry_performance_stats(db)
            db.commit()

        message = f"完成。模式: {mode}, 成功: {success_count}, 失败: {fail_count}"
        _set_screening_progress(
            db,
            task_id,
            status='finished',
            message=message,
            success_count=success_count,
            fail_count=fail_count,
            current_item='',
        )
        screening_update_status['running'] = False
        return {
            'success': True,
            'total': screening_update_status['total'],
            'success_count': success_count,
            'fail_count': fail_count,
        }
    except Exception as exc:
        db.rollback()
        message = f"更新失败: {exc}"
        screening_update_status['message'] = message
        screening_update_status['running'] = False
        _set_screening_progress(db, task_id, status='failed', message=message, error_message=str(exc))
        return {'success': False, 'error': str(exc)}
    finally:
        screening_update_status['running'] = False
        db.close()


def batch_fill_risk_metrics(db=None, task_id=None):
    """批量补全缺失的风险指标（回撤/波动率/夏普/卡玛）。
    处理那些已有足够净值历史(>=30条)但尚未计算风险指标的基金。
    作为后台任务运行，支持停止信号。"""
    global screening_update_status, screening_stop_flag

    own_db = None
    if db is None:
        own_db = SessionLocal()
        db = own_db

    try:
        # 查找所有有基本信息但无风险指标或风险数据过期(>7天)的基金
        cutoff_date = datetime.now() - timedelta(days=7)
        all_basic_codes = set()
        for row in db.query(FundBasicInfo.fund_code).all():
            all_basic_codes.add(row[0])

        risk_codes = set()
        stale_risk_codes = set()
        for row in db.query(FundRiskMetrics.fund_code, FundRiskMetrics.sharpe_ratio_1y, FundRiskMetrics.updated_time).all():
            if row.sharpe_ratio_1y is not None:
                risk_codes.add(row.fund_code)
                # 检查是否过期（超过7天未更新）
                if row.updated_time is None or row.updated_time < cutoff_date:
                    stale_risk_codes.add(row.fund_code)

        missing_risk_codes = all_basic_codes - risk_codes
        # 缺失 + 过期
        need_risk_update_codes = missing_risk_codes | stale_risk_codes
        missing_industry_codes = set(_fund_codes_needing_industry_refresh(db, all_basic_codes, force=False))
        missing_codes = sorted(need_risk_update_codes | missing_industry_codes)
        industry_tag_count = db.query(FundIndustryTag.fund_code).count()

        print(f"[补充风险] 待处理: {len(missing_codes)}只 (缺风险:{len(missing_risk_codes)}, 过期风险:{len(stale_risk_codes)}, 缺行业:{len(missing_industry_codes)}, 总计:{len(all_basic_codes)}, 有风险:{len(risk_codes)}, 行业标签:{industry_tag_count})", flush=True)

        if not missing_codes:
            print("[补充风险] 无需补充，所有基金已有风险指标", flush=True)
            rebuild_industry_performance_stats(db)
            db.commit()
            screening_update_status['running'] = False
            return {'success': True, 'total': 0, 'message': '所有基金已有风险指标'}

        screening_update_status.update({
            'running': True,
            'progress': 0,
            'total': len(missing_codes),
            'current_fund': '',
            'success_count': 0,
            'fail_count': 0,
            'message': '补充风险指标...',
        })
        _set_screening_progress(
            db, task_id,
            status='running',
            message='补充风险指标...',
            target_count=len(missing_codes),
            current_count=0,
            success_count=0,
            fail_count=0,
        )

        success = 0
        fail = 0
        for idx, code in enumerate(missing_codes, 1):
            if screening_stop_flag:
                _set_screening_progress(db, task_id, status='stopped',
                    message=f"已停止。成功{success}, 失败{fail}")
                return {'success': False, 'stopped': True}

            current_item = f"{code}"
            screening_update_status.update({
                'progress': idx,
                'current_fund': current_item,
                'success_count': success,
                'fail_count': fail,
            })

            if code in missing_risk_codes or code in stale_risk_codes:
                ok = update_single_fund_risk_metrics(code, db)
            else:
                ok = _refresh_fund_industry_tag(db, code, fetch_holdings_if_missing=True) is not None

            if ok:
                success += 1
            else:
                fail += 1

            db.commit()
            _set_screening_progress(db, task_id,
                current_count=idx, current_item=current_item,
                success_count=success, fail_count=fail)

            if idx % 100 == 0:
                print(f"[补充风险] 进度: {idx}/{len(missing_codes)} (成功{success}, 失败{fail})", flush=True)

        rebuild_industry_performance_stats(db)
        db.commit()

        print(f"[补充风险] 完成: 成功{success}, 失败{fail}", flush=True)
        _set_screening_progress(db, task_id, status='finished',
            message=f"补充完成。成功{success}, 失败{fail}",
            success_count=success, fail_count=fail)
        screening_update_status['running'] = False
        return {'success': True, 'success_count': success, 'fail_count': fail}

    except Exception as e:
        print(f"[补充风险] 失败: {e}", flush=True)
        screening_update_status['running'] = False
        _set_screening_progress(db, task_id, status='failed', message=str(e))
        return {'success': False, 'error': str(e)}
    finally:
        if own_db:
            own_db.close()


@app.route('/api/screening/fill-risk', methods=['POST'])
def start_fill_risk():
    """启动后台补全风险指标任务"""
    global screening_update_status
    if screening_update_status.get('running'):
        return jsonify({'error': '已有更新任务在进行中'}), 409

    def _run():
        db = SessionLocal()
        try:
            task = _create_data_fetch_task(db, 'fill_risk', message='补充风险指标...')
            batch_fill_risk_metrics(db=db, task_id=task.id)
        except Exception as e:
            print(f"[补充风险] 线程异常: {e}", flush=True)
            screening_update_status['running'] = False
        finally:
            db.close()

    import threading
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return jsonify({'message': '补充风险指标任务已启动'})



@app.route('/api/screening/status', methods=['GET'])
def get_screening_status():
    """获取筛选数据库状态"""
    db = get_db()
    
    # 统计各表数量
    basic_count = db.query(FundBasicInfo).count()
    risk_count = db.query(FundRiskMetrics).filter(
        FundRiskMetrics.sharpe_ratio_1y.isnot(None)
    ).count()
    rank_count = db.query(FundScreeningRank).count()
    industry_tag_count = db.query(FundIndustryTag).count()
    nav_history_count = db.query(FundNavHistory).count()
    pass_4433_count = db.query(FundScreeningRank).filter(
        FundScreeningRank.pass_4433 == 1
    ).count()
    
    # 获取最新更新时间
    latest = db.query(FundBasicInfo).order_by(
        desc(FundBasicInfo.updated_time)
    ).first()
    latest_update = latest.updated_time.isoformat() if latest and latest.updated_time else None
    
    # 获取各类型数量
    type_counts = {}
    types = db.query(FundBasicInfo.fund_type, func.count(FundBasicInfo.fund_code)).group_by(
        FundBasicInfo.fund_type
    ).all()
    for t, c in types:
        if t:
            type_counts[t] = c
    
    latest_task = _latest_active_task(db)
    if latest_task and latest_task.status == 'running' and not _is_active_task(latest_task):
        db.commit()

    return jsonify({
        'basic_count': basic_count,
        'risk_metrics_count': risk_count,
        'ranking_count': rank_count,
        'industry_tag_count': industry_tag_count,
        'nav_history_count': nav_history_count,
        'pass_4433_count': pass_4433_count,
        'latest_update': latest_update,
        'type_counts': type_counts,
        'update_status': _task_to_update_status(latest_task)
    })


@app.route('/api/screening/progress', methods=['GET'])
def get_screening_progress():
    """获取更新进度 — 以DB DataFetchTask 为准，内存仅作补充"""
    try:
        db = get_db()
        task = _latest_active_task(db)
    except Exception:
        task = None

    # 先从内存读取
    result = {
        'running': screening_update_status.get('running', False),
        'progress': screening_update_status.get('progress', 0),
        'total': screening_update_status.get('total', 0),
        'current_fund': screening_update_status.get('current_fund', ''),
        'success_count': screening_update_status.get('success_count', 0),
        'fail_count': screening_update_status.get('fail_count', 0),
        'message': screening_update_status.get('message', ''),
    }

    # DB中有运行中任务时，以DB为准覆盖（后台线程每只基金commit一次，DB数据最可靠）
    if task:
        if task.status == 'running':
            result['running'] = True
            # DB值优先（只要DB有数据就用DB的）
            if task.target_count:
                result['total'] = task.target_count
            if task.current_count is not None:
                result['progress'] = task.current_count
            if task.current_item:
                result['current_fund'] = task.current_item
            if task.success_count is not None:
                result['success_count'] = task.success_count
            if task.fail_count is not None:
                result['fail_count'] = task.fail_count
            if task.message:
                result['message'] = task.message
            # 同步内存状态（修复线程异常导致running=false的问题）
            if not screening_update_status.get('running'):
                screening_update_status['running'] = True
        elif task.status in ('finished', 'stopped', 'failed'):
            result['running'] = False
            result['message'] = task.message or result['message']

    return jsonify(result)


@app.route('/api/screening/update', methods=['POST'])
def start_screening_update():
    """启动基金数据批量更新"""
    data = request.get_json() or {}
    fund_types = data.get('fund_types') or []
    limit = data.get('limit')
    mode = data.get('mode') or 'sync_nav'
    industry_limit = data.get('industry_limit')
    build_industry_dictionary = bool(data.get('build_industry_dictionary', True))
    tasks = data.get('tasks') or {}
    db = get_db()
    latest_task = _latest_active_task(db)

    active_task_running = _is_active_task(latest_task)
    if latest_task and latest_task.status != 'running':
        db.commit()

    if screening_update_status['running'] or active_task_running:
        return jsonify({
            'error': '更新任务正在进行中',
            'status': _task_to_update_status(latest_task)
        }), 409

    # 在后台线程执行更新
    task = _create_data_fetch_task(
        db,
        'screening_update',
        {
            'fund_types': fund_types,
            'limit': limit,
            'mode': mode,
            'industry_limit': industry_limit,
            'build_industry_dictionary': build_industry_dictionary,
            'tasks': tasks,
        },
        message='更新任务已启动',
    )

    thread = threading.Thread(
        target=batch_update_fund_data,
        args=(fund_types, limit, mode, task.id, industry_limit, tasks, build_industry_dictionary)
    )
    thread.daemon = True
    thread.start()

    return jsonify({
        'message': '更新任务已启动',
        'fund_types': fund_types,
        'limit': limit,
        'mode': mode,
        'industry_limit': industry_limit,
        'build_industry_dictionary': build_industry_dictionary,
        'tasks': tasks,
        'task_id': task.id
    })


@app.route('/api/screening/stop', methods=['POST'])
def stop_screening_update():
    """停止基金数据更新"""
    global screening_stop_flag, screening_update_status
    screening_stop_flag = True
    # 同时强制重置状态，防止卡死
    screening_update_status['running'] = False
    screening_update_status['message'] = '已手动停止'
    # 更新DB中的最新任务状态
    db = get_db()
    try:
        task = _latest_active_task(db)
        if task and task.status == 'running':
            task.status = 'stopped'
            task.message = '已手动停止'
            task.finished_time = datetime.now()
            task.updated_time = datetime.now()
            db.commit()
    except Exception:
        pass
    return jsonify({'message': '已停止并重置状态'})


@app.route('/api/screening/recalculate-rankings', methods=['POST'])
def recalculate_rankings():
    """重新计算同类型排名和4433法则标记"""
    db = get_db()
    try:
        calculate_same_type_rankings(db)
        
        # 统计结果
        stats = db.query(
            FundBasicInfo.fund_type,
            func.count(FundScreeningRank.fund_code).label('total'),
            func.sum(FundScreeningRank.pass_4433).label('pass_count')
        ).join(
            FundScreeningRank, FundBasicInfo.fund_code == FundScreeningRank.fund_code
        ).group_by(FundBasicInfo.fund_type).all()
        
        type_stats = {}
        for fund_type, total, pass_count in stats:
            if fund_type:
                type_stats[fund_type] = {
                    'total': total,
                    'pass_4433': pass_count or 0,
                    'pass_rate': round((pass_count or 0) / total * 100, 2) if total > 0 else 0
                }
        
        return jsonify({
            'success': True,
            'message': '同类型排名计算完成',
            'stats': type_stats
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@app.route('/api/screening/available-types', methods=['POST'])
def get_available_fund_types():
    """获取符合当前筛选条件的所有基金类型（用于快速筛选）"""
    data = request.get_json() or {}
    strategy = data.get('strategy')
    filters = data.get('filters', {})
    
    db = get_db()
    
    # 基础查询
    query = db.query(FundBasicInfo.fund_type).distinct()
    
    # 关联排名表（用于4433筛选）
    if strategy == '4433':
        query = query.join(
            FundScreeningRank, FundBasicInfo.fund_code == FundScreeningRank.fund_code
        ).filter(FundScreeningRank.pass_4433 == 1)
    
    # 关联风险指标表（用于其他策略）
    if strategy in ['high_sharpe', 'low_volatility', 'anti_fragile']:
        query = query.join(
            FundRiskMetrics, FundBasicInfo.fund_code == FundRiskMetrics.fund_code
        )
        
        if strategy == 'high_sharpe':
            query = query.filter(
                FundRiskMetrics.sharpe_ratio_1y > 2,
                FundRiskMetrics.volatility_1y < 25
            )
        elif strategy == 'low_volatility':
            query = query.filter(
                FundRiskMetrics.volatility_1y < 15,
                FundRiskMetrics.max_drawdown_1y < 15
            )
        elif strategy == 'anti_fragile':
            query = query.filter(
                FundRiskMetrics.max_drawdown_1y < 20,
                FundRiskMetrics.annual_return_1y > 0
            )
    
    # 应用自定义类型筛选
    if filters.get('fund_types'):
        type_conditions = []
        for t in filters['fund_types']:
            type_conditions.append(FundBasicInfo.fund_type.like(f'%{t}%'))
        if type_conditions:
            query = query.filter(or_(*type_conditions))
    
    # 获取所有符合条件的类型
    result = query.filter(FundBasicInfo.fund_type != None).all()
    types = sorted([r[0] for r in result if r[0]])
    
    return jsonify({'types': types})


# 申万行业标签 → 一级行业映射
SHENWAN_SECTOR_MAP = {
    # 电子
    '半导体': '电子', '元件': '电子', '电子': '电子', '消费电子': '电子',
    '光学光电子': '电子', 'PCB': '电子', '集成电路': '电子', '芯片': '电子',
    # 计算机
    '计算机': '计算机', '软件': '计算机', 'IT服务': '计算机', '通信设备': '计算机',
    '计算机设备': '计算机', '云计算': '计算机', '大数据': '计算机',
    # 通信
    '通信': '通信', '通讯': '通信', '电信': '通信', '5G': '通信',
    '通信服务': '通信', '电信运营商': '通信',
    # 医药生物
    '医药': '医药生物', '医药生物': '医药生物', '药品及科技': '医药生物',
    '化学制药': '医药生物', '中药': '医药生物', '创新药': '医药生物',
    '医疗器械': '医药生物', '医疗': '医药生物', '生物医药': '医药生物',
    '医疗服务': '医药生物', '医药商业': '医药生物', '生物制品': '医药生物',
    # 电力设备
    '电力设备': '电力设备', '新能源': '电力设备', '光伏': '电力设备',
    '风电': '电力设备', '储能': '电力设备', '电池': '电力设备',
    '电网': '电力设备', '特高压': '电力设备',
    # 机械设备
    '机械设备': '机械设备', '机械': '机械设备', '专用设备': '机械设备',
    '通用设备': '机械设备', '自动化设备': '机械设备', '仪器仪表': '机械设备',
    '工程机械': '机械设备', '机器人': '机械设备',
    # 汽车
    '汽车': '汽车', '汽车零部件': '汽车', '整车': '汽车',
    '汽车电子': '汽车', '摩托车': '汽车',
    # 国防军工
    '军工': '国防军工', '国防': '国防军工', '航天': '国防军工',
    '航空': '国防军工', '武器装备': '国防军工', '航海装备': '国防军工',
    # 食品饮料
    '食品饮料': '食品饮料', '食品': '食品饮料', '白酒': '食品饮料',
    '饮料': '食品饮料', '调味品': '食品饮料', '乳制品': '食品饮料',
    # 银行
    '银行': '银行',
    # 非银金融
    '证券': '非银金融', '保险': '非银金融', '券商': '非银金融',
    '多元金融': '非银金融', '金融科技': '非银金融',
    # 有色金属
    '有色金属': '有色金属', '有色': '有色金属', '黄金': '有色金属',
    '贵金属': '有色金属', '稀土': '有色金属', '矿业': '有色金属',
    # 基础化工
    '化工': '基础化工', '化学': '基础化工', '化学制品': '基础化工',
    '化学原料': '基础化工', '农药': '基础化工', '塑料': '基础化工',
    '橡胶': '基础化工', '新材料': '基础化工',
    # 房地产
    '房地产': '房地产', '地产': '房地产', '房地产开发': '房地产',
    # 建筑装饰
    '建筑': '建筑装饰', '建材': '建筑装饰', '建筑装饰': '建筑装饰',
    '基建': '建筑装饰', '工程': '建筑装饰', '装修': '建筑装饰',
    # 交通运输
    '交通运输': '交通运输', '物流': '交通运输', '航运': '交通运输',
    '港口': '交通运输', '铁路': '交通运输', '航空运输': '交通运输',
    # 公用事业
    '电力': '公用事业', '发电': '公用事业', '水电': '公用事业',
    '核电': '公用事业', '环保': '公用事业', '燃气': '公用事业',
    # 传媒
    '传媒': '传媒', '游戏': '传媒', '广告': '传媒', '影视': '传媒',
    '出版': '传媒', '互联网媒体': '传媒',
    # 农林牧渔
    '农业': '农林牧渔', '农牧': '农林牧渔', '畜牧': '农林牧渔',
    '养殖': '农林牧渔', '种业': '农林牧渔', '渔业': '农林牧渔',
    '饲料': '农林牧渔', '种植': '农林牧渔',
    # 家用电器
    '家电': '家用电器', '家用电器': '家用电器', '白色家电': '家用电器',
    '黑色家电': '家用电器', '厨电': '家用电器',
    # 纺织服饰
    '纺织': '纺织服饰', '服装': '纺织服饰', '家纺': '纺织服饰',
    '饰品': '纺织服饰', '鞋帽': '纺织服饰',
    # 轻工制造
    '轻工': '轻工制造', '造纸': '轻工制造', '包装': '轻工制造',
    '家具': '轻工制造', '文娱用品': '轻工制造',
    # 商贸零售
    '零售': '商贸零售', '商贸': '商贸零售', '电商': '商贸零售',
    '贸易': '商贸零售', '百货': '商贸零售',
    # 社会服务
    '旅游': '社会服务', '酒店': '社会服务', '餐饮': '社会服务',
    '教育': '社会服务', '会展': '社会服务',
    # 煤炭
    '煤炭': '煤炭', '煤': '煤炭',
    # 石油石化
    '石油': '石油石化', '石化': '石油石化', '油气': '石油石化',
    # 钢铁
    '钢铁': '钢铁', '冶钢': '钢铁',
    # 综合
    '综合': '综合',
    # 美容护理
    '美容': '美容护理', '护理': '美容护理',
    # 环保 (already in 公用事业 above but also has standalone)
    '环境': '公用事业', '水处理': '公用事业',
}

@app.route('/api/screening/industry-tags', methods=['GET'])
def get_screening_industry_tags():
    """Return available fund industry tags grouped by Shenwan sector hierarchy."""
    db = get_db()
    if db.query(FundIndustryTag).count() == 0:
        fund_codes = [
            row[0]
            for row in db.query(FundPortfolio.fund_code).all()
            if row[0]
        ]
        _screening_industry_context(db, fund_codes)
        rebuild_industry_performance_stats(db)
        try:
            db.commit()
        except Exception:
            db.rollback()

    stats = {}
    rows = db.query(FundIndustryTag.industry_tag, func.count(FundIndustryTag.fund_code)).group_by(
        FundIndustryTag.industry_tag
    ).all()
    for name, count in rows:
        name = name or '混合型'
        item = stats.setdefault(name, {
            'name': name,
            'count': 0,
        })
        item['count'] += count or 0

    items = sorted(stats.values(), key=lambda item: item['count'], reverse=True)

    # 非申万标签的额外分组（市场区域、基金大类、策略概念）
    FUND_TYPE_GROUPS = {'固收类', '宽基指数', '策略概念'}
    NON_SHENWAN_GROUPS = {
        '全球市场': ['港股', '美股', '全球市场', '印度市场', '越南市场', '日本市场',
                    '德国市场', '法国市场', '英国市场', '韩国市场', '东南亚市场',
                    '新兴市场', '港股科技', '美股科技', '海外'],
        '固收类': ['债券型', '货币型', '纯债', '可转债'],
        '策略概念': ['红利', '量化', '灵活配置', '行业轮动', '价值', '成长'],
        '宽基指数': ['沪深300', '中证500', '上证50', '创业板指', '科创50',
                    '中证1000', '中证2000', '宽基', '指数', '指数基金', '指数联接'],
    }

    groups = {}
    ungrouped = []
    for item in items:
        name = item['name']
        sector = SHENWAN_SECTOR_MAP.get(name)
        if not sector:
            for group_name, tag_list in NON_SHENWAN_GROUPS.items():
                if name in tag_list:
                    sector = group_name
                    break
        if sector:
            g = groups.setdefault(sector, {'name': sector, 'tags': [], 'count': 0})
            g['tags'].append(item)
            g['count'] += item['count']
        else:
            ungrouped.append(item)

    result_groups = sorted(groups.values(), key=lambda g: -g['count'])
    for g in result_groups:
        g['tags'] = sorted(g['tags'], key=lambda t: -t['count'])

    # 分为两大类：基金大类 vs 行业/市场板块
    fund_type_groups = []
    sector_groups = []
    for g in result_groups:
        if g['name'] in FUND_TYPE_GROUPS:
            fund_type_groups.append(g)
        else:
            sector_groups.append(g)

    return jsonify({
        'fundTypeGroups': fund_type_groups,
        'sectorGroups': sector_groups,
        'ungrouped': sorted(ungrouped, key=lambda t: -t['count']),
    })
    return jsonify({
        'data': items,
        'total': len(items),
    })


@app.route('/api/screening/rebuild-industry-tags', methods=['POST'])
def rebuild_screening_industry_tags():
    """Rebuild persisted fund industry tags from cached holdings and stock industries."""
    db = get_db()
    data = request.get_json() or {}
    force = bool(data.get('force', True))
    limit = data.get('limit')
    fund_codes = [
        row[0]
        for row in db.query(FundBasicInfo.fund_code).all()
        if row[0]
    ]
    try:
        result = batch_refresh_fund_industry_tags(
            db,
            fund_codes=fund_codes,
            force=force,
            limit=limit,
            build_full_dictionary=True,
            allow_missing_stock_network=True,
        )
        rebuild_industry_performance_stats(db)
    except Exception as exc:
        db.rollback()
        return jsonify({'success': False, 'error': str(exc)}), 500
    db.commit()

    return jsonify({
        'success': True,
        'total': len(fund_codes),
        'processed': result,
    })


@app.route('/api/screening/stock-industry/status', methods=['GET'])
def get_stock_industry_status():
    db = get_db()
    total = db.query(StockIndustry).count()
    with_industry = db.query(StockIndustry).filter(
        StockIndustry.industry.isnot(None),
        StockIndustry.industry != '',
    ).count()
    portfolio_stock_total = len(_collect_stock_codes_from_portfolios(db))
    latest = db.query(StockIndustry).order_by(desc(StockIndustry.updated_time)).first()
    return jsonify({
        'total': total,
        'with_industry': with_industry,
        'missing_industry': max(total - with_industry, 0),
        'portfolio_stock_total': portfolio_stock_total,
        'portfolio_stock_unmapped': max(portfolio_stock_total - with_industry, 0),
        'latest_update': latest.updated_time.isoformat() if latest and latest.updated_time else None,
    })


@app.route('/api/screening/stock-industry/warmup', methods=['POST'])
def warmup_stock_industry():
    db = get_db()
    data = request.get_json() or {}
    force = bool(data.get('force'))
    limit = data.get('limit')
    fund_codes = data.get('fund_codes')
    if isinstance(fund_codes, str):
        fund_codes = [code.strip() for code in fund_codes.split(',') if code.strip()]
    try:
        result = warm_stock_industry_dictionary(
            db,
            fund_codes=fund_codes if isinstance(fund_codes, list) else None,
            force=force,
            limit=limit,
        )
        db.commit()
    except Exception as exc:
        db.rollback()
        return jsonify({'success': False, 'error': str(exc)}), 500
    return jsonify({
        'success': True,
        'data': result,
    })


@app.route('/api/screening/stock-industry/build-akshare', methods=['POST'])
def build_stock_industry_from_akshare():
    db = get_db()
    data = request.get_json() or {}
    try:
        result = build_stock_industry_dictionary_from_akshare(
            db,
            force=bool(data.get('force')),
            board_limit=data.get('board_limit'),
            stock_limit=data.get('stock_limit'),
        )
    except Exception as exc:
        db.rollback()
        return jsonify({'success': False, 'error': str(exc)}), 500
    return jsonify({
        'success': True,
        'data': result,
    })


@app.route('/api/screening/query', methods=['POST'])
def query_screening_funds():
    """
    高级基金筛选查询（使用 JOIN 关联查询）
    数据来源：FundBasicInfo + FundRiskMetrics + FundScreeningRank
    """
    data = request.get_json() or {}
    
    # 筛选条件
    filters = data.get('filters', {})
    
    # 排序
    sort_by = data.get('sort_by', 'sharpe_ratio_1y')
    sort_order = data.get('sort_order', 'desc')
    
    # 分页
    page = data.get('page', 1)
    page_size = data.get('page_size', 20)
    
    # 预设策略
    strategy = data.get('strategy')
    
    db = get_db()
    
    # 基础查询：JOIN 三个表
    query = db.query(
        FundBasicInfo,
        FundRiskMetrics,
        FundScreeningRank,
        FundIndustryTag
    ).outerjoin(
        FundRiskMetrics, FundBasicInfo.fund_code == FundRiskMetrics.fund_code
    ).outerjoin(
        FundScreeningRank, FundBasicInfo.fund_code == FundScreeningRank.fund_code
    ).outerjoin(
        FundIndustryTag, FundBasicInfo.fund_code == FundIndustryTag.fund_code
    )
    
    # 应用预设策略
    if strategy == '4433':
        query = query.filter(FundScreeningRank.pass_4433 == 1)
    
    elif strategy == 'high_sharpe':
        query = query.filter(
            FundRiskMetrics.sharpe_ratio_1y > 2,
            FundRiskMetrics.volatility_1y < 25
        )
    
    elif strategy == 'low_volatility':
        query = query.filter(
            FundRiskMetrics.volatility_1y < 15,
            FundRiskMetrics.max_drawdown_1y < 15
        )
    
    elif strategy == 'anti_fragile':
        query = query.filter(
            FundRiskMetrics.max_drawdown_1y < 20,
            FundRiskMetrics.annual_return_1y > 0
        )
    
    # 应用自定义筛选条件
    if filters.get('fund_types'):
        type_conditions = []
        for t in filters['fund_types']:
            type_conditions.append(FundBasicInfo.fund_type.like(f'%{t}%'))
        if type_conditions:
            query = query.filter(or_(*type_conditions))

    # 关键词搜索（基金代码/名称）
    if filters.get('keyword'):
        kw = f'%{filters["keyword"]}%'
        query = query.filter(or_(
            FundBasicInfo.fund_code.like(kw),
            FundBasicInfo.fund_name.like(kw)
        ))

    # 近1年收益率范围
    if filters.get('return_1y_min') is not None:
        query = query.filter(FundBasicInfo.return_1y >= filters['return_1y_min'])
    if filters.get('return_1y_max') is not None:
        query = query.filter(FundBasicInfo.return_1y <= filters['return_1y_max'])

    extra_return_filter_map = {
        'return_1m': cast(func.json_extract(FundBasicInfo.performance_json, '$.1_month_return'), Float),
        'return_3m': cast(func.json_extract(FundBasicInfo.performance_json, '$.3_month_return'), Float),
        'return_6m': cast(func.json_extract(FundBasicInfo.performance_json, '$.6_month_return'), Float),
        'return_3y': cast(func.json_extract(FundBasicInfo.performance_json, '$.3_year_return'), Float),
    }
    for key, column in extra_return_filter_map.items():
        if filters.get(f'{key}_min') is not None:
            query = query.filter(column >= filters[f'{key}_min'])
        if filters.get(f'{key}_max') is not None:
            query = query.filter(column <= filters[f'{key}_max'])

    # 快速类型筛选（精确匹配）
    if filters.get('quick_fund_type'):
        query = query.filter(FundBasicInfo.fund_type == filters['quick_fund_type'])
    
    # 风险指标筛选
    if filters.get('sharpe_min') is not None:
        query = query.filter(FundRiskMetrics.sharpe_ratio_1y >= filters['sharpe_min'])
    
    if filters.get('volatility_max') is not None:
        query = query.filter(FundRiskMetrics.volatility_1y <= filters['volatility_max'])
    
    if filters.get('max_drawdown_max') is not None:
        query = query.filter(FundRiskMetrics.max_drawdown_1y <= filters['max_drawdown_max'])
    
    if filters.get('calmar_min') is not None:
        query = query.filter(FundRiskMetrics.calmar_ratio_1y >= filters['calmar_min'])

    risk_max_filter_map = {
        'max_drawdown_3m': FundRiskMetrics.max_drawdown_3m,
        'max_drawdown_6m': FundRiskMetrics.max_drawdown_6m,
        'max_drawdown_1y': FundRiskMetrics.max_drawdown_1y,
        'max_drawdown_3y': FundRiskMetrics.max_drawdown_3y,
        'max_drawdown_all': FundRiskMetrics.max_drawdown_all,
        'volatility_1y': FundRiskMetrics.volatility_1y,
        'volatility_3y': FundRiskMetrics.volatility_3y,
    }
    risk_min_filter_map = {
        'sharpe_ratio_1y': FundRiskMetrics.sharpe_ratio_1y,
        'sharpe_ratio_3y': FundRiskMetrics.sharpe_ratio_3y,
        'calmar_ratio_1y': FundRiskMetrics.calmar_ratio_1y,
        'calmar_ratio_3y': FundRiskMetrics.calmar_ratio_3y,
    }
    risk_range_filter_map = {
        'annual_return_1y': FundRiskMetrics.annual_return_1y,
        'annual_return_3y': FundRiskMetrics.annual_return_3y,
    }
    for key, column in risk_max_filter_map.items():
        if filters.get(f'{key}_max') is not None:
            query = query.filter(column <= filters[f'{key}_max'])
    for key, column in risk_min_filter_map.items():
        if filters.get(f'{key}_min') is not None:
            query = query.filter(column >= filters[f'{key}_min'])
    for key, column in risk_range_filter_map.items():
        if filters.get(f'{key}_min') is not None:
            query = query.filter(column >= filters[f'{key}_min'])
        if filters.get(f'{key}_max') is not None:
            query = query.filter(column <= filters[f'{key}_max'])
    
    # 排名筛选
    if filters.get('rank_1y_max') is not None:
        query = query.filter(FundScreeningRank.rank_pct_1y <= filters['rank_1y_max'])
    if filters.get('rank_3m_max') is not None:
        query = query.filter(FundScreeningRank.rank_pct_3m <= filters['rank_3m_max'])

    rank_filter_map = {
        'rank_pct_1m': FundScreeningRank.rank_pct_1m,
        'rank_pct_3m': FundScreeningRank.rank_pct_3m,
        'rank_pct_6m': FundScreeningRank.rank_pct_6m,
        'rank_pct_1y': FundScreeningRank.rank_pct_1y,
        'rank_pct_2y': FundScreeningRank.rank_pct_2y,
        'rank_pct_3y': FundScreeningRank.rank_pct_3y,
    }
    for key, column in rank_filter_map.items():
        if filters.get(f'{key}_max') is not None:
            query = query.filter(column <= filters[f'{key}_max'])
    if filters.get('pass_4433') is True:
        query = query.filter(FundScreeningRank.pass_4433 == 1)
    
    # 排序（根据排序字段选择对应的表）
    sort_map = {
        'sharpe_ratio_1y': FundRiskMetrics.sharpe_ratio_1y,
        'sharpe_ratio_3y': FundRiskMetrics.sharpe_ratio_3y,
        'return_1m': cast(func.json_extract(FundBasicInfo.performance_json, '$.1_month_return'), Float),
        'return_3m': cast(func.json_extract(FundBasicInfo.performance_json, '$.3_month_return'), Float),
        'return_6m': cast(func.json_extract(FundBasicInfo.performance_json, '$.6_month_return'), Float),
        'return_1y': FundBasicInfo.return_1y,
        'return_3y': cast(func.json_extract(FundBasicInfo.performance_json, '$.3_year_return'), Float),
        'volatility_1y': FundRiskMetrics.volatility_1y,
        'volatility_3y': FundRiskMetrics.volatility_3y,
        'max_drawdown_1y': FundRiskMetrics.max_drawdown_1y,
        'max_drawdown_3m': FundRiskMetrics.max_drawdown_3m,
        'max_drawdown_6m': FundRiskMetrics.max_drawdown_6m,
        'max_drawdown_3y': FundRiskMetrics.max_drawdown_3y,
        'max_drawdown_all': FundRiskMetrics.max_drawdown_all,
        'calmar_ratio_1y': FundRiskMetrics.calmar_ratio_1y,
        'calmar_ratio_3y': FundRiskMetrics.calmar_ratio_3y,
        'annual_return_1y': FundRiskMetrics.annual_return_1y,
        'annual_return_3y': FundRiskMetrics.annual_return_3y,
        'rank_pct_1y': FundScreeningRank.rank_pct_1y,
        'rank_pct_1m': FundScreeningRank.rank_pct_1m,
        'rank_pct_3m': FundScreeningRank.rank_pct_3m,
        'rank_pct_6m': FundScreeningRank.rank_pct_6m,
        'rank_pct_2y': FundScreeningRank.rank_pct_2y,
        'rank_pct_3y': FundScreeningRank.rank_pct_3y,
        'fund_code': FundBasicInfo.fund_code,
        'fund_name': FundBasicInfo.fund_name,
        'fund_type': FundBasicInfo.fund_type,
        'industry_tag_name': FundIndustryTag.industry_tag,
        'updated_time': FundBasicInfo.updated_time,
    }
    
    sort_column = sort_map.get(sort_by, FundRiskMetrics.sharpe_ratio_1y)
    if sort_order == 'desc':
        query = query.order_by(desc(sort_column))
    else:
        query = query.order_by(asc(sort_column))
    
    # 计算总数
    industry_filters = [
        str(item).strip()
        for item in (filters.get('industry_tags') or [])
        if str(item).strip()
    ]

    if industry_filters:
        query = query.filter(FundIndustryTag.industry_tag.in_(industry_filters))

    total_count = query.count()
    offset = (page - 1) * page_size
    results = query.offset(offset).limit(page_size).all()
    
    # 构建返回数据
    fund_list = []
    
    # 脏数据自动清理标记（不立即清理，而是返回NULL，防止展示离谱数据）
    # 如果用户需要修复，可以点击“更新数据”
    for basic, risk, rank, industry_tag in results:
        # 解析业绩数据
        perf = _json_loads(basic.performance_json, {}) if basic else {}
        
        # 脏数据检测：如果波动率 > 1000%，视为无效数据
        is_dirty_risk = risk and risk.volatility_1y and risk.volatility_1y > 1000
        
        fund_list.append({
            'fund_code': basic.fund_code if basic else None,
            'fund_name': basic.fund_name if basic else None,
            'fund_type': basic.fund_type if basic else None,
            # 业绩数据（来自 FundBasicInfo.performance_json）
            'return_1m': perf.get('1_month_return'),
            'return_3m': perf.get('3_month_return'),
            'return_6m': perf.get('6_month_return'),
            # 如果近1年收益率为 "0.00" 且实际上可能是空数据，则转为 None 或 "--"
            'return_1y': perf.get('1_year_return') if perf.get('1_year_return') not in ['0.00', 0.0, 0, ''] else None,
            'return_3y': perf.get('3_year_return') if perf.get('3_year_return') not in ['0.00', 0.0, 0, ''] else None,
            # 风险指标（来自 FundRiskMetrics），如果脏数据则隐藏
            'max_drawdown_3m': (risk.max_drawdown_3m if risk else None) if not is_dirty_risk else None,
            'max_drawdown_6m': (risk.max_drawdown_6m if risk else None) if not is_dirty_risk else None,
            'max_drawdown_1y': (risk.max_drawdown_1y if risk else None) if not is_dirty_risk else None,
            'max_drawdown_3y': (risk.max_drawdown_3y if risk else None) if not is_dirty_risk else None,
            'max_drawdown_all': (risk.max_drawdown_all if risk else None) if not is_dirty_risk else None,
            'volatility_1y': (risk.volatility_1y if risk else None) if not is_dirty_risk else None,
            'volatility_3y': (risk.volatility_3y if risk else None) if not is_dirty_risk else None,
            'sharpe_ratio_1y': (risk.sharpe_ratio_1y if risk else None) if not is_dirty_risk else None,
            'sharpe_ratio_3y': (risk.sharpe_ratio_3y if risk else None) if not is_dirty_risk else None,
            'calmar_ratio_1y': (risk.calmar_ratio_1y if risk else None) if not is_dirty_risk else None,
            'calmar_ratio_3y': (risk.calmar_ratio_3y if risk else None) if not is_dirty_risk else None,
            'annual_return_1y': (risk.annual_return_1y if risk else None) if not is_dirty_risk else None,
            'annual_return_3y': (risk.annual_return_3y if risk else None) if not is_dirty_risk else None,
            # 排名数据（来自 FundScreeningRank）
            'rank_pct_1m': rank.rank_pct_1m if rank else None,
            'rank_pct_3m': rank.rank_pct_3m if rank else None,
            'rank_pct_6m': rank.rank_pct_6m if rank else None,
            'rank_pct_1y': rank.rank_pct_1y if rank else None,
            'rank_pct_2y': rank.rank_pct_2y if rank else None,
            'rank_pct_3y': rank.rank_pct_3y if rank else None,
            'pass_4433': (rank.pass_4433 == 1) if rank else False,
            'industry_tag': ({
                'name': industry_tag.industry_tag,
                'ratio': industry_tag.industry_ratio,
                'count': industry_tag.industry_count,
                'basis': industry_tag.basis,
                'source': industry_tag.source,
            } if industry_tag else None),
            'industry_tag_name': industry_tag.industry_tag if industry_tag else None,
            # 时间戳
            'updated_time': basic.updated_time.isoformat() if basic and basic.updated_time else None
        })
    
    return jsonify({
        'total': total_count,
        'page': page,
        'page_size': page_size,
        'total_pages': math.ceil(total_count / page_size) if total_count > 0 else 0,
        'data': fund_list
    })


@app.route('/api/screening/strategies', methods=['GET'])
def get_screening_strategies():
    """获取预设筛选策略列表"""
    strategies = [
        {
            'id': '4433',
            'name': '4433法则',
            'description': '同类型基金中：近1/2/3年排名前25%，近3/6个月排名前33%',
            'tags': ['经典策略', '同类排名', '业绩稳定']
        },
        {
            'id': 'high_sharpe',
            'name': '高夏普比率',
            'description': '夏普比率 > 2，单位风险收益最优',
            'tags': ['风险调整', '收益优化']
        },
        {
            'id': 'low_volatility',
            'name': '低波动策略',
            'description': '波动率 < 15%，最大回撤 < 15%，稳健型',
            'tags': ['低风险', '稳健']
        },
        {
            'id': 'anti_fragile',
            'name': '反脆弱策略',
            'description': '在极端行情中表现稳健的基金',
            'tags': ['抗跌', '极端行情']
        },
        {
            'id': 'high_calmar',
            'name': '高卡玛比率',
            'description': '年化收益/最大回撤比值高，性价比最优',
            'tags': ['风险调整', '性价比']
        }
    ]
    return jsonify({'strategies': strategies})


@app.route('/api/screening/fund/<fund_code>', methods=['GET'])
def get_screening_fund_detail(fund_code):
    fund_code = _normalize_fund_code(fund_code)
    """获取单只基金的筛选详情数据（JOIN 查询）"""
    db = get_db()
    
    # JOIN 查询
    result = db.query(
        FundBasicInfo,
        FundRiskMetrics,
        FundScreeningRank,
        FundExtraData
    ).outerjoin(
        FundRiskMetrics, FundBasicInfo.fund_code == FundRiskMetrics.fund_code
    ).outerjoin(
        FundScreeningRank, FundBasicInfo.fund_code == FundScreeningRank.fund_code
    ).outerjoin(
        FundExtraData, FundBasicInfo.fund_code == FundExtraData.fund_code
    ).filter(
        FundBasicInfo.fund_code == fund_code
    ).first()
    
    if not result:
        return jsonify({'error': 'Fund not found'}), 404
    
    basic, risk, rank, extra = result
    perf = _json_loads(basic.performance_json, {})
    
    return jsonify({
        'fund_code': basic.fund_code,
        'fund_name': basic.fund_name,
        'fund_type': basic.fund_type,
        'returns': {
            '1m': perf.get('1_month_return'),
            '3m': perf.get('3_month_return'),
            '6m': perf.get('6_month_return'),
            '1y': perf.get('1_year_return'),
            '2y': perf.get('2_year_return'),
            '3y': perf.get('3_year_return'),
        },
        'risk_metrics': {
            'max_drawdown_1y': risk.max_drawdown_1y if risk else None,
            'max_drawdown_3y': risk.max_drawdown_3y if risk else None,
            'volatility_1y': risk.volatility_1y if risk else None,
            'volatility_3y': risk.volatility_3y if risk else None,
            'sharpe_ratio_1y': risk.sharpe_ratio_1y if risk else None,
            'sharpe_ratio_3y': risk.sharpe_ratio_3y if risk else None,
            'calmar_ratio_1y': risk.calmar_ratio_1y if risk else None,
            'calmar_ratio_3y': risk.calmar_ratio_3y if risk else None
        },
        'rankings': {
            '1m': rank.rank_pct_1m if rank else None,
            '3m': rank.rank_pct_3m if rank else None,
            '6m': rank.rank_pct_6m if rank else None,
            '1y': rank.rank_pct_1y if rank else None,
            '2y': rank.rank_pct_2y if rank else None,
            '3y': rank.rank_pct_3y if rank else None,
        },
        'pass_4433': (rank.pass_4433 == 1) if rank else False,
        'updated_time': basic.updated_time.isoformat() if basic.updated_time else None
    })


@app.route('/api/screening/update-single/<fund_code>', methods=['POST'])
def update_single_fund(fund_code):
    fund_code = _normalize_fund_code(fund_code)
    """更新单只基金数据"""
    db = get_db()
    
    success = update_single_fund_risk_metrics(fund_code, db)
    
    if success:
        return jsonify({'message': f'Fund {fund_code} updated successfully'})
    else:
        return jsonify({'error': f'Failed to update fund {fund_code}'}), 500


@app.route('/api/fund/<fund_code>/data-versions', methods=['GET'])
def get_fund_data_versions(fund_code):
    fund_code = _normalize_fund_code(fund_code)
    """
    获取单只基金各数据源的版本时间
    用于前端检测数据一致性
    """
    db = get_db()
    
    basic = db.query(FundBasicInfo).filter(FundBasicInfo.fund_code == fund_code).first()
    trend = db.query(FundTrend).filter(FundTrend.fund_code == fund_code).first()
    risk = db.query(FundRiskMetrics).filter(FundRiskMetrics.fund_code == fund_code).first()
    rank = db.query(FundScreeningRank).filter(FundScreeningRank.fund_code == fund_code).first()
    
    return jsonify({
        'fund_code': fund_code,
        'basic_info': {
            'exists': basic is not None,
            'updated_time': basic.updated_time.isoformat() if basic and basic.updated_time else None
        },
        'trend': {
            'exists': trend is not None,
            'updated_time': trend.updated_time.isoformat() if trend and trend.updated_time else None
        },
        'risk_metrics': {
            'exists': risk is not None,
            'updated_time': risk.updated_time.isoformat() if risk and risk.updated_time else None,
            'has_valid_data': risk.sharpe_ratio_1y is not None if risk else False
        },
        'screening_rank': {
            'exists': rank is not None,
            'updated_time': rank.updated_time.isoformat() if rank and rank.updated_time else None,
            'pass_4433': (rank.pass_4433 == 1) if rank else None
        }
    })


@app.route('/api/data/stats', methods=['GET'])
def get_data_stats():
    """获取数据库统计信息"""
    db = get_db()
    
    stats = {
        'fund_basic_info': db.query(FundBasicInfo).count(),
        'fund_trend': db.query(FundTrend).count(),
        'fund_risk_metrics': db.query(FundRiskMetrics).filter(
            FundRiskMetrics.sharpe_ratio_1y.isnot(None)
        ).count(),
        'fund_screening_rank': db.query(FundScreeningRank).count(),
        'fund_watchlist': db.query(FundWatchlist).count(),
        'pass_4433_count': db.query(FundScreeningRank).filter(
            FundScreeningRank.pass_4433 == 1
        ).count(),
    }
    
    # 按类型统计
    type_stats = db.query(
        FundBasicInfo.fund_type,
        func.count(FundBasicInfo.fund_code)
    ).group_by(FundBasicInfo.fund_type).all()
    
    stats['by_type'] = {t: c for t, c in type_stats if t}
    
    return jsonify(stats)


# ==================== 基金回测功能 ====================

def _extract_nav_points_from_payload(payload):
    if not isinstance(payload, dict):
        return []
    data = payload.get('data', payload)
    if isinstance(data, dict):
        if isinstance(data.get('items'), list):
            return data.get('items')
        if isinstance(data.get('navHistory'), dict):
            nav_data = data['navHistory'].get('data')
            if isinstance(nav_data, dict) and isinstance(nav_data.get('items'), list):
                return nav_data.get('items')
            if isinstance(nav_data, list):
                return nav_data
    return []


def _load_backtest_nav_points(db, fund_code, start_date, end_date):
    trend = db.query(FundTrend).filter(FundTrend.fund_code == fund_code).first()
    nav_points = []
    if trend:
        raw_points = _json_loads(trend.net_worth_trend_json, [])
        for item in raw_points:
            date_str = str(item.get('date') or '').split(' ')[0]
            nav = item.get('net_worth')
            if date_str and start_date <= date_str <= end_date:
                nav_points.append({'date': date_str, 'net_worth': nav})

    if len(nav_points) >= 2:
        return nav_points

    try:
        payload = get_data_service_client().get_fund_nav_history(
            fund_code,
            start_date=start_date.replace('-', ''),
            end_date=end_date.replace('-', ''),
        )
        items = _extract_nav_points_from_payload(payload)
        nav_points = []
        for item in items:
            date_str = str(item.get('date') or item.get('navDate') or '').split(' ')[0]
            if len(date_str) == 8 and date_str.isdigit():
                date_str = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
            nav = item.get('nav')
            if nav is None:
                nav = item.get('net_worth')
            if date_str and start_date <= date_str <= end_date:
                nav_points.append({'date': date_str, 'nav': nav})
        return nav_points
    except Exception as exc:
        print(f"backtest nav fallback unavailable for {fund_code}: {exc}", flush=True)
        return nav_points


def _safe_float_param(data, key, default):
    value = data.get(key)
    if value is None or value == '':
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _execute_strategy_backtest(data):
    fund_code = str(data.get('fund_code') or '').strip()
    start_date = str(data.get('start_date') or '').strip()
    end_date = str(data.get('end_date') or '').strip()
    strategy_type = data.get('strategy_type') or 'fixed_amount'
    params = data.get('params') if isinstance(data.get('params'), dict) else {}

    if not all([fund_code, start_date, end_date]):
        return jsonify({'error': 'Missing required parameters'}), 400

    try:
        datetime.strptime(start_date, '%Y-%m-%d')
        datetime.strptime(end_date, '%Y-%m-%d')
    except ValueError:
        return jsonify({'error': 'Invalid date format, expected YYYY-MM-DD'}), 400

    capital = _safe_float_param(data, 'capital', 10000)
    fee_rate = _safe_float_param(data, 'fee_rate', 0.15) / 100
    if capital <= 0:
        return jsonify({'error': 'capital must be greater than 0'}), 400

    db = get_db()
    nav_points = _load_backtest_nav_points(db, fund_code, start_date, end_date)
    if len(nav_points) < 2:
        return jsonify({'error': f'Insufficient data in range {start_date} to {end_date}. Found {len(nav_points)} records.'}), 400

    result = run_strategy_backtest(
        nav_points=nav_points,
        strategy_type=strategy_type,
        capital=capital,
        fee_rate=fee_rate,
        params=params,
    )
    if 'error' in result:
        return jsonify(result), 400
    return jsonify(result)


@app.route('/api/backtest/strategy', methods=['POST'])
def backtest_strategy():
    return _execute_strategy_backtest(request.get_json() or {})


def _execute_legacy_fixed_investment(data):
    investment_type = data.get('investment_type', 'monthly')
    payload = {
        'fund_code': data.get('fund_code'),
        'start_date': data.get('start_date'),
        'end_date': data.get('end_date'),
        'strategy_type': 'fixed_amount',
        'capital': _safe_float_param(data, 'capital', _safe_float_param(data, 'total_capital', 10000)),
        'fee_rate': _safe_float_param(data, 'fee_rate', 0.15),
        'params': {
            'frequency': investment_type,
            'investment_type': investment_type,
            'investment_day': data.get('investment_day'),
            'amount': _safe_float_param(data, 'amount', 1000),
            'initial_amount': _safe_float_param(data, 'initial_amount', 0),
        }
    }
    return _execute_strategy_backtest(payload)


@app.route('/api/backtest/fixed-investment', methods=['POST'])
def backtest_fixed_investment():
    """
    基金定投回测
    
    请求参数：
    {
        "fund_code": "000001",
        "start_date": "2020-01-01",
        "end_date": "2023-12-31",
        "investment_type": "monthly",  // monthly, weekly, lump_sum
        "amount": 1000,  // 每期投资金额
        "initial_amount": 0, // 初始资金（可选）
        "fee_rate": 0.15,  // 手续费率（百分比）
        "take_profit_rate": 20, // 止盈率（百分比，可选）
        "stop_loss_rate": 10 // 止损率（百分比，可选，正数）
    }
    """
    data = request.get_json() or {}
    return _execute_legacy_fixed_investment(data)
    
    try:
        fund_code = data.get('fund_code')
        start_date = data.get('start_date')
        end_date = data.get('end_date')
        investment_type = data.get('investment_type', 'monthly')
        
        # 处理可能为空的数值输入
        def safe_float(val, default):
            if val is None or val == '':
                return default
            return float(val)

        amount = safe_float(data.get('amount'), 1000)
        initial_amount = safe_float(data.get('initial_amount'), 0)
        fee_rate = safe_float(data.get('fee_rate'), 0.15) / 100
        
        take_profit_rate = data.get('take_profit_rate')
        if take_profit_rate is not None and take_profit_rate != '':
            take_profit_rate = float(take_profit_rate) / 100
        else:
            take_profit_rate = None
            
        stop_loss_rate = data.get('stop_loss_rate')
        if stop_loss_rate is not None and stop_loss_rate != '':
            stop_loss_rate = float(stop_loss_rate) / 100
        else:
            stop_loss_rate = None
    
        if not all([fund_code, start_date, end_date]):
            return jsonify({'error': 'Missing required parameters'}), 400
        
        db = get_db()
        
        # 获取净值数据
        trend = db.query(FundTrend).filter(FundTrend.fund_code == fund_code).first()
        if not trend:
            return jsonify({'error': f'Fund data not found for code {fund_code}'}), 404
        
        net_worth_data = _json_loads(trend.net_worth_trend_json, [])
        if not net_worth_data:
            return jsonify({'error': 'No net worth data available'}), 404
        
        # 转换日期格式并排序
        nav_dict = {}
        for item in net_worth_data:
            date_str = item.get('date')
            nav = item.get('net_worth')
            # 修改判断逻辑，允许 net_worth 为 0 (虽然少见) 但不能为空
            if date_str and nav is not None:
                try:
                    nav_dict[date_str] = float(nav)
                except (ValueError, TypeError):
                    continue
        
        # 按日期排序
        sorted_dates = sorted(nav_dict.keys())
        
        if not sorted_dates:
             return jsonify({'error': 'Valid net worth data is empty'}), 404

        # 辅助日期解析函数
        def parse_date(date_str):
            for fmt in ['%Y-%m-%d', '%Y/%m/%d', '%Y%m%d', '%Y-%m-%d %H:%M:%S']:
                try:
                    return datetime.strptime(date_str, fmt)
                except ValueError:
                    continue
            raise ValueError(f"Unknown date format: {date_str}")

        # 过滤日期范围
        try:
            # 只取日期部分进行比较
            start_dt = parse_date(start_date).replace(hour=0, minute=0, second=0, microsecond=0)
            end_dt = parse_date(end_date).replace(hour=23, minute=59, second=59, microsecond=999999)
        except ValueError as e:
            return jsonify({'error': f'Invalid date format: {str(e)}'}), 400
        
        filtered_dates = []
        for d in sorted_dates:
            try:
                current_dt = datetime.strptime(d, '%Y-%m-%d')
                if start_dt <= current_dt <= end_dt:
                    filtered_dates.append(d)
            except ValueError:
                continue

        if len(filtered_dates) < 2:
            return jsonify({'error': f'Insufficient data in range {start_date} to {end_date}. Found {len(filtered_dates)} records.'}), 400
        
        # 执行回测
        result = _run_backtest(
            nav_dict=nav_dict,
            dates=filtered_dates,
            investment_type=investment_type,
            amount=amount,
            initial_amount=initial_amount,
            fee_rate=fee_rate,
            take_profit_rate=take_profit_rate,
            stop_loss_rate=stop_loss_rate
        )
        
        if 'error' in result:
             return jsonify(result), 400

        return jsonify(result)

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': f'Backtest execution failed: {str(e)}'}), 500


def _run_backtest(nav_dict, dates, investment_type, amount, initial_amount, fee_rate, take_profit_rate=None, stop_loss_rate=None):
    """
    执行回测计算
    """
    timeline = []
    total_invested = 0
    total_shares = 0
    
    # 确定投资日期
    investment_dates = []
    
    if investment_type == 'lump_sum':
        # 一次性投资：只在第一天
        investment_dates = [dates[0]]
    elif investment_type == 'monthly':
        # 每月定投：每月第一个交易日
        current_month = None
        for date in dates:
            dt = datetime.strptime(date, '%Y-%m-%d')
            month_key = (dt.year, dt.month)
            if month_key != current_month:
                investment_dates.append(date)
                current_month = month_key
    elif investment_type == 'weekly':
        # 每周定投：每周第一个交易日
        current_week = None
        for date in dates:
            dt = datetime.strptime(date, '%Y-%m-%d')
            week_key = (dt.year, dt.isocalendar()[1])
            if week_key != current_week:
                investment_dates.append(date)
                current_week = week_key
    
    # 状态标记
    sold_out = False
    exit_reason = None
    exit_date = None
    cash = 0
    
    # 遍历所有日期，计算持仓
    for i, date in enumerate(dates):
        nav = nav_dict[date]
        
        # 如果已经清仓止盈止损，后续只计算现金价值（假设不重新买入）
        if sold_out:
            timeline.append({
                'date': date,
                'invested': round(total_invested, 2),
                'shares': 0,
                'nav': round(nav, 4),
                'value': round(cash, 2),
                'return': round(cash - total_invested, 2),
                'return_rate': round((cash - total_invested) / total_invested * 100, 2) if total_invested > 0 else 0,
                'is_investment_day': False,
                'status': 'sold',
                'exit_reason': exit_reason
            })
            continue

        # 1. 处理初始资金 (仅第一天)
        if i == 0 and initial_amount > 0:
            actual_amount = initial_amount * (1 - fee_rate)
            shares_bought = actual_amount / nav
            total_shares += shares_bought
            total_invested += initial_amount
            
        # 2. 处理定投
        is_invest_day = False
        if investment_type != 'lump_sum' and date in investment_dates:
            actual_amount = amount * (1 - fee_rate)
            shares_bought = actual_amount / nav
            total_shares += shares_bought
            total_invested += amount
            is_invest_day = True
        elif investment_type == 'lump_sum' and i == 0 and amount > 0:
             # 如果是 lump_sum 且 amount > 0，视为第一天投入
             # 叠加 initial_amount
             actual_amount = amount * (1 - fee_rate)
             shares_bought = actual_amount / nav
             total_shares += shares_bought
             total_invested += amount
             is_invest_day = True
        
        # 计算当前市值
        current_value = total_shares * nav
        total_return = current_value - total_invested
        return_rate = (total_return / total_invested * 100) if total_invested > 0 else 0
        
        # 3. 检查止盈止损
        triggered = False
        if total_invested > 0:
            if take_profit_rate and return_rate >= (take_profit_rate * 100):
                sold_out = True
                exit_reason = 'take_profit'
                triggered = True
            elif stop_loss_rate and return_rate <= -(stop_loss_rate * 100):
                sold_out = True
                exit_reason = 'stop_loss'
                triggered = True
        
        if triggered:
            exit_date = date
            cash = current_value 
            
            timeline.append({
                'date': date,
                'invested': round(total_invested, 2),
                'shares': 0,
                'nav': round(nav, 4),
                'value': round(cash, 2),
                'return': round(cash - total_invested, 2),
                'return_rate': round((cash - total_invested) / total_invested * 100, 2),
                'is_investment_day': is_invest_day,
                'status': 'sold',
                'exit_reason': exit_reason
            })
            continue

        timeline.append({
            'date': date,
            'invested': round(total_invested, 2),
            'shares': round(total_shares, 4),
            'nav': round(nav, 4),
            'value': round(current_value, 2),
            'return': round(total_return, 2),
            'return_rate': round(return_rate, 2),
            'is_investment_day': is_invest_day,
            'status': 'holding'
        })
    
    # 计算汇总指标
    if len(timeline) == 0:
        return {'error': 'No data to backtest'}
    
    final_record = timeline[-1]
    
    # 计算最大回撤
    max_drawdown = 0
    peak_value = 0
    for record in timeline:
        value = record['value']
        if value > peak_value:
            peak_value = value
        if peak_value > 0:
            drawdown = (peak_value - value) / peak_value * 100
            if drawdown > max_drawdown:
                max_drawdown = drawdown
    
    # 计算年化收益率
    start_date = datetime.strptime(timeline[0]['date'], '%Y-%m-%d')
    end_date = datetime.strptime(timeline[-1]['date'], '%Y-%m-%d')
    days = (end_date - start_date).days
    years = days / 365.25
    
    total_return_rate = final_record['return_rate'] / 100
    annual_return = 0
    if years > 0 and total_return_rate > -1:
        annual_return = (pow(1 + total_return_rate, 1 / years) - 1) * 100
    
    # 计算夏普比率（简化版，假设无风险利率2%）
    returns = []
    for i in range(1, len(timeline)):
        if timeline[i-1]['value'] > 0:
            daily_return = (timeline[i]['value'] - timeline[i-1]['value']) / timeline[i-1]['value']
            returns.append(daily_return)
    
    sharpe_ratio = 0
    if len(returns) > 0:
        mean_return = sum(returns) / len(returns)
        if len(returns) > 1:
            variance = sum((r - mean_return) ** 2 for r in returns) / (len(returns) - 1)
            std_dev = math.sqrt(variance)
            if std_dev > 0:
                # 年化夏普比率
                risk_free_rate = 0.02 / 252  # 日无风险利率
                sharpe_ratio = (mean_return - risk_free_rate) / std_dev * math.sqrt(252)
    
    summary = {
        'total_invested': round(final_record['invested'], 2),
        'final_value': round(final_record['value'], 2),
        'total_return': round(final_record['return'], 2),
        'return_rate': round(final_record['return_rate'], 2),
        'annual_return': round(annual_return, 2),
        'max_drawdown': round(-max_drawdown, 2),
        'sharpe_ratio': round(sharpe_ratio, 2),
        'investment_count': len(investment_dates) + (1 if initial_amount > 0 else 0),
        'days': days,
        'exit_reason': exit_reason,
        'exit_date': exit_date
    }
    
    return {
        'summary': summary,
        'timeline': timeline
    }


# ---------------------------------------------------------------------------
# Research dashboard
# ---------------------------------------------------------------------------

# P0: 移除了 ETF 分组（数据库中无 fund_type 含"ETF"的分类，ETF 归入指数型）
# P0: 货币型标记为 unavailable，因为 pingzhongdata API 不支持货币基金的百分比收益率
RESEARCH_FUND_GROUPS = [
    {"key": "equity", "name": "股票型", "keywords": ["股票"]},
    {"key": "hybrid", "name": "混合型", "keywords": ["混合"]},
    {"key": "bond", "name": "债券型", "keywords": ["债券", "债券型"]},
    {"key": "index", "name": "指数型", "keywords": ["指数", "联接"]},
    {"key": "qdii", "name": "QDII", "keywords": ["QDII"]},
    {"key": "fof", "name": "FOF", "keywords": ["FOF"]},
    {"key": "money", "name": "货币型（暂不支持）", "keywords": ["货币"]},
]

UNAVAILABLE_FUND_GROUPS = {"money"}  # 数据源不支持的分组


def _to_float(value):
    if value is None or value == '' or value == '--':
        return None
    try:
        text = str(value).replace('%', '').replace(',', '').strip()
        return float(text)
    except (TypeError, ValueError):
        return None


def _round_or_none(value, digits=2):
    num = _to_float(value)
    return round(num, digits) if num is not None else None


def _median(values):
    nums = sorted(v for v in (_to_float(item) for item in values) if v is not None)
    if not nums:
        return None
    mid = len(nums) // 2
    if len(nums) % 2:
        return round(nums[mid], 2)
    return round((nums[mid - 1] + nums[mid]) / 2, 2)


def _avg(values):
    nums = [v for v in (_to_float(item) for item in values) if v is not None]
    return round(sum(nums) / len(nums), 2) if nums else None


def _positive_rate(values):
    nums = [v for v in (_to_float(item) for item in values) if v is not None]
    return round(sum(1 for v in nums if v > 0) / len(nums) * 100, 2) if nums else None


def _sum(values):
    nums = [v for v in (_to_float(item) for item in values) if v is not None]
    return round(sum(nums), 2) if nums else None


def _fund_type_matches(fund_type, fund_name, keywords):
    text = f"{fund_type or ''} {fund_name or ''}".upper()
    return any(str(keyword).upper() in text for keyword in keywords)


def _fund_group_key(fund_type, fund_name):
    for group in RESEARCH_FUND_GROUPS:
        if _fund_type_matches(fund_type, fund_name, group["keywords"]):
            return group["key"]
    return "other"


def _research_fund_row(basic, risk=None, rank=None, estimate=None):
    perf = _json_loads(basic.performance_json, {}) if basic else {}
    return {
        "fund_code": basic.fund_code if basic else None,
        "fund_name": basic.fund_name if basic else None,
        "fund_type": basic.fund_type if basic else None,
        "return_1m": _round_or_none(perf.get("1_month_return")),
        "return_3m": _round_or_none(perf.get("3_month_return")),
        "return_6m": _round_or_none(perf.get("6_month_return")),
        "return_1y": _round_or_none(perf.get("1_year_return") if perf else basic.return_1y),
        "return_3y": _round_or_none(perf.get("3_year_return")),
        "max_drawdown_1y": _round_or_none(risk.max_drawdown_1y if risk else None),
        "volatility_1y": _round_or_none(risk.volatility_1y if risk else None),
        "sharpe_ratio_1y": _round_or_none(risk.sharpe_ratio_1y if risk else None),
        "calmar_ratio_1y": _round_or_none(risk.calmar_ratio_1y if risk else None),
        "rank_pct_1y": _round_or_none(rank.rank_pct_1y if rank else None),
        "pass_4433": bool(rank and rank.pass_4433 == 1),
        "estimate_change": _round_or_none(estimate.estimate_change if estimate else None),
        "estimate_time": estimate.estimate_time if estimate else None,
        "nav": _round_or_none(estimate.net_worth if estimate else None, 4),
        "nav_date": estimate.net_worth_date if estimate else None,
        "updated_time": basic.updated_time.isoformat() if basic and basic.updated_time else None,
    }


def _research_base_rows(db):
    return db.query(
        FundBasicInfo,
        FundRiskMetrics,
        FundScreeningRank,
        FundEstimate,
    ).outerjoin(
        FundRiskMetrics, FundBasicInfo.fund_code == FundRiskMetrics.fund_code
    ).outerjoin(
        FundScreeningRank, FundBasicInfo.fund_code == FundScreeningRank.fund_code
    ).outerjoin(
        FundEstimate, FundBasicInfo.fund_code == FundEstimate.fund_code
    ).all()


def _build_research_market_stats(db):
    rows = _research_base_rows(db)
    items = [_research_fund_row(basic, risk, rank, estimate) for basic, risk, rank, estimate in rows]
    total = len(items)
    risk_ready = sum(1 for _, risk, _, _ in rows if risk and risk.sharpe_ratio_1y is not None)
    rank_ready = sum(1 for _, _, rank, _ in rows if rank)
    pass_4433 = sum(1 for _, _, rank, _ in rows if rank and rank.pass_4433 == 1)

    # P2: QDII 子类型合并为 "QDII"，避免碎片化
    def _consolidate_type(fund_type):
        ft = fund_type or "未分类"
        if ft.startswith("QDII"):
            return "QDII"
        return ft

    type_map = {}
    group_map = {}
    all_sharpe_values = []
    all_volatility_values = []
    all_drawdown_values = []
    all_return_3y_values = []
    for item, (_, risk, _, _) in zip(items, rows):
        fund_type = _consolidate_type(item.get("fund_type"))
        type_stat = type_map.setdefault(fund_type, {
            "fund_type": fund_type,
            "count": 0,
            "pass_4433": 0,
            "return_1y_values": [],
            "return_3m_values": [],
        })
        type_stat["count"] += 1
        type_stat["pass_4433"] += 1 if item.get("pass_4433") else 0
        type_stat["return_1y_values"].append(item.get("return_1y"))
        type_stat["return_3m_values"].append(item.get("return_3m"))

        group_key = _fund_group_key(item.get("fund_type"), item.get("fund_name"))
        group_stat = group_map.setdefault(group_key, {
            "key": group_key,
            "name": next((g["name"] for g in RESEARCH_FUND_GROUPS if g["key"] == group_key), "其他"),
            "count": 0,
            "return_1y_values": [],
        })
        group_stat["count"] += 1
        group_stat["return_1y_values"].append(item.get("return_1y"))

        if risk:
            all_sharpe_values.append(risk.sharpe_ratio_1y)
            all_volatility_values.append(risk.volatility_1y)
            all_drawdown_values.append(risk.max_drawdown_1y)
        all_return_3y_values.append(item.get("return_3y"))

    type_stats = []
    for stat in type_map.values():
        type_stats.append({
            "fund_type": stat["fund_type"],
            "count": stat["count"],
            "ratio": round(stat["count"] / total * 100, 2) if total else 0,
            "pass_4433": stat["pass_4433"],
            "pass_rate": round(stat["pass_4433"] / stat["count"] * 100, 2) if stat["count"] else 0,
            "return_1y_median": _median(stat["return_1y_values"]),
            "return_3m_median": _median(stat["return_3m_values"]),
        })
    type_stats.sort(key=lambda item: item["count"], reverse=True)

    group_stats = []
    for stat in group_map.values():
        group_stats.append({
            "key": stat["key"],
            "name": stat["name"],
            "count": stat["count"],
            "ratio": round(stat["count"] / total * 100, 2) if total else 0,
            "return_1y_median": _median(stat["return_1y_values"]),
            "available": stat["key"] not in UNAVAILABLE_FUND_GROUPS,
        })
    group_stats.sort(key=lambda item: item["count"], reverse=True)

    latest_update = max(
        (basic.updated_time for basic, _, _, _ in rows if basic and basic.updated_time),
        default=None,
    )

    # P2: 市场覆盖率（相对于天天基金全量列表）
    market_total = 27038  # 天天基金 fundcode_search.js 全量
    coverage_rate = round(total / market_total * 100, 2) if market_total else None

    return {
        "summary": {
            "total_funds": total,
            "market_total": market_total,
            "coverage_rate": coverage_rate,
            "risk_ready": risk_ready,
            "risk_ready_rate": round(risk_ready / total * 100, 2) if total else 0,
            "rank_ready": rank_ready,
            "rank_ready_rate": round(rank_ready / total * 100, 2) if total else 0,
            "pass_4433": pass_4433,
            "pass_4433_rate": round(pass_4433 / total * 100, 2) if total else 0,
            "return_1y_median": _median(item.get("return_1y") for item in items),
            "return_3m_median": _median(item.get("return_3m") for item in items),
            "return_3y_median": _median(all_return_3y_values),
            "sharpe_1y_median": _median(all_sharpe_values),
            "volatility_1y_median": _median(all_volatility_values),
            "max_drawdown_1y_median": _median(all_drawdown_values),
            "positive_1y_rate": _positive_rate(item.get("return_1y") for item in items),
            "latest_update": latest_update.isoformat() if latest_update else None,
        },
        "type_stats": type_stats[:30],
        "group_stats": group_stats,
    }


def _build_research_fund_dashboard(db, limit=5):
    rows = _research_base_rows(db)
    grouped = {
        group["key"]: {
            "key": group["key"],
            "name": group["name"],
            "items": [],
            "summary": {},
        }
        for group in RESEARCH_FUND_GROUPS
    }
    grouped["other"] = {"key": "other", "name": "其他", "items": [], "summary": {}}

    for basic, risk, rank, estimate in rows:
        item = _research_fund_row(basic, risk, rank, estimate)
        group_key = _fund_group_key(item.get("fund_type"), item.get("fund_name"))
        grouped.setdefault(group_key, {"key": group_key, "name": "其他", "items": [], "summary": {}})
        grouped[group_key]["items"].append(item)

    cards = []
    for group in grouped.values():
        items = group["items"]
        if not items:
            continue
        sorted_items = sorted(
            items,
            key=lambda item: (
                item.get("pass_4433") is True,
                item.get("sharpe_ratio_1y") if item.get("sharpe_ratio_1y") is not None else -999,
                item.get("return_1y") if item.get("return_1y") is not None else -999,
            ),
            reverse=True,
        )
        cards.append({
            "key": group["key"],
            "name": group["name"],
            "available": group["key"] not in UNAVAILABLE_FUND_GROUPS,
            "summary": {
                "total": len(items),
                "pass_4433": sum(1 for item in items if item.get("pass_4433")),
                "return_1y_avg": _avg(item.get("return_1y") for item in items),
                "sharpe_1y_avg": _avg(item.get("sharpe_ratio_1y") for item in items),
            },
            "items": sorted_items[:limit],
        })

    cards.sort(key=lambda card: card["summary"]["total"], reverse=True)
    return {"cards": cards, "limit": limit}


# ── ETF 基金池：从天天基金排行榜 API 批量拉取 ETF 业绩数据 ──

_etf_performance_cache = {}     # fund_code -> {name, return_1m, return_3m, return_6m, return_1y, return_3y, ...}
_etf_performance_cache_time = None


def _fetch_etf_performance_from_ranking(max_pages=40):
    """从天天基金排行榜 API 拉取指数型基金，过滤 ETF，缓存业绩数据。

    数据源: fund.eastmoney.com/data/rankhandler.aspx?ft=zs
    返回 ~2,400 只 ETF（来自 4,357 只指数基金，~55% 含"ETF"名称）。
    每页 50 条，40 页 = 2,000 只指数基金 → ~1,100 只 ETF。
    """
    global _etf_performance_cache, _etf_performance_cache_time
    now = datetime.now()
    # 缓存 2 小时（业绩数据一天才变一次）
    if _etf_performance_cache_time and (now - _etf_performance_cache_time).seconds < 7200:
        return _etf_performance_cache

    new_cache = {}
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Referer': 'https://fund.eastmoney.com/data/fundranking.html',
    }
    total_etfs = 0

    for page in range(1, max_pages + 1):
        params = {
            'op': 'ph', 'dt': 'kf', 'ft': 'zs',
            'rs': '', 'gs': 0, 'sc': '1nzf', 'st': 'desc',
            'sd': '', 'ed': '', 'qdii': '',
            'tabSubtype': ',,,,,',
            'pi': str(page), 'pn': '50', 'dx': '1',
        }
        try:
            resp = requests.get(
                'https://fund.eastmoney.com/data/rankhandler.aspx',
                params=params, headers=headers, timeout=12,
            )
            resp.raise_for_status()
            match = re.search(r'var rankData\s*=\s*(\{.*?\});', resp.text, re.DOTALL)
            if not match:
                break
            json_str = re.sub(r'(\w+):', r'"\1":', match.group(1))
            payload = json.loads(json_str)
            rows = payload.get('datas', [])
            if not rows:
                break

            for item in rows:
                parts = item.split(',')
                if len(parts) < 17:
                    continue
                code = _normalize_fund_code(parts[0])
                name = parts[1]
                if not code or 'ETF' not in name:
                    continue
                new_cache[code] = {
                    'fund_code': code,
                    'fund_name': name,
                    'fund_type': 'ETF',
                    'nav': _to_float(parts[4]),
                    'daily_change': _to_float(parts[6]),
                    'return_1w': _to_float(parts[7]),
                    'return_1m': _to_float(parts[8]),
                    'return_3m': _to_float(parts[9]),
                    'return_6m': _to_float(parts[10]),
                    'return_1y': _to_float(parts[11]),
                    'return_2y': _to_float(parts[12]),
                    'return_3y': _to_float(parts[13]),
                    'return_ytd': _to_float(parts[14]),
                    'return_since_inception': _to_float(parts[15]),
                }

            time.sleep(0.25)  # 礼貌间隔
        except Exception as exc:
            print(f'ETF ranking fetch page {page} failed: {exc}', flush=True)
            break

    total_etfs = len(new_cache)
    print(f'ETF ranking: fetched {total_etfs} ETFs from {page} pages', flush=True)
    _etf_performance_cache = new_cache
    _etf_performance_cache_time = now
    return new_cache


def _row_value(row, index, *names):
    for name in names:
        try:
            value = row.get(name)
            if value is not None:
                return value
        except Exception:
            pass
    try:
        return row.iloc[index]
    except Exception:
        return None


def _parse_etf_trade_time(value):
    if value is None or value == '':
        return None
    if isinstance(value, datetime):
        return value
    try:
        num = int(float(value))
        if num > 1000000000:
            return datetime.fromtimestamp(num)
    except Exception:
        pass
    try:
        return datetime.fromisoformat(str(value))
    except Exception:
        return None


def _refresh_etf_tracking_from_akshare(db, limit=500):
    field_names = (
        "f12,f14,f2,f441,f402,f4,f3,f5,f6,f17,f15,f16,f18,f7,f8,f10,"
        "f30,f31,f32,f38,f21,f20,f297,f124"
    )
    params = {
        "pn": "1",
        "pz": str(max(limit, 100)),
        "po": "1",
        "np": "1",
        "ut": "bd1d9ddb04089700cf9c27f6f7426281",
        "fltt": "2",
        "invt": "2",
        "fid": "f12",
        "fs": "b:MK0021,b:MK0022,b:MK0023,b:MK0024,b:MK0827",
        "fields": field_names,
    }
    rows = []
    source = 'eastmoney.push2'
    try:
        response = requests.get(
            "https://88.push2.eastmoney.com/api/qt/clist/get",
            params=params,
            timeout=8,
        )
        response.raise_for_status()
        payload = response.json()
        rows = (payload.get('data') or {}).get('diff') or []
    except Exception as direct_exc:
        print(f"ETF tracking: eastmoney direct unavailable: {direct_exc}", flush=True)
        try:
            import akshare as ak
            df = ak.fund_etf_spot_em()
            if df is None or getattr(df, 'empty', True):
                rows = []
            else:
                rows = [
                    {
                        'f12': _row_value(row, 0, '代码'),
                        'f14': _row_value(row, 1, '名称'),
                        'f2': _row_value(row, 2, '最新价'),
                        'f441': _row_value(row, 3, 'IOPV实时估值'),
                        'f402': _row_value(row, 4, '折价率'),
                        'f4': _row_value(row, 5, '涨跌额'),
                        'f3': _row_value(row, 6, '涨跌幅'),
                        'f5': _row_value(row, 7, '成交量'),
                        'f6': _row_value(row, 8, '成交额'),
                        'f8': _row_value(row, 14, '换手率'),
                        'f38': _row_value(row, 32, '最新份额'),
                        'f20': _row_value(row, 34, '总市值'),
                        'f124': _row_value(row, 36, '更新时间'),
                    }
                    for _, row in df.head(limit).iterrows()
                ]
                source = 'akshare.eastmoney'
        except Exception as ak_exc:
            raise RuntimeError(f"eastmoney and akshare unavailable: {direct_exc}; {ak_exc}")

    if not rows:
        return 0

    saved = 0
    now = datetime.now()
    for row in rows[:limit]:
        code = _normalize_fund_code(row.get('f12') if isinstance(row, dict) else _row_value(row, 0, '代码'))
        if not code:
            continue

        record = db.query(FundEtfTracking).filter(FundEtfTracking.fund_code == code).first()
        if not record:
            record = FundEtfTracking(fund_code=code)
            db.add(record)

        record.fund_name = str(row.get('f14') or record.fund_name or '')
        record.latest_price = _to_float(row.get('f2'))
        record.iopv = _to_float(row.get('f441'))
        record.discount_rate = _to_float(row.get('f402'))
        record.change_amount = _to_float(row.get('f4'))
        record.change_percent = _to_float(row.get('f3'))
        record.volume = _to_float(row.get('f5'))
        record.amount = _to_float(row.get('f6'))
        record.turnover_rate = _to_float(row.get('f8'))
        record.fund_share = _to_float(row.get('f38'))
        record.market_value = _to_float(row.get('f20'))
        record.trade_time = _parse_etf_trade_time(row.get('f124')) or now
        record.source = source
        record.detail_json = _json_dumps({
            'fields': field_names.split(','),
        })
        record.updated_time = now
        saved += 1

    return saved


def _build_etf_tracking_from_cache(limit=80):
    funds = []
    for fund in getattr(fund_list_cache, 'fund_list', []) or []:
        name = fund.get('NAME') or fund.get('name') or ''
        code = _normalize_fund_code(fund.get('CODE') or fund.get('code'))
        if code and 'ETF' in name.upper():
            funds.append({
                'fund_code': code,
                'fund_name': name,
                'fund_type': 'ETF',
                'estimate_change': None,
                'return_1y': None,
                'nav_date': None,
                'source': 'fund_list_cache',
            })

    return {
        'items': funds[:limit],
        'categories': [],
        'summary': {
            'total': len(funds),
            'with_estimate': 0,
            'avg_estimate_change': None,
            'positive_estimate_rate': None,
            'total_amount': None,
            'net_flow_available': False,
            'source': 'fund_list_cache',
            'stale': True,
            'net_flow_note': '已从本地基金列表识别 ETF 池；实时行情需要点击刷新获取东方财富快照。',
        },
    }


def _build_etf_tracking_snapshot(db, limit=80, refresh=False):
    source_error = None
    # P1: 检查数据是否需要刷新（无数据 或 最后更新不是今天）
    existing_count = db.query(FundEtfTracking).count()
    should_refresh = refresh or existing_count == 0
    if not should_refresh and existing_count > 0:
        latest = db.query(FundEtfTracking.updated_time).order_by(
            desc(FundEtfTracking.updated_time)
        ).first()
        if latest and latest[0]:
            latest_date = latest[0].date() if hasattr(latest[0], 'date') else latest[0]
            if isinstance(latest_date, datetime):
                latest_date = latest_date.date()
            today = datetime.now().date()
            if latest_date < today:
                should_refresh = True
    if should_refresh:
        try:
            _refresh_etf_tracking_from_akshare(db, limit=800)
            db.commit()
        except Exception as exc:
            db.rollback()
            source_error = str(exc)
            print(f"ETF tracking: akshare unavailable: {exc}", flush=True)

    # P1: 从排行榜 API 拉取 ETF 业绩数据（~1,100 只 ETF，含近1月/3月/6月/1年/3年收益）
    perf_data = {}
    perf_source = None
    try:
        perf_data = _fetch_etf_performance_from_ranking(max_pages=40)
        perf_source = 'eastmoney.ranking'
    except Exception as perf_exc:
        print(f'ETF performance fetch failed: {perf_exc}', flush=True)

    rows = db.query(FundEtfTracking).order_by(
        desc(FundEtfTracking.change_percent)
    ).all()  # 取全部（通常 ~100-800 只），便于合并和统计

    # 如果没有实时行情也没有业绩数据，回退到 fund_list_cache
    if not rows and not perf_data:
        payload = _build_etf_tracking_from_cache(limit=limit)
        payload['summary']['source_error'] = source_error
        return payload

    # 构建 live items，按 code 索引
    live_items = {}
    for row in (rows or []):
        live_items[row.fund_code] = {
            'fund_code': row.fund_code,
            'fund_name': row.fund_name,
            'fund_type': 'ETF',
            'latest_price': row.latest_price,
            'iopv': row.iopv,
            'discount_rate': row.discount_rate,
            'estimate_change': row.change_percent,
            'change_percent': row.change_percent,
            'change_amount': row.change_amount,
            'volume': row.volume,
            'amount': row.amount,
            'turnover_rate': row.turnover_rate,
            'fund_share': row.fund_share,
            'market_value': row.market_value,
            'nav_date': row.trade_time.date().isoformat() if row.trade_time else None,
            'updated_time': row.updated_time.isoformat() if row.updated_time else None,
            'source': row.source or 'eastmoney.push2',
            '_has_live': True,
        }

    # 合并：按 code 去重，live+perf 双有 ＞ live only ＞ perf only
    matched = []   # live + perf
    live_only = []
    perf_only = []

    for code in set(list(live_items.keys()) + list(perf_data.keys())):
        live = live_items.get(code, {})
        perf = perf_data.get(code, {})
        if not live and not perf:
            continue
        base = {
            'fund_code': code,
            'fund_name': live.get('fund_name') or perf.get('fund_name') or '',
            'fund_type': 'ETF',
            'estimate_change': live.get('estimate_change') or perf.get('daily_change'),
            'latest_price': live.get('latest_price'),
            'iopv': live.get('iopv'),
            'discount_rate': live.get('discount_rate'),
            'change_percent': live.get('change_percent'),
            'change_amount': live.get('change_amount'),
            'volume': live.get('volume'),
            'amount': live.get('amount'),
            'turnover_rate': live.get('turnover_rate'),
            'fund_share': live.get('fund_share'),
            'market_value': live.get('market_value'),
            'return_1m': perf.get('return_1m'),
            'return_3m': perf.get('return_3m'),
            'return_6m': perf.get('return_6m'),
            'return_1y': perf.get('return_1y'),
            'return_3y': perf.get('return_3y'),
            'return_ytd': perf.get('return_ytd'),
            'daily_change': perf.get('daily_change'),
            'nav_date': live.get('nav_date'),
            'updated_time': live.get('updated_time'),
            'source': live.get('source') or 'eastmoney.ranking',
            '_has_live': bool(live),
            '_has_perf': bool(perf),
        }
        if live and perf:
            matched.append(base)
        elif live:
            live_only.append(base)
        else:
            perf_only.append(base)

    # 排序：live 按涨跌幅降序，perf 按 1年收益降序
    live_only.sort(key=lambda it: -(it.get('estimate_change') or -999))
    perf_only.sort(key=lambda it: -(it.get('return_1y') or -999))
    matched.sort(key=lambda it: (
        -(it.get('estimate_change') or -999),
        -(it.get('return_1y') or -999),
    ))

    # 展示：交替取 live 和 perf，优先展示 matched + 混合
    half = max(limit // 2, 10)
    live_display = matched + live_only
    perf_display = matched + perf_only
    displayed = (live_display[:half] + perf_display[:half])[:limit]

    # 全量池（用于分类统计）
    all_items = matched + live_only + perf_only

    # 分类统计（基于全量池）
    categories = {}
    for item in all_items:
        name = item.get('fund_name') or ''
        if any(word in name for word in ['债', '货币']):
            category = '债券/货币 ETF'
        elif any(word in name for word in ['港', '纳斯达克', '标普', '日经', '德国', 'QDII']):
            category = '跨境 ETF'
        elif any(word in name for word in ['黄金', '商品', '能源', '豆粕']):
            category = '商品 ETF'
        elif any(word in name for word in ['医药', '消费', '芯片', '半导体', '证券', '银行', '军工', 'AI', '机器人']):
            category = '行业主题 ETF'
        else:
            category = '宽基/普通指数 ETF'
        stat = categories.setdefault(category, {
            'category': category,
            'count': 0,
            'estimate_values': [],
            'return_1y_values': [],
            'amount_values': [],
            'volume_values': [],
        })
        stat['count'] += 1
        stat['estimate_values'].append(item.get('estimate_change'))
        stat['return_1y_values'].append(item.get('return_1y'))
        stat['amount_values'].append(item.get('amount'))
        stat['volume_values'].append(item.get('volume'))

    category_items = [
        {
            'category': stat['category'],
            'count': stat['count'],
            'estimate_change_avg': _avg(stat['estimate_values']),
            'return_1y_median': _median(stat['return_1y_values']),
            'amount_total': _sum(stat['amount_values']),
            'net_flow': None,
        }
        for stat in categories.values()
    ]
    category_items.sort(key=lambda item: item['count'], reverse=True)

    latest_update = max((item.get('updated_time') for item in all_items if item.get('updated_time')), default=None)
    total_amount = _sum(item.get('amount') for item in all_items)
    live_total = len(live_items)
    perf_total = len(perf_data)
    return {
        'items': displayed,
        'categories': category_items,
        'summary': {
            'total': len(all_items),
            'with_estimate': len(live_items),
            'with_performance': len(perf_data),
            'avg_estimate_change': _avg(item.get('estimate_change') for item in live_items.values()),
            'positive_estimate_rate': _positive_rate(item.get('estimate_change') for item in live_items.values()),
            'return_1y_median': _median(item.get('return_1y') for item in perf_data.values()),
            'total_amount': total_amount,
            'net_flow_available': False,
            'source': perf_source or 'funds.db:fund_etf_tracking',
            'source_error': source_error,
            'latest_update': latest_update,
            'net_flow_note': f'ETF 池 {perf_total} 只（来自天天基金排行榜），实时行情 {live_total} 只（来自东方财富）。',
        },
    }


def _build_research_etf_tracking(db, limit=80):
    rows = _research_base_rows(db)
    etf_items = []
    for basic, risk, rank, estimate in rows:
        if _fund_type_matches(basic.fund_type, basic.fund_name, ["ETF", "交易型开放式指数", "指数"]):
            etf_items.append(_research_fund_row(basic, risk, rank, estimate))

    etf_items.sort(
        key=lambda item: (
            item.get("estimate_change") if item.get("estimate_change") is not None else -999,
            item.get("return_1y") if item.get("return_1y") is not None else -999,
        ),
        reverse=True,
    )
    snapshot_count = db.query(FundEtfTracking).count()
    estimate_count = sum(1 for item in etf_items if item.get("estimate_change") is not None)
    if snapshot_count > 0 or not etf_items or estimate_count < max(5, int(len(etf_items) * 0.05)):
        return _build_etf_tracking_snapshot(db, limit=limit)

    category_stats = {}
    for item in etf_items:
        name = item.get("fund_name") or ""
        fund_type = item.get("fund_type") or ""
        if "债" in name or "债" in fund_type:
            category = "债券ETF/指数"
        elif "港" in name or "QDII" in fund_type.upper() or "纳斯达克" in name or "标普" in name:
            category = "跨境ETF/指数"
        elif "黄金" in name or "商品" in name:
            category = "商品ETF/指数"
        elif "行业" in fund_type or any(word in name for word in ["医药", "消费", "芯片", "半导体", "证券", "银行", "军工"]):
            category = "行业主题ETF/指数"
        else:
            category = "宽基/普通指数"
        stat = category_stats.setdefault(category, {"category": category, "count": 0, "estimate_values": [], "return_1y_values": []})
        stat["count"] += 1
        stat["estimate_values"].append(item.get("estimate_change"))
        stat["return_1y_values"].append(item.get("return_1y"))

    categories = []
    for stat in category_stats.values():
        categories.append({
            "category": stat["category"],
            "count": stat["count"],
            "estimate_change_avg": _avg(stat["estimate_values"]),
            "return_1y_median": _median(stat["return_1y_values"]),
            "net_flow": None,
        })
    categories.sort(key=lambda item: item["count"], reverse=True)

    return {
        "items": etf_items[:limit],
        "categories": categories,
        "summary": {
            "total": len(etf_items),
            "with_estimate": sum(1 for item in etf_items if item.get("estimate_change") is not None),
            "avg_estimate_change": _avg(item.get("estimate_change") for item in etf_items),
            "positive_estimate_rate": _positive_rate(item.get("estimate_change") for item in etf_items),
            "total_amount": None,
            "net_flow_available": False,
            "stale": True,
            "net_flow_note": "当前展示的是基金基础信息中匹配的指数型基金，不含实时行情。点击刷新获取 ETF 实时数据。",
        },
    }


def _build_research_sector_summary(limit=50):
    try:
        payload = get_data_service_client().get_market_sectors()
        data = payload.get('data', {}) if isinstance(payload, dict) else {}
        rows = data.get('items', []) if isinstance(data, dict) else []
    except Exception as exc:
        print(f"research sector summary: DataService unavailable: {exc}")
        try:
            fallback = get_fund_master_service().get_sector_rank(limit=limit)
            fallback_rows = fallback.get('data', []) if isinstance(fallback, dict) else []
            rows = [
                {
                    'code': item.get('code') or '',
                    'name': item.get('name') or '',
                    'changePercent': item.get('raw_change', item.get('change_pct')),
                    'mainNetInflow': item.get('raw_main_inflow', item.get('main_inflow')),
                }
                for item in fallback_rows
                if isinstance(item, dict)
            ]
        except Exception as fallback_exc:
            print(f"research sector summary: fallback unavailable: {fallback_exc}")
            rows = []

    items = []
    for row in rows[:limit]:
        if not isinstance(row, dict):
            continue
        change = _to_float(row.get('changePercent'))
        inflow = _to_float(row.get('mainNetInflow'))
        if change is None:
            mood = 'unknown'
            summary = '暂无涨跌幅数据'
        elif change >= 2:
            mood = 'strong'
            summary = '强势上涨，短线热度较高'
        elif change >= 0:
            mood = 'positive'
            summary = '温和上涨，表现好于弱势板块'
        elif change <= -2:
            mood = 'weak'
            summary = '明显回调，注意波动风险'
        else:
            mood = 'negative'
            summary = '小幅回落，走势偏弱'

        flow_summary = ''
        if inflow is not None:
            if inflow > 0:
                flow_summary = '，主力资金净流入'
            elif inflow < 0:
                flow_summary = '，主力资金净流出'

        items.append({
            'code': row.get('code') or '',
            'name': row.get('name') or '',
            'change_percent': change,
            'main_net_inflow': inflow,
            'mood': mood,
            'summary': f"{summary}{flow_summary}",
        })

    top_gainers = sorted(
        [item for item in items if item.get('change_percent') is not None],
        key=lambda item: item['change_percent'],
        reverse=True,
    )[:8]
    top_losers = sorted(
        [item for item in items if item.get('change_percent') is not None],
        key=lambda item: item['change_percent'],
    )[:8]
    inflow_leaders = sorted(
        [item for item in items if item.get('main_net_inflow') is not None],
        key=lambda item: item['main_net_inflow'],
        reverse=True,
    )[:8]

    return {
        'items': items,
        'top_gainers': top_gainers,
        'top_losers': top_losers,
        'inflow_leaders': inflow_leaders,
        'summary': {
            'total': len(items),
            'strong_count': sum(1 for item in items if item.get('mood') == 'strong'),
            'positive_count': sum(1 for item in items if item.get('change_percent') is not None and item['change_percent'] >= 0),
            'negative_count': sum(1 for item in items if item.get('change_percent') is not None and item['change_percent'] < 0),
        },
    }


@app.route('/api/research/market-stats', methods=['GET'])
def get_research_market_stats():
    db = get_db()
    return jsonify(_build_research_market_stats(db))


@app.route('/api/research/fund-dashboard', methods=['GET'])
def get_research_fund_dashboard():
    db = get_db()
    limit = request.args.get('limit', 5, type=int)
    return jsonify(_build_research_fund_dashboard(db, limit=max(1, min(limit, 20))))


@app.route('/api/research/etf-tracking', methods=['GET'])
def get_research_etf_tracking():
    db = get_db()
    limit = request.args.get('limit', 80, type=int)
    refresh = str(request.args.get('refresh', '')).lower() in ('1', 'true', 'yes')
    if refresh:
        return jsonify(_build_etf_tracking_snapshot(db, limit=max(10, min(limit, 300)), refresh=True))
    return jsonify(_build_research_etf_tracking(db, limit=max(10, min(limit, 300))))


@app.route('/api/research/sector-summary', methods=['GET'])
def get_research_sector_summary():
    limit = request.args.get('limit', 50, type=int)
    return jsonify(_build_research_sector_summary(limit=max(10, min(limit, 200))))


@app.route('/api/research/industry-performance', methods=['GET'])
def get_research_industry_performance():
    db = get_db()
    if db.query(FundIndustryPerformance).count() == 0:
        latest_task = _latest_research_industry_task(db)
        if not _is_active_task(latest_task):
            task = _create_data_fetch_task(
                db,
                'research_industry_performance',
                {},
                message='后台汇总板块行情...',
            )
            thread = threading.Thread(target=_run_research_industry_performance_rebuild, args=(task.id,), daemon=True)
            thread.start()
            latest_task = task
    payload = _industry_performance_payload(db)
    payload['task_status'] = _task_to_research_status(_latest_research_industry_task(db))
    return jsonify(payload)


@app.route('/api/research/rebuild-industry-performance', methods=['POST'])
def rebuild_research_industry_performance():
    db = get_db()
    latest_task = _latest_research_industry_task(db)
    if _is_active_task(latest_task):
        return jsonify({
            'success': True,
            'already_running': True,
            'task_status': _task_to_research_status(latest_task),
            'data': _industry_performance_payload(db),
        }), 202

    task = _create_data_fetch_task(
        db,
        'research_industry_performance',
        {},
        message='后台汇总板块行情...',
    )
    thread = threading.Thread(target=_run_research_industry_performance_rebuild, args=(task.id,), daemon=True)
    thread.start()
    return jsonify({
        'success': True,
        'accepted': True,
        'task_status': _task_to_research_status(task),
        'data': _industry_performance_payload(db),
    }), 202


@app.route('/api/research/dashboard', methods=['GET'])
def get_research_dashboard():
    db = get_db()
    limit = request.args.get('limit', 5, type=int)
    etf_limit = request.args.get('etf_limit', 80, type=int)
    if db.query(FundIndustryPerformance).count() == 0:
        latest_task = _latest_research_industry_task(db)
        if not _is_active_task(latest_task):
            task = _create_data_fetch_task(
                db,
                'research_industry_performance',
                {},
                message='后台汇总板块行情...',
            )
            thread = threading.Thread(target=_run_research_industry_performance_rebuild, args=(task.id,), daemon=True)
            thread.start()
    return jsonify({
        "market_stats": _build_research_market_stats(db),
        "fund_dashboard": _build_research_fund_dashboard(db, limit=max(1, min(limit, 20))),
        "etf_tracking": _build_research_etf_tracking(db, limit=max(10, min(etf_limit, 300))),
        "industry_performance": _industry_performance_payload(db),
        "industry_performance_task": _task_to_research_status(_latest_research_industry_task(db)),
        "updated_at": datetime.now().isoformat(),
        "data_source": {
            "primary": "funds.db",
            "industry_performance": "funds.db fund industry tags + screening performance",
            "etf_net_flow": "not_available",
        },
    })

def preload_services():
    """
    服务启动时的预加载任务
    在后台线程执行，不阻塞服务启动
    """
    import threading
    
    def _preload():
        import time
        time.sleep(2)  # 等待服务完全启动
        try:
            ai_service = get_ai_service()
            if ai_service.is_available():
                print("AI 服务已就绪（硅基流动 API）")
        except Exception as e:
            print(f"Preload failed: {e}")
    
    thread = threading.Thread(target=_preload, daemon=True)
    thread.start()

# 启动预加载（在应用启动时执行）
preload_services()

from flask import send_from_directory

@app.route('/', defaults={'path': ''})
@app.route('/<path:path>')
def serve_frontend(path):
    """服务前端静态文件，SPA 路由支持"""
    import os
    static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static')
    if path and os.path.exists(os.path.join(static_dir, path)):
        return send_from_directory(static_dir, path)
    return send_from_directory(static_dir, 'index.html')

def _auto_ranking_scheduler():
    """每周自动重新计算一次同类排名（后台线程，每7天执行一次）"""
    import threading
    def _run_weekly():
        while True:
            time.sleep(3600)  # 每小时检查一次
            try:
                db = SessionLocal()
                # 检查上次排名计算时间
                latest_ranking = db.query(FundScreeningRank).order_by(
                    desc(FundScreeningRank.updated_time)
                ).first()
                should_run = False
                if latest_ranking and latest_ranking.updated_time:
                    delta = datetime.now() - latest_ranking.updated_time
                    if delta.total_seconds() > 7 * 24 * 3600:  # 7天
                        should_run = True
                else:
                    should_run = True  # 从未计算过
                db.close()

                if should_run:
                    print("[自动排名] 开始每周同类排名计算...", flush=True)
                    db2 = SessionLocal()
                    try:
                        calculate_same_type_rankings(db2)
                        db2.commit()
                        print("[自动排名] 每周同类排名计算完成", flush=True)
                    except Exception as e:
                        db2.rollback()
                        print(f"[自动排名] 计算失败: {e}", flush=True)
                    finally:
                        db2.close()
            except Exception as e:
                print(f"[自动排名] 调度检查失败: {e}", flush=True)

    t = threading.Thread(target=_run_weekly, daemon=True)
    t.start()
    print("[自动排名] 后台周度排名调度已启动", flush=True)


def _cleanup_stale_tasks_on_startup():
    """启动时清理上次崩溃残留的运行中任务"""
    try:
        db = SessionLocal()
        stale = db.query(DataFetchTask).filter(
            DataFetchTask.status == 'running'
        ).all()
        for task in stale:
            task.status = 'failed'
            task.message = '服务器重启，任务中断'
            task.finished_time = datetime.now()
            task.updated_time = datetime.now()
        if stale:
            db.commit()
            print(f"[启动清理] 已将 {len(stale)} 个残留任务标记为失败", flush=True)
        db.close()
    except Exception as e:
        print(f"[启动清理] 失败: {e}", flush=True)


# ==================== 量化分析 API ====================

@app.route('/api/quant/decide', methods=['POST'])
def quant_ai_decide():
    """
    AI 驱动的投资决策引擎 — 核心端点。

    量化 + AI 二合一：
    1. 后端计算标准化因子得分（收益/风险/经理/稳定性，0-100分）
    2. 将所有数据发给 LLM（DeepSeek），让 AI 做最终决策
    3. 返回结构化的买入/卖出方案

    POST JSON:
    {
        "total_capital": 100000,
        "risk_profile": "balanced",       // conservative/balanced/aggressive/momentum
        "investment_goal": "长期增值",
        "investment_horizon": "long",     // short/medium/long
        "fund_type": "股票型",            // 可选
        "fund_codes": [...]              // 可选，限定范围
    }
    """
    try:
        data = request.get_json() or {}
        db = get_db()
        qs = get_quant_service()

        result = qs.ai_decide(
            db=db,
            total_capital=float(data.get("total_capital", 100000)),
            risk_profile=data.get("risk_profile", "balanced"),
            investment_goal=data.get("investment_goal", "长期资产增值"),
            investment_horizon=data.get("investment_horizon", "long"),
            fund_codes=data.get("fund_codes"),
            fund_type=data.get("fund_type"),
        )
        return jsonify({"success": True, **result})

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/quant/analyze/<fund_code>', methods=['GET'])
def quant_ai_analyze_fund(fund_code):
    """AI 深度分析单只基金"""
    fund_code = _normalize_fund_code(fund_code)
    db = get_db()
    qs = get_quant_service()
    result = qs.ai_analyze_single_fund(db, fund_code)
    return jsonify({"success": "error" not in result, **result})


@app.route('/api/quant/score-funds', methods=['POST'])
def quant_score_funds():
    """多因子打分（纯量化，不调用 LLM）"""
    try:
        data = request.get_json() or {}
        risk_profile = data.get("risk_profile", "balanced")
        fund_type = data.get("fund_type")
        min_return_1y = data.get("min_return_1y")
        total_capital = data.get("total_capital", 100000)
        max_single_pct = data.get("max_single_fund_pct", 20)
        top_n = data.get("top_n", 20)
        fund_codes = data.get("fund_codes")

        db = get_db()
        qs = get_quant_service()

        pos = PositionParams(
            total_capital=float(total_capital),
            max_single_fund_pct=float(max_single_pct),
            max_total_funds=8,
        )

        results = qs._score_funds_internal(
            db=db, fund_codes=fund_codes, fund_type=fund_type,
            risk_profile=risk_profile, total_capital=total_capital, top_n=top_n,
        )

        return jsonify({
            "success": True,
            "risk_profile": risk_profile,
            "total_capital": total_capital,
            "data": [r.__dict__ for r in results],
            "count": len(results),
        })

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/quant/backtest-advanced', methods=['POST'])
def quant_backtest_advanced():
    """智能买入策略回测"""
    try:
        data = request.get_json() or {}
        fund_code = data.get("fund_code", "")
        if not fund_code:
            return jsonify({"error": "fund_code is required"}), 400

        db = get_db()
        qs = get_quant_service()

        params = BacktestAdvancedParams(
            fund_code=fund_code,
            start_date=data.get("start_date", "2020-01-01"),
            end_date=data.get("end_date", ""),
            strategy=data.get("strategy", "dca"),
            base_amount=float(data.get("base_amount", 1000)),
            fee_rate=float(data.get("fee_rate", 0.15)),
            grid_step=float(data.get("grid_step", 5)),
            pe_series=data.get("pe_series"),
        )

        result = qs.backtest_advanced(db, params)
        return jsonify(result)

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/quant/strategies', methods=['GET'])
def quant_get_strategies():
    """所有策略说明"""
    return jsonify({
        "ai_decision": {
            "name": "AI 投资决策（推荐使用）",
            "url": "/api/quant/decide",
            "description": "AI 综合分析所有基金数据，给出完整的买入/卖出/仓位方案",
        },
        "ai_analyze": {
            "name": "AI 单基金深度分析",
            "url": "/api/quant/analyze/{fund_code}",
            "description": "AI 深度分析单只基金的买卖时机、风险、建议",
        },
        "scoring": {
            "name": "多因子打分",
            "url": "/api/quant/score-funds",
            "description": "纯量化评分（不调用AI），四维度 0-100 分",
            "risk_profiles": ["conservative", "balanced", "aggressive", "momentum"],
        },
        "backtest": {
            "name": "智能买入策略",
            "url": "/api/quant/backtest-advanced",
            "strategies": ["dca", "value_averaging", "grid", "adaptive"],
        },
        "exit_signals": {
            "name": "动态卖出信号",
            "url": "/api/quant/exit-signals/{fund_code}",
        },
    })


@app.route('/api/quant/exit-signals/<fund_code>', methods=['GET'])
def quant_exit_signals(fund_code):
    """动态卖出信号检测（含 AI 复核）"""
    fund_code = _normalize_fund_code(fund_code)
    db = get_db()
    qs = get_quant_service()
    result = qs.exit_signals(db, fund_code)
    return jsonify({"success": "error" not in result, **result})


@app.route('/api/quant/advisor-context', methods=['POST'])
def quant_advisor_context():
    """AI 投资顾问（等同于 /api/quant/decide，语义化别名）"""
    try:
        data = request.get_json() or {}
        db = get_db()
        qs = get_quant_service()
        result = qs.chat_advisor(
            db=db,
            total_capital=float(data.get("total_capital", 100000)),
            risk_profile=data.get("risk_profile", "balanced"),
            investment_goal=data.get("investment_goal", "长期资产增值"),
            investment_horizon=data.get("investment_horizon", "long"),
            fund_codes=data.get("fund_codes"),
        )
        return jsonify({"success": True, **result})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


if __name__ == '__main__':
    _cleanup_stale_tasks_on_startup()
    _auto_ranking_scheduler()
    app.run(debug=True, host='0.0.0.0', port=5000, threaded=True)
