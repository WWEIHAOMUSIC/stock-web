#!/usr/bin/env python3
"""
股票深度分析 Web 服务 - Flask 后端
五维交叉验证: 技术面/资金面/消息面/政策面/情绪面
多数据源备份架构
端口: 5001
"""

import os
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
import re
import warnings

warnings.filterwarnings('ignore')
app = Flask(__name__, template_folder='templates', static_folder='static')

SSL_CTX = ssl._create_unverified_context()
SINA_HEADERS = {
    'Referer': 'http://finance.sina.com.cn',
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
}
COMMON_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
}


def make_request(url, headers=None, timeout=8, encoding='utf-8'):
    headers = headers or COMMON_HEADERS
    for attempt in range(2):
        try:
            req = urllib.request.Request(url, headers=headers)
            resp = urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX)
            return resp.read().decode(encoding, errors='replace')
        except Exception:
            if attempt == 0:
                import time; time.sleep(0.5)
    return None


def first_valid(*values):
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

def calculate_kdj(df, n=9):
    low_n = df['low'].rolling(window=n).min()
    high_n = df['high'].rolling(window=n).max()
    rsv = (df['close'] - low_n) / (high_n - low_n + 1e-10) * 100
    k = rsv.ewm(com=2, adjust=False).mean()
    d = k.ewm(com=2, adjust=False).mean()
    j = 3 * k - 2 * d
    return k, d, j


# ============================================================
# 实时行情 (Sina → Tencent)
# ============================================================
def _sina_quote(code):
    market = 'sh' if code.startswith(('6', '5')) else 'sz'
    url = f'https://hq.sinajs.cn/list={market}{code}'
    raw = make_request(url, headers=SINA_HEADERS, encoding='gbk')
    if not raw: return None
    content = raw.split('"')[1] if '"' in raw else ''
    if not content: return None
    parts = content.split(',')
    if len(parts) < 35: return None
    try:
        name = parts[0]; open_p = float(parts[1]) if parts[1] else 0
        yesterday = float(parts[2]) if parts[2] else 0; current = float(parts[3]) if parts[3] else 0
        high = float(parts[4]) if parts[4] else 0; low = float(parts[5]) if parts[5] else 0
        vol = int(parts[8]) if parts[8] else 0; amount = float(parts[9]) if parts[9] else 0
        buy1 = float(parts[6]) if parts[6] else 0; sell1 = float(parts[7]) if parts[7] else 0
        outer = float(parts[33]) if len(parts) > 33 and parts[33] else 0
        inner = float(parts[34]) if len(parts) > 34 and parts[34] else 0
        pct = (current - yesterday) / yesterday * 100 if yesterday else 0
        return {
            "代码": code, "名称": name, "最新价": current, "今开": open_p,
            "最高": high, "最低": low, "昨收": yesterday, "涨跌幅": round(pct, 2),
            "涨跌额": round(current - yesterday, 2), "成交量": vol, "成交额": amount,
            "成交额亿": round(amount / 1e8, 2), "买一价": buy1, "卖一价": sell1,
            "外盘": outer, "内盘": inner, "市场": '沪' if market == 'sh' else '深',
            "_source": "sina",
        }
    except Exception: return None

def _tencent_quote(code):
    prefix = 'sh' if code.startswith(('6', '5')) else 'sz'
    url = f'https://qt.gtimg.cn/q={prefix}{code}'
    raw = make_request(url, encoding='gbk')
    if not raw: return None
    try:
        line = raw.strip().split('\n')[0]
        val_str = line.split('"')[1] if '"' in line else ''
        parts = val_str.split('~')
        if len(parts) < 40: return None
        name = parts[1]; current = float(parts[3]) if parts[3] else 0
        yesterday = float(parts[4]) if parts[4] else 0; open_p = float(parts[5]) if parts[5] else 0
        vol = int(float(parts[6])) if parts[6] else 0; high = float(parts[33]) if parts[33] else 0
        low = float(parts[34]) if parts[34] else 0; amount = float(parts[37]) if parts[37] else 0
        pct = (current - yesterday) / yesterday * 100 if yesterday else 0
        return {
            "代码": code, "名称": name, "最新价": current, "今开": open_p,
            "最高": high, "最低": low, "昨收": yesterday, "涨跌幅": round(pct, 2),
            "涨跌额": round(current - yesterday, 2), "成交量": vol, "成交额": amount,
            "成交额亿": round(amount / 1e8, 2), "买一价": 0, "卖一价": 0,
            "外盘": 0, "内盘": 0, "市场": '沪' if code.startswith(('6', '5')) else '深',
            "_source": "tencent",
        }
    except Exception: return None

