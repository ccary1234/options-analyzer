# Options Premium Screener

A small static app: you give it a basket of stocks, a target % return and a time
frame; it ranks the basket by which names pay that return with the most cushion,
for both cash-secured puts and covered calls, and surfaces earnings, ex-dividend
dates, volatility context and headlines for each.

No API keys. No server. A Python script runs once a day on GitHub Actions, writes
`docs/data.json`, and GitHub Pages serves the folder. WordPress embeds it.

```
basket.txt                  your 20-30 tickers, one per line
build_data.py               nightly builder (CBOE delayed chains + Yahoo context)
docs/index.html             the app (single file, no dependencies)
docs/data.json              generated output
.github/workflows/build.yml daily schedule + manual "Run workflow" button
```

## Setup (about 10 minutes)

1. **Create a GitHub repo** (public is fine; Pages is free on public repos) and push
   this folder to it.

2. **Enable Pages.** Repo → Settings → Pages → Source: *Deploy from a branch*,
   Branch: `main`, folder: `/docs`. Your app will live at
   `https://<your-user>.github.io/<repo>/`.

3. **Let Actions commit.** Repo → Settings → Actions → General → Workflow
   permissions → *Read and write permissions*. Save.

4. **Run it once by hand.** Repo → Actions → *Build options data* → *Run workflow*.
   It takes 1–3 minutes for 25 tickers. When it finishes, `docs/data.json` is
   committed and the Pages URL shows real quotes.

From then on it runs every weekday at 5:30pm Central (edit the cron in
`build.yml` to change that) and any time you push a change to `basket.txt`.
Hit *Run workflow* whenever you want a fresh pull.

### Running locally instead

```
pip install -r requirements.txt
python build_data.py                       # real data -> docs/data.json
python build_data.py --demo                # synthetic data, no network
cd docs && python -m http.server 8000      # open http://localhost:8000
```

(Opening `index.html` directly from disk won't work — browsers block `fetch()`
on `file://`. Serve the folder.)

## Embedding in WordPress

Simplest: add a **Custom HTML** block to any page or post:

```html
<iframe src="https://<your-user>.github.io/<repo>/?target=2&days=30"
        style="width:100%;height:1400px;border:0" loading="lazy"></iframe>
```

`?target=` and `?days=` preset the inputs. Adjust the height to taste; the page
grows when rows are expanded, so err generous or use one of the auto-resizing
iframe plugins.

The iframe keeps the app isolated from your theme's CSS, which is why it's the
recommended route. You can also link straight to the Pages URL from a menu item.

## How the ranking works

For each stock:

1. Pick the expiration whose days-to-expiry is closest to your time frame.
2. Walk the chain from far out-of-the-money toward the current price and stop at
   the first strike whose **bid** pays your target:
   - **Put:** premium ÷ strike (return on the cash you'd set aside).
   - **Call:** premium ÷ share price (return on the shares you'd hold).
3. **Cushion** = distance from the current price to that strike.
4. **Score** = cushion ÷ expected move over the window, where expected move =
   30-day ATM implied vol × √(days/365). Higher = the market pays your target
   while asking you to take less risk. Rank by score.

Strikes with open interest below the *Min open interest* box are skipped. Stocks
that can't reach the target at any strike are shown greyed out with
"not enough premium".

Flags: **Earn** (earnings date lands inside the window — the single biggest
gotcha for premium selling), **Div** (ex-dividend inside the window — matters for
covered calls and early assignment), **Rich IV / Cheap IV** (today's implied vol
vs. the past year of the stock's own realized vol, as a percentile).

## Data sources and what to check on the first live run

- **Option chains:** CBOE's public delayed-quotes JSON
  (`cdn.cboe.com/api/global/delayed_quotes/options/<SYM>.json`) — includes IV,
  delta, open interest; 15-min delayed; no key. If it fails for a symbol the
  script falls back to Yahoo via `yfinance` and computes delta itself.
- **Prices, earnings dates, dividends, headlines:** `yfinance`.

Both are free and unofficial, so on the first real run look at the Actions log
and `data.json` for:

- `"source": "cboe"` on most tickers (a wall of `yahoo` means CBOE's URL or
  response format changed — see `fetch_cboe_chain`).
- Sensible `spot`, `iv30` (0.15–0.90 for most stocks) and `earnings` values.
- Anything listed under `"errors"`.

If Yahoo starts rate-limiting, raise the `time.sleep(0.8)` between tickers.

## Tuning

Top of `build_data.py`: `MAX_DTE` (how far out to keep expirations, default
120 days), `MONEYNESS_BAND` (strikes within ±30% of spot), `MIN_OPEN_INTEREST`,
`NEWS_ITEMS`. Defaults produce ~250 KB of JSON for 25 tickers.

## Not advice

Delayed quotes, refreshed daily, ranked by a simple heuristic. Use it to decide
what to look at, not what to trade.
