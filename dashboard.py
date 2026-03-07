"""
Robinhood Signal Bot — Web Dashboard
Run: python dashboard.py
Then open: http://localhost:5000
"""

import json
import threading
from datetime import datetime

from flask import Flask, jsonify, render_template_string, request

from src.macro.regime import detect_market_regime
from src.scanner.daily_scan import run_daily_scan, scan_single_ticker
from src.utils import config
from src.utils.logger import get_logger

log = get_logger("dashboard")
app = Flask(__name__)

# In-memory cache for last scan result
_last_scan: dict = {}
_scan_lock = threading.Lock()

# ── HTML template ────────────────────────────────────────────────────────────

HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>UFGenius — Signal Bot</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: 'Segoe UI', system-ui, sans-serif; background: #0d1117; color: #c9d1d9; min-height: 100vh; }
    header { background: #161b22; border-bottom: 1px solid #30363d; padding: 16px 24px; display: flex; align-items: center; gap: 12px; }
    header h1 { font-size: 1.3rem; color: #58a6ff; }
    header .disclaimer { font-size: 0.7rem; color: #f85149; margin-left: auto; }
    main { max-width: 1200px; margin: 0 auto; padding: 24px; }

    /* Regime banner */
    .regime-bar { border-radius: 8px; padding: 12px 20px; margin-bottom: 24px;
                  display: flex; gap: 24px; align-items: center; flex-wrap: wrap; }
    .regime-BULL_RISK_ON   { background: #0d2a0d; border: 1px solid #238636; }
    .regime-MILD_BULL      { background: #0d2a0d; border: 1px solid #2ea043; }
    .regime-NEUTRAL_CHOPPY { background: #1a1a0d; border: 1px solid #9e6a03; }
    .regime-MILD_BEAR      { background: #2a0d0d; border: 1px solid #b62324; }
    .regime-BEAR_RISK_OFF  { background: #2a0d0d; border: 1px solid #f85149; }
    .regime-bar .label { font-size: 0.75rem; color: #8b949e; }
    .regime-bar .value { font-weight: 600; font-size: 1rem; }

    /* Controls */
    .controls { display: flex; gap: 12px; margin-bottom: 24px; flex-wrap: wrap; }
    input, select, button {
      background: #161b22; border: 1px solid #30363d; color: #c9d1d9;
      border-radius: 6px; padding: 8px 14px; font-size: 0.875rem;
    }
    input:focus, select:focus { outline: none; border-color: #58a6ff; }
    button { cursor: pointer; font-weight: 600; transition: background 0.15s; }
    button.primary { background: #238636; border-color: #238636; color: #fff; }
    button.primary:hover { background: #2ea043; }
    button.secondary { background: #1f6feb; border-color: #1f6feb; color: #fff; }
    button.secondary:hover { background: #388bfd; }
    button:disabled { opacity: 0.5; cursor: not-allowed; }

    /* Spinner */
    .spinner { display: none; width: 20px; height: 20px; border: 2px solid #30363d;
               border-top-color: #58a6ff; border-radius: 50%; animation: spin 0.7s linear infinite; }
    @keyframes spin { to { transform: rotate(360deg); } }

    /* Signal cards */
    .section-title { font-size: 1rem; font-weight: 600; margin-bottom: 12px; color: #e6edf3; }
    .cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(340px, 1fr)); gap: 16px; margin-bottom: 32px; }
    .card { background: #161b22; border: 1px solid #30363d; border-radius: 10px; padding: 20px; }
    .card.STRONG_BUY { border-color: #238636; }
    .card.BUY        { border-color: #2ea043; }
    .card.WEAK_BUY   { border-color: #9e6a03; }

    .card-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 14px; }
    .ticker { font-size: 1.3rem; font-weight: 700; color: #e6edf3; }
    .signal-badge { font-size: 0.7rem; font-weight: 700; padding: 3px 10px; border-radius: 20px; }
    .badge-STRONG_BUY { background: #238636; color: #fff; }
    .badge-BUY        { background: #2ea043; color: #fff; }
    .badge-WEAK_BUY   { background: #9e6a03; color: #fff; }

    .score-bar-wrap { margin-bottom: 14px; }
    .score-label { display: flex; justify-content: space-between; font-size: 0.75rem; color: #8b949e; margin-bottom: 4px; }
    .score-bar { height: 6px; background: #21262d; border-radius: 3px; overflow: hidden; }
    .score-fill { height: 100%; border-radius: 3px; transition: width 0.4s; }
    .fill-green  { background: #238636; }
    .fill-yellow { background: #9e6a03; }
    .fill-red    { background: #b62324; }

    .levels { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-bottom: 14px; }
    .level { background: #0d1117; border-radius: 6px; padding: 8px 10px; }
    .level .lbl { font-size: 0.68rem; color: #8b949e; margin-bottom: 2px; }
    .level .val { font-size: 0.95rem; font-weight: 600; color: #e6edf3; }
    .val.green { color: #3fb950; }
    .val.red   { color: #f85149; }
    .val.blue  { color: #58a6ff; }

    .targets { margin-bottom: 14px; }
    .target-row { display: flex; justify-content: space-between; font-size: 0.8rem;
                  padding: 4px 0; border-bottom: 1px solid #21262d; }
    .target-row:last-child { border-bottom: none; }

    .reasons { list-style: none; }
    .reasons li { font-size: 0.78rem; color: #8b949e; padding: 2px 0; }
    .reasons li::before { content: "• "; color: #58a6ff; }

    .no-signals { text-align: center; padding: 40px; color: #8b949e; font-size: 0.9rem; }

    /* Error / alert */
    .alert { background: #2a0d0d; border: 1px solid #f85149; border-radius: 8px;
             padding: 16px; margin-bottom: 24px; color: #f85149; }

    /* Scores breakdown */
    .scores-grid { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 14px; }
    .score-chip { background: #0d1117; border: 1px solid #30363d; border-radius: 6px;
                  padding: 4px 10px; font-size: 0.72rem; text-align: center; }
    .score-chip .sc-lbl { color: #8b949e; }
    .score-chip .sc-val { font-weight: 700; color: #e6edf3; }

    footer { text-align: center; padding: 24px; font-size: 0.7rem; color: #484f58;
             border-top: 1px solid #21262d; margin-top: 32px; }
  </style>
</head>
<body>

<header>
  <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="#58a6ff" stroke-width="2">
    <polyline points="22 7 13.5 15.5 8.5 10.5 2 17"/><polyline points="16 7 22 7 22 13"/>
  </svg>
  <h1>UFGenius Signal Bot</h1>
  <span class="disclaimer">⚠️ NOT FINANCIAL ADVICE — Educational only</span>
</header>

<main>

  <!-- Regime bar -->
  <div id="regimeBar" class="regime-bar regime-NEUTRAL_CHOPPY">
    <div><div class="label">Market Regime</div><div class="value" id="regimeLabel">—</div></div>
    <div><div class="label">VIX</div><div class="value" id="vixLabel">—</div></div>
    <div><div class="label">SPY vs 200SMA</div><div class="value" id="spyLabel">—</div></div>
    <div><div class="label">Regime Strategy</div><div class="value" id="biasLabel">—</div></div>
    <div style="margin-left:auto"><div class="label">Last updated</div><div class="value" id="tsLabel">—</div></div>
  </div>

  <!-- Controls -->
  <div class="controls">
    <input id="tickerInput" type="text" placeholder="Ticker (e.g. AAPL)" style="width:150px; text-transform:uppercase">
    <input id="accountInput" type="number" placeholder="Account size ($)" value="10000" style="width:160px">
    <button class="secondary" onclick="scanTicker()">Analyse Ticker</button>
    <button class="primary"   onclick="runFullScan()">Full Market Scan</button>
    <div id="spinner" class="spinner"></div>
  </div>

  <div id="alertBox"></div>

  <!-- Strong Buy -->
  <div id="strongBuySection" style="display:none">
    <div class="section-title">🚀 Strong Buy</div>
    <div id="strongBuyCards" class="cards"></div>
  </div>

  <!-- Buy -->
  <div id="buySection" style="display:none">
    <div class="section-title">📈 Buy</div>
    <div id="buyCards" class="cards"></div>
  </div>

  <!-- Watch -->
  <div id="watchSection" style="display:none">
    <div class="section-title">🔍 Watch List</div>
    <div id="watchCards" class="cards"></div>
  </div>

  <div id="emptyState" class="no-signals" style="display:none">
    No signals found for current market conditions.
  </div>

</main>

<footer>
  UFGenius Robinhood Signal Bot — for educational purposes only.<br>
  All trading involves risk of loss. Never invest money you cannot afford to lose.
  Paper trade for ≥ 30 days before using real money.
</footer>

<script>
const $ = id => document.getElementById(id);

function setLoading(on) {
  $('spinner').style.display = on ? 'block' : 'none';
  document.querySelectorAll('button').forEach(b => b.disabled = on);
}

function showAlert(msg) {
  $('alertBox').innerHTML = `<div class="alert">${msg}</div>`;
}

function clearAlert() { $('alertBox').innerHTML = ''; }

function scoreColor(s) {
  if (s >= 65) return 'fill-green';
  if (s >= 45) return 'fill-yellow';
  return 'fill-red';
}

function renderCard(plan) {
  const signal  = plan.signal || '?';
  const ticker  = plan.ticker || '?';
  const score   = (plan.composite_score || 0).toFixed(1);
  const entry   = plan.entry   || {};
  const stop    = plan.stop_loss || {};
  const targets = plan.targets || {};
  const pos     = plan.position || {};
  const scores  = plan.scores  || {};
  const reasons = (plan.reasoning || []).slice(0, 5);

  const chipKeys = ['technical','momentum','volume','sentiment','fundamental','macro'];
  const chips = chipKeys
    .filter(k => scores[k] != null)
    .map(k => `<div class="score-chip"><div class="sc-lbl">${k}</div><div class="sc-val">${Number(scores[k]).toFixed(0)}</div></div>`)
    .join('');

  const trows = Object.entries(targets)
    .map(([lbl, t]) => `
      <div class="target-row">
        <span style="color:#3fb950">${lbl}: $${t.price}</span>
        <span style="color:#8b949e">${t.rr} R:R · exit ${t.exit_pct}%</span>
      </div>`).join('');

  const reasonItems = reasons.map(r => `<li>${r}</li>`).join('');

  return `
  <div class="card ${signal}">
    <div class="card-header">
      <span class="ticker">${ticker}</span>
      <span class="signal-badge badge-${signal}">${signal.replace('_',' ')}</span>
    </div>

    <div class="score-bar-wrap">
      <div class="score-label"><span>Composite Score</span><span>${score}/100</span></div>
      <div class="score-bar"><div class="score-fill ${scoreColor(score)}" style="width:${score}%"></div></div>
    </div>

    <div class="scores-grid">${chips}</div>

    <div class="levels">
      <div class="level"><div class="lbl">Entry (LIMIT)</div><div class="val blue">$${entry.price || '?'}</div></div>
      <div class="level"><div class="lbl">Stop Loss</div><div class="val red">$${stop.price || '?'} (${stop.pct_below_entry || '?'}%)</div></div>
      <div class="level"><div class="lbl">Shares</div><div class="val">${pos.shares || '?'}</div></div>
      <div class="level"><div class="lbl">Risk</div><div class="val">$${pos.risk_dollars || '?'} (${pos.risk_percent || '?'}%)</div></div>
    </div>

    <div class="targets">${trows}</div>

    <ul class="reasons">${reasonItems}</ul>
  </div>`;
}

function renderResults(data) {
  clearAlert();

  if (data.alert) { showAlert(data.alert); }

  const groups = [
    { key: 'strong_buys', sectionId: 'strongBuySection', cardsId: 'strongBuyCards' },
    { key: 'buys',        sectionId: 'buySection',        cardsId: 'buyCards'       },
    { key: 'watch_list',  sectionId: 'watchSection',      cardsId: 'watchCards'     },
  ];

  let total = 0;
  groups.forEach(({ key, sectionId, cardsId }) => {
    const plans = data[key] || [];
    total += plans.length;
    $(sectionId).style.display = plans.length ? 'block' : 'none';
    $(cardsId).innerHTML = plans.map(renderCard).join('');
  });

  $('emptyState').style.display = total === 0 && !data.alert ? 'block' : 'none';

  // Regime bar
  const r = data.regime || {};
  const reg = r.regime || data.market_regime || 'NEUTRAL_CHOPPY';
  const bar = $('regimeBar');
  bar.className = 'regime-bar regime-' + reg;
  $('regimeLabel').textContent = reg.replace(/_/g, ' ');
  $('vixLabel').textContent    = r.vix != null ? r.vix.toFixed(1) : '—';
  $('spyLabel').textContent    = r.spy_vs_200 != null ? r.spy_vs_200.toFixed(1) + '%' : '—';
  $('biasLabel').textContent   = (r.strategy || {}).bias || '—';
  $('tsLabel').textContent     = data.scan_date || new Date().toLocaleTimeString();
}

async function scanTicker() {
  const ticker  = $('tickerInput').value.trim().toUpperCase();
  const account = parseFloat($('accountInput').value) || 10000;
  if (!ticker) { showAlert('Please enter a ticker symbol.'); return; }

  setLoading(true); clearAlert();
  try {
    const res  = await fetch(`/api/scan-ticker?ticker=${ticker}&account_size=${account}`);
    const data = await res.json();
    if (data.error) { showAlert(data.error); return; }

    // Wrap single plan into scan-like structure for renderResults
    const sig = data.signal || 'UNKNOWN';
    const scanlike = {
      scan_date: new Date().toLocaleString(),
      market_regime: data.regime || '—',
      regime: {},
      strong_buys: sig === 'STRONG_BUY' ? [data] : [],
      buys:        sig === 'BUY'         ? [data] : [],
      watch_list:  sig === 'WEAK_BUY'   ? [data] : [],
    };
    renderResults(scanlike);
    if (!['STRONG_BUY','BUY','WEAK_BUY'].includes(sig)) {
      showAlert(`Signal for ${ticker}: <strong>${sig}</strong> (score: ${data.composite_score || 0})`);
    }
  } catch(e) {
    showAlert('Request failed: ' + e.message);
  } finally {
    setLoading(false);
  }
}

async function runFullScan() {
  const account = parseFloat($('accountInput').value) || 10000;
  setLoading(true); clearAlert();
  $('emptyState').style.display = 'none';

  try {
    const res  = await fetch(`/api/scan?account_size=${account}`);
    const data = await res.json();
    renderResults(data);
  } catch(e) {
    showAlert('Scan failed: ' + e.message);
  } finally {
    setLoading(false);
  }
}

// Load regime on page load
(async () => {
  try {
    const res = await fetch('/api/regime');
    const r   = await res.json();
    const reg = r.regime || 'NEUTRAL_CHOPPY';
    const bar = $('regimeBar');
    bar.className = 'regime-bar regime-' + reg;
    $('regimeLabel').textContent = reg.replace(/_/g, ' ');
    $('vixLabel').textContent    = r.vix != null ? r.vix.toFixed(1) : '—';
    $('spyLabel').textContent    = r.spy_vs_200 != null ? r.spy_vs_200.toFixed(1) + '%' : '—';
    $('biasLabel').textContent   = (r.strategy || {}).bias || '—';
    $('tsLabel').textContent     = new Date().toLocaleTimeString();
  } catch(e) {}
})();

// Allow Enter key in ticker input
$('tickerInput').addEventListener('keydown', e => {
  if (e.key === 'Enter') scanTicker();
});
</script>
</body>
</html>
"""

# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template_string(HTML)


@app.route("/api/regime")
def api_regime():
    try:
        regime = detect_market_regime()
        # Remove non-serialisable objects
        regime.pop("_df", None)
        return jsonify(regime)
    except Exception as e:
        return jsonify({"error": str(e), "regime": "NEUTRAL_CHOPPY", "strategy": {"bias": "NEUTRAL"}})


@app.route("/api/scan-ticker")
def api_scan_ticker():
    ticker = request.args.get("ticker", "").upper().strip()
    account_size = float(request.args.get("account_size", config.ACCOUNT_SIZE))

    if not ticker:
        return jsonify({"error": "ticker parameter is required"}), 400

    try:
        plan = scan_single_ticker(ticker, account_size=account_size)
        return jsonify(_clean(plan))
    except Exception as e:
        log.error(f"Scan ticker error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/scan")
def api_scan():
    account_size = float(request.args.get("account_size", config.ACCOUNT_SIZE))

    try:
        result = run_daily_scan(account_size=account_size)
        return jsonify(_clean(result))
    except Exception as e:
        log.error(f"Full scan error: {e}")
        return jsonify({"error": str(e)}), 500


def _clean(obj):
    """Recursively strip non-JSON-serialisable objects (DataFrames, Series)."""
    import pandas as pd

    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items() if k != "_df"}
    if isinstance(obj, list):
        return [_clean(i) for i in obj]
    if isinstance(obj, pd.Series):
        return None
    if isinstance(obj, pd.DataFrame):
        return None
    try:
        json.dumps(obj)
        return obj
    except (TypeError, ValueError):
        return str(obj)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("""
╔══════════════════════════════════════════════════════╗
║         UFGenius — Robinhood Signal Bot              ║
║         Dashboard running at http://localhost:5000   ║
║                                                      ║
║  ⚠️  NOT FINANCIAL ADVICE — Educational only         ║
╚══════════════════════════════════════════════════════╝
""")
    app.run(host="0.0.0.0", port=5000, debug=False)