def get_quote(code):
    return first_valid(_sina_quote(code), _tencent_quote(code)) \
        or {"代码": code, "名称": "—", "_source": "none", "error": "行情不可用"}


# ============================================================
# K线 (Sina → Tencent)
# ============================================================
def _sina_kline(code, days=120):
    market = 'sh' if code.startswith(('6', '5')) else 'sz'
    symbol = f'{market}{code}'
    url = (f'https://money.finance.sina.com.cn/quotes_service/api/json_v2.php'
           f'/CN_MarketData.getKLineData?symbol={symbol}&scale=240&ma=no&datalen={days}')
    raw = make_request(url, headers=SINA_HEADERS)
    if not raw: return None
    try:
        data = json.loads(raw)
        if not data: return None
        df = pd.DataFrame(data)
        df.columns = ['date', 'open', 'close', 'high', 'low', 'volume']
        df['date'] = pd.to_datetime(df['date'])
        for col in ['open', 'close', 'high', 'low', 'volume']:
            df[col] = pd.to_numeric(df[col], errors='coerce')
        df['pct_change'] = df['close'].pct_change() * 100
        df.dropna(inplace=True)
        return df.tail(days).reset_index(drop=True), "sina"
    except Exception: return None

def _tencent_kline(code, days=120):
    prefix = 'sh' if code.startswith(('6', '5')) else 'sz'
    code_tc = f"{prefix}{code}"
    url = (f'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get'
           f'?_var=kline_dayhfq&param={code_tc},day,,,{days},qfq')
    raw = make_request(url, headers=SINA_HEADERS)
    if not raw: return None
    try:
        json_str = raw.strip()
        if '=' in json_str:
            json_str = json_str.split('=', 1)[1]
        data = json.loads(json_str)
        tc_data = data.get('data', {}).get(code_tc, {})
        day_data = tc_data.get('qfqday', []) or tc_data.get('day', [])
        rows = day_data[-days:] if len(day_data) > days else day_data
        if not rows: return None
        df = pd.DataFrame(rows, columns=['date', 'open', 'close', 'high', 'low', 'volume'])
        df['date'] = pd.to_datetime(df['date'])
        for col in ['open', 'close', 'high', 'low', 'volume']:
            df[col] = pd.to_numeric(df[col], errors='coerce')
        df['pct_change'] = df['close'].pct_change() * 100
        df.dropna(inplace=True)
        return df.reset_index(drop=True), "tencent"
    except Exception: return None

def get_kline(code, days=120):
    result = first_valid(_sina_kline(code, days), _tencent_kline(code, days))
    if result is None: return None, "none"
    return result


# ============================================================
# 资金流向 (eastmoney → Sina内外盘)
# ============================================================
def get_fund_flow(code):
    try:
        market = "sh" if code.startswith("6") else "sz"
        df = ak.stock_individual_fund_flow(stock=code, market=market)
        result = {}
        for col in df.columns:
            col_str = str(col)
            if any(kw in col_str for kw in ['主力', '超大单', '大单', '中单', '小单']):
                try: result[col_str] = float(df.iloc[-1].get(col, 0) or 0)
                except: pass
        if result:
            result['_source'] = 'eastmoney_ak'
            return result
    except: pass
    # Sina 降级
    quote = _sina_quote(code)
    if quote and quote.get('外盘') is not None:
        return {
            "外盘(万元)": round(quote['外盘'] / 10000, 2),
            "内盘(万元)": round(quote['内盘'] / 10000, 2),
            "内外盘差(万元)": round((quote['外盘'] - quote['内盘']) / 10000, 2),
            "_source": "sina_interpreted",
        }
    return {"_source": "none", "error": "资金数据不可用"}


# ============================================================
# 新闻 (AkShare → Sina搜索)
# ============================================================
def get_news(code, name=None):
    """获取个股相关新闻"""
    # Provider-1: AkShare
    try:
        df = ak.stock_news_em(symbol=code)
        if df is not None and len(df) > 0:
            news = []
            for _, row in df.head(8).iterrows():
                news.append({
                    "标题": str(row.get('新闻标题', '')),
                    "来源": str(row.get('文章来源', '')),
                    "时间": str(row.get('发布时间', '')),
                    "内容": str(row.get('新闻内容', ''))[:200],
                })
            return {"新闻": news, "_source": "eastmoney_ak"}
    except: pass

    # Provider-2: Sina搜索
    if name:
        try:
            encoded = urllib.parse.quote(name)
            url = f'https://feed.mix.sina.com.cn/api/roll/get?pageid=153&lid=2516&k={encoded}&num=8'
            raw = make_request(url)
            if raw:
                data = json.loads(raw)
                items = data.get('result', {}).get('data', [])
                news = []
                for item in items[:8]:
                    news.append({
                        "标题": item.get('title', ''),
                        "来源": item.get('author', '').replace('\\n', ''),
                        "时间": item.get('ctime', ''),
                        "内容": item.get('intro', '')[:200],
                    })
                if news:
                    return {"新闻": news, "_source": "sina_search"}
        except: pass

    return {"新闻": [], "_source": "none"}


