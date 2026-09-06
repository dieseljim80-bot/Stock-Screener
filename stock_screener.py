#!/usr/bin/env python3
"""
Market-Wide Stock Signal Screener
===================================
Scans essentially the entire US common-stock market (NYSE + NASDAQ + AMEX,
pulled fresh from NASDAQ's public symbol directory — no fixed watchlist)
for a blend of:
  - Technical "strong buy" signals (RSI oversold, MACD bullish crossover, golden cross)
  - Valuation / "undervalued" signals (trailing P/E, forward P/E, PEG ratio)

Scores each ticker by how many signals it trips. Writes a small results.json
(meant to be published somewhere cheap/free, like GitHub Pages, for an app
to poll) and OPTIONALLY emails an HTML summary if email env vars are set.

This tool is for research/screening automation only. It is not financial
advice and does not execute trades. Always do your own due diligence.

HOW IT SCALES TO THE WHOLE MARKET (important to understand):
  Pulling full price history one ticker at a time for 6,000+ stocks would
  take hours and get you rate-limited by Yahoo. So this script:
    1. Fetches price history in BATCHES (yf.download supports many tickers
       per request), not one call per ticker.
    2. Only pulls valuation data (P/E, PEG) for stocks that ALREADY tripped
       at least one technical signal — that typically cuts a 6,000-ticker
       problem down to a few hundred detail lookups.
    3. Applies an optional liquidity filter (min price / min average
       volume) so you're not scanning obscure illiquid tickers.

  Even so, expect a full-market run to take a while (tens of minutes) and
  yfinance/Yahoo's free data is unofficial — it can rate-limit or hiccup.
  If reliability becomes a problem, a paid data API (Polygon.io, Financial
  Modeling Prep, EOD Historical Data) would be a sturdier foundation.

Setup:
  pip install -r requirements.txt

Environment variables (all OPTIONAL — omit them entirely to skip email
and just get results.json):
  EMAIL_ADDRESS   - the "from" address (e.g. a Gmail address)
  EMAIL_PASSWORD  - an app password (NOT your normal account password)
  EMAIL_TO        - where alerts should be sent (can be same as EMAIL_ADDRESS)
  SMTP_SERVER     - default: smtp.gmail.com
  SMTP_PORT       - default: 587

Usage:
  python stock_screener.py --dry-run                      # scan whole market, print only
  python stock_screener.py --min-score 3                  # scan + write results.json (+ email if configured)
  python stock_screener.py --tickers-file mylist.txt       # scan a custom list instead
  python stock_screener.py --max-tickers 500 --dry-run     # quick test run
"""

import argparse
import io
import json
import os
import smtplib
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import numpy as np
import pandas as pd
import requests
import yfinance as yf

NASDAQ_TRADER_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqtraded.txt"


# ---------------------------------------------------------------------------
# Universe: fetch (almost) every US common stock, fresh, no fixed list
# ---------------------------------------------------------------------------

def fetch_market_universe(exclude_etfs: bool = True, exclude_test_issues: bool = True) -> list:
    """
    Downloads NASDAQ Trader's symbol directory, which lists every security
    on NASDAQ, NYSE, and AMEX/ARCA. Filters out ETFs and test issues so
    we're left with (mostly) common stocks. This is a free, public,
    no-API-key data source maintained by Nasdaq.
    """
    resp = requests.get(NASDAQ_TRADER_URL, timeout=30)
    resp.raise_for_status()

    # Pipe-delimited; last line is a footer ("File Creation Time...") to drop.
    lines = resp.text.strip().splitlines()
    data_lines = [l for l in lines if not l.startswith("File Creation Time")]
    df = pd.read_csv(io.StringIO("\n".join(data_lines)), sep="|")

    if "Test Issue" in df.columns and exclude_test_issues:
        df = df[df["Test Issue"] == "N"]
    if "ETF" in df.columns and exclude_etfs:
        df = df[df["ETF"] == "N"]

    symbol_col = "Symbol" if "Symbol" in df.columns else "NASDAQ Symbol"
    symbols = df[symbol_col].dropna().astype(str).tolist()

    # Drop symbols with suffixes (warrants, units, preferred shares etc.)
    # which show up with a '.' or '$' in the NASDAQ directory.
    clean = [s for s in symbols if s.isalpha() and s.isupper() and len(s) <= 5]
    return sorted(set(clean))


# ---------------------------------------------------------------------------
# Indicator math
# ---------------------------------------------------------------------------

def compute_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi


def compute_macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

