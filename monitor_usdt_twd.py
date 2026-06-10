import html
import os
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import urllib3


# =========================
# 基本設定
# =========================

# Discord Webhook URL 請放在 GitHub Actions Secrets：DISCORD_WEBHOOK_URL
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()

# 最近幾個平日用來計算「平日正常溢價」
ROLLING_WEEKDAYS = 20

# 抓 MAX 日 K 數量
MAX_K_LIMIT = 500

# 央行分頁數，每頁 40 筆，20 頁約 800 筆，足夠一年以上
CBC_MAX_PAGES = 20

# 是否只在週末通知
# False = 每天都監控
WEEKEND_ONLY_NOTIFY = False

# 是否在 console 顯示每次檢查結果
PRINT_EVERY_CHECK = True

# 央行網站在某些 Python/Windows 環境會 SSL 驗證失敗，因此只對央行頁面關閉驗證
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# =========================
# 告警門檻
# =========================
# rank 越高，訊號越強
SELL_ALERT_LEVELS = [
    {
        "name": "極端賣出訊號",
        "threshold": 0.30,
        "rank": 5,
        "emoji": "🚨",
        "suggestion": "異常溢價 >= 0.30，屬於極端訊號，建議立刻觀察 MAX 深度與成交量。",
    },
    {
        "name": "強賣出訊號",
        "threshold": 0.25,
        "rank": 4,
        "emoji": "🔥",
        "suggestion": "異常溢價 >= 0.25，屬於強訊號，可認真考慮分批賣出 USDT。",
    },
    {
        "name": "正式賣出訊號",
        "threshold": 0.20,
        "rank": 3,
        "emoji": "🟠",
        "suggestion": "異常溢價 >= 0.20，達正式告警門檻，可開始評估是否賣出。",
    },
    {
        "name": "注意訊號",
        "threshold": 0.15,
        "rank": 2,
        "emoji": "🟡",
        "suggestion": "異常溢價 >= 0.15，尚未到正式門檻，但值得關注。",
    },
    {
        "name": "觀察訊號",
        "threshold": 0.12,
        "rank": 1,
        "emoji": "👀",
        "suggestion": "異常溢價 >= 0.12，開始出現溢價，但通常不建議急著操作。",
    },
]


# =========================
# 工具函數
# =========================

def now_taipei() -> datetime:
    return datetime.now(ZoneInfo("Asia/Taipei"))


def is_weekend(dt: datetime) -> bool:
    # Monday=0, Sunday=6
    return dt.weekday() >= 5


def read_url_text(url: str, timeout: int = 20) -> str:
    """
    抓央行 HTML。
    央行網站在某些 Python 3.13 / Windows 環境會遇到 SSL 憑證問題，
    所以這裡使用 verify=False。
    """
    headers = {"User-Agent": "Mozilla/5.0"}
    r = requests.get(url, headers=headers, timeout=timeout, verify=False)
    r.raise_for_status()
    return r.text


def send_discord_message(content: str) -> None:
    if not DISCORD_WEBHOOK_URL:
        raise RuntimeError("DISCORD_WEBHOOK_URL 未設定，請到 GitHub Actions Secrets 新增同名 Secret。")

    payload = {"content": content}
    r = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=20)
    r.raise_for_status()
    print("[Discord] 已發送通知")


# =========================
# 資料抓取
# =========================

def fetch_max_ticker() -> dict:
    """
    抓 MAX USDT/TWD 即時 ticker。
    buy = 買一價，你賣 USDT 時可參考
    sell = 賣一價，你買回 USDT 時可參考
    """
    url = "https://max-api.maicoin.com/api/v2/tickers/usdttwd"
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    data = r.json()

    ticker = data.get("ticker", data)

    return {
        "buy": float(ticker["buy"]),
        "sell": float(ticker["sell"]),
        "last": float(ticker["last"]),
        "raw": data,
    }