# ============================================================
# 板块 (eastmoney → Sina行业)
# ============================================================
def get_sectors():
    try:
        df = ak.stock_board_industry_name_em()
        top = df.nlargest(15, '涨跌幅')
        return [{"板块": str(r.get('板块名称', '')), "涨跌幅": float(r.get('涨跌幅', 0)),
                 "领涨": str(r.get('领涨股票', '')), "_source": "eastmoney_ak"}
                for _, r in top.iterrows()]
    except: pass
    try:
        url = 'https://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php'
        raw = make_request(url, headers=SINA_HEADERS, encoding='gbk')
        if raw:
            content = raw.replace('var S_Finance_bankuai_sinaindustry =', '').strip()
            data = json.loads(content)
            items = []
            for key, val in data.items():
                parts = val.split(',')
                if len(parts) < 12: continue
                try:
                    items.append({"板块": parts[1], "涨跌幅": round(float(parts[3]) * 100, 2),
                                  "领涨": parts[12] if len(parts) > 12 else parts[8], "_source": "sina_industry"})
                except: continue
            items.sort(key=lambda x: x['涨跌幅'], reverse=True)
            return items[:15]
    except: pass
    return []


# ============================================================
# 搜索 (Sina suggest3)
# ============================================================
def search_stocks(keyword):
    keyword = keyword.strip()
    if not keyword: return []
    encoded = urllib.parse.quote(keyword)
    url = f'https://suggest3.sinajs.cn/suggest/type=11,12,13,14,15&key={encoded}'
    try:
        req = urllib.request.Request(url, headers=SINA_HEADERS)
        resp = urllib.request.urlopen(req, timeout=8, context=SSL_CTX)
        raw = resp.read().decode('gbk', errors='replace')
        content = raw.split('"')[1] if '"' in raw else ''
        results = []; seen = set()
        for item in content.split(';'):
            parts = item.split(',')
            if len(parts) < 7: continue
            try:
                name = parts[6] if parts[6] else parts[0]
                code = parts[2].zfill(6)
                fullcode = parts[3]
                if not code.isdigit() or code in seen: continue
                seen.add(code)
                market = 'sh' if fullcode.startswith('sh') else 'sz'
                q_url = f'https://hq.sinajs.cn/list={market}{code}'
                q_raw = make_request(q_url, headers=SINA_HEADERS, encoding='gbk')
                current = yesterday = pct = 0
                if q_raw:
                    q_content = q_raw.split('"')[1] if '"' in q_raw else ''
                    if q_content:
                        q_parts = q_content.split(',')
                        if len(q_parts) >= 4:
                            try:
                                current = float(q_parts[3]); yesterday = float(q_parts[2])
                                pct = (current - yesterday) / yesterday * 100 if yesterday else 0
                            except: pass
                results.append({"代码": code, "名称": name, "最新价": current,
                                "涨跌幅": round(pct, 2), "_source": "sina_suggest"})
            except: continue
        return results[:10]
    except: return []


# ============================================================
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 五维深度分析引擎
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ============================================================

def analyze_stock(code):
    """五维交叉验证分析主函数"""
    quote = get_quote(code)
    kline_result = get_kline(code)
    fund = get_fund_flow(code)
    news_data = get_news(code, quote.get('名称'))

    df, kline_src = kline_result if kline_result else (None, "none")

    # ── 技术面 ──
    tech = _analyze_technical(quote, df)

    # ── 资金面 ──
    capital = _analyze_capital(quote, fund)

    # ── 消息面 ──
    message = _analyze_message(news_data, quote)

    # ── 政策面 ──
    policy = _analyze_policy(quote, news_data)

    # ── 情绪面 ──
    sentiment = _analyze_sentiment(quote, df)

    # ── 综合研判 ──
    scores = {
        "技术面": tech["score"], "资金面": capital["score"],
        "消息面": message["score"], "政策面": policy["score"],
        "情绪面": sentiment["score"],
    }
    composite = _synthesize(scores, tech, capital, message, policy, sentiment, quote)

    return {
        "股票": quote,
        "技术面": tech, "资金面": capital, "消息面": message,
        "政策面": policy, "情绪面": sentiment,
        "综合": composite,
        "K线历史": _kline_to_json(df) if df is not None else [],
        "K线数据源": kline_src,
    }


