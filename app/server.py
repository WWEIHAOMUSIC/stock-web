#!/usr/bin/env python3
"""
股票查询 Web 服务 - Flask 后端
多数据源备份架构：每个数据类型注册多个 provider，按优先级顺序尝试
端口: 5001
"""

import os
# 禁用代理，避免 eastmoney 域名被本地代理阻断
for var in ['HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy', 'ALL_PROXY']:
    os.environ.pop(var, None)
os.environ['NO_PROXY'] = '*'

from flask import Flask, request, jsonify, render_template
import json
import akshare as ak
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import urllib.request
import urllib.parse
import urllib.error
import ssl
import warnings

warnings.filterwarnings('ignore')
app = Flask(__name__, template_folder='templates', static_folder='static')


# ============================================================
# 全局请求配置
# ============================================================
SSL_CTX = ssl._create_unverified_context()  # 兼容旧SSL证书

SINA_HEADERS = {
    'Referer': 'http://finance.sina.com.cn',
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
}
COMMON_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
}


def make_request(url, headers=None, timeout=8, encoding='utf-8'):
    """统一请求封装：超时 + 重试一次 + 编码处理"""
    headers = headers or COMMON_HEADERS
    for attempt in range(2):
        try:
            req = urllib.request.Request(url, headers=headers)
            resp = urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX)
            raw = resp.read().decode(encoding, errors='replace')
            return raw
        except Exception:
            if attempt == 0:
                import time; time.sleep(0.5)
    return None


def first_valid(*values):
    """返回第一个非 None / 非空列表 / 非空字典的值"""
    for v in values:
        if v is not None and v != [] and v != {}:
            return v
    return values[-1]


# ============================================================
# 工具函数
# ============================================================
def calculate_ema(series, n):
    return series.ewm(span=n, adjust=False).mean()


def calculate_rsi(series, n=14):
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=n).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=n).mean()
    rs = gain / (loss + 1e-10)
    return 100 - (100 / (1 + rs))


def calculate_macd(series):
    dif = series.ewm(span=12, adjust=False).mean() - series.ewm(span=26, adjust=False).mean()
    dea = dif.ewm(span=9, adjust=False).mean()
    macd = (dif - dea) * 2
    return dif, dea, macd


def calculate_bollinger(df, n=20, k=2):
    ma = df['close'].rolling(window=n).mean()
    std = df['close'].rolling(window=n).std()
    return ma + k * std, ma, ma - k * std


# ============================================================
# ───────────────────────────────────────────────────────────
# 数据类型 1: 实时行情
# Provider:  1. Sina  →  2. Tencent
# ───────────────────────────────────────────────────────────
# ============================================================

def _sina_quote(code):
    """[Provider-1] 新浪财经实时行情"""
    market = 'sh' if code.startswith(('6', '5')) else 'sz'
    url = f'https://hq.sinajs.cn/list={market}{code}'
    raw = make_request(url, headers=SINA_HEADERS, encoding='gbk')
    if not raw:
        return None
    content = raw.split('"')[1] if '"' in raw else ''
    if not content:
        return None
    parts = content.split(',')
    if len(parts) < 10:
        return None
    try:
        name      = parts[0]
        open_p    = float(parts[1]) if parts[1] else 0
        yesterday = float(parts[2]) if parts[2] else 0
        current   = float(parts[3]) if parts[3] else 0
        high      = float(parts[4]) if parts[4] else 0
        low       = float(parts[5]) if parts[5] else 0
        vol       = int(parts[8])   if parts[8] else 0
        amount    = float(parts[9]) if parts[9] else 0
        buy1      = float(parts[6]) if parts[6] else 0
        sell1     = float(parts[7]) if parts[7] else 0
        pct       = (current - yesterday) / yesterday * 100 if yesterday else 0
        return {
            "代码": code,
            "名称": name,
            "最新价": current,
            "今开": open_p,
            "最高": high,
            "最低": low,
            "昨收": yesterday,
            "涨跌幅": round(pct, 2),
            "涨跌额": round(current - yesterday, 2),
            "成交量": vol,
            "成交额": amount,
            "成交额万": round(amount / 10000, 2),
            "成交额亿": round(amount / 1e8, 2),
            "买一价": buy1,
            "卖一价": sell1,
            "市场": '沪' if market == 'sh' else '深',
            "_source": "sina",
        }
    except Exception:
        return None


