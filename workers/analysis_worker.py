#!/opt/hermes/.venv/bin/python3
"""analysis_worker.py — 분석 태스크 큐 처리기

Queue: /opt/data/state/pending_analysis.jsonl
Flow: 태스크 읽기 → 데이터 수집 → HTML 생성 → (브라우저가 스크린샷) → 전송

Usage (cron):
    python3 analysis_worker.py --once      # 단일 태스크 처리 후 종료
    python3 analysis_worker.py --watch     # 지속 폴링 (개발용)
"""

import os, sys, json, time, re
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(__file__))
from config import ALPACA_API_KEY, ALPACA_SECRET_KEY

QUEUE_PATH = "/opt/data/state/pending_analysis.jsonl"
OUTPUT_DIR = "/opt/data/analysis_output"
os.makedirs(OUTPUT_DIR, exist_ok=True)

# ─── Alpaca API helpers ───
import urllib.request
TRADING_BASE = "https://paper-api.alpaca.markets"
DATA_BASE = "https://data.alpaca.markets"

def _alpaca_get(base, path):
    req = urllib.request.Request(f"{base}{path}")
    req.add_header("APCA-API-KEY-ID", ALPACA_API_KEY)
    req.add_header("APCA-API-SECRET-KEY", ALPACA_SECRET_KEY)
    return json.loads(urllib.request.urlopen(req, timeout=15).read())

def _extract_tickers(text):
    """텍스트에서 주식 티커 추출"""
    pattern = re.compile(r'(?<![A-Za-z])[A-Za-z]{2,5}(?![A-Za-z])')
    words = text.split()
    tickers = [w.upper() for w in words if pattern.match(w) and not w.isdigit()
               and w.upper() not in ("A", "I", "VS", "SNP500", "S&P")]
    return list(dict.fromkeys(tickers))  # 중복 제거, 순서 유지

def _is_comparison(text):
    """비교/대비 요청인지 감지"""
    lower = text.lower()
    return any(kw in lower for kw in ["비교", "대비", "vs", "수익률", "백테스트"])

def _fetch_stock_snapshot(ticker):
    """Alpaca snapshot + daily bar"""
    try:
        snap = _alpaca_get(DATA_BASE, f"/v2/stocks/{ticker}/snapshot")
        d = snap.get("dailyBar", {}) or {}
        q = snap.get("quote", {}) or {}
        t = snap.get("trade", {}) or {}
        latest = (t or q).get("p") or d.get("c", 0)
        return {
            "ticker": ticker,
            "price": latest,
            "open": d.get("o"),
            "high": d.get("h"),
            "low": d.get("l"),
            "close": d.get("c"),
            "volume": d.get("v", 0),
            "change": (d.get("c", 0) or 0) - (d.get("o", 0) or 0) if d.get("o") else None,
        }
    except Exception as e:
        return {"ticker": ticker, "error": str(e)}

# ─── HTML table generation ───
def _build_comparison_table(stocks):
    """주식 비교 HTML 테이블"""
    rows_html = ""
    for s in stocks:
        if "error" in s:
            rows_html += f"<tr><td>{s['ticker']}</td><td colspan='6' class='error'>Error: {s['error']}</td></tr>"
            continue
        change = s.get("change")
        change_str = f"${change:+.2f}" if change is not None else "N/A"
        change_pct = (change / s['open'] * 100) if change is not None and s.get('open') else None
        pct_str = f"({change_pct:+.2f}%)" if change_pct is not None else ""
        vol_str = f"{s['volume']:,}" if s.get('volume') else "N/A"
        rows_html += (
            f"<tr>"
            f"<td class='ticker'>{s['ticker']}</td>"
            f"<td class='num'>${s['price']:.2f}</td>"
            f"<td class='num'>${s['open']:.2f}</td>"
            f"<td class='num'>${s['high']:.2f}</td>"
            f"<td class='num'>${s['low']:.2f}</td>"
            f"<td class='num'>{vol_str}</td>"
            f"<td class='num change'>{change_str} {pct_str}</td>"
            f"</tr>"
        )
    return rows_html

def generate_comparison_html(tickers, title="Stock Comparison"):
    """HTML 파일 생성"""
    stocks = [_fetch_stock_snapshot(t) for t in tickers]
    rows = _build_comparison_table(stocks)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    html = f"""<!DOCTYPE html>
<html lang="ko">
<head><meta charset="utf-8">
<style>
  body {{
    font-family: -apple-system, 'Segoe UI', Roboto, sans-serif;
    background: #1a1a2e; color: #e0e0e0; padding: 24px;
  }}
  h2 {{ color: #a0a0e0; margin-bottom: 8px; }}
  .ts {{ color: #666; font-size: 0.8rem; margin-bottom: 16px; }}
  table {{
    border-collapse: collapse; width: 100%;
    background: #16213e; border-radius: 8px; overflow: hidden;
  }}
  th {{
    background: #0f3460; color: #888; padding: 10px 12px;
    text-align: center; font-size: 0.8rem; text-transform: uppercase;
    letter-spacing: 1px;
  }}
  td {{ padding: 10px 12px; text-align: center; border-bottom: 1px solid #0f3460; }}
  tr:last-child td {{ border-bottom: none; }}
  .ticker {{ font-weight: 600; color: #4fc3f7; text-align: left; }}
  .num {{ font-family: 'SF Mono', 'Fira Code', monospace; }}
  .change {{ color: #4caf50; }}
</style>
</head>
<body>
<h2>{title}</h2>
<div class="ts">{timestamp}</div>
<table>
<tr><th>Ticker</th><th>Price</th><th>Open</th><th>High</th><th>Low</th><th>Volume</th><th>Change</th></tr>
{rows}
</table>
</body>
</html>"""
    filename = f"analysis_{int(time.time())}.html"
    path = os.path.join(OUTPUT_DIR, filename)
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"[WORKER] HTML saved: {path}")
    return path

def pop_task():
    """큐에서 첫 번째 태스크 읽기 + 제거"""
    if not os.path.exists(QUEUE_PATH):
        return None
    try:
        with open(QUEUE_PATH, "r", encoding="utf-8") as f:
            lines = [l.strip() for l in f if l.strip()]
        if not lines:
            return None
        task = json.loads(lines[0])
        with open(QUEUE_PATH, "w", encoding="utf-8") as f:
            f.writelines(l + "\n" for l in lines[1:])
        return task
    except (OSError, json.JSONDecodeError, IndexError) as e:
        print(f"[WORKER] Queue read error: {e}")
        return None

def main():
    task = pop_task()
    if not task:
        return

    content = task.get("content", "")
    author = task.get("author", "unknown")
    channel_id = task.get("channel_id", "")
    print(f"[WORKER] Processing task | author={author} content={content[:60]}...")

    tickers = _extract_tickers(content)
    if not tickers:
        print("[WORKER] No tickers found in task, skipping")
        return

    is_compare = _is_comparison(content)
    title = f"{' vs '.join(tickers)} Comparison" if is_compare else f"{', '.join(tickers)} Data"
    html_path = generate_comparison_html(tickers, title=title)

    # 결과를 스크린샷을 위해 표시
    print(f"[WORKER] Ready for screenshot: {html_path}")
    print(f"[WORKER] Task complete, waiting for browser screenshot")
    print(f"[WORKER] HTML_PATH={html_path}")
    print(f"[WORKER] CHANNEL_ID={channel_id}")
    print(f"[WORKER] AUTHOR={author}")

if __name__ == "__main__":
    if "--watch" in sys.argv:
        print("[WORKER] Watch mode — polling every 5s...")
        while True:
            main()
            time.sleep(5)
    else:
        main()