def _analyze_technical(quote, df):
    """技术面分析: 均线/MACD/RSI/KDJ/布林/量价/支撑压力"""
    score = 50
    details = []

    if df is None or len(df) < 30:
        return {"score": 50, "signal": "数据不足", "details": [{"item": "K线数据不足，无法分析", "type": "neutral"}]}

    close = df['close']
    latest = df.iloc[-1]
    cur = float(latest['close'])

    # 均线排列
    ema5 = float(calculate_ema(close, 5).iloc[-1])
    ema10 = float(calculate_ema(close, 10).iloc[-1])
    ema20 = float(calculate_ema(close, 20).iloc[-1])
    ema30 = float(calculate_ema(close, 30).iloc[-1])
    if ema5 > ema10 > ema20:
        details.append({"item": f"均线多头排列(EMA5={ema5:.2f}>10={ema10:.2f}>20={ema20:.2f})", "type": "bull"})
        score += 18
    elif ema5 < ema10 < ema20:
        details.append({"item": f"均线空头排列(EMA5={ema5:.2f}<10={ema10:.2f}<20={ema20:.2f})", "type": "bear"})
        score -= 18
    else:
        details.append({"item": f"均线震荡排列，短期无明确方向", "type": "neutral"})

    # MACD
    dif, dea, macd = calculate_macd(close)
    dif_v, dea_v, macd_v = float(dif.iloc[-1]), float(dea.iloc[-1]), float(macd.iloc[-1])
    if dif_v > dea_v and macd_v > 0:
        details.append({"item": f"MACD金叉+红柱扩张(DIF={dif_v:.3f}>DEA={dea_v:.3f})", "type": "bull"})
        score += 12
    elif dif_v < dea_v and macd_v < 0:
        details.append({"item": f"MACD死叉+绿柱扩张(DIF={dif_v:.3f}<DEA={dea_v:.3f})", "type": "bear"})
        score -= 12
    elif dif_v > dea_v and macd_v < 0:
        details.append({"item": f"MACD金叉但红柱缩短，动能减弱", "type": "neutral"})
    elif dif_v < dea_v and macd_v > 0:
        details.append({"item": f"MACD死叉但绿柱缩短，动能衰减", "type": "neutral"})

    # RSI
    rsi14 = float(calculate_rsi(close, 14).iloc[-1])
    if rsi14 > 80:
        details.append({"item": f"RSI14={rsi14:.1f}，严重超买，回调风险大", "type": "bear"})
        score -= 12
    elif rsi14 > 70:
        details.append({"item": f"RSI14={rsi14:.1f}，进入超买区，警惕回调", "type": "bear"})
        score -= 6
    elif rsi14 < 20:
        details.append({"item": f"RSI14={rsi14:.1f}，严重超卖，关注反弹", "type": "bull"})
        score += 12
    elif rsi14 < 30:
        details.append({"item": f"RSI14={rsi14:.1f}，进入超卖区，关注支撑", "type": "bull"})
        score += 6
    else:
        details.append({"item": f"RSI14={rsi14:.1f}，中性区间，暂无极端信号", "type": "neutral"})

    # KDJ
    k_val, d_val, j_val = calculate_kdj(df)
    k_v, d_v, j_v = float(k_val.iloc[-1]), float(d_val.iloc[-1]), float(j_val.iloc[-1])
    if j_v > 100:
        details.append({"item": f"KDJ-J={j_v:.1f}超买区，短期见顶风险", "type": "bear"})
        score -= 5
    elif j_v < 0:
        details.append({"item": f"KDJ-J={j_v:.1f}超卖区，短期反弹可能", "type": "bull"})
        score += 5
    if k_v > d_v and len(k_val) > 1 and float(k_val.iloc[-2]) <= float(d_val.iloc[-2]):
        details.append({"item": f"KDJ低位金叉(K={k_v:.1f}>D={d_v:.1f})", "type": "bull"})
        score += 8
    elif k_v < d_v and len(k_val) > 1 and float(k_val.iloc[-2]) >= float(d_val.iloc[-2]):
        details.append({"item": f"KDJ高位死叉(K={k_v:.1f}<D={d_v:.1f})", "type": "bear"})
        score -= 8

    # 布林带
    bb_up, bb_mid, bb_low = calculate_bollinger(df)
    bb_upper, bb_lower = float(bb_up.iloc[-1]), float(bb_low.iloc[-1])
    if cur > bb_upper:
        details.append({"item": f"突破布林上轨({bb_upper:.2f})，强势但需警惕回调", "type": "bear"})
        score -= 4
    elif cur < bb_lower:
        details.append({"item": f"跌破布林下轨({bb_lower:.2f})，超跌关注支撑", "type": "bull"})
        score += 4

    # 量价配合
    if len(df) >= 5:
        vol_5 = df['volume'].rolling(5).mean().iloc[-1]
        vol_today = float(latest['volume'])
        vol_ratio = vol_today / vol_5 if vol_5 > 0 else 1
        pct_today = float(latest.get('pct_change', 0))
        if vol_ratio > 1.5 and pct_today > 2:
            details.append({"item": f"放量大涨(量比={vol_ratio:.1f}，涨幅={pct_today:.1f}%)，量价配合良好", "type": "bull"})
            score += 10
        elif vol_ratio > 1.5 and pct_today < -2:
            details.append({"item": f"放量大跌(量比={vol_ratio:.1f}，跌幅={pct_today:.1f}%)，恐慌性抛售", "type": "bear"})
            score -= 10
        elif vol_ratio < 0.7 and pct_today < -1:
            details.append({"item": f"缩量下跌(量比={vol_ratio:.1f})，卖压可能减弱", "type": "neutral"})
        elif vol_ratio < 0.7 and pct_today > 1:
            details.append({"item": f"缩量上涨(量比={vol_ratio:.1f})，上涨动能不足", "type": "bear"})
            score -= 5

    # 支撑/压力位
    if len(df) >= 20:
        high_20 = df['high'].rolling(20).max().iloc[-1]
        low_20 = df['low'].rolling(20).min().iloc[-1]
        details.append({"item": f"20日压力位: {high_20:.2f} | 支撑位: {low_20:.2f} | 当前: {cur:.2f}", "type": "info"})

    score = max(0, min(100, score))
    signal = "偏多" if score >= 60 else "偏空" if score <= 40 else "中性"
    return {"score": score, "signal": signal, "details": details}