def _default_signal_flags() -> dict:
    # Structured, machine-readable version of the "signals" list — lets a
    # downstream consumer (like the Android app) apply its own weights
    # instead of trusting a fixed 1-point-per-signal score.
    return {
        "rsi_oversold": False,
        "macd_bullish_crossover": False,
        "golden_cross": False,
        "above_golden_cross_trend": False,
        "low_trailing_pe": False,
        "low_forward_pe": False,
        "attractive_peg": False,
    }


@dataclass
class TickerResult:
    ticker: str
    score: int = 0
    signals: list = field(default_factory=list)
    signal_flags: dict = field(default_factory=_default_signal_flags)
    price: float = None
    avg_volume: float = None
    rsi: float = None
    trailing_pe: float = None
    forward_pe: float = None
    peg: float = None
    target_mean_price: float = None
    target_upside_pct: float = None
    error: str = None


# ---------------------------------------------------------------------------
# Stage 1: bulk technical scan (batched price history, no per-ticker calls)
# ---------------------------------------------------------------------------

def chunked(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def technical_scan(tickers: list,
                    batch_size: int = 150,
                    sleep_between_batches: float = 1.5,
                    min_price: float = 1.0,
                    min_avg_volume: float = 100_000,
                    rsi_oversold: float = 30) -> list:
    """
    Fetches 1y of daily price history in batches and computes technical
    signals for every ticker. Returns a TickerResult for every ticker that
    had usable data (whether or not it tripped a signal) so stage 2 can
    filter on it.
    """
    results = []
    batches = list(chunked(tickers, batch_size))

    for i, batch in enumerate(batches, 1):
        print(f"  [technical] batch {i}/{len(batches)} ({len(batch)} tickers)...", file=sys.stderr)
        try:
            data = yf.download(
                tickers=" ".join(batch),
                period="1y",
                interval="1d",
                group_by="ticker",
                threads=True,
                progress=False,
                auto_adjust=True,
            )
        except Exception as exc:
            for t in batch:
                results.append(TickerResult(ticker=t, error=f"Batch download failed: {exc}"))
            continue

        for t in batch:
            result = TickerResult(ticker=t)
            try:
                if len(batch) == 1:
                    hist = data
                else:
                    if t not in data.columns.get_level_values(0):
                        result.error = "No data returned"
                        results.append(result)
                        continue
                    hist = data[t]

                hist = hist.dropna(how="all")
                if hist.empty or "Close" not in hist.columns or len(hist) < 200:
                    result.error = "Insufficient price history"
                    results.append(result)
                    continue

                close = hist["Close"].dropna()
                volume = hist["Volume"].dropna() if "Volume" in hist.columns else pd.Series(dtype=float)
                if close.empty or len(close) < 200:
                    result.error = "Insufficient price history"
                    results.append(result)
                    continue

                result.price = round(float(close.iloc[-1]), 2)
                result.avg_volume = round(float(volume.tail(30).mean()), 0) if not volume.empty else None

                # Liquidity filter — skip illiquid / penny stocks
                if result.price < min_price:
                    result.error = f"Below min price (${min_price})"
                    results.append(result)
                    continue
                if result.avg_volume is not None and result.avg_volume < min_avg_volume:
                    result.error = f"Below min avg volume ({min_avg_volume:,.0f})"
                    results.append(result)
                    continue

                # --- Technical signals ---
                rsi = compute_rsi(close)
                last_rsi = rsi.iloc[-1]
                result.rsi = round(float(last_rsi), 1) if pd.notna(last_rsi) else None
                if pd.notna(last_rsi) and last_rsi < rsi_oversold:
                    result.score += 1
                    result.signals.append(f"RSI oversold ({result.rsi})")
                    result.signal_flags["rsi_oversold"] = True

                macd_line, signal_line, _ = compute_macd(close)
                if len(macd_line) > 1:
                    crossed_up = (
                        macd_line.iloc[-2] < signal_line.iloc[-2]
                        and macd_line.iloc[-1] > signal_line.iloc[-1]
                    )
                    if crossed_up:
                        result.score += 1
                        result.signals.append("MACD bullish crossover")
                        result.signal_flags["macd_bullish_crossover"] = True

                sma50 = close.rolling(50).mean()
                sma200 = close.rolling(200).mean()
                if len(sma50) > 1 and pd.notna(sma50.iloc[-2]) and pd.notna(sma200.iloc[-2]):
                    golden_cross = (
                        sma50.iloc[-2] < sma200.iloc[-2] and sma50.iloc[-1] > sma200.iloc[-1]
                    )
                    if golden_cross:
                        result.score += 1
                        result.signals.append("Golden cross (50/200 SMA)")
                        result.signal_flags["golden_cross"] = True
                    elif sma50.iloc[-1] > sma200.iloc[-1]:
                        result.signals.append("Above golden cross trend (50 SMA > 200 SMA)")
                        result.signal_flags["above_golden_cross_trend"] = True

            except Exception as exc:
                result.error = str(exc)

            results.append(result)

        if i < len(batches):
            time.sleep(sleep_between_batches)

    return results


# ---------------------------------------------------------------------------
# Stage 2: valuation enrichment — ONLY for tickers that already scored >= 1
# ---------------------------------------------------------------------------

def enrich_with_fundamentals(candidates: list,
                              pe_undervalued: float = 15,
                              peg_undervalued: float = 1.0,
                              sleep_between_calls: float = 0.3) -> None:
    """
    Mutates each candidate TickerResult in place, adding valuation signals.
    Only called on tickers that already tripped >=1 technical signal, to
    keep the number of (slow, rate-limit-prone) .info calls manageable.
    """
    print(f"  [fundamentals] enriching {len(candidates)} candidate(s)...", file=sys.stderr)
    for idx, result in enumerate(candidates, 1):
        try:
            info = yf.Ticker(result.ticker).info or {}
        except Exception:
            info = {}

        trailing_pe = info.get("trailingPE")
        forward_pe = info.get("forwardPE")
        peg = info.get("pegRatio") or info.get("trailingPegRatio")
        target_mean_price = info.get("targetMeanPrice")

        result.trailing_pe = round(trailing_pe, 2) if trailing_pe else None
        result.forward_pe = round(forward_pe, 2) if forward_pe else None
        result.peg = round(peg, 2) if peg else None

        # Informational only — analyst average target price, and the
        # implied upside/downside vs. the current price. Not a signal,
        # not part of the score; just data along for the ride.
        if target_mean_price and result.price:
            result.target_mean_price = round(target_mean_price, 2)
            result.target_upside_pct = round(
                (target_mean_price - result.price) / result.price * 100, 1
            )

        if trailing_pe and 0 < trailing_pe < pe_undervalued:
            result.score += 1
            result.signals.append(f"Low trailing P/E ({result.trailing_pe})")
            result.signal_flags["low_trailing_pe"] = True
        if forward_pe and 0 < forward_pe < pe_undervalued:
            result.score += 1
            result.signals.append(f"Low forward P/E ({result.forward_pe})")
            result.signal_flags["low_forward_pe"] = True
        if peg and 0 < peg < peg_undervalued:
            result.score += 1
            result.signals.append(f"Attractive PEG ratio ({result.peg})")
            result.signal_flags["attractive_peg"] = True

        if idx % 50 == 0:
            print(f"    ...{idx}/{len(candidates)}", file=sys.stderr)
        time.sleep(sleep_between_calls)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def run_screener(tickers: list, **kwargs) -> list:
    technical_kwargs = {
        k: v for k, v in kwargs.items()
        if k in ("batch_size", "sleep_between_batches", "min_price", "min_avg_volume", "rsi_oversold")
    }
    results = technical_scan(tickers, **technical_kwargs)

    # Only enrich tickers that already have at least one technical signal —
    # this is what makes a whole-market scan practical.
    candidates = [r for r in results if not r.error and r.score >= 1]
    fundamentals_kwargs = {
        k: v for k, v in kwargs.items()
        if k in ("pe_undervalued", "peg_undervalued")
    }
    if candidates:
        enrich_with_fundamentals(candidates, **fundamentals_kwargs)

    return results


# ---------------------------------------------------------------------------
# results.json output — the small file an app (or anything else) can poll
# ---------------------------------------------------------------------------

def write_results_json(results: list, min_score: int, universe_size: int, path: str) -> dict:
    hits = [r for r in results if not r.error and r.score >= min_score]
    hits.sort(key=lambda r: r.score, reverse=True)

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "universe_size": universe_size,
        "min_score": min_score,
        "hit_count": len(hits),
        "hits": [
            {k: v for k, v in asdict(r).items() if k != "error"}
            for r in hits
        ],
    }
    with open(path, "w") as f:
        json.dump(payload, f, indent=2)
    return payload