def fetch_max_daily_k() -> pd.DataFrame:
    """
    抓 MAX USDT/TWD 日 K。
    MAX K 線格式通常是：
    [timestamp, open, high, low, close, volume]
    """
    url = f"https://max-api.maicoin.com/api/v2/k?market=usdttwd&period=1440&limit={MAX_K_LIMIT}"
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    data = r.json()

    df = pd.DataFrame(data, columns=["ts", "open", "high", "low", "close", "volume"])

    df["time"] = pd.to_datetime(df["ts"], unit="s", utc=True).dt.tz_convert("Asia/Taipei")
    df["date"] = pd.to_datetime(df["time"].dt.date).astype("datetime64[ns]")

    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col])

    df = df.sort_values("date").reset_index(drop=True)

    return df[["date", "open", "high", "low", "close", "volume"]]


def fetch_cbc_usdtwd_closing(max_pages: int = CBC_MAX_PAGES) -> pd.DataFrame:
    """
    抓央行 NT$/US$ Closing Rate。
    只需要 date + usd_twd。
    """
    rows = []

    for page in range(1, max_pages + 1):
        url = f"https://www.cbc.gov.tw/en/lp-700-2-{page}-40.html"
        raw_html = read_url_text(url)

        # 清掉 HTML tag
        text = re.sub(r"<[^>]+>", " ", raw_html)
        text = html.unescape(text)
        text = re.sub(r"\s+", " ", text)

        # 抓 2026/06/05 31.475 這種格式
        matches = re.findall(r"(\d{4}/\d{2}/\d{2})\s+(\d+\.\d+)", text)

        for d, rate in matches:
            rows.append({
                "date": pd.to_datetime(d),
                "usd_twd": float(rate),
            })

    if not rows:
        raise RuntimeError("沒有從央行頁面解析到任何 USD/TWD 資料，請檢查網頁格式或網址。")

    fx = pd.DataFrame(rows)
    fx = fx.drop_duplicates("date")
    fx["date"] = pd.to_datetime(fx["date"]).astype("datetime64[ns]")
    fx = fx.sort_values("date").reset_index(drop=True)

    return fx


# =========================
# 模型計算
# =========================

def build_model(max_df: pd.DataFrame, fx_df: pd.DataFrame) -> dict:
    """
    建立合理價模型：
    平日正常溢價 = 最近 ROLLING_WEEKDAYS 個平日的
                  MAX close - USD/TWD 的中位數

    合理價 = 最新央行 USD/TWD + 平日正常溢價
    """
    max_df = max_df.copy()
    fx_df = fx_df.copy()

    max_df["date"] = pd.to_datetime(max_df["date"]).astype("datetime64[ns]")
    fx_df["date"] = pd.to_datetime(fx_df["date"]).astype("datetime64[ns]")

    max_df = max_df.sort_values("date").reset_index(drop=True)
    fx_df = fx_df.sort_values("date").reset_index(drop=True)

    # 假日沒有央行資料，用前一個營業日匯率
    df = pd.merge_asof(
        max_df,
        fx_df,
        on="date",
        direction="backward",
    )

    df["weekday"] = df["date"].dt.weekday

    # 本地溢價
    df["local_premium_close"] = df["close"] - df["usd_twd"]

    # 只用週一到週五建立平日正常溢價
    weekday_df = df[df["weekday"] <= 4][["date", "local_premium_close"]].copy()

    weekday_df["normal_premium"] = (
        weekday_df["local_premium_close"]
        .rolling(ROLLING_WEEKDAYS, min_periods=5)
        .median()
        .shift(1)
    )

    valid_normal = weekday_df.dropna(subset=["normal_premium"]).copy()

    if valid_normal.empty:
        raise RuntimeError("平日正常溢價資料不足，請增加 MAX_K_LIMIT 或檢查資料來源。")

    latest_normal_premium = float(valid_normal.iloc[-1]["normal_premium"])

    latest_fx = fx_df.iloc[-1]
    latest_usd_twd = float(latest_fx["usd_twd"])
    latest_fx_date = latest_fx["date"].date()

    fair_price = latest_usd_twd + latest_normal_premium

    return {
        "normal_premium": latest_normal_premium,
        "usd_twd": latest_usd_twd,
        "usd_twd_date": latest_fx_date,
        "fair_price": fair_price,
        "history_df": df,
    }