def _analyze_capital(quote, fund):
    """资金面分析: 主力/散户/内外盘"""
    score = 50
    details = []

    if fund.get('_source') == 'none':
        # 用内外盘估算
        outer = quote.get('外盘', 0); inner = quote.get('内盘', 0)
        if outer > inner:
            diff_pct = (outer - inner) / (outer + inner + 1e-10) * 100
            details.append({"item": f"外盘({outer/10000:.0f}万)>内盘({inner/10000:.0f}万)，主动买盘占比{diff_pct:.1f}%", "type": "bull"})
            score += 8
        elif inner > outer:
            diff_pct = (inner - outer) / (outer + inner + 1e-10) * 100
            details.append({"item": f"内盘({inner/10000:.0f}万)>外盘({outer/10000:.0f}万)，主动卖盘占比{diff_pct:.1f}%", "type": "bear"})
            score -= 8
        score = max(0, min(100, score))
        signal = "偏多" if score >= 60 else "偏空" if score <= 40 else "中性"
        return {"score": score, "signal": signal, "details": details, "来源": "内外盘估算"}

    # 主力净流入
    main_net = fund.get('主力净流入-净额', 0)
    main_pct = fund.get('主力净流入-净占比', 0)
    super_net = fund.get('超大单净流入-净额', 0)
    large_net = fund.get('大单净流入-净额', 0)
    mid_net = fund.get('中单净流入-净额', 0)
    small_net = fund.get('小单净流入-净额', 0)

    # 主力方向
    if main_net > 0:
        details.append({"item": f"主力净流入{main_net/10000:.0f}万(占比{main_pct:.1f}%)，机构看多", "type": "bull"})
        score += 15
    elif main_net < 0:
        details.append({"item": f"主力净流出{abs(main_net)/10000:.0f}万(占比{abs(main_pct):.1f}%)，机构看空", "type": "bear"})
        score -= 15

    # 超大单
    if super_net > 0:
        details.append({"item": f"超大单净流入{super_net/10000:.0f}万，大资金入场", "type": "bull"})
        score += 8
    elif super_net < 0:
        details.append({"item": f"超大单净流出{abs(super_net)/10000:.0f}万，大资金离场", "type": "bear"})
        score -= 8

    # 散户 vs 机构对倒
    if small_net < 0 and main_net > 0:
        details.append({"item": "散户离场+主力吸筹，典型的主力吃货信号", "type": "bull"})
        score += 10
    elif small_net > 0 and main_net < 0:
        details.append({"item": "散户接盘+主力出货，警惕派发风险", "type": "bear"})
        score -= 10

    # 超大单与大单分歧
    if super_net < 0 and large_net > 0:
        details.append({"item": "超大单流出+大单流入，资金面分歧，需关注后续走向", "type": "neutral"})
    elif super_net > 0 and large_net < 0:
        details.append({"item": "超大单护盘+大单流出，机构托底但分歧犹存", "type": "neutral"})

    # 涨跌与资金背离
    pct = float(quote.get('涨跌幅', 0))
    if pct > 3 and main_net < 0:
        details.append({"item": f"⚠️ 涨{pct:.1f}%但主力流出，诱多风险", "type": "bear"})
        score -= 8
    elif pct < -3 and main_net > 0:
        details.append({"item": f"⚠️ 跌{abs(pct):.1f}%但主力逆势买入，护盘信号", "type": "bull"})
        score += 8

    score = max(0, min(100, score))
    signal = "偏多" if score >= 60 else "偏空" if score <= 40 else "中性"
    return {"score": score, "signal": signal, "details": details, "来源": "东方财富"}