def _tencent_quote(code):
    """[Provider-2] 腾讯财经实时行情（备用）"""
    prefix = 'sh' if code.startswith(('6', '5')) else 'sz'
    url = f'https://qt.gtimg.cn/q={prefix}{code}'
    raw = make_request(url, encoding='gbk')
    if not raw:
        return None
    try:
        line = raw.strip().split('\n')[0]
        val_str = line.split('"')[1] if '"' in line else ''
        parts = val_str.split('~')
        if len(parts) < 40:
            return None
        name      = parts[1]
        current   = float(parts[3])  if parts[3]  else 0
        yesterday = float(parts[4])  if parts[4]  else 0
        open_p   = float(parts[5])  if parts[5]  else 0
        vol      = int(float(parts[6])) if parts[6] else 0
        high     = float(parts[33]) if parts[33] else 0
        low      = float(parts[34]) if parts[34] else 0
        buy1     = float(parts[9])  if parts[9]  else 0
        sell1    = float(parts[19]) if parts[19] else 0
        amount   = float(parts[37]) if parts[37] else 0
        pct      = (current - yesterday) / yesterday * 100 if yesterday else 0
        return {
            "代码": code,
            "名称": name,
            "最新价": current,
            "今开": open_p,
            "最高": high,
            "最低": low,
            "昨收": yesterday,
            "涨跌幅": round(pct, 2),
            "涨跌额": round(current - yesterday, 2),
            "成交量": vol,
            "成交额": amount,
            "成交额万": round(amount / 10000, 2),
            "成交额亿": round(amount / 1e8, 2),
            "买一价": buy1,
            "卖一价": sell1,
            "市场": '沪' if code.startswith(('6', '5')) else '深',
            "_source": "tencent",
        }
    except Exception:
        return None


def get_quote(code):
    """实时行情：Sina → Tencent（自动切换）"""
    return first_valid(_sina_quote(code), _tencent_quote(code)) \
        or {"代码": code, "_source": "none", "error": "行情数据暂时不可用"}


def get_quote_batch(codes):
    """批量实时行情（Sina 批量接口）"""
    symbols = ','.join([
        ('sh' if c.startswith(('6', '5')) else 'sz') + c for c in codes
    ])
    url = f'https://hq.sinajs.cn/list={symbols}'
    raw = make_request(url, headers=SINA_HEADERS, encoding='gbk')
    if not raw:
        return []
    results = []
    for line in raw.strip().split('\n'):
        if '=' not in line:
            continue
        code_part = line.split('=')[0].split('_')[-1]
        content = line.split('"')[1] if '"' in line else ''
        if not content:
            continue
        parts = content.split(',')
        if len(parts) < 10:
            continue
        try:
            name      = parts[0]
            yesterday = float(parts[2]) if parts[2] else 0
            current   = float(parts[3]) if parts[3] else 0
            pct       = (current - yesterday) / yesterday * 100 if yesterday else 0
            results.append({
                "代码": code_part,
                "名称": name,
                "最新价": current,
                "涨跌幅": round(pct, 2),
                "_source": "sina_batch",
            })
        except Exception:
            pass
    return results


# ============================================================
# ───────────────────────────────────────────────────────────
# 数据类型 2: 历史K线
# Provider:  1. Sina  →  2. Tencent
# ───────────────────────────────────────────────────────────
# ============================================================

def _sina_kline(code, days=90):
    """[Provider-1] 新浪财经日K线"""
    market = 'sh' if code.startswith(('6', '5')) else 'sz'
    symbol = f'{market}{code}'
    url = (f'https://money.finance.sina.com.cn/quotes_service/api/json_v2.php'
           f'/CN_MarketData.getKLineData?symbol={symbol}'
           f'&scale=240&ma=no&datalen={days}')
    raw = make_request(url, headers=SINA_HEADERS)
    if not raw:
        return None
    try:
        data = json.loads(raw)
        if not data:
            return None
        df = pd.DataFrame(data)
        df.columns = ['date', 'open', 'close', 'high', 'low', 'volume']
        df['date'] = pd.to_datetime(df['date'])
        for col in ['open', 'close', 'high', 'low', 'volume']:
            df[col] = pd.to_numeric(df[col], errors='coerce')
        df['pct_change'] = df['close'].pct_change() * 100
        df.dropna(inplace=True)
        return df.tail(days).reset_index(drop=True), "sina"
    except Exception:
        return None


