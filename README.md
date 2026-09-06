# Stock Signal Screener

Scans essentially the **entire US stock market** (pulled fresh from
NASDAQ's public symbol directory — no fixed watchlist to maintain) for a
blend of technical "strong buy" signals and valuation ("undervalued")
signals. Writes a small `results.json` summary — a plain list, no charts —
meant to be published somewhere cheap so an app (or anything else) can
poll it, and can optionally also send an email.

**Not financial advice.** This is a screening/automation tool. Signals
are simple, well-known heuristics, not guarantees, and can be wrong.
Always do your own research before trading.

## How it works

1. **Universe**: fetches NASDAQ Trader's public symbol directory
   (~6,000-11,000 listings across NYSE/NASDAQ/AMEX), filters out ETFs and
   test issues, leaving common stocks.
2. **Technical scan (bulk)**: fetches 1 year of daily price history in
   batches (not one request per ticker — that would take hours and get
   rate-limited) and computes:
   - RSI oversold (default: RSI < 30)
   - MACD bullish crossover
   - Golden cross (50-day SMA crosses above 200-day SMA)
3. **Valuation enrichment (targeted)**: only for tickers that already
   tripped at least one technical signal — typically a few hundred out of
   several thousand — it pulls:
   - Trailing P/E, forward P/E (default: "low" = under 15)
   - PEG ratio (default: "attractive" = under 1.0)
4. **Liquidity filter**: skips stocks below a minimum price and average
   volume, so results aren't dominated by illiquid junk.
5. **Output**: writes `results.json` with every ticker that met your
   score threshold, and optionally emails an HTML summary if email
   environment variables are set (entirely optional — omit them and it
   just writes the file).

Each triggered signal adds 1 to a ticker's score. Default minimum score
to appear in results is 3 (tune with `--min-score`).

These thresholds are generic rules of thumb, not tuned per sector — a
"cheap" P/E for a bank looks very different from a "cheap" P/E for a
software company. Treat the valuation checks as a rough first pass.

## 1. Local setup / test run

```bash
pip install -r requirements.txt

# Quick test on a small slice of the market first:
python stock_screener.py --max-tickers 300 --dry-run
```

`--dry-run` prints results without writing `results.json` or emailing —
good for sanity-checking before a full run. A full market scan of
6,000+ tickers takes a while (tens of minutes) since it's pulling real
data in batches; test small first.

Full run:

```bash
python stock_screener.py --min-score 3 --output results.json
```

## 2. Publishing results.json for free (GitHub Pages)

The included `.github/workflows/daily_screen.yml` runs the scan on a
schedule and publishes `results.json` to GitHub Pages automatically —
free, no separate hosting account needed.

1. Push this repo to GitHub (private or public both work — Pages
   publishes at its own public URL either way; see note below).
2. In the repo: **Settings → Pages** → set source to "Deploy from a
   branch" → branch `gh-pages`. GitHub will show a URL like:
   `https://yourusername.github.io/stock-screener/results.json`
3. That's it. On the workflow's schedule (or triggered manually from the
   **Actions** tab), it scans the market and updates that file.

**Note on privacy**: GitHub Pages URLs are publicly reachable by anyone
with the link, even from a private repo — there's no login wall on the
free tier. The results file only contains tickers/scores/signals (no
personal or account data), so this is low-risk content-wise, but the URL
itself isn't truly access-controlled. If that matters to you, an
alternative is publishing to a private Google Drive file instead (more
setup, real access control) — ask if you want that path instead.

## 3. Email alerts (optional)

Email is entirely optional. To enable it, use an **app password**, never
your real account password:

- **Gmail**: turn on 2-Step Verification, then create an App Password
  at https://myaccount.google.com/apppasswords.
- Other providers: look for "app password" in account security settings.

Set as GitHub Actions secrets (**Settings → Secrets and variables →
Actions**): `EMAIL_ADDRESS`, `EMAIL_PASSWORD`, `EMAIL_TO`. Leave them
unset entirely to skip email and just get `results.json`.

## 4. Scheduling

Edit the `cron` line in `.github/workflows/daily_screen.yml`:

```yaml
- cron: "30 21 * * 1-5"   # weekdays, ~4:30pm ET (after market close)
- cron: "30 21 * * 1"     # once a week, Monday only
```

You can also trigger it manually any time from the repo's **Actions**
tab (`workflow_dispatch`).

## Tuning it

```bash
python stock_screener.py \
  --min-score 4 \
  --rsi-oversold 25 \
  --pe-undervalued 12 \
  --peg-undervalued 1.2 \
  --min-price 5 \
  --min-avg-volume 500000
```

- Raise `--min-score` for fewer, higher-conviction results.
- Raise `--min-price` / `--min-avg-volume` to filter out thinly-traded names.
- `--tickers-file mylist.txt` scans a custom list instead of the whole market.

## results.json format

```json
{
  "generated_at": "2026-09-06T21:35:00+00:00",
  "universe_size": 6482,
  "min_score": 3,
  "hit_count": 12,
  "hits": [
    {
      "ticker": "XYZ",
      "score": 4,
      "signals": ["RSI oversold (24.1)", "MACD bullish crossover", "Low trailing P/E (11.2)", "Attractive PEG ratio (0.8)"],
      "price": 42.17,
      "avg_volume": 1250000,
      "rsi": 24.1,
      "trailing_pe": 11.2,
      "forward_pe": 10.5,
      "peg": 0.8
    }
  ]
}
```

This is the shape an Android app (or anything else) would parse when
polling the published file.

## Known limitations

- `yfinance` pulls from Yahoo Finance's unofficial endpoints — fine for
  personal use, but it can occasionally rate-limit or change format on
  a full-market run. If it starts erroring on many batches, that's why.
- P/E, forward P/E, and PEG data quality varies by ticker.
- This does **not** place trades. It only screens and publishes results —
  pairing it with a broker API to auto-trade is a much bigger step with
  real money risk, and isn't included here.
- A full-market run can take a while and consumes noticeably more free
  Actions minutes than a small watchlist would; if you're on a private
  repo watch your Actions usage (public repos get unlimited free minutes).
