#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日自動 Scanner → Telegram
============================
跑 Martin Luk screener 邏輯，然後分三個訊息（Lead / Mediocre / Lag）
傳去 Telegram，方便直接 copy & paste 落 TradingView。

需要設定：下面 BOT_TOKEN 同 CHAT_ID（README_TELEGRAM.txt 有教點攞）。

用法：  python daily_scan_telegram.py
"""

import sys, os, io, time, math, csv, json, urllib.request, urllib.parse
from pathlib import Path
from datetime import datetime, timezone, timedelta

TPE = timezone(timedelta(hours=8))   # 台北/香港/澳門時區


def now_tpe():
    """一律用台北時間判斷星期（GitHub 伺服器係 UTC）。"""
    return datetime.now(TPE)

# 直接重用主程式嘅所有邏輯同參數
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import classify_watchlist as cw
except ImportError:
    sys.exit("搵唔到 classify_watchlist.py，請確保兩個檔喺同一個資料夾。")


# ============================================================
#  Telegram 設定（填你自己嘅）
#  也可以改用環境變數 TG_BOT_TOKEN / TG_CHAT_ID
# ============================================================
BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
CHAT_ID   = os.environ.get("TG_CHAT_ID", "")

MAX_PER_MSG = 3500   # Telegram 單則上限 4096 字，留啲位；超過會自動分段
SKIP_WEEKEND = True  # 美股週末自動跳過（唔使抓資料，慳時間）
NOTIFY_HOLIDAY = True  # 休市／冇新資料時，仍然傳一則通知（False = 靜靜哋唔傳）
# ============================================================


def tg_send(text):
    """傳一則訊息去 Telegram。回傳 True/False。"""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": CHAT_ID,
        "text": text,
        "disable_web_page_preview": "true",
    }).encode()
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, data=data)
            with urllib.request.urlopen(req, timeout=30) as r:
                res = json.loads(r.read().decode())
                if res.get("ok"):
                    return True
                print("  Telegram 回覆錯誤：", res.get("description"))
        except Exception as e:
            print(f"  傳送失敗（第 {attempt+1} 次）：{e}")
            time.sleep(3)
    return False


def send_chunked(header, symbols):
    """一個 tier 一則訊息；太長就自動分段（TradingView 逗號格式）。"""
    if not symbols:
        tg_send(f"{header}\n（今日冇）")
        return

    body = ", ".join(symbols)
    if len(body) <= MAX_PER_MSG:
        tg_send(f"{header}\n\n{body}")
        return

    # 分段：按逗號切，唔會切爛代號
    parts, cur = [], ""
    for s in symbols:
        add = (", " if cur else "") + s
        if len(cur) + len(add) > MAX_PER_MSG:
            parts.append(cur); cur = s
        else:
            cur += add
    if cur:
        parts.append(cur)
    for i, p in enumerate(parts, 1):
        tg_send(f"{header}　({i}/{len(parts)})\n\n{p}")
        time.sleep(1)


def log_run(note):
    """寫執行紀錄，方便查邊日跑咗、邊日休市。"""
    out = Path(__file__).resolve().parent / "out"
    out.mkdir(exist_ok=True)
    with open(out / "run_log.txt", "a", encoding="utf-8") as f:
        f.write(f"{now_tpe():%Y-%m-%d %H:%M}　{note}\n")


def expected_session_date(now=None):
    """
    台北 07:30 跑 → 睇緊嘅係前一晚美股。
    回傳「應該有資料嘅最近一個美股交易日」（週末往回推）。
    只處理星期，假期唔靠清單 —— 靠實際資料比對（見 check_freshness）。
    """
    from datetime import timedelta
    now = now or now_tpe()
    d = now.date() - timedelta(days=1)      # 昨日（美股嗰日）
    while d.weekday() >= 5:                 # 5=六, 6=日 → 往回推到週五
        d -= timedelta(days=1)
    return d


def latest_data_date(frames):
    """
    喺已抓到嘅資料中，搵最新一根 K 嘅日期（取眾數最大者，避免個別股停牌拖低）。
    回傳 datetime.date 或 None。
    """
    dates = []
    for df in frames.values():
        try:
            dates.append(df.index[-1].date())
        except Exception:
            pass
    return max(dates) if dates else None


def last_sent_date():
    """讀返上次成功傳送嘅資料日期（避免重覆出同一日清單）。"""
    f = Path(__file__).resolve().parent / "out" / ".last_session"
    try:
        from datetime import date
        return date.fromisoformat(f.read_text(encoding="utf-8").strip())
    except Exception:
        return None


def save_sent_date(d):
    out = Path(__file__).resolve().parent / "out"
    out.mkdir(exist_ok=True)
    (out / ".last_session").write_text(d.isoformat(), encoding="utf-8")


def check_freshness(frames, now=None):
    """
    判斷今次跑到嘅係咪「未出過」嘅新資料。
    以實際資料日期為準（唔靠假期清單），並同上次傳送過嘅日期比對，
    所以公眾假期、臨時休市、資料源未更新都自動涵蓋。
    回傳 (is_fresh, data_date, reason_text)
    """
    now = now or now_tpe()
    actual = latest_data_date(frames)
    if actual is None:
        return False, None, "抓唔到任何資料"

    prev = last_sent_date()
    if prev is not None and actual <= prev:
        wd = now.weekday()
        if wd == 6:
            reason = "美股星期六冇開市"
        elif wd == 0:
            reason = "美股星期日冇開市"
        else:
            reason = "美股休市（公眾假期）或資料源未更新"
        return False, actual, reason

    if prev is None:                      # 第一次跑，未有紀錄
        if actual < expected_session_date(now):
            return False, actual, "美股休市或資料源未更新"

    return True, actual, ""


def main():
    if not BOT_TOKEN or not CHAT_ID:
        sys.exit("未設定 TG_BOT_TOKEN / TG_CHAT_ID。"
                 "喺 GitHub repo → Settings → Secrets and variables → Actions 加入。")

    stamp = now_tpe().strftime("%Y-%m-%d")
    print(f"[{stamp}] 開始每日掃描…")

    # ---- 讀 tickers ----
    tpath = Path(__file__).resolve().parent / "tickers.txt"
    if not tpath.exists():
        tg_send(f"⚠️ Scanner 出錯（{stamp}）：搵唔到 tickers.txt")
        sys.exit("搵唔到 tickers.txt")
    tickers = cw.parse_tickers(tpath.read_text(encoding="utf-8"))
    if not tickers:
        tg_send(f"⚠️ Scanner 出錯（{stamp}）：tickers.txt 冇有效代號")
        sys.exit("tickers.txt 冇有效代號")
    print(f"共 {len(tickers)} 隻")

    # ---- 週末快速判斷：台北星期日/星期一 = 美股週末，唔使抓資料 ----
    wd = now_tpe().weekday()          # 0=一 ... 6=日
    if SKIP_WEEKEND and wd in (0, 6):
        which = "星期六" if wd == 6 else "星期日"
        if NOTIFY_HOLIDAY:
            tg_send(f"💤 休市　{stamp}\n\n美股{which}冇開市，今日冇新資料。")
        print(f"美股{which}休市，跳過。")
        log_run(f"休市（美股{which}）")
        return

    # ---- 抓資料 + 分類（重用主程式邏輯）----
    try:
        frames = cw.download_history(tickers)
        caps = cw.fetch_market_caps(tickers) if cw.CHECK_MKTCAP else {s: None for s in tickers}
        rows = [cw.classify(s, frames.get(s), caps.get(s)) for s in tickers]
    except Exception as e:
        tg_send(f"⚠️ Scanner 出錯（{stamp}）：{e}")
        raise

    # ---- 檢查資料新鮮度：假期／未更新自動偵測 ----
    is_fresh, data_date, reason = check_freshness(frames)
    if not is_fresh:
        dd = data_date.strftime("%Y-%m-%d") if data_date else "—"
        if NOTIFY_HOLIDAY:
            tg_send(f"💤 冇新資料　{stamp}\n\n{reason}\n"
                    f"最新資料日期：{dd}\n（今日唔會出清單，避免同昨日重覆）")
        print(f"冇新資料：{reason}（最新 {dd}）")
        log_run(f"冇新資料 - {reason}（最新 {dd}）")
        return

    session = data_date.strftime("%Y-%m-%d")

    ok      = [r for r in rows if not r["error"]]
    errs    = [r for r in rows if r["error"]]
    qual    = [r for r in ok if r["qualified"]]
    dropped = [r for r in ok if not r["qualified"]]
    by_ext  = lambda r: -r["ext"]
    lead = sorted([r for r in qual if r["tier"] == "lead"], key=by_ext)
    med  = sorted([r for r in qual if r["tier"] == "mediocre"], key=by_ext)
    lag  = sorted([r for r in qual if r["tier"] == "lag"], key=by_ext)

    syms = lambda g: [r["sym"] for r in g]

    # ---- 三則訊息 ----
    print("傳送去 Telegram…")
    send_chunked(f"🟢 LEAD 領先　{session}　({len(lead)})", syms(lead))
    time.sleep(1)
    send_chunked(f"🟡 MEDIOCRE 普通　{session}　({len(med)})", syms(med))
    time.sleep(1)
    send_chunked(f"⚪️ LAG 落後　{session}　({len(lag)})", syms(lag))

    # ---- 第四則：摘要（想關就將下面兩行加 # 註解）----
    time.sleep(1)
    tg_send(f"📊 摘要　{session}（收市資料）\n"
            f"掃描 {len(tickers)} 隻　·　Lead {len(lead)}　·　Mediocre {len(med)}"
            f"　·　Lag {len(lag)}\n已跌出資格 {len(dropped)}　·　抓取失敗 {len(errs)}")

    # ---- 同時照舊寫檔案（方便對照）----
    out = Path(__file__).resolve().parent / "out"
    out.mkdir(exist_ok=True)
    for name, g in {"lead": lead, "mediocre": med, "lag": lag, "dropped": dropped}.items():
        (out / f"{name}.txt").write_text(", ".join(syms(g)), encoding="utf-8")

    # ---- 執行紀錄（方便查邊日冇跑到）----
    save_sent_date(data_date)
    log_run(f"資料 {session}　掃描 {len(tickers)}　Lead {len(lead)}　"
            f"Med {len(med)}　Lag {len(lag)}　出局 {len(dropped)}　失敗 {len(errs)}")

    print("完成。")


if __name__ == "__main__":
    main()