def _tencent_kline(code, days=90):
    """[Provider-2] 腾讯财经K线（备用）"""
    prefix = 'sh' if code.startswith(('6', '5')) else 'sz'
    code_tc = f"{prefix}{code}"
    url = (f'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get'
           f'?_var=kline_dayhfq&param={code_tc},day,,,{days},qfq')
    raw = make_request(url, headers=SINA_HEADERS)
    if not raw:
        return None
    try:
        json_str = raw.strip()
        # 格式: kline_dayhfq={...} 或 var kline_dayhfq={...}
        if '=' in json_str:
            json_str = json_str.split('=', 1)[1]  # 只切第一个 =
        data = json.loads(json_str)
        tc_data = data.get('data', {}).get(code_tc, {})
        # 优先取前复权日K，其次普通日K
        day_data = tc_data.get('qfqday', []) or tc_data.get('day', [])
        rows = day_data[-days:] if len(day_data) > days else day_data
        if not rows:
            return None
        df = pd.DataFrame(rows, columns=['date', 'open', 'close', 'high', 'low', 'volume'])
        df['date'] = pd.to_datetime(df['date'])
        for col in ['open', 'close', 'high', 'low', 'volume']:
            df[col] = pd.to_numeric(df[col], errors='coerce')
        df['pct_change'] = df['close'].pct_change() * 100
        df.dropna(inplace=True)
        return df.reset_index(drop=True), "tencent"
    except Exception:
        return None


def get_kline(code, days=90):
    """K线：Sina → Tencent（自动切换）"""
    result = first_valid(_sina_kline(code, days), _tencent_kline(code, days))
    if result is None:
        return None, "none"
    return result


# ============================================================
# ───────────────────────────────────────────────────────────
# 数据类型 3: 技术指标（从K线计算，依赖上述 provider）
# ───────────────────────────────────────────────────────────
# ============================================================