def _analyze_message(news_data, quote):
    """消息面分析: 新闻情绪/关键事件"""
    score = 50
    details = []
    news = news_data.get('新闻', [])
    source = news_data.get('_source', 'none')

    if not news:
        details.append({"item": "暂无相关新闻数据，消息面中性", "type": "neutral"})
        return {"score": 50, "signal": "中性", "details": details, "新闻": [], "来源": source}

    # 关键词情绪分析
    bull_kw = ['增持', '回购', '利好', '突破', '新高', '业绩大增', '超预期', '获批',
                '中标', '签约', '扩产', '涨价', '分红', '并购', '重组', '翻倍', '强势']
    bear_kw = ['减持', '质押', '利空', '破位', '新低', '亏损', '下滑', '违规', '处罚',
                '警示', '退市', '暴雷', '诉讼', '债务', '违约', '下降', '缩减', '停工']

    bull_count = 0; bear_count = 0
    important_news = []

    for item in news:
        title = item.get('标题', '')
        content = item.get('内容', '')
        text = title + content
        has_bull = any(kw in text for kw in bull_kw)
        has_bear = any(kw in text for kw in bear_kw)
        if has_bull: bull_count += 1
        if has_bear: bear_count += 1
        # 提取重要新闻
        if has_bull or has_bear:
            important_news.append({
                "标题": title[:40],
                "情绪": "正面" if has_bull and not has_bear else "负面" if has_bear and not has_bull else "混合",
                "时间": item.get('时间', '')[:10],
            })

    if bull_count > bear_count + 1:
        details.append({"item": f"近期新闻偏正面({bull_count}条利好 vs {bear_count}条利空)", "type": "bull"})
        score += 12
    elif bear_count > bull_count + 1:
        details.append({"item": f"近期新闻偏负面({bear_count}条利空 vs {bull_count}条利好)", "type": "bear"})
        score -= 12
    else:
        details.append({"item": f"近期新闻多空交织({bull_count}条利好 / {bear_count}条利空)", "type": "neutral"})

    # 识别关键事件
    for item in important_news[:3]:
        emo = "正面" if item['情绪'] == '正面' else "负面" if item['情绪'] == '负面' else "混合"
        t = "bull" if item['情绪'] == '正面' else "bear" if item['情绪'] == '负面' else "neutral"
        details.append({"item": f"[{emo}] {item['标题']}", "type": t})

    score = max(0, min(100, score))
    signal = "偏多" if score >= 60 else "偏空" if score <= 40 else "中性"
    return {"score": score, "signal": signal, "details": details, "新闻": important_news[:5], "来源": source}