# ---------------------------------------------------------------------------
# Optional email delivery (skipped entirely if env vars aren't set)
# ---------------------------------------------------------------------------

def email_configured() -> bool:
    return bool(os.environ.get("EMAIL_ADDRESS") and os.environ.get("EMAIL_PASSWORD"))


def build_email_body(payload: dict) -> str:
    hits = payload["hits"]
    lines = [f"<h2>Stock Screener Alert — {datetime.now().strftime('%Y-%m-%d %H:%M')}</h2>"]
    lines.append(f"<p style='color:#888;font-size:0.85em'>Scanned {payload['universe_size']:,} tickers.</p>")
    if not hits:
        lines.append("<p>No tickers met the score threshold today.</p>")
    else:
        lines.append("<table border='1' cellpadding='6' cellspacing='0' style='border-collapse:collapse'>")
        lines.append(
            "<tr><th>Ticker</th><th>Score</th><th>Price</th><th>RSI</th>"
            "<th>Trail P/E</th><th>Fwd P/E</th><th>PEG</th><th>Signals</th></tr>"
        )
        for r in hits:
            lines.append(
                f"<tr><td><b>{r['ticker']}</b></td><td>{r['score']}</td><td>{r['price']}</td>"
                f"<td>{r['rsi'] if r['rsi'] is not None else '-'}</td>"
                f"<td>{r['trailing_pe'] if r['trailing_pe'] is not None else '-'}</td>"
                f"<td>{r['forward_pe'] if r['forward_pe'] is not None else '-'}</td>"
                f"<td>{r['peg'] if r['peg'] is not None else '-'}</td>"
                f"<td>{'; '.join(r['signals'])}</td></tr>"
            )
        lines.append("</table>")

    lines.append(
        "<p style='color:#888;font-size:0.85em'>This is an automated screening tool, "
        "not financial advice. Signals are informational and may be wrong — do your "
        "own research before trading.</p>"
    )
    return "\n".join(lines)