def get_model() -> dict:
    print("[模型] 抓取 MAX 日 K 與央行 USD/TWD...")

    max_df = fetch_max_daily_k()
    fx_df = fetch_cbc_usdtwd_closing()
    model = build_model(max_df, fx_df)

    print("[模型] 更新完成")
    print(f"  最新央行 USD/TWD：{model['usd_twd']:.3f}，日期：{model['usd_twd_date']}")
    print(f"  平日正常溢價：{model['normal_premium']:.4f}")
    print(f"  模型合理價：{model['fair_price']:.4f}")

    return model


# =========================
# 訊號判斷
# =========================

def classify_sell_signal(abnormal_premium: float) -> dict:
    for level in SELL_ALERT_LEVELS:
        if abnormal_premium >= level["threshold"]:
            return level

    return {
        "name": "無訊號",
        "threshold": None,
        "rank": 0,
        "emoji": "⚪",
        "suggestion": "異常溢價尚未達觀察門檻。",
    }


def format_report(
    ticker: dict,
    model: dict,
    abnormal_sell_premium: float,
    abnormal_buy_premium: float,
    signal: dict,
) -> str:
    now = now_taipei()

    content = f"""
{signal['emoji']} **MAX USDT/TWD {signal['name']}**

時間：{now.strftime('%Y-%m-%d %H:%M:%S')} Asia/Taipei

MAX 買一價，也就是你賣 USDT 可參考：`{ticker['buy']:.4f}`
MAX 賣一價，也就是你買回 USDT 可參考：`{ticker['sell']:.4f}`
MAX 最新成交：`{ticker['last']:.4f}`

央行 USD/TWD：`{model['usd_twd']:.4f}`
央行匯率日期：`{model['usd_twd_date']}`
平日正常溢價：`{model['normal_premium']:.4f}`
模型合理價：`{model['fair_price']:.4f}`

賣出用異常溢價：
`MAX 買一價 - 合理價 = {ticker['buy']:.4f} - {model['fair_price']:.4f} = {abnormal_sell_premium:.4f}`

買回參考異常溢價：
`MAX 賣一價 - 合理價 = {ticker['sell']:.4f} - {model['fair_price']:.4f} = {abnormal_buy_premium:.4f}`

判斷：{signal['suggestion']}
""".strip()

    return content


def check_once() -> None:
    now = now_taipei()

    if WEEKEND_ONLY_NOTIFY and not is_weekend(now):
        print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 非週末，跳過通知檢查")
        return

    model = get_model()
    ticker = fetch_max_ticker()

    fair_price = model["fair_price"]

    # 賣出 USDT 時，看 MAX 買一價
    abnormal_sell_premium = ticker["buy"] - fair_price

    # 買回 USDT 時，看 MAX 賣一價
    abnormal_buy_premium = ticker["sell"] - fair_price

    signal = classify_sell_signal(abnormal_sell_premium)

    if PRINT_EVERY_CHECK:
        print("-" * 80)
        print(f"時間：{now.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"MAX 買一價：{ticker['buy']:.4f}")
        print(f"MAX 賣一價：{ticker['sell']:.4f}")
        print(f"USD/TWD：{model['usd_twd']:.4f}")
        print(f"平日正常溢價：{model['normal_premium']:.4f}")
        print(f"合理價：{fair_price:.4f}")
        print(f"賣出異常溢價：{abnormal_sell_premium:.4f}")
        print(f"買回異常溢價：{abnormal_buy_premium:.4f}")
        print(f"訊號：{signal['name']}")

    if signal["rank"] <= 0:
        print("[通知] 未達通知門檻，本次不發 Discord")
        return

    message = format_report(
        ticker=ticker,
        model=model,
        abnormal_sell_premium=abnormal_sell_premium,
        abnormal_buy_premium=abnormal_buy_premium,
        signal=signal,
    )
    send_discord_message(message)


def main() -> None:
    print("MAX USDT/TWD 異常溢價監控器啟動，一次性執行")
    check_once()
    print("本次檢查結束")


if __name__ == "__main__":
    main()