def _analyze_policy(quote, news_data):
    """政策面分析: 行业政策/宏观环境"""
    score = 50
    details = []

    # 从新闻中提取政策关键词
    news = news_data.get('新闻', [])
    policy_kw = ['政策', '规划', '扶持', '补贴', '改革', '监管', '规范', '自贸',
                  '试点', '示范', '战略', '振兴', '扩大内需', '新基建', '双碳', '碳中和',
                  '新能源', '半导体', '人工智能', '数字经济', '国产替代', '专精特新',
                  '禁止', '限制', '整顿', '约谈', '反垄断', '收紧']
    positive_policy = ['扶持', '补贴', '规划', '战略', '振兴', '试点', '示范', '扩大',
                       '新基建', '双碳', '国产替代', '专精特新', '数字经济']
    negative_policy = ['禁止', '限制', '整顿', '约谈', '反垄断', '收紧', '规范', '监管']

    pos_count = 0; neg_count = 0
    for item in news:
        text = item.get('标题', '') + item.get('内容', '')
        has_pos = any(kw in text for kw in positive_policy)
        has_neg = any(kw in text for kw in negative_policy)
        if has_pos: pos_count += 1
        if has_neg: neg_count += 1

    if pos_count > 0:
        details.append({"item": f"检测到{pos_count}条政策利好信号", "type": "bull"})
        score += pos_count * 5
    if neg_count > 0:
        details.append({"item": f"检测到{neg_count}条政策监管/收紧信号", "type": "bear"})
        score -= neg_count * 5

    if pos_count == 0 and neg_count == 0:
        details.append({"item": "近期无显著行业政策信号，政策面中性", "type": "neutral"})

    score = max(0, min(100, score))
    signal = "偏多" if score >= 60 else "偏空" if score <= 40 else "中性"
    return {"score": score, "signal": signal, "details": details}


def _analyze_sentiment(quote, df):
    """情绪面分析: 量比/波动/涨跌/内外盘"""
    score = 50
    details = []

    pct = float(quote.get('涨跌幅', 0))

    # 涨跌情绪
    if pct > 5:
        details.append({"item": f"大涨{pct:.1f}%，市场情绪亢奋，追高需谨慎", "type": "bear"})
        score += 8  # 短期亢奋但可能过热
    elif pct > 2:
        details.append({"item": f"上涨{pct:.1f}%，市场情绪偏暖", "type": "bull"})
        score += 12
    elif pct < -5:
        details.append({"item": f"大跌{abs(pct):.1f}%，恐慌情绪蔓延，关注超跌反弹", "type": "neutral"})
        score -= 5
    elif pct < -2:
        details.append({"item": f"下跌{abs(pct):.1f}%，市场情绪偏冷", "type": "bear"})
        score -= 12

    # 内外盘情绪
    outer = quote.get('外盘', 0); inner = quote.get('内盘', 0)
    if outer > 0 and inner > 0:
        ratio = outer / (outer + inner)
        if ratio > 0.6:
            details.append({"item": f"主动买盘占比{ratio*100:.0f}%，买方情绪占优", "type": "bull"})
            score += 8
        elif ratio < 0.4:
            details.append({"item": f"主动卖盘占比{(1-ratio)*100:.0f}%，卖方情绪占优", "type": "bear"})
            score -= 8

    # 波动率情绪
    if df is not None and len(df) >= 20:
        vol20 = close_pct_vol = df['close'].pct_change().rolling(20).std().iloc[-1] * np.sqrt(252) * 100
        if vol20 > 50:
            details.append({"item": f"年化波动率{vol20:.0f}%，市场极度波动，情绪不稳", "type": "bear"})
            score -= 6
        elif vol20 < 20:
            details.append({"item": f"年化波动率{vol20:.0f}%，市场平稳，情绪稳定", "type": "neutral"})

    # 成交额情绪
    amount_yi = float(quote.get('成交额亿', 0))
    if amount_yi > 0:
        if pct > 0 and amount_yi > 5:
            details.append({"item": f"放量上涨(成交{amount_yi:.1f}亿)，资金积极参与", "type": "bull"})
            score += 5
        elif pct < 0 and amount_yi > 5:
            details.append({"item": f"放量下跌(成交{amount_yi:.1f}亿)，抛压沉重", "type": "bear"})
            score -= 5

    score = max(0, min(100, score))
    signal = "偏多" if score >= 60 else "偏空" if score <= 40 else "中性"
    return {"score": score, "signal": signal, "details": details}