def send_email(subject: str, html_body: str):
    email_from = os.environ["EMAIL_ADDRESS"]
    email_to = os.environ.get("EMAIL_TO", email_from)
    password = os.environ["EMAIL_PASSWORD"]
    smtp_server = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = email_from
    msg["To"] = email_to
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP(smtp_server, smtp_port) as server:
        server.starttls()
        server.login(email_from, password)
        server.sendmail(email_from, [email_to], msg.as_string())


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Scan the whole US stock market for technical + valuation signals."
    )
    parser.add_argument("--tickers-file", default=None,
                         help="Optional: scan tickers from this file instead of the whole market")
    parser.add_argument("--max-tickers", type=int, default=None,
                         help="Optional cap on universe size, useful for quick test runs")
    parser.add_argument("--min-score", type=int, default=3,
                         help="Minimum signal count to include in the results (default 3, since a "
                              "full-market scan finds many single-signal matches)")
    parser.add_argument("--rsi-oversold", type=float, default=30, help="RSI level considered oversold")
    parser.add_argument("--pe-undervalued", type=float, default=15, help="P/E below this counts as undervalued")
    parser.add_argument("--peg-undervalued", type=float, default=1.0, help="PEG below this counts as undervalued")
    parser.add_argument("--min-price", type=float, default=1.0, help="Skip stocks priced below this")
    parser.add_argument("--min-avg-volume", type=float, default=100_000,
                         help="Skip stocks with 30-day average volume below this")
    parser.add_argument("--batch-size", type=int, default=150, help="Tickers per bulk price-history request")
    parser.add_argument("--output", default="results.json", help="Path to write the results JSON file")
    parser.add_argument("--dry-run", action="store_true",
                         help="Print results only — skip writing results.json and skip email")
    args = parser.parse_args()

    if args.tickers_file:
        with open(args.tickers_file) as f:
            tickers = [line.strip().upper() for line in f if line.strip() and not line.startswith("#")]
        print(f"Loaded {len(tickers)} tickers from {args.tickers_file}")
    else:
        print("Fetching full US stock market universe from NASDAQ Trader...")
        tickers = fetch_market_universe()
        print(f"Universe size: {len(tickers)} tickers")

    if args.max_tickers:
        tickers = tickers[: args.max_tickers]
        print(f"Capped to {len(tickers)} tickers for this run")

    results = run_screener(
        tickers,
        rsi_oversold=args.rsi_oversold,
        pe_undervalued=args.pe_undervalued,
        peg_undervalued=args.peg_undervalued,
        min_price=args.min_price,
        min_avg_volume=args.min_avg_volume,
        batch_size=args.batch_size,
    )

    hits = [r for r in results if not r.error and r.score >= args.min_score]
    print(f"\n{len(hits)} ticker(s) met threshold (score >= {args.min_score}) out of {len(tickers)} scanned:")
    for r in sorted(hits, key=lambda x: x.score, reverse=True):
        print(f"  {r.ticker:6s} score={r.score}  price={r.price}  signals={r.signals}")

    if args.dry_run:
        print("\n--dry-run set: not writing results.json and not emailing.")
        return

    payload = write_results_json(results, args.min_score, universe_size=len(tickers), path=args.output)
    print(f"\nWrote {args.output} ({len(payload['hits'])} hit(s)).")

    if not email_configured():
        print("Email env vars not set — skipping email (results.json is still written).")
        return

    if not hits:
        print("No hits above threshold — skipping email.")
        return

    send_email(f"Stock Screener: {len(hits)} signal(s) found", build_email_body(payload))
    print("Email sent.")


if __name__ == "__main__":
    main()
