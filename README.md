# UFGenius — Robinhood Signal Bot

> ⚠️ **DISCLAIMER**: This project is for **educational and informational purposes only**.
> It does NOT constitute financial advice. All trading involves substantial risk of loss.
> Never invest more than you can afford to lose. Always paper trade before using real money.
> Consult a licensed financial advisor.

---

An autonomous stock signal bot that scans the US equity market daily and generates
**BUY / SELL / HOLD** signals with confidence scores, actionable trade plans, and
built-in risk management across four analysis dimensions:

| Dimension | Weight | What it covers |
|-----------|--------|----------------|
| Technical | 35% | Trend, momentum, volatility, volume, S/R levels |
| Volume | 20% | OBV, CMF, Relative Volume, accumulation/distribution |
| Sentiment | 20% | News (VADER), Reddit (WSB), SEC insider filings |
| Fundamental | 15% | Piotroski F-Score, Altman Z-Score, valuation, growth |
| Macro | 10% | Market regime (VIX, SPY vs 200 SMA, breadth) |

---

## Architecture

```
bot.py                     ← Main CLI entry point
├── src/data/              ← Market data fetching + caching
├── src/technical/         ← 20+ technical indicators
├── src/fundamental/       ← Financial health scoring
├── src/sentiment/         ← News, social, insider analysis
├── src/macro/             ← Market regime detection
├── src/signals/           ← Master signal generator + trade plan
├── src/scanner/           ← Daily market scanner
├── src/alerts/            ← Telegram + email notifications
├── src/backtest/          ← Historical simulation
└── src/robinhood/         ← Read-only portfolio view
```

---

## Quick Start

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure API keys

```bash
cp .env.example .env
# Edit .env and fill in your API keys
# Only yfinance is strictly required (free, no key needed)
```

### 3. Configure settings (optional)

Edit `config.yaml` to set your account size, risk parameters, and scan universe.

### 4. Run a single ticker scan

```bash
python bot.py --mode scan --ticker AAPL
python bot.py --mode scan --ticker AAPL --account-size 25000
```

### 5. Run a full market scan

```bash
python bot.py --mode scan
python bot.py --mode scan --universe RUSSELL1000
```

### 6. Paper trade mode (runs on schedule, no live alerts)

```bash
python bot.py --mode paper
```

### 7. Backtest

```bash
python bot.py --mode backtest --start 2022-01-01 --end 2023-12-31
python bot.py --mode backtest --ticker AAPL --start 2020-01-01 --end 2024-12-31
```

### 8. View Robinhood portfolio (read-only)

```bash
python bot.py --mode portfolio
```

---

## API Keys

| Service | Purpose | Free Tier | Required? |
|---------|---------|-----------|-----------|
| yfinance | Price data, financials | Yes (unlimited) | **Yes** |
| NewsAPI | News sentiment | 100 req/day | Optional |
| Reddit PRAW | Social sentiment | Free | Optional |
| SEC EDGAR | Insider activity | Free (public) | Optional |
| FRED API | 10-yr treasury yield | Free | Optional |
| Telegram Bot | Push notifications | Free | Optional |
| Alpha Vantage | Backup price data | 500 req/day free | Optional |
| Financial Modeling Prep | Fundamentals backup | 250 req/day free | Optional |

The bot works with **zero API keys** using yfinance alone.
Each optional service adds more signal quality.

---

## Signal Output

```json
{
  "ticker": "AAPL",
  "signal": "BUY",
  "confidence": "HIGH",
  "composite_score": 74.2,
  "entry": { "type": "LIMIT", "price": 189.50 },
  "stop_loss": { "price": 184.20, "pct_below_entry": 2.8 },
  "targets": {
    "T1": { "price": 197.45, "exit_pct": 30, "rr": "1.5:1" },
    "T2": { "price": 202.75, "exit_pct": 40, "rr": "2.5:1" },
    "T3": { "price": 210.70, "exit_pct": 30, "rr": "4.0:1" }
  },
  "position": {
    "shares": 18, "position_value": 3411,
    "risk_dollars": 95, "risk_percent": 0.95
  }
}
```

---

## Risk Management Rules

- **1% risk per trade** — max loss per trade = 1% of account
- **10% max position** — never more than 10% of account in one stock
- **5 max positions** — never hold more than 5 stocks simultaneously
- **ATR-based stops** — stop = entry − (2× ATR₁₄)
- **Bear market protection** — bot goes to cash in BEAR_RISK_OFF regime
- **Hard disqualification filters**:
  - Price < $1.00 (penny stock)
  - Avg volume < 100K shares (illiquid)
  - Altman Z-Score < 1.0 (bankruptcy risk)
  - Already up >50% in 5 days (chaser trap)

---

## Testing

```bash
# Unit tests only (no API calls)
python -m pytest tests/test_technical.py tests/test_fundamental.py tests/test_signals.py -v

# All tests excluding slow integration tests
python -m pytest tests/ -v -m "not integration"
```

---

## Minimum Acceptance Criteria

Before using with real money, backtest results MUST pass:

| Metric | Minimum |
|--------|---------|
| Sharpe Ratio | > 1.0 |
| Win Rate | > 38% |
| Profit Factor | > 1.3 |
| Max Drawdown | < 25% |

**Paper trade for ≥30 days** and verify the above in live market conditions.

---

## Workflow

```
BUILD → BACKTEST → PAPER TRADE (30+ days) → REVIEW → ONLY THEN: LIVE
```

---

⚠️ **The market can stay irrational longer than you can stay solvent.**
**When in doubt, stay in cash.**