def _synthesize(scores, tech, capital, message, policy, sentiment, quote):
    """综合研判: 多空观点 + 核心矛盾 + 短期/中期结论"""
    # 加权综合分 (技术面权重最高)
    weights = {"技术面": 0.30, "资金面": 0.25, "消息面": 0.15, "政策面": 0.10, "情绪面": 0.20}
    composite = sum(scores[k] * weights[k] for k in weights)

    # 收集多空观点
    bull_points = []; bear_points = []
    for dim_name, dim_data in [("技术面", tech), ("资金面", capital), ("消息面", message),
                                ("政策面", policy), ("情绪面", sentiment)]:
        for d in dim_data.get('details', []):
            if d['type'] == 'bull' and len(bull_points) < 3:
                bull_points.append(f"[{dim_name}] {d['item']}")
            elif d['type'] == 'bear' and len(bear_points) < 3:
                bear_points.append(f"[{dim_name}] {d['item']}")

    # 确保每侧至少1条
    if not bull_points: bull_points.append("暂无明确多头信号")
    if not bear_points: bear_points.append("暂无明确空头信号")

    # 核心矛盾
    if composite >= 60:
        contradiction = f"多方主导：多头信号集中({len(bull_points)}条)，短期趋势偏强，但需关注{'、'.join(bear_points[:2])}的压制"
    elif composite <= 40:
        contradiction = f"空方主导：空头信号集中({len(bear_points)}条)，短期承压，关注{'、'.join(bull_points[:2])}是否形成支撑"
    else:
        contradiction = f"多空交织：多头({len(bull_points)}条)与空头({len(bear_points)}条)信号接近，方向不明朗，需等待信号强化"

    # 短期结论(1-3天)
    if composite >= 60:
        short_dir = "偏多"; short_logic = "技术面和资金面共振偏多，短线可关注回调买入机会"
    elif composite <= 40:
        short_dir = "偏空"; short_logic = "技术面和资金面共振偏空，短线建议观望或减仓"
    else:
        short_dir = "震荡"; short_logic = "多空信号交织，短线无明确方向，建议等待右侧信号"

    # 中期结论(1-4周)
    if composite >= 55:
        mid_dir = "偏多"; mid_logic = "中期趋势有望延续偏多格局，关注均线支撑"
    elif composite <= 45:
        mid_dir = "偏空"; mid_logic = "中期趋势偏弱，需等待技术面和资金面企稳信号"
    else:
        mid_dir = "中性"; mid_logic = "中期方向不明，建议低仓观望，等待趋势确认"

    return {
        "综合评分": round(composite, 1),
        "多头观点": bull_points[:3],
        "空头观点": bear_points[:3],
        "核心矛盾": contradiction,
        "短期": {"方向": short_dir, "时间": "1-3天", "逻辑": short_logic},
        "中期": {"方向": mid_dir, "时间": "1-4周", "逻辑": mid_logic},
    }


def _kline_to_json(df):
    """K线DataFrame转JSON"""
    return [
        {"date": row['date'].strftime('%Y-%m-%d'), "open": float(row['open']),
         "close": float(row['close']), "high": float(row['high']),
         "low": float(row['low']), "volume": float(row['volume']),
         "pct": float(row.get('pct_change', 0))}
        for _, row in df.tail(90).iterrows()
    ]


# ============================================================
# Flask 路由
# ============================================================
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/search')
def api_search():
    return jsonify(search_stocks(request.args.get('q', '').strip()))

@app.route('/api/quote/<code>')
def api_quote(code):
    return jsonify(get_quote(code.strip().zfill(6)))

@app.route('/api/technical/<code>')
def api_technical(code):
    code = code.strip().zfill(6)
    df, src = get_kline(code)
    if df is None or len(df) < 30:
        return jsonify({"error": "K线数据不足"})
    # 兼容旧接口
    tech = _analyze_technical(get_quote(code), df)
    result = {k: v for k, v in tech.items() if k != 'details'}
    result.update({"K线历史": _kline_to_json(df), "K线数据源": src,
                    "MACD信号": tech['details'][1]['item'] if len(tech['details']) > 1 else '',
                    "RSI信号": tech['details'][2]['item'] if len(tech['details']) > 2 else '',
                    "均线排列": tech['details'][0]['item'] if tech['details'] else ''})
    return jsonify(result)

@app.route('/api/fund_flow/<code>')
def api_fund(code):
    return jsonify(get_fund_flow(code.strip().zfill(6)))

@app.route('/api/sectors')
def api_sectors():
    return jsonify(get_sectors())

@app.route('/api/analysis/<code>')
def api_analysis(code):
    """五维深度分析主接口"""
    return jsonify(analyze_stock(code.strip().zfill(6)))


if __name__ == '__main__':
    print("=" * 60)
    print("  📊 股票深度分析系统  (五维交叉验证)")
    print("  http://localhost:5001")
    print("  技术面 | 资金面 | 消息面 | 政策面 | 情绪面")
    print("  按 Ctrl+C 停止")
    print("=" * 60)
    app.run(host='0.0.0.0', port=5001, debug=False, threaded=True)