def get_technical(code):
    """技术指标：依赖K线，无K线则返回空"""
    kline_result = get_kline(code)
    if kline_result is None:
        return {}
    df, source = kline_result
    if df is None or len(df) < 30:
        return {}

    close = df['close']
    latest = df.iloc[-1]

    ema5  = calculate_ema(close, 5).iloc[-1]
    ema10 = calculate_ema(close, 10).iloc[-1]
    ema20 = calculate_ema(close, 20).iloc[-1]
    ema30 = calculate_ema(close, 30).iloc[-1]
    dif, dea, macd = calculate_macd(close)
    rsi6  = calculate_rsi(close, 6).iloc[-1]
    rsi14 = calculate_rsi(close, 14).iloc[-1]
    bb_up, bb_mid, bb_low = calculate_bollinger(df)
    bb_upper = float(bb_up.iloc[-1])
    bb_mid_v = float(bb_mid.iloc[-1])
    bb_lower = float(bb_low.iloc[-1])
    cur_close = float(latest['close'])
    bbp = (cur_close - bb_lower) / (bb_upper - bb_lower + 1e-10)
    vol20 = close.pct_change().rolling(20).std()
    volatility = float(vol20.iloc[-1] * np.sqrt(252) * 100)
    gain_5d  = float((close.iloc[-1] / close.iloc[-6] - 1) * 100)   if len(close) >= 6  else 0
    gain_10d = float((close.iloc[-1] / close.iloc[-11] - 1) * 100) if len(close) >= 11 else 0

    # 信号判断
    if ema5 > ema10 > ema20:
        ma排列 = "多头排列"
    elif ema5 < ema10 < ema20:
        ma排列 = "空头排列"
    else:
        ma排列 = "震荡排列"

    dif_v, dea_v, macd_v = float(dif.iloc[-1]), float(dea.iloc[-1]), float(macd.iloc[-1])
    if dif_v > dea_v and macd_v > 0:
        macd信号 = "金叉-买方主导"
    elif dif_v < dea_v and macd_v < 0:
        macd信号 = "死叉-卖方主导"
    else:
        macd信号 = "中性整理"

    rsi14_v = float(rsi14)
    if rsi14_v > 70:
        rsi信号 = "超买"
    elif rsi14_v < 30:
        rsi信号 = "超卖"
    else:
        rsi信号 = "中性"

    if cur_close > bb_upper:
        bb信号 = "突破上轨-警惕回调"
    elif cur_close < bb_lower:
        bb信号 = "跌破下轨-关注支撑"
    else:
        bb信号 = "布林中轨区域"

    kline_history = [
        {
            "date":   row['date'].strftime('%Y-%m-%d'),
            "open":   float(row['open']),
            "close":  float(row['close']),
            "high":   float(row['high']),
            "low":    float(row['low']),
            "volume": float(row['volume']),
            "pct":    float(row.get('pct_change', 0)),
        }
        for _, row in df.tail(90).iterrows()
    ]

    return {
        "K线数据源": source,
        "最新价":   cur_close,
        "EMA5":     round(float(ema5), 2),
        "EMA10":    round(float(ema10), 2),
        "EMA20":    round(float(ema20), 2),
        "EMA30":    round(float(ema30), 2),
        "均线排列": ma排列,
        "MACD_DIF": round(dif_v, 4),
        "MACD_DEA": round(dea_v, 4),
        "MACD柱":   round(macd_v, 4),
        "MACD信号": macd信号,
        "RSI6":     round(float(rsi6), 1),
        "RSI14":    round(rsi14_v, 1),
        "RSI信号":  rsi信号,
        "布林上轨": round(bb_upper, 2),
        "布林中轨": round(bb_mid_v, 2),
        "布林下轨": round(bb_lower, 2),
        "布林位置": round(float(bbp * 100), 1),
        "布林信号": bb信号,
        "年化波动率": round(volatility, 2),
        "5日涨幅":  round(gain_5d, 2),
        "10日涨幅": round(gain_10d, 2),
        "K线历史":  kline_history,
    }


# ============================================================
# ───────────────────────────────────────────────────────────
# 数据类型 4: 资金流向
# Provider:  1. eastmoney(AkShare)  →  2. Sina内外盘估算
# ───────────────────────────────────────────────────────────
# ============================================================

def _eastmoney_fund_flow(code):
    """[Provider-1] 东方财富资金流向（via AkShare）"""
    try:
        market = "sh" if code.startswith("6") else "sz"
        df = ak.stock_individual_fund_flow(stock=code, market=market)
        result = {}
        for col in df.columns:
            col_str = str(col)
            if any(kw in col_str for kw in ['主力', '超大单', '大单', '中单', '小单']):
                try:
                    result[col_str] = float(df.iloc[-1].get(col, 0) or 0)
                except Exception:
                    pass
        if result:
            result['_source'] = 'eastmoney_ak'
        return result or None
    except Exception:
        return None


def _sina_fund_flow(code):
    """[Provider-2] 新浪内外盘估算（降级，数据有限）"""
    market = 'sh' if code.startswith(('6', '5')) else 'sz'
    url = f'https://hq.sinajs.cn/list={market}{code}'
    raw = make_request(url, headers=SINA_HEADERS, encoding='gbk')
    if not raw:
        return None
    try:
        content = raw.split('"')[1] if '"' in raw else ''
        if not content:
            return None
        parts = content.split(',')
        if len(parts) < 35:
            return None
        try:
            outer = float(parts[33]) if parts[33] else 0  # 外盘：主动买
            inner = float(parts[34]) if parts[34] else 0  # 内盘：主动卖
        except (ValueError, IndexError):
            outer, inner = 0, 0
        amount_yi = float(parts[9]) / 1e8 if parts[9] else 0
        diff = outer - inner
        return {
            "外盘(主动买,万元)": round(outer / 10000, 2),
            "内盘(主动卖,万元)": round(inner / 10000, 2),
            "内外盘差(万元)":    round(diff / 10000, 2),
            "成交额(亿元)":      round(amount_yi, 2),
            "_source": "sina_interpreted",
            "_note": "基于内外盘比例估算，仅供参考",
        }
    except Exception:
        return None


