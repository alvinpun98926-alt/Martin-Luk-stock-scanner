#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
EMA 三級 Watchlist 分類器（本地版 · yfinance）
================================================
一日 500+ 隻都得，冇 API key、冇每日上限。

邏輯同 HTML 版一致：
  抓 EOD 資料 → 自己計 9/21/50 EMA、價、價×量、週波動率
  → 過 4 條 screener 資格 → 按 EMA 排列分 Lead / Mediocre / Lag
  → 標出已唔符合資格（跌出 screener）嘅股票。

用法
----
1. 裝套件：   pip install -U yfinance pandas
2. 開一個 tickers.txt，貼晒你嘅股票（逗號／空格／換行都得，
   可含 NASDAQ: 前綴，會自動清走）。
3. 跑：       python classify_watchlist.py
   或指定檔： python classify_watchlist.py my_list.txt
   或用管道： cat list.txt | python classify_watchlist.py -

輸出
----
- 終端機：每個 tier 嘅清單 + 統計
- out/watchlist_tiers.csv ：完整資料表
- out/lead.txt / mediocre.txt / lag.txt / dropped.txt ：純代號，方便貼返 TradingView
"""

import sys, time, math, csv
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import yfinance as yf
    import pandas as pd
except ImportError:
    sys.exit("缺套件。請先跑：  pip install -U yfinance pandas")

# ============================================================
#  參數（自己改）  —  對應 HTML 版進階參數
# ============================================================
MKTCAP_MIN  = 100e6     # 市值下限（USD）
DVOL_MIN    = 50e6      # 價×量 下限（USD）
VOL_MIN     = 3.5       # 週波動率下限（%）
VOL_METHOD  = "range"   # "range" = 近5日 (high-low)/low ; "adr" = 近5日平均日內幅度%
DVOL_BASIS  = "latest"  # "latest" = 最近一日價×量 ; "avg20" = 20日平均
EMA_FAST    = 9
EMA_MID     = 21
EMA_SLOW    = 50        # 你嘅分級條件用 50；若 Martin Luk 實際用 40 就改呢度
CHECK_MKTCAP = True     # False = 完全唔理市值（screener 上游已篩過嘅話可關掉提速）

PERIOD      = "1y"      # 抓一年日線，計 EMA50 + 20日平均綽綽有餘
CHUNK       = 100       # 每次 bulk download 嘅股票數
MAX_WORKERS = 6         # 抓市值嘅併發數（自動 universe 模式下用唔著）
PAUSE       = 1.0       # 每批之間停幾多秒（大量股票時避免被 Yahoo 限流）
# ============================================================


def parse_tickers(text):
    seen, out = set(), []
    for tok in text.replace(",", " ").replace(";", " ").split():
        s = tok.strip().upper()
        if ":" in s:
            s = s.split(":")[-1]
        s = "".join(ch for ch in s if ch.isalnum() or ch in ".-")
        if s and s not in seen:
            seen.add(s); out.append(s)
    return out


def load_tickers():
    # 管道輸入
    if len(sys.argv) > 1 and sys.argv[1] == "-":
        return parse_tickers(sys.stdin.read())
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("tickers.txt")
    if not path.exists():
        sys.exit(f"搵唔到 {path} 。請開一個 tickers.txt 貼晒你嘅股票，或者：python {Path(sys.argv[0]).name} 你嘅檔案.txt")
    return parse_tickers(path.read_text(encoding="utf-8"))


def ema_last(closes, n):
    """SMA 起手嘅標準 EMA，最後一個值。同 HTML 版一致。"""
    closes = [float(c) for c in closes if c == c]  # 去 NaN
    if len(closes) < n:
        return None
    k = 2.0 / (n + 1)
    e = sum(closes[:n]) / n
    for c in closes[n:]:
        e = c * k + e * (1 - k)
    return e


def weekly_vol(df):
    last5 = df.tail(5)
    if VOL_METHOD == "adr":
        lows = last5["Low"]
        if (lows <= 0).any():
            return 0.0
        return float(((last5["High"] / lows) - 1).mean() * 100)
    hi, lo = float(last5["High"].max()), float(last5["Low"].min())
    return (hi - lo) / lo * 100 if lo > 0 else 0.0


def dollar_vol(df):
    if DVOL_BASIS == "avg20":
        tail = df.tail(20)
        return float((tail["Close"] * tail["Volume"]).mean())
    return float(df["Close"].iloc[-1] * df["Volume"].iloc[-1])


def fmt_big(v):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "—"
    if v >= 1e9:  return f"{v/1e9:.2f}B"
    if v >= 1e6:  return f"{v/1e6:.1f}M"
    if v >= 1e3:  return f"{v/1e3:.0f}K"
    return str(int(v))


def fetch_market_caps(tickers):
    """用 fast_info 併發抓市值。"""
    caps = {}
    def one(sym):
        try:
            fi = yf.Ticker(sym).fast_info
            mc = None
            for key in ("market_cap", "marketCap"):
                try:
                    v = fi[key]
                except Exception:
                    v = getattr(fi, key, None)
                if v:
                    mc = float(v); break
            return sym, mc
        except Exception:
            return sym, None
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        for sym, mc in ex.map(one, tickers):
            caps[sym] = mc
    return caps


def download_history(tickers, verbose=True):
    """
    分批 bulk download，回傳 {sym: DataFrame}。
    針對大量股票（4000+）做咗加固：分批、重試、失敗批次縮細再試。
    """
    frames = {}
    total = len(tickers)
    batches = [tickers[i:i + CHUNK] for i in range(0, total, CHUNK)]

    def grab(batch, attempt=1):
        """抓一批；失敗會重試，再唔得就拆細。"""
        try:
            data = yf.download(batch, period=PERIOD, interval="1d",
                               auto_adjust=True, group_by="ticker",
                               threads=True, progress=False)
        except Exception as e:
            if attempt < 3:
                time.sleep(3 * attempt)
                return grab(batch, attempt + 1)
            if len(batch) > 10:                      # 拆細再試
                mid = len(batch) // 2
                grab(batch[:mid]); time.sleep(1); grab(batch[mid:])
                return
            print(f"    放棄呢批（{len(batch)} 隻）：{e}")
            return

        if len(batch) == 1:
            df = data.dropna(how="all")
            if not df.empty:
                frames[batch[0]] = df
            return
        for sym in batch:
            try:
                df = data[sym].dropna(how="all")
                if not df.empty:
                    frames[sym] = df
            except Exception:
                pass

    for i, batch in enumerate(batches, 1):
        if verbose:
            done = min(i * CHUNK, total)
            print(f"  抓價量 {done} / {total}　（第 {i}/{len(batches)} 批）", flush=True)
        grab(batch)
        time.sleep(PAUSE)          # 對 Yahoo 客氣啲，減少被限流

    if verbose:
        print(f"  成功取得 {len(frames)} / {total} 隻")
    return frames


def classify(sym, df, mktcap):
    if df is None or len(df) == 0:
        return {"sym": sym, "error": True, "msg": "搵唔到資料 / 代號錯"}

    # 只保留 Close/High/Low/Volume 都有效嘅列（停牌、新上市會有空值）
    try:
        df = df.dropna(subset=["Close", "High", "Low", "Volume"])
    except Exception:
        return {"sym": sym, "error": True, "msg": "資料格式異常"}

    if len(df) < EMA_SLOW:
        return {"sym": sym, "error": True,
                "msg": f"資料不足（{len(df)} 根有效 K，需 ≥ {EMA_SLOW}）"}

    closes = [float(c) for c in df["Close"].tolist()]
    price  = float(df["Close"].iloc[-1])
    e1 = ema_last(closes, EMA_FAST)
    e2 = ema_last(closes, EMA_MID)
    e3 = ema_last(closes, EMA_SLOW)

    if e1 is None or e2 is None or e3 is None or price <= 0:
        return {"sym": sym, "error": True, "msg": "EMA 計唔到（有效資料不足）"}

    try:
        dvol = dollar_vol(df)
        wvol = weekly_vol(df)
    except Exception:
        return {"sym": sym, "error": True, "msg": "成交額/波動率計唔到"}

    if not (math.isfinite(dvol) and math.isfinite(wvol)):
        return {"sym": sym, "error": True, "msg": "成交額/波動率數值異常"}

    reasons = []
    if CHECK_MKTCAP:
        if mktcap is None:
            reasons.append("市值資料缺(未當作不合格)")  # 缺資料只提示，唔判出局
        elif mktcap < MKTCAP_MIN:
            reasons.append(f"市值 {fmt_big(mktcap)} < {fmt_big(MKTCAP_MIN)}")
    if not (price > e2):
        reasons.append(f"價 ≤ EMA{EMA_MID}")
    if not (dvol > DVOL_MIN):
        reasons.append(f"價×量 {fmt_big(dvol)} ≤ {fmt_big(DVOL_MIN)}")
    if not (wvol > VOL_MIN):
        reasons.append(f"波動率 {wvol:.2f}% ≤ {VOL_MIN}%")

    # 「市值缺」唔當失格
    hard = [r for r in reasons if "未當作不合格" not in r]
    qualified = len(hard) == 0

    if   e1 > e2 > e3: tier = "lead"
    elif e1 < e2 < e3: tier = "lag"
    else:              tier = "mediocre"

    return {"sym": sym, "error": False, "price": price,
            "e1": e1, "e2": e2, "e3": e3,
            "mktcap": mktcap, "dvol": dvol, "wvol": wvol,
            "qualified": qualified, "reasons": reasons, "tier": tier,
            "ext": (price / e2 - 1) * 100}


def main():
    tickers = load_tickers()
    if not tickers:
        sys.exit("冇有效代號。")
    print(f"\n共 {len(tickers)} 隻\n")

    frames = download_history(tickers)
    caps = fetch_market_caps(tickers) if CHECK_MKTCAP else {s: None for s in tickers}

    rows = [classify(s, frames.get(s), caps.get(s)) for s in tickers]

    ok       = [r for r in rows if not r["error"]]
    errs     = [r for r in rows if r["error"]]
    qual     = [r for r in ok if r["qualified"]]
    dropped  = [r for r in ok if not r["qualified"]]
    by_ext   = lambda r: -r["ext"]
    lead = sorted([r for r in qual if r["tier"] == "lead"], key=by_ext)
    med  = sorted([r for r in qual if r["tier"] == "mediocre"], key=by_ext)
    lag  = sorted([r for r in qual if r["tier"] == "lag"], key=by_ext)
    dropped.sort(key=by_ext)

    # ---- 終端機輸出 ----
    def show(title, group):
        print(f"\n{'─'*64}\n{title}　({len(group)})\n{'─'*64}")
        if not group:
            print("  （無）"); return
        print(f"  {'代號':<8}{'價':>10}{'EMA'+str(EMA_FAST):>10}{'EMA'+str(EMA_MID):>10}"
              f"{'EMA'+str(EMA_SLOW):>10}{'市值':>9}{'價×量':>9}{'波動率':>9}   備註")
        for r in group:
            note = "✓" if r["qualified"] else "；".join(r["reasons"])
            print(f"  {r['sym']:<8}{r['price']:>10.2f}{r['e1']:>10.2f}{r['e2']:>10.2f}"
                  f"{r['e3']:>10.2f}{fmt_big(r['mktcap']):>9}{fmt_big(r['dvol']):>9}"
                  f"{r['wvol']:>8.2f}%   {note}")

    show("LEAD 領先 — 完美多頭", lead)
    show("MEDIOCRE 普通 — 排列混亂", med)
    show("LAG 落後 — 空頭排列", lag)
    show("已唔符合資格 — 跌出 screener", dropped)
    if errs:
        print(f"\n{'─'*64}\n抓取失敗 / 代號錯　({len(errs)})\n{'─'*64}")
        for r in errs:
            print(f"  {r['sym']:<8} {r['msg']}")

    print(f"\n{'='*64}")
    print(f"  Lead {len(lead)}  ·  Mediocre {len(med)}  ·  Lag {len(lag)}  "
          f"·  已出局 {len(dropped)}  ·  錯誤 {len(errs)}")
    print(f"{'='*64}")

    # ---- 檔案輸出 ----
    out = Path("out"); out.mkdir(exist_ok=True)
    groups = {"lead": lead, "mediocre": med, "lag": lag, "dropped": dropped}
    for name, g in groups.items():
        # 逗號分隔一行，可直接貼落 TradingView watchlist（例如 NVDA, GOOG, TSLA）
        (out / f"{name}.txt").write_text(", ".join(r["sym"] for r in g), encoding="utf-8")

    with open(out / "watchlist_tiers.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["symbol", "tier", "qualified", "price",
                    f"ema{EMA_FAST}", f"ema{EMA_MID}", f"ema{EMA_SLOW}",
                    "marketCap", "dollarVol", "weeklyVol%", "ext%", "reasons"])
        for r in lead + med + lag + dropped:
            w.writerow([r["sym"], r["tier"], "Y" if r["qualified"] else "N",
                        f"{r['price']:.4f}", f"{r['e1']:.4f}", f"{r['e2']:.4f}",
                        f"{r['e3']:.4f}", int(r["mktcap"] or 0), int(r["dvol"]),
                        f"{r['wvol']:.2f}", f"{r['ext']:.2f}", "；".join(r["reasons"])])

    print(f"\n已寫入 out/watchlist_tiers.csv 及 out/lead|mediocre|lag|dropped.txt\n")


if __name__ == "__main__":
    main()
