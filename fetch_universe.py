#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
自動抓取全美股 universe（NASDAQ + NYSE + AMEX）
================================================
資料源：rreichel3/US-Stock-Symbols（每日自動更新，免費，無需 API key）
呢個 JSON 本身已經含 marketCap / lastsale / volume，
所以可以喺「下載歷史價格之前」先粗篩，大幅減少要抓嘅股票數。

流程：
  約 7000 隻全市場（每日重新抓，新上市/除牌自動跟到）
    → 剔走權證/單位/優先股（非純字母代號）        約 6800
    → 市值 >= 門檻（市值變動慢，用快照安全）      約 4400  ← 落呢個數抓真實價格
    → 用真實 EOD 資料計 EMA、價×量、波動率 → 最終篩選同分級

設計原則：
  市值變動慢 → 可以用快照粗篩，安全。
  成交額同波動率變動快 → 唔做粗篩，一律用真實 EOD 資料判斷，
  咁先捉到「平時靜、今日突然爆量」嗰種股票。
"""

import json, re, urllib.request

BASE = "https://raw.githubusercontent.com/rreichel3/US-Stock-Symbols/main"
EXCHANGES = ["nasdaq", "nyse", "amex"]

# 粗篩參數
PREFILTER_MKTCAP = 100e6   # 同你 screener 一致
PREFILTER_DVOL   = 0       # 0 = 唔做成交額粗篩（建議）
                           # 成交額同波動率係最善變嘅條件，一律交俾真實 EOD 資料判斷，
                           # 咁先唔會漏咗「平時靜、今日突然爆量」嗰種股票。
                           # 若想慳時間可設 10e6~20e6，但有機會漏股。
SYMBOL_PATTERN   = re.compile(r"^[A-Z]{1,5}$")   # 剔走 .W / .U / 優先股等


def _num(s):
    """把 '$12.34' / '1,234' / '' 轉成 float。"""
    if s is None:
        return 0.0
    t = str(s).replace("$", "").replace(",", "").replace("%", "").strip()
    if not t or t in ("--", "N/A"):
        return 0.0
    try:
        return float(t)
    except ValueError:
        return 0.0


def fetch_universe(mktcap_min=PREFILTER_MKTCAP, dvol_min=PREFILTER_DVOL, verbose=True):
    """
    回傳 (tickers list, marketcap dict)。
    marketcap dict 可直接俾主程式用，唔使再逐隻查市值（慳幾千個 request）。
    """
    rows = []
    for ex in EXCHANGES:
        url = f"{BASE}/{ex}/{ex}_full_tickers.json"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=60) as r:
                data = json.loads(r.read().decode())
            rows.extend(data)
            if verbose:
                print(f"  {ex.upper()}: {len(data)} 隻")
        except Exception as e:
            print(f"  ⚠️ {ex} 抓取失敗：{e}")

    if not rows:
        raise RuntimeError("抓唔到任何 universe 資料")

    seen, tickers, caps = set(), [], {}
    n_sym = n_cap = 0
    for r in rows:
        sym = str(r.get("symbol", "")).strip().upper()
        if not sym or sym in seen:
            continue
        seen.add(sym)
        if not SYMBOL_PATTERN.match(sym):      # 剔走權證/單位/優先股
            continue
        n_sym += 1
        mc = _num(r.get("marketCap"))
        if mc < mktcap_min:
            continue
        n_cap += 1
        if dvol_min > 0:
            dv = _num(r.get("lastsale")) * _num(r.get("volume"))
            if dv < dvol_min:
                continue
        tickers.append(sym)
        caps[sym] = mc

    if verbose:
        if dvol_min > 0:
            print(f"  全市場 {len(seen)} → 有效代號 {n_sym} → 市值達標 {n_cap} "
                  f"→ 粗篩後 {len(tickers)} 隻")
        else:
            print(f"  全市場 {len(seen)} → 有效代號 {n_sym} "
                  f"→ 市值達標 {len(tickers)} 隻（成交額/波動率交俾真實資料判斷）")
    return tickers, caps


if __name__ == "__main__":
    t, c = fetch_universe()
    print(f"\n共 {len(t)} 隻")
    print("頭 20 隻:", ", ".join(t[:20]))