def get_fund_flow(code):
    """资金流向：eastmoney → Sina降级"""
    return first_valid(_eastmoney_fund_flow(code), _sina_fund_flow(code)) \
        or {"代码": code, "_source": "none", "error": "资金流向暂时不可用"}


# ============================================================
# ───────────────────────────────────────────────────────────
# 数据类型 5: 板块数据
# Provider:  1. eastmoney(AkShare)  →  2. Sina行业板块
# ───────────────────────────────────────────────────────────
# ============================================================

def _eastmoney_sectors():
    """[Provider-1] 东方财富板块（via AkShare）"""
    try:
        df = ak.stock_board_industry_name_em()
        top = df.nlargest(15, '涨跌幅')
        return [
            {
                "板块":     str(row.get('板块名称', '')),
                "涨跌幅":   float(row.get('涨跌幅', 0)),
                "领涨":     str(row.get('领涨股票', '')),
                "上涨家数": int(row.get('上涨家数', 0)),
                "下跌家数": int(row.get('下跌家数', 0)),
                "_source":  "eastmoney_ak",
            }
            for _, row in top.iterrows()
        ]
    except Exception:
        return None


def _sina_sectors():
    """[Provider-2] 新浪行业板块（降级备用）"""
    url = 'https://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php'
    raw = make_request(url, headers=SINA_HEADERS, encoding='gbk')
    if not raw:
        return None
    try:
        # 去掉 var 声明，直接当 JSON 解析
        content = raw.replace('var S_Finance_bankuai_sinaindustry =', '').strip()
        data = json.loads(content)
        if not data:
            return None
        items = []
        for key, val in data.items():
            parts = val.split(',')
            # parts[1]=板块名, parts[3]=平均涨跌幅(小数), parts[11]=领涨涨跌额(元)
            # parts[8]=领涨股代码, parts[9]=领涨股价, parts[10]=领涨股涨跌额
            if len(parts) < 12:
                continue
            try:
                avg_pct   = float(parts[3]) * 100   # 小数→百分比
                leader_pct = float(parts[11]) if parts[11] else 0
                leader_code = parts[8] if len(parts) > 8 else ''
                leader_price = parts[9] if len(parts) > 9 else ''
                leader_chg  = parts[10] if len(parts) > 10 else ''
                leader_name = parts[12] if len(parts) > 12 else leader_code
                items.append({
                    "板块":        parts[1],
                    "涨跌幅":      round(avg_pct, 2),
                    "领涨":        leader_name or leader_code or '—',
                    "领涨代码":    leader_code,
                    "领涨价格":    leader_price,
                    "领涨涨跌额":  leader_chg,
                    "领涨涨跌幅":  round(leader_pct, 2),
                    "上涨家数":    0,
                    "下跌家数":    0,
                    "_source":     "sina_industry",
                })
            except (ValueError, IndexError):
                continue
        items.sort(key=lambda x: x['涨跌幅'], reverse=True)
        return items[:15] if items else None
    except Exception:
        return None


def get_sectors():
    """板块：eastmoney → Sina行业板块（降级）"""
    return first_valid(_eastmoney_sectors(), _sina_sectors()) or []


# ============================================================
# ───────────────────────────────────────────────────────────
# 数据类型 6: 股票搜索
# Provider:  1. Sina suggest3  →  2. Sina批量行情
# ───────────────────────────────────────────────────────────
# ============================================================

def _sina_suggest_search(keyword):
    """[Provider-1] 新浪 suggest3 搜索（支持中文/代码）"""
    encoded = urllib.parse.quote(keyword)
    url = (f'https://suggest3.sinajs.cn/suggest/type=11,12,13,14,15'
           f'&key={encoded}')
    # 注意：此接口返回 GBK 编码
    for attempt in range(2):
        try:
            req = urllib.request.Request(url, headers=SINA_HEADERS)
            resp = urllib.request.urlopen(req, timeout=8, context=SSL_CTX)
            raw = resp.read().decode('gbk', errors='replace')
            break
        except Exception:
            if attempt == 0:
                import time; time.sleep(0.5)
            else:
                return []
    else:
        return []

    content = raw.split('"')[1] if '"' in raw else ''
    results = []
    seen = set()
    for item in content.split(';'):
        parts = item.split(',')
        if len(parts) < 4:
            continue
        try:
            name = parts[6] if parts[6] else parts[0]  # parts[6]是标准名称，parts[0]对代码查询含前缀
            code     = parts[2].zfill(6)   # 6位代码
            fullcode = parts[3]             # sh600519 / sz000001
            if not code.isdigit():
                continue
            if code in seen:
                continue
            seen.add(code)
            # 获取实时价格
            market = 'sh' if fullcode.startswith('sh') else 'sz'
            quote_url = f'https://hq.sinajs.cn/list={market}{code}'
            quote_raw = make_request(quote_url, headers=SINA_HEADERS, encoding='gbk')
            current, yesterday, pct = 0, 0, 0
            if quote_raw:
                q_content = quote_raw.split('"')[1] if '"' in quote_raw else ''
                if q_content:
                    q_parts = q_content.split(',')
                    if len(q_parts) >= 4:
                        try:
                            current   = float(q_parts[3])
                            yesterday = float(q_parts[2])
                            pct       = (current - yesterday) / yesterday * 100 if yesterday else 0
                        except (ValueError, IndexError):
                            pass
            results.append({
                "代码": code,
                "名称": name,
                "最新价": current,
                "涨跌幅": round(pct, 2),
                "_source": "sina_suggest",
            })
        except Exception:
            continue
    return results[:10]


def _sina_batch_search(keyword):
    """[Provider-2] Sina批量行情搜索（仅代码匹配）"""
    if not keyword.isdigit():
        return []
    code = keyword[:6].zfill(6)
    results = get_quote_batch([code])
    if results:
        for r in results:
            r['_source'] = 'sina_batch'
    return results


def search_stocks(keyword):
    """股票搜索：Sina suggest3 → Sina批量行情"""
    keyword = keyword.strip()
    if not keyword:
        return []
    results = _sina_suggest_search(keyword)
    if not results:
        results = _sina_batch_search(keyword)
    return results


# ============================================================
# ───────────────────────────────────────────────────────────
# Flask 路由
# ───────────────────────────────────────────────────────────
# ============================================================
@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/search')
def api_search():
    keyword = request.args.get('q', '').strip()
    return jsonify(search_stocks(keyword))


@app.route('/api/quote/<code>')
def api_quote(code):
    code = code.strip().zfill(6)
    result = get_quote(code)
    if result.get('error') and result.get('_source') == 'none':
        return jsonify({"error": "行情数据暂时不可用，请检查网络"}), 503
    return jsonify(result)


@app.route('/api/fund_flow/<code>')
def api_fund_flow(code):
    code = code.strip().zfill(6)
    return jsonify(get_fund_flow(code))


@app.route('/api/technical/<code>')
def api_technical(code):
    code = code.strip().zfill(6)
    result = get_technical(code)
    if not result:
        return jsonify({
            "error": "K线数据暂时不可用，请稍后重试",
            "_source": "none",
        }), 503
    return jsonify(result)


@app.route('/api/sectors')
def api_sectors():
    return jsonify(get_sectors())


# ============================================================
if __name__ == '__main__':
    print("=" * 60)
    print("  📊 股票查询 Web 服务  (多数据源备份版)")
    print("  访问地址: http://localhost:5001")
    print()
    print("  数据源优先级（自动切换）:")
    print("  行情:  新浪 → 腾讯")
    print("  K线:   新浪 → 腾讯")
    print("  指标:  依赖K线（跟随）")
    print("  资金:  东方财富 → 新浪内外盘估算")
    print("  板块:  东方财富 → 新浪行业板块")
    print("  搜索:  新浪suggest3 → 新浪批量行情")
    print()
    print("  按 Ctrl+C 停止服务")
    print("=" * 60)
    app.run(host='0.0.0.0', port=5001, debug=False, threaded=True)
