import requests
import time
import json
import os
import threading
from datetime import datetime
from flask import Flask, render_template_string, jsonify

# ========================= CONFIG =========================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "aadi00bot")

# Multiple Groq API keys — loaded from environment
_RAW_KEYS = [
    os.environ.get("GROQ_API_KEY",   ""),
    os.environ.get("GROQ_API_KEY_2", ""),
    os.environ.get("GROQ_API_KEY_3", ""),
]
GROQ_KEYS = [k for k in _RAW_KEYS if k.strip()]

# All Groq models to try per key (fastest/best first)
GROQ_MODELS = [
    "llama-3.3-70b-versatile",
    "llama3-70b-8192",
    "mixtral-8x7b-32768",
    "llama-3.1-8b-instant",
    "gemma2-9b-it",
]

# Flat rotation list: every (key, model) combination
GROQ_SLOTS = [(key, model) for key in GROQ_KEYS for model in GROQ_MODELS]

# ---- Simulation Settings ----
INITIAL_BALANCE_USD = 20.0
TARGET_BALANCE_USD  = 50.0
MIN_TRADE_USD       = 5.0
TP_PERCENT          = 15.0    # Take profit target
SL_PERCENT          = 5.0     # Hard stop-loss — at $20 balance this = $1 max loss
CHECK_INTERVAL      = 10      # ⚡ Check every 10s — catch reversals fast
MAX_POSITIONS       = 1

# ---- AI Confidence Threshold (STRICT — Real Money Mindset) ----
MIN_AI_CONFIDENCE   = 85      # MINIMUM 85% AI confidence required to enter a trade
                               # Better to miss 10 trades than take 1 bad one

# ---- Aggressive Risk Management ----
MAX_LOSS_USD              = 1.0    # Absolute max loss per trade in dollars
EARLY_EXIT_PCT            = -0.8   # At -0.8% loss → immediately consult AI for early exit
HARD_FAST_CUT_PCT         = -1.5   # At -1.5% loss → exit WITHOUT waiting for AI (too dangerous)

# ---- Aggressive Profit Protection / Trailing Stop ----
TRAIL_TRIGGER_PROFIT_USD  = 1.0    # Only activate trailing once we're up $1+ in profit
TRAIL_DROP_USD            = 1.0    # Exit if profit drops by $1 from its peak
TRAIL_FAST_DROP_PCT       = 2.0    # If in profit and price drops 2% since last check → exit immediately

# ---- Filter Thresholds — STRICTER than before ----
MIN_LIQUIDITY        = 15_000    # $15K min liq (raised from $8K)
MAX_LIQUIDITY        = 400_000   # $400K max liq
MIN_5M_VOLUME        = 8_000     # $8K+ 5-min volume (raised from $3K)
MIN_1H_VOLUME        = 20_000    # $20K+ 1h volume (raised from $10K)
MIN_BUY_RATIO_5M     = 1.7       # >70% buys in 5m (raised from 1.4x) — strong buyer dominance
MIN_BUY_RATIO_1H     = 1.2       # healthy buy pressure over 1h
MIN_MC               = 50_000    # $50K min FDV (raised from $30K)
MAX_MC               = 2_000_000 # $2M max FDV (tighter than $3M)
MIN_LP_LOCKED_PCT    = 50
MIN_PRICE_CHANGE_5M  = 0.5       # must be moving up meaningfully
MAX_PRICE_CHANGE_5M  = 60.0      # reject parabolic spikes
MAX_PRICE_CHANGE_1H  = 80.0      # reject coins already pumped >80% in 1h (tighter than 120%)
MIN_PRICE_CHANGE_1H  = -10.0
MIN_VOLUME_MCAP_RATIO= 0.01      # higher vol/mc ratio required
MIN_BUYS_5M          = 25        # need at least 25 buy txns in 5m (raised from 15)
MIN_CONFIRMATIONS    = 9         # need 9/12 checks to pass (was 8/12)

# Web server port
PORT = int(os.environ.get("PORT", 5000))
# =========================================================

balance_usd    = INITIAL_BALANCE_USD
positions      = {}
trade_history  = []
seen_tokens    = set()          # resets every 30 min — within-session dedup
traded_coins   = set()         # PERMANENT — coins already traded, never re-buy
start_time     = datetime.now()
bot_paused     = False
last_update_id = 0
scan_count     = 0
NEW_COIN_MAX_AGE_HOURS = 8     # reduced from 12 — prefer very fresh launches


# ==================== FLASK WEB DASHBOARD ====================

app = Flask(__name__)

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AI Trading Bot — STRICT MODE</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: #0d0f14;
    color: #e2e8f0;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    min-height: 100vh;
    padding: 12px;
  }
  .header {
    text-align: center;
    padding: 18px 12px;
    background: linear-gradient(135deg, #1a1d2e, #12151f);
    border-radius: 16px;
    margin-bottom: 14px;
    border: 1px solid #2d3748;
  }
  .header h1 {
    font-size: 1.4rem;
    font-weight: 700;
    background: linear-gradient(90deg, #00d4ff, #7b2ff7);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    margin-bottom: 6px;
  }
  .mode-badge {
    display: inline-block;
    background: rgba(252,129,129,0.15);
    color: #fc8181;
    border: 1px solid rgba(252,129,129,0.4);
    padding: 3px 10px;
    border-radius: 8px;
    font-size: 0.72rem;
    font-weight: 700;
    letter-spacing: 1px;
    margin-bottom: 6px;
  }
  .status-dot {
    display: inline-block;
    width: 10px; height: 10px;
    border-radius: 50%;
    margin-right: 6px;
    animation: pulse 1.5s infinite;
  }
  .status-dot.running { background: #48bb78; }
  .status-dot.paused  { background: #f6ad55; }
  @keyframes pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.4; }
  }
  .grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 10px;
    margin-bottom: 14px;
  }
  .card {
    background: #1a1d2e;
    border: 1px solid #2d3748;
    border-radius: 14px;
    padding: 14px 12px;
  }
  .card.full { grid-column: 1 / -1; }
  .card-label {
    font-size: 0.7rem;
    text-transform: uppercase;
    letter-spacing: 1px;
    color: #718096;
    margin-bottom: 4px;
  }
  .card-value {
    font-size: 1.5rem;
    font-weight: 700;
    color: #fff;
  }
  .card-value.green  { color: #48bb78; }
  .card-value.red    { color: #fc8181; }
  .card-value.blue   { color: #63b3ed; }
  .card-value.yellow { color: #f6ad55; }
  .progress-bar {
    background: #2d3748;
    border-radius: 999px;
    height: 8px;
    margin-top: 8px;
    overflow: hidden;
  }
  .progress-fill {
    height: 100%;
    background: linear-gradient(90deg, #00d4ff, #7b2ff7);
    border-radius: 999px;
    transition: width 0.5s;
  }
  .section-title {
    font-size: 0.85rem;
    font-weight: 600;
    color: #a0aec0;
    text-transform: uppercase;
    letter-spacing: 1px;
    margin: 16px 0 8px;
    display: flex;
    align-items: center;
    gap: 6px;
  }
  .position-card {
    background: #1a1d2e;
    border: 1px solid #2d3748;
    border-radius: 14px;
    padding: 14px;
    margin-bottom: 10px;
  }
  .position-header {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    margin-bottom: 10px;
  }
  .coin-name {
    font-size: 1.1rem;
    font-weight: 700;
    color: #fff;
  }
  .contract {
    font-size: 0.62rem;
    color: #4a5568;
    word-break: break-all;
    margin-top: 2px;
  }
  .pnl-badge {
    font-size: 1rem;
    font-weight: 700;
    padding: 4px 10px;
    border-radius: 8px;
    white-space: nowrap;
  }
  .pnl-badge.pos { background: rgba(72,187,120,0.15); color: #48bb78; }
  .pnl-badge.neg { background: rgba(252,129,129,0.15); color: #fc8181; }
  .pos-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 6px;
    font-size: 0.78rem;
  }
  .pos-item { display: flex; flex-direction: column; gap: 2px; }
  .pos-item .lbl { color: #718096; font-size: 0.68rem; text-transform: uppercase; }
  .pos-item .val { color: #e2e8f0; font-weight: 600; }
  .tp-sl-bar {
    display: flex;
    gap: 8px;
    margin-top: 10px;
  }
  .tp-box, .sl-box, .trail-box {
    flex: 1;
    padding: 6px 10px;
    border-radius: 8px;
    font-size: 0.72rem;
    font-weight: 600;
    text-align: center;
  }
  .tp-box    { background: rgba(72,187,120,0.12); color: #48bb78; border: 1px solid rgba(72,187,120,0.3); }
  .sl-box    { background: rgba(252,129,129,0.12); color: #fc8181; border: 1px solid rgba(252,129,129,0.3); }
  .trail-box { background: rgba(246,173,85,0.12); color: #f6ad55; border: 1px solid rgba(246,173,85,0.3); }
  .confidence-bar { margin-top: 10px; }
  .conf-label {
    display: flex;
    justify-content: space-between;
    font-size: 0.7rem;
    color: #718096;
    margin-bottom: 4px;
  }
  .conf-fill {
    height: 6px;
    border-radius: 999px;
    background: linear-gradient(90deg, #f6ad55, #48bb78);
  }
  .trade-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 10px 0;
    border-bottom: 1px solid #1e2433;
    font-size: 0.8rem;
  }
  .trade-row:last-child { border-bottom: none; }
  .trade-symbol { font-weight: 700; color: #e2e8f0; }
  .trade-detail { color: #718096; font-size: 0.7rem; margin-top: 2px; }
  .badge-tp       { background: rgba(72,187,120,0.15); color: #48bb78; padding: 3px 8px; border-radius: 6px; font-size: 0.7rem; font-weight: 700; }
  .badge-sl       { background: rgba(252,129,129,0.15); color: #fc8181; padding: 3px 8px; border-radius: 6px; font-size: 0.7rem; font-weight: 700; }
  .badge-ai-exit  { background: rgba(159,122,234,0.15); color: #b794f4; padding: 3px 8px; border-radius: 6px; font-size: 0.7rem; font-weight: 700; }
  .badge-trail    { background: rgba(246,173,85,0.15); color: #f6ad55; padding: 3px 8px; border-radius: 6px; font-size: 0.7rem; font-weight: 700; }
  .badge-recovery { background: rgba(99,179,237,0.15); color: #63b3ed; padding: 3px 8px; border-radius: 6px; font-size: 0.7rem; font-weight: 700; }
  .badge-fastcut  { background: rgba(252,129,129,0.25); color: #fc8181; padding: 3px 8px; border-radius: 6px; font-size: 0.7rem; font-weight: 700; }
  .trade-pnl { font-weight: 700; }
  .trade-pnl.pos { color: #48bb78; }
  .trade-pnl.neg { color: #fc8181; }
  .no-data {
    text-align: center;
    color: #4a5568;
    padding: 24px;
    font-size: 0.85rem;
  }
  .refresh-note {
    text-align: center;
    color: #4a5568;
    font-size: 0.68rem;
    margin-top: 12px;
    padding-bottom: 12px;
  }
  .ai-reason {
    margin-top: 8px;
    font-size: 0.72rem;
    color: #a0aec0;
    background: #12151f;
    padding: 6px 10px;
    border-radius: 8px;
    border-left: 3px solid #7b2ff7;
    line-height: 1.4;
  }
  .peak-info {
    margin-top: 8px;
    font-size: 0.72rem;
    color: #f6ad55;
    background: rgba(246,173,85,0.07);
    padding: 6px 10px;
    border-radius: 8px;
    border-left: 3px solid #f6ad55;
  }
</style>
</head>
<body>
<div class="header">
  <div class="mode-badge">⚡ STRICT MODE — 85%+ AI CONFIDENCE</div>
  <h1>🤖 AI Trading Bot</h1>
  <div id="bot-status" style="font-size:0.8rem; color:#a0aec0;"></div>
</div>

<div class="grid">
  <div class="card">
    <div class="card-label">Balance</div>
    <div class="card-value green" id="balance">$--</div>
  </div>
  <div class="card">
    <div class="card-label">Total PnL</div>
    <div class="card-value" id="total-pnl">$--</div>
  </div>
  <div class="card">
    <div class="card-label">Win Rate</div>
    <div class="card-value blue" id="win-rate">--%</div>
  </div>
  <div class="card">
    <div class="card-label">Trades (W/L)</div>
    <div class="card-value" id="trades-wl">--</div>
  </div>
  <div class="card full">
    <div class="card-label">Progress to Target ($<span id="target-val">50</span>)</div>
    <div style="display:flex;justify-content:space-between;margin-top:4px;font-size:0.75rem;color:#a0aec0;">
      <span>$20 start</span><span id="progress-pct" style="font-weight:700;color:#fff;">0%</span>
    </div>
    <div class="progress-bar"><div class="progress-fill" id="progress-bar" style="width:0%"></div></div>
  </div>
</div>

<div class="section-title">📌 Open Position</div>
<div id="open-positions">
  <div class="no-data">No open positions</div>
</div>

<div class="section-title">📜 Trade History</div>
<div class="card full">
  <div id="trade-history">
    <div class="no-data">No trades yet</div>
  </div>
</div>

<div class="refresh-note" id="last-updated">Auto-refreshing every 10s</div>

<script>
async function fetchData() {
  try {
    const r = await fetch('/api/status');
    const d = await r.json();

    const dot = d.paused ? '<span class="status-dot paused"></span>' : '<span class="status-dot running"></span>';
    document.getElementById('bot-status').innerHTML = dot + (d.paused ? 'PAUSED' : 'RUNNING') + ' &nbsp;|&nbsp; Scan #' + d.scan_count + ' &nbsp;|&nbsp; Runtime: ' + d.runtime;

    const bal = parseFloat(d.balance);
    document.getElementById('balance').textContent = '$' + bal.toFixed(2);

    const pnl = parseFloat(d.total_pnl);
    const pnlEl = document.getElementById('total-pnl');
    pnlEl.textContent = (pnl >= 0 ? '+' : '') + '$' + pnl.toFixed(2);
    pnlEl.className = 'card-value ' + (pnl >= 0 ? 'green' : 'red');

    document.getElementById('win-rate').textContent = d.win_rate.toFixed(0) + '%';
    document.getElementById('trades-wl').textContent = d.wins + 'W / ' + d.losses + 'L';

    const target = parseFloat(d.target);
    document.getElementById('target-val').textContent = target.toFixed(0);
    const prog = Math.max(0, Math.min(100, (bal - 20) / (target - 20) * 100));
    document.getElementById('progress-pct').textContent = prog.toFixed(1) + '%';
    document.getElementById('progress-bar').style.width = prog + '%';

    const posEl = document.getElementById('open-positions');
    if (d.positions.length === 0) {
      posEl.innerHTML = '<div class="no-data">No open positions</div>';
    } else {
      posEl.innerHTML = d.positions.map(p => {
        const pnlClass = p.pnl_pct >= 0 ? 'pos' : 'neg';
        const pnlSign  = p.pnl_pct >= 0 ? '+' : '';
        const peakInfo = p.peak_pnl_usd > 0.5
          ? `<div class="peak-info">🏔 Peak Profit: +$${p.peak_pnl_usd.toFixed(2)} | Trail drops $1 from peak to exit</div>`
          : '';
        return `<div class="position-card">
          <div class="position-header">
            <div>
              <div class="coin-name">${p.symbol}</div>
              <div class="contract">${p.address}</div>
            </div>
            <div class="pnl-badge ${pnlClass}">${pnlSign}${p.pnl_pct.toFixed(2)}%<br><small>${pnlSign}$${Math.abs(p.pnl_usd).toFixed(2)}</small></div>
          </div>
          <div class="pos-grid">
            <div class="pos-item"><span class="lbl">Entry Price</span><span class="val">$${p.entry_price.toFixed(8)}</span></div>
            <div class="pos-item"><span class="lbl">Current Price</span><span class="val">$${p.current_price.toFixed(8)}</span></div>
            <div class="pos-item"><span class="lbl">Entry MC</span><span class="val">$${formatNum(p.entry_mc)}</span></div>
            <div class="pos-item"><span class="lbl">Current MC</span><span class="val">$${formatNum(p.current_mc)}</span></div>
            <div class="pos-item"><span class="lbl">Amount</span><span class="val">$${p.amount_usd.toFixed(2)}</span></div>
            <div class="pos-item"><span class="lbl">Since</span><span class="val">${p.entry_time}</span></div>
          </div>
          <div class="tp-sl-bar">
            <div class="tp-box">🎯 TP +${d.tp_pct}%</div>
            <div class="sl-box">🛑 SL -${d.sl_pct}% (max -$1)</div>
            <div class="trail-box">📉 Trail -$1 from peak</div>
          </div>
          <div class="confidence-bar">
            <div class="conf-label"><span>🤖 AI Confidence (min 85% required)</span><span>${p.ai_confidence}%</span></div>
            <div style="background:#2d3748;border-radius:999px;height:6px;overflow:hidden;">
              <div class="conf-fill" style="width:${p.ai_confidence}%"></div>
            </div>
          </div>
          ${peakInfo}
          ${p.ai_reason ? '<div class="ai-reason">💡 ' + p.ai_reason + '</div>' : ''}
        </div>`;
      }).join('');
    }

    const histEl = document.getElementById('trade-history');
    if (d.history.length === 0) {
      histEl.innerHTML = '<div class="no-data">No trades yet</div>';
    } else {
      histEl.innerHTML = d.history.slice().reverse().map(t => {
        const pnlClass = t.pnl_usd >= 0 ? 'pos' : 'neg';
        const pnlSign  = t.pnl_usd >= 0 ? '+' : '';
        const badgeKey = t.result.toLowerCase().replace('-', '');
        return `<div class="trade-row">
          <div>
            <div class="trade-symbol">${t.symbol}</div>
            <div class="trade-detail">${t.time}</div>
          </div>
          <div><span class="badge-${badgeKey}">${t.result}</span></div>
          <div class="trade-pnl ${pnlClass}">${pnlSign}$${Math.abs(t.pnl_usd).toFixed(2)}<br><small style="font-weight:400;">${pnlSign}${t.pnl_pct.toFixed(1)}%</small></div>
        </div>`;
      }).join('');
    }

    document.getElementById('last-updated').textContent = 'Last updated: ' + new Date().toLocaleTimeString();
  } catch(e) {
    console.error(e);
  }
}

function formatNum(n) {
  if (n >= 1000000) return (n/1000000).toFixed(2) + 'M';
  if (n >= 1000)    return (n/1000).toFixed(1) + 'K';
  return n.toFixed(0);
}

fetchData();
setInterval(fetchData, 10000);
</script>
</body>
</html>"""


@app.route("/")
def dashboard():
    return render_template_string(DASHBOARD_HTML)


@app.route("/api/status")
def api_status():
    wins   = [t for t in trade_history if t["result"] == "TP"]
    losses = [t for t in trade_history if t["result"] in ("SL", "FASTCUT", "AI-EXIT")]
    total_pnl = sum(t["pnl_usd"] for t in trade_history)
    win_rate  = (len(wins) / len(trade_history) * 100) if trade_history else 0
    runtime   = datetime.now() - start_time
    hours     = int(runtime.total_seconds() // 3600)
    minutes   = int((runtime.total_seconds() % 3600) // 60)

    positions_data = []
    for addr, pos in positions.items():
        current_price = pos.get("current_price", pos["entry_price"])
        current_mc    = pos.get("current_mc", pos.get("entry_mc", 0))
        pnl_pct       = (current_price - pos["entry_price"]) / pos["entry_price"] * 100
        pnl_usd       = (current_price - pos["entry_price"]) * pos["amount_tokens"]
        positions_data.append({
            "address":       addr,
            "symbol":        pos["symbol"],
            "entry_price":   pos["entry_price"],
            "current_price": current_price,
            "entry_mc":      pos.get("entry_mc", 0),
            "current_mc":    current_mc,
            "amount_usd":    pos["amount_usd"],
            "amount_tokens": pos["amount_tokens"],
            "tp_price":      pos["tp_price"],
            "sl_price":      pos["sl_price"],
            "pnl_pct":       pnl_pct,
            "pnl_usd":       pnl_usd,
            "peak_pnl_usd":  pos.get("peak_pnl_usd", 0.0),
            "ai_confidence": pos.get("ai_confidence", 0),
            "ai_reason":     pos.get("ai_reason", ""),
            "entry_time":    pos["entry_time"].strftime("%H:%M:%S"),
        })

    history_data = []
    for t in trade_history:
        history_data.append({
            "symbol":  t["symbol"],
            "result":  t["result"],
            "pnl_usd": t["pnl_usd"],
            "pnl_pct": t["pnl_pct"],
            "time":    t["time"].strftime("%d %b %H:%M"),
        })

    return jsonify({
        "balance":    round(balance_usd, 4),
        "target":     TARGET_BALANCE_USD,
        "total_pnl":  round(total_pnl, 4),
        "wins":       len(wins),
        "losses":     len(losses),
        "win_rate":   round(win_rate, 1),
        "paused":     bot_paused,
        "scan_count": scan_count,
        "runtime":    f"{hours}h {minutes}m",
        "positions":  positions_data,
        "history":    history_data,
        "tp_pct":     TP_PERCENT,
        "sl_pct":     SL_PERCENT,
    })


def run_web():
    import socket, os, signal
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("0.0.0.0", PORT))
    except OSError:
        try:
            import subprocess
            result = subprocess.check_output(["fuser", f"{PORT}/tcp"], stderr=subprocess.DEVNULL)
            for pid in result.split():
                try:
                    os.kill(int(pid), signal.SIGKILL)
                except:
                    pass
        except:
            pass
        import time as _t; _t.sleep(1)
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False, threaded=True)


# ==================== TELEGRAM ====================

def send_telegram(message, chat_id=None):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, json={
            "chat_id":    chat_id or TELEGRAM_CHAT_ID,
            "text":       message,
            "parse_mode": "HTML"
        }, timeout=10)
    except Exception as e:
        print(f"[Telegram Error] {e}")


def get_updates(offset=0):
    try:
        url  = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
        resp = requests.get(url, params={
            "offset":          offset,
            "timeout":         30,
            "allowed_updates": ["message"]
        }, timeout=35)
        if resp.status_code == 200:
            return resp.json().get("result", [])
    except Exception as e:
        print(f"[getUpdates Error] {e}")
    return []


# ==================== COMMANDS ====================

def cmd_start(chat_id):
    send_telegram(f"""🤖 <b>AI TRADING BOT — STRICT MODE</b>
━━━━━━━━━━━━━━━━━━━━
⚡ Real-Money-Mindset paper trading bot.
🧠 AI (Groq llama-3.3-70b) only enters at 85%+ confidence.
💰 Max loss per trade: $1 | Trail profit: drops $1 from peak → exit

<b>Available Commands:</b>
/start — Welcome message
/status — Full bot status
/balance — Current balance
/positions — Open trade with full details
/history — All closed trades
/pause — Trading rok do
/resume — Trading dubara shuru karo
/help — Ye list

━━━━━━━━━━━━━━━━━━━━
💡 Scan har {CHECK_INTERVAL}s mein
🎯 Target: $20 → ${TARGET_BALANCE_USD:.0f}
📈 TP: +{TP_PERCENT}% | SL: -{SL_PERCENT}%
🛡 Min AI Confidence: {MIN_AI_CONFIDENCE}%
📡 Mode: PAPER TRADING (STRICT)""", chat_id)


def cmd_status(chat_id):
    global balance_usd, positions, trade_history, bot_paused
    wins      = [t for t in trade_history if t["result"] == "TP"]
    losses    = [t for t in trade_history if t["result"] in ("SL", "FASTCUT", "AI-EXIT")]
    total_pnl = sum(t["pnl_usd"] for t in trade_history)
    win_rate  = (len(wins) / len(trade_history) * 100) if trade_history else 0
    progress_pct = max(0, (balance_usd - INITIAL_BALANCE_USD) / (TARGET_BALANCE_USD - INITIAL_BALANCE_USD) * 100)
    runtime   = datetime.now() - start_time
    hours     = int(runtime.total_seconds() // 3600)
    minutes   = int((runtime.total_seconds() % 3600) // 60)

    open_pos_text = ""
    for addr, pos in positions.items():
        cp      = pos.get("current_price", pos["entry_price"])
        pnl     = (cp - pos["entry_price"]) / pos["entry_price"] * 100
        pnl_usd = (cp - pos["entry_price"]) * pos["amount_tokens"]
        peak    = pos.get("peak_pnl_usd", 0)
        open_pos_text += f"\n  • <b>{pos['symbol']}</b>: {pnl:+.1f}% (${pnl_usd:+.2f}) | Peak: +${peak:.2f}"

    status_icon = "⏸" if bot_paused else "▶️"
    send_telegram(f"""📊 <b>BOT STATUS — STRICT MODE</b>
━━━━━━━━━━━━━━━━━━━━
{status_icon} Bot: {'PAUSED' if bot_paused else 'RUNNING'}
⏱ Runtime: {hours}h {minutes}m

💼 Balance: <b>${balance_usd:.2f}</b>
🎯 Target: ${TARGET_BALANCE_USD:.2f}
📈 Progress: {progress_pct:.1f}%

💰 Total PnL: <b>${total_pnl:+.2f}</b>
✅ Wins: {len(wins)} | ❌ Losses: {len(losses)}
🎯 Win Rate: {win_rate:.0f}%
🔄 Total Trades: {len(trade_history)}

📌 Open Positions: {len(positions)}/{MAX_POSITIONS}{open_pos_text}
━━━━━━━━━━━━━━━━━━━━
⚡ Min AI: {MIN_AI_CONFIDENCE}% | Early Exit: {EARLY_EXIT_PCT}% | Hard Cut: {HARD_FAST_CUT_PCT}%
🔒 Trail: -$1 from peak profit""", chat_id)


def cmd_balance(chat_id):
    progress_pct = max(0, (balance_usd - INITIAL_BALANCE_USD) / (TARGET_BALANCE_USD - INITIAL_BALANCE_USD) * 100)
    bar_filled   = int(progress_pct / 10)
    bar          = "🟩" * bar_filled + "⬜" * (10 - bar_filled)
    send_telegram(f"""💼 <b>BALANCE</b>
━━━━━━━━━━━━━━━━━━━━
Current: <b>${balance_usd:.2f}</b>
Start:   ${INITIAL_BALANCE_USD:.2f}
Target:  ${TARGET_BALANCE_USD:.2f}

{bar}
Progress: {progress_pct:.1f}%
Needed:  ${max(0, TARGET_BALANCE_USD - balance_usd):.2f} more
━━━━━━━━━━━━━━━━━━━━""", chat_id)


def cmd_positions(chat_id):
    if not positions:
        send_telegram("📌 <b>Open Positions:</b> Koi bhi open trade nahi hai abhi.", chat_id)
        return

    msg = "📌 <b>OPEN POSITION — FULL DETAILS</b>"
    for addr, pos in positions.items():
        cp         = pos.get("current_price", pos["entry_price"])
        current_mc = pos.get("current_mc", pos.get("entry_mc", 0))
        pnl_pct    = (cp - pos["entry_price"]) / pos["entry_price"] * 100
        pnl_usd    = (cp - pos["entry_price"]) * pos["amount_tokens"]
        peak       = pos.get("peak_pnl_usd", 0)
        entry_time = pos["entry_time"].strftime("%d %b %H:%M:%S")
        icon       = "📈" if pnl_usd >= 0 else "📉"

        def fmt_mc(v):
            if v >= 1_000_000: return f"${v/1_000_000:.2f}M"
            if v >= 1_000:     return f"${v/1_000:.1f}K"
            return f"${v:.0f}"

        msg += f"""

{icon} <b>{pos['symbol']}</b>
🔗 <code>{addr}</code>

📊 Entry MC:   <b>{fmt_mc(pos.get('entry_mc',0))}</b>
📊 Now MC:     <b>{fmt_mc(current_mc)}</b>

💵 Entry:  <code>${pos['entry_price']:.10f}</code>
💵 Now:    <code>${cp:.10f}</code>

📈 Live PnL: <b>{pnl_pct:+.2f}% (${pnl_usd:+.2f})</b>
🏔 Peak Profit: <b>+${peak:.2f}</b>
💰 Invested: ${pos['amount_usd']:.2f}

🎯 TP: <code>${pos['tp_price']:.10f}</code> (+{TP_PERCENT}%)
🛑 SL: <code>${pos['sl_price']:.10f}</code> (-{SL_PERCENT}%)
⚡ Early Exit: {EARLY_EXIT_PCT}% | Hard Cut: {HARD_FAST_CUT_PCT}%

🤖 AI Confidence: <b>{pos.get('ai_confidence','?')}%</b>
💡 {pos.get('ai_reason','N/A')}

⏱ Opened: {entry_time}"""

    msg += "\n━━━━━━━━━━━━━━━━━━━━"
    send_telegram(msg, chat_id)


def cmd_history(chat_id):
    if not trade_history:
        send_telegram("📜 <b>History:</b> Abhi tak koi trade close nahi hua.", chat_id)
        return

    msg = f"📜 <b>ALL TRADES ({len(trade_history)} total)</b>\n━━━━━━━━━━━━━━━━━━━━"
    for t in reversed(trade_history):
        icons = {"TP": "✅", "SL": "❌", "AI-EXIT": "🤖", "TRAIL": "📉", "FASTCUT": "✂️", "RECOVERY": "🔄"}
        icon  = icons.get(t["result"], "❓")
        trade_time = t["time"].strftime("%d %b %H:%M")
        msg  += f"\n{icon} <b>{t['symbol']}</b> [{t['result']}] {t['pnl_pct']:+.1f}% (${t['pnl_usd']:+.2f}) — {trade_time}"

    total_pnl = sum(t["pnl_usd"] for t in trade_history)
    wins      = len([t for t in trade_history if t["result"] == "TP"])
    win_rate  = wins / len(trade_history) * 100 if trade_history else 0
    msg += f"\n━━━━━━━━━━━━━━━━━━━━\nTotal PnL: <b>${total_pnl:+.2f}</b> | Win Rate: {win_rate:.0f}% ({wins}/{len(trade_history)})"
    send_telegram(msg, chat_id)


def cmd_pause(chat_id):
    global bot_paused
    bot_paused = True
    send_telegram("⏸ <b>Bot PAUSED.</b>\nNaye trades nahi lega.\n/resume se dubara shuru karo.", chat_id)
    print("[BOT] Paused by Telegram command")


def cmd_resume(chat_id):
    global bot_paused
    bot_paused = False
    send_telegram("▶️ <b>Bot RESUMED.</b>\nPhir se tokens scan karega.", chat_id)
    print("[BOT] Resumed by Telegram command")


def cmd_help(chat_id):
    send_telegram("""❓ <b>BOT COMMANDS</b>
━━━━━━━━━━━━━━━━━━━━
/start — Welcome message
/status — Full bot status
/balance — Balance aur progress
/positions — Open trade full details
/history — All closed trades
/pause — Naye trades rokna
/resume — Trading resume karna
/help — Ye list
━━━━━━━━━━━━━━━━━━━━""", chat_id)


def handle_command(text, chat_id):
    text = text.strip().lower().split()[0]
    if text in ["/start", "/start@bot"]:
        cmd_start(chat_id)
    elif text == "/status":
        cmd_status(chat_id)
    elif text == "/balance":
        cmd_balance(chat_id)
    elif text == "/positions":
        cmd_positions(chat_id)
    elif text == "/history":
        cmd_history(chat_id)
    elif text == "/pause":
        cmd_pause(chat_id)
    elif text == "/resume":
        cmd_resume(chat_id)
    elif text == "/help":
        cmd_help(chat_id)
    else:
        send_telegram(f"❓ Unknown command: <code>{text}</code>\nType /help for all commands.", chat_id)


def command_listener():
    global last_update_id
    print("[Commands] Listener started — waiting for Telegram messages...")
    while True:
        try:
            updates = get_updates(offset=last_update_id + 1)
            for update in updates:
                last_update_id = update["update_id"]
                msg    = update.get("message", {})
                text   = msg.get("text", "")
                chat_id = msg.get("chat", {}).get("id")
                if text and text.startswith("/") and chat_id:
                    print(f"[Command] Received: {text} from chat {chat_id}")
                    handle_command(text, chat_id)
        except Exception as e:
            print(f"[Command Listener Error] {e}")
            time.sleep(5)


# ==================== AI BRAIN (STRICT ENTRY — REAL MONEY MINDSET) ====================

_ai_down          = False
_ai_down_notified = False


def _build_prompt(token_data: dict) -> str:
    """
    AI entry prompt — STRICT REAL-MONEY mindset.
    Only BUY at 85%+ confidence with overwhelming evidence.
    Treat every dollar as irreplaceable real money.
    """
    age         = token_data.get("pair_age_hours", 999)
    age_str     = f"{age:.1f}h old" if age < 999 else "age unknown"
    age_note    = "🆕 VERY FRESH LAUNCH" if age <= 1 else ("fresh" if age <= 4 else ("new coin" if age <= 8 else "older coin"))
    social_line = (
        f"Twitter={'✅' if token_data.get('has_twitter') else '❌'} | "
        f"Telegram={'✅' if token_data.get('has_telegram_social') else '❌'} | "
        f"Website={'✅' if token_data.get('has_website') else '❌'}"
    )

    return f"""You are a PROFESSIONAL Solana meme coin trader managing REAL MONEY with extreme caution.

⚠️ CRITICAL MINDSET: Treat every dollar as if it physically hurts to lose. 
- If there is ANY doubt, SKIP — it is better to miss 10 trades than lose $1.
- Only BUY with overwhelming conviction and 85%+ confidence.
- Prefer quality over quantity. 2-3 great trades per day >> 10 average trades.
- A SKIP never loses money. A bad BUY can wipe your balance.

TOKEN ANALYSIS:
- Symbol: {token_data.get('symbol')} ({age_str}) [{age_note}]
- Price: ${token_data.get('price', 0):.10f}
- FDV (Market Cap): ${token_data.get('mc', 0):,.0f}  [ideal: $100K–$1.5M for big upside]
- Liquidity: ${token_data.get('liquidity', 0):,.0f}  [ideal: $20K–$200K]
- 5min Volume: ${token_data.get('vol_5m', 0):,.0f}  [need: >$8K to confirm real buying]
- 1hr Volume: ${token_data.get('vol_1h', 0):,.0f}  [need: >$20K sustained]
- Buy/Sell Ratio 5m: {token_data.get('buy_ratio_5m', 0):.2f}x  [NEED >1.7x = buyers dominating heavily]
- Buy/Sell Ratio 1h: {token_data.get('buy_ratio_1h', 0):.2f}x  [>1.2x preferred]
- Price Change 5m: {token_data.get('price_change_5m', 0):.2f}%  [ideal: +1% to +30%, already moving]
- Price Change 1h: {token_data.get('price_change_1h', 0):.2f}%  [MUST be <80% or already pumped]
- LP Locked: {token_data.get('lp_locked', 0):.1f}% | Mint Revoked: {token_data.get('mint_revoked', False)}
- Socials: {social_line}
- Confirmations: {token_data.get('confirmations_passed', 0)}/12

STRICT BUY CONDITIONS — ALL must be true for high conviction BUY:
1. ✅ Buy ratio 5m >= 1.7x — strong buyer dominance RIGHT NOW
2. ✅ FDV $100K–$1.5M — significant upside room exists
3. ✅ Liquidity $20K–$200K — real liquidity, not a ghost pool
4. ✅ Price NOT already pumped >80% in 1h — not buying at the top
5. ✅ Vol 5m >= $8K — real money flowing in this very minute  
6. ✅ Vol 1h >= $20K — sustained interest, not just one spike
7. ✅ At least 2 socials present (Twitter/TG/Website) — real community
8. ✅ Coin < 8h old — early entry with good potential
9. ✅ LP locked OR mint revoked — basic safety

INSTANT SKIP if ANY of these:
- Buy ratio < 1.7x (sellers competing or neutral)
- Already pumped >80% in 1h (bought at top = extreme risk)
- No socials at all (rug risk high)
- FDV > $2M (limited upside for a quick trade)
- Vol 5m < $5K (not enough buying activity)

CONFIDENCE CALIBRATION:
- 90-100%: Near-perfect setup, ALL criteria satisfied, strong momentum
- 85-89%: Strong setup, most criteria satisfied, good entry point
- 70-84%: Good setup but some doubts — SKIP (we need 85%+)
- Below 70%: Clear SKIP

Remember: You are protecting REAL money. A confident SKIP is a WIN.

Respond ONLY in this exact JSON (no markdown, no extra text):
{{"decision": "BUY" or "SKIP", "confidence": 0-100, "reason": "one sentence explaining the key reason"}}"""


def ask_ai_brain(token_data: dict) -> dict:
    global _ai_down, _ai_down_notified

    if not GROQ_SLOTS:
        return _rule_based_fallback(token_data, notify=True)

    prompt      = _build_prompt(token_data)
    payload_base = {
        "messages": [
            {"role": "system", "content": "You are a strict crypto trading AI protecting real money. Respond ONLY in valid JSON. No markdown."},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.1,   # Very low temperature — we want consistent, conservative decisions
        "max_tokens":  150,
    }

    for idx, (api_key, model) in enumerate(GROQ_SLOTS):
        try:
            payload = {**payload_base, "model": model}
            resp    = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=payload,
                timeout=15,
            )
            if resp.status_code == 200:
                content = resp.json()["choices"][0]["message"]["content"].strip()
                content = content.replace("```json", "").replace("```", "").strip()
                result  = json.loads(content)
                if _ai_down:
                    _ai_down          = False
                    _ai_down_notified = False
                    key_num = GROQ_KEYS.index(api_key) + 1
                    send_telegram(f"✅ <b>AI Brain BACK ONLINE</b>\nKey #{key_num} | Model: <code>{model}</code>")
                key_num = GROQ_KEYS.index(api_key) + 1 if api_key in GROQ_KEYS else "?"
                print(f"[🤖 AI Key#{key_num} {model}] {result.get('decision')} {result.get('confidence')}%")
                return result
            else:
                print(f"[Groq Slot {idx+1}] Error {resp.status_code} {model}: {resp.text[:80]}")
        except json.JSONDecodeError:
            print(f"[Groq Slot {idx+1}] JSON parse error — {model}")
        except Exception as e:
            print(f"[Groq Slot {idx+1}] Exception {model}: {e}")

    _ai_down = True
    return _rule_based_fallback(token_data, notify=True)


def _rule_based_fallback(token_data: dict, notify: bool = False) -> dict:
    """Conservative rule-based fallback when all Groq slots fail. Stricter than before."""
    global _ai_down_notified

    br5m   = token_data.get("buy_ratio_5m", 0)
    pc5m   = token_data.get("price_change_5m", 0)
    pc1h   = token_data.get("price_change_1h", 0)
    liq    = token_data.get("liquidity", 0)
    vol5m  = token_data.get("vol_5m", 0)
    vol1h  = token_data.get("vol_1h", 0)
    confs  = token_data.get("confirmations_passed", 0)
    sym    = token_data.get("symbol", "?")
    social = token_data.get("social_score", 0)

    # Hard rejects (no AI = be very conservative, don't trade without good reason)
    if br5m < 1.7:
        result = {"decision": "SKIP", "confidence": 0, "reason": f"[NoAI] Buy pressure weak BR={br5m:.2f}<1.7"}
    elif pc1h > 80:
        result = {"decision": "SKIP", "confidence": 0, "reason": f"[NoAI] Already pumped {pc1h:.0f}% in 1h — too risky"}
    elif liq < 15000 or liq > 400000:
        result = {"decision": "SKIP", "confidence": 0, "reason": f"[NoAI] Liq ${liq:,.0f} outside safe range"}
    elif vol5m < 8000:
        result = {"decision": "SKIP", "confidence": 0, "reason": f"[NoAI] Vol5m ${vol5m:,.0f} < $8K insufficient"}
    elif vol1h < 20000:
        result = {"decision": "SKIP", "confidence": 0, "reason": f"[NoAI] Vol1h ${vol1h:,.0f} < $20K insufficient"}
    elif social < 2:
        result = {"decision": "SKIP", "confidence": 0, "reason": f"[NoAI] Only {social} social — rug risk high"}
    else:
        score = 0
        if br5m >= 3.0:            score += 25
        elif br5m >= 2.5:          score += 20
        elif br5m >= 2.0:          score += 15
        elif br5m >= 1.7:          score += 10
        if 2 <= pc5m <= 25:        score += 20
        elif pc5m > 0:             score += 8
        if pc1h < 40:              score += 15
        elif pc1h < 60:            score += 8
        if 30000 <= liq <= 150000: score += 15
        elif 20000 <= liq <= 200000: score += 8
        if vol5m >= 30000:         score += 15
        elif vol5m >= 15000:       score += 8
        if confs >= 10:            score += 10
        elif confs >= 9:           score += 5
        if social >= 3:            score += 10
        elif social >= 2:          score += 5
        confidence = min(score, 84)  # NoAI fallback caps at 84% — never hits our 85% threshold
        result = {
            "decision":   "SKIP",
            "confidence": confidence,
            "reason":     f"[NoAI] Score {confidence}% below {MIN_AI_CONFIDENCE}% threshold — skipping without AI"
        }

    if notify and not _ai_down_notified:
        _ai_down_notified = True
        send_telegram(
            f"⚠️ <b>AI Brain DOWN — All Groq APIs failed</b>\n"
            f"Bot switching to conservative <b>SKIP-all</b> mode (no AI = no trades).\n"
            f"Token <b>{sym}</b> skipped to protect capital.\n\n"
            f"📌 {len(GROQ_SLOTS)} slots tried ({len(GROQ_KEYS)} keys × {len(GROQ_MODELS)} models)\n"
            f"Bot will auto-resume AI when Groq comes back online."
        )
    return result


# ==================== AI EXIT DECISION (AGGRESSIVE PROFIT PROTECTION) ====================

def _ask_ai_exit(pos: dict, current_price: float, current_mc: float,
                 pnl_percent: float, pnl_usd: float, trigger_reason: str) -> dict:
    """
    AI exit check — EXTREMELY aggressive profit/capital protection.
    Bias is strongly toward EXIT. We do NOT ride losses or give back profits.
    """
    peak_pnl_usd  = pos.get("peak_pnl_usd", 0.0)
    minutes_in    = int((datetime.now() - pos["entry_time"]).total_seconds() / 60)
    drop_from_peak = peak_pnl_usd - pnl_usd if peak_pnl_usd > 0 else 0

    prompt = f"""You are a STRICT risk manager protecting real money in a Solana meme coin trade.

CURRENT TRADE STATE:
- Token: {pos['symbol']}
- Entry Price: ${pos['entry_price']:.10f}
- Current Price: ${current_price:.10f}
- Current MC: ${current_mc:,.0f}
- Current PnL: {pnl_percent:+.2f}% (${pnl_usd:+.2f} USD)
- Peak Profit Reached: +${peak_pnl_usd:.2f} USD
- Profit Given Back from Peak: ${drop_from_peak:.2f} USD
- Time in Trade: {minutes_in} minutes
- Exit Trigger Reason: {trigger_reason}

OUR RISK RULES:
- Maximum acceptable loss: $1.00 (we entered to protect capital, not gamble)
- Once in loss, every second risks more capital
- If we had profit and are giving it back, that hurts as much as a real loss
- Speed of price drop matters — fast drops = EXIT immediately

DECISION FRAMEWORK:
EXIT immediately if ANY of these:
1. In loss ({pnl_percent:.1f}%) and momentum has NOT clearly reversed upward
2. Was profitable but now giving back profit (drop from peak: ${drop_from_peak:.2f})
3. Price action is choppy/sideways in loss territory (wasting time = opportunity cost)
4. Volume is drying up while price is falling (no support incoming)

HOLD only if ALL of these:
1. This is a brief dip with clear bounce signals
2. Buy volume is still strong (buyers defending a level)
3. The overall trend is clearly still up
4. Loss is very small (<0.3%) and bounce is likely within 30-60 seconds

Remember: A fast small loss is far better than a slow big loss.
The correct answer is almost always EXIT when things go wrong.

Respond ONLY in valid JSON:
{{"action": "EXIT" or "HOLD", "reason": "one sentence max", "urgency": "HIGH" or "NORMAL"}}"""

    for api_key, model in GROQ_SLOTS:
        try:
            resp = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model":    model,
                    "messages": [
                        {"role": "system", "content": "You are a strict risk manager. Respond ONLY in valid JSON. Bias toward EXIT to protect capital."},
                        {"role": "user",   "content": prompt}
                    ],
                    "temperature": 0.05,   # Near-zero temp — conservative, consistent decisions
                    "max_tokens":  100,
                },
                timeout=10,   # Faster timeout — we need decisions quickly
            )
            if resp.status_code == 200:
                content = resp.json()["choices"][0]["message"]["content"].strip()
                content = content.replace("```json", "").replace("```", "").strip()
                return json.loads(content)
        except:
            continue

    # Fallback: if AI is down, exit immediately on any loss — capital protection first
    if pnl_percent < -0.5:
        return {"action": "EXIT", "reason": "[NoAI] Loss detected — exiting to protect capital (AI unavailable)", "urgency": "HIGH"}
    return {"action": "EXIT", "reason": "[NoAI] AI unavailable — exiting to protect profit/capital", "urgency": "NORMAL"}


# ==================== SCANNING ====================

def rugcheck_token(token_address):
    try:
        url  = f"https://api.rugcheck.xyz/v1/tokens/{token_address}/report"
        resp = requests.get(url, timeout=8)
        if resp.status_code == 200:
            data          = resp.json()
            sec           = data.get("security", {})
            lp_locked     = sec.get("lpLockedPercentage") or 0
            mint_revoked  = sec.get("mintAuthorityRevoked", False)
            return {
                "lp_locked_pct": lp_locked,
                "mint_revoked":  mint_revoked,
                "is_safe":       lp_locked >= MIN_LP_LOCKED_PCT or mint_revoked
            }
    except Exception as e:
        print(f"[Rugcheck Error] {e}")
    return {"lp_locked_pct": 0, "mint_revoked": False, "is_safe": False}


def _pair_age_hours(pair: dict) -> float:
    created_at = pair.get("pairCreatedAt")
    if not created_at:
        return 999.0
    try:
        created_ts = int(created_at) / 1000
        return (time.time() - created_ts) / 3600
    except:
        return 999.0


def _fetch_pairs_concurrent(addresses: list, max_workers: int = 6) -> list:
    """Fetch multiple token pairs concurrently using threads — faster scanning."""
    results   = []
    lock      = threading.Lock()

    def fetch_one(addr):
        if not addr:
            return
        try:
            r = requests.get(
                f"https://api.dexscreener.com/latest/dex/tokens/{addr}",
                timeout=7
            )
            if r.status_code == 200:
                for p in r.json().get("pairs", []):
                    with lock:
                        results.append(p)
        except:
            pass

    threads = []
    for addr in addresses:
        t = threading.Thread(target=fetch_one, args=(addr,), daemon=True)
        threads.append(t)
        t.start()
        # Throttle — launch at most max_workers at a time
        if len([x for x in threads if x.is_alive()]) >= max_workers:
            time.sleep(0.05)

    for t in threads:
        t.join(timeout=8)

    return results


def get_dexscreener_pairs():
    """
    Fetch Solana token pairs from multiple DexScreener sources concurrently.
    New coins (<4h) are prioritized. Strict dedup on address.
    """
    new_pairs   = []
    old_pairs   = []
    seen_addrs  = set()

    def add_pair(p):
        a = p.get("baseToken", {}).get("address")
        if not a or a in seen_addrs:
            return
        seen_addrs.add(a)
        age = _pair_age_hours(p)
        if age <= NEW_COIN_MAX_AGE_HOURS:
            new_pairs.append(p)
        else:
            old_pairs.append(p)

    # Collect all token addresses from boost endpoints first (non-blocking, fast)
    boost_addresses = []
    for endpoint in [
        "https://api.dexscreener.com/token-boosts/top/v1",
        "https://api.dexscreener.com/token-boosts/latest/v1",
    ]:
        try:
            resp = requests.get(endpoint, timeout=10)
            if resp.status_code == 200:
                boosted = resp.json() if isinstance(resp.json(), list) else []
                for b in boosted:
                    if b.get("chainId") == "solana":
                        addr = b.get("tokenAddress")
                        if addr:
                            boost_addresses.append(addr)
        except Exception as e:
            print(f"[DexScreener Boost Error] {e}")

    # Collect addresses from latest token profiles
    profile_addresses = []
    try:
        resp = requests.get("https://api.dexscreener.com/token-profiles/latest/v1", timeout=10)
        if resp.status_code == 200:
            profiles = resp.json() if isinstance(resp.json(), list) else []
            for p in profiles:
                if p.get("chainId") == "solana":
                    addr = p.get("tokenAddress")
                    if addr:
                        profile_addresses.append(addr)
    except Exception as e:
        print(f"[DexScreener Profiles Error] {e}")

    # Fetch all token data concurrently — much faster than serial
    all_addresses = list(dict.fromkeys(boost_addresses[:30] + profile_addresses[:30]))
    if all_addresses:
        pairs_from_addresses = _fetch_pairs_concurrent(all_addresses, max_workers=8)
        for p in pairs_from_addresses:
            add_pair(p)

    # Search queries — serial (API rate limit friendly)
    for query in ["SOL meme new", "pump", "solana"]:
        try:
            resp = requests.get(
                f"https://api.dexscreener.com/latest/dex/search?q={query}",
                timeout=10
            )
            if resp.status_code == 200:
                for p in resp.json().get("pairs", []):
                    add_pair(p)
        except Exception as e:
            print(f"[DexScreener Search Error q={query}] {e}")

    all_pairs = new_pairs + old_pairs
    print(f"[DexScreener] {len(new_pairs)} NEW (<{NEW_COIN_MAX_AGE_HOURS}h) + {len(old_pairs)} older = {len(all_pairs)} total pairs")
    return all_pairs


def run_12_confirmations(pair, rug_data):
    """Run 12-point confirmation check. Now requires 9/12 (was 8/12) for strict mode."""
    base  = pair.get("baseToken", {})
    quote = pair.get("quoteToken", {})

    if quote.get("symbol") != "SOL":
        return False, 0, {}

    token_addr = base.get("address")
    if not token_addr:
        return False, 0, {}

    try:
        price_usd  = float(pair.get("priceUsd") or 0)
        liquidity  = float(pair.get("liquidity", {}).get("usd") or 0)
        mc         = float(pair.get("fdv") or pair.get("marketCap") or 0)

        vol        = pair.get("volume", {})
        vol_5m     = float(vol.get("m5") or 0)
        vol_1h     = float(vol.get("h1") or 0)
        vol_24h    = float(vol.get("h24") or 0)

        price_change = pair.get("priceChange", {})
        pc_5m        = float(price_change.get("m5") or 0)
        pc_1h        = float(price_change.get("h1") or 0)
        pc_24h       = float(price_change.get("h24") or 0)

        txns_5m     = pair.get("txns", {}).get("m5", {})
        buys_5m     = int(txns_5m.get("buys") or 0)
        sells_5m    = int(txns_5m.get("sells") or 1)

        txns_1h     = pair.get("txns", {}).get("h1", {})
        buys_1h     = int(txns_1h.get("buys") or 0)
        sells_1h    = int(txns_1h.get("sells") or 1)

        buy_ratio_5m  = buys_5m / sells_5m if sells_5m > 0 else 0
        buy_ratio_1h  = buys_1h / sells_1h if sells_1h > 0 else 0
        vol_mc_ratio  = vol_1h / mc if mc > 0 else 0

        lp_locked     = rug_data.get("lp_locked_pct", 0)
        mint_revoked  = rug_data.get("mint_revoked", False)

        info          = pair.get("info", {})
        socials       = info.get("socials", [])
        websites      = info.get("websites", [])
        has_twitter   = any(s.get("type", "").lower() in ("twitter", "x") for s in socials)
        has_telegram_social = any(s.get("type", "").lower() == "telegram" for s in socials)
        has_website   = len(websites) > 0
        social_score  = sum([has_twitter, has_telegram_social, has_website])

        confirmations = 0
        check_results = []

        checks = [
            (MIN_LIQUIDITY <= liquidity <= MAX_LIQUIDITY,              f"1. Liq ${liquidity:,.0f} in [${MIN_LIQUIDITY:,}–${MAX_LIQUIDITY:,}]"),
            (vol_5m >= MIN_5M_VOLUME,                                  f"2. Vol5m ${vol_5m:,.0f}>=${MIN_5M_VOLUME:,}"),
            (vol_1h >= MIN_1H_VOLUME,                                  f"3. Vol1h ${vol_1h:,.0f}>=${MIN_1H_VOLUME:,}"),
            (round(buy_ratio_5m, 2) >= MIN_BUY_RATIO_5M,              f"4. BR5m {buy_ratio_5m:.2f}>={MIN_BUY_RATIO_5M} (>70% buys)"),
            (round(buy_ratio_1h, 2) >= MIN_BUY_RATIO_1H,              f"5. BR1h {buy_ratio_1h:.2f}>={MIN_BUY_RATIO_1H}"),
            (MIN_MC <= mc <= MAX_MC,                                   f"6. FDV ${mc:,.0f} in [${MIN_MC:,}–${MAX_MC:,}]"),
            (lp_locked >= MIN_LP_LOCKED_PCT or mint_revoked,           f"7. Safety LP={lp_locked:.0f}% Rev={mint_revoked}"),
            (MIN_PRICE_CHANGE_5M <= pc_5m <= MAX_PRICE_CHANGE_5M,     f"8. PC5m {pc_5m:.1f}% in range"),
            (MIN_PRICE_CHANGE_1H <= pc_1h <= MAX_PRICE_CHANGE_1H,     f"9. PC1h {pc_1h:.1f}% in range"),
            (vol_mc_ratio >= MIN_VOLUME_MCAP_RATIO,                   f"10. VolMC {vol_mc_ratio:.4f}>={MIN_VOLUME_MCAP_RATIO}"),
            (buys_5m >= MIN_BUYS_5M,                                   f"11. Buys5m {buys_5m}>={MIN_BUYS_5M}"),
            (social_score >= 2,                                        f"12. Socials({social_score}/3) Twitter={has_twitter} TG={has_telegram_social} Web={has_website}"),
        ]

        for passed, label in checks:
            check_results.append(f"{label}: {'✅' if passed else '❌'}")
            if passed:
                confirmations += 1

        token_data = {
            "address":             token_addr,
            "symbol":              base.get("symbol", "UNKNOWN"),
            "price":               price_usd,
            "liquidity":           liquidity,
            "mc":                  mc,
            "vol_5m":              vol_5m,
            "vol_1h":              vol_1h,
            "vol_24h":             vol_24h,
            "buy_ratio_5m":        buy_ratio_5m,
            "buy_ratio_1h":        buy_ratio_1h,
            "price_change_5m":     pc_5m,
            "price_change_1h":     pc_1h,
            "price_change_24h":    pc_24h,
            "lp_locked":           lp_locked,
            "mint_revoked":        mint_revoked,
            "vol_mc_ratio":        vol_mc_ratio,
            "buys_5m":             buys_5m,
            "has_twitter":         has_twitter,
            "has_telegram_social": has_telegram_social,
            "has_website":         has_website,
            "social_score":        social_score,
            "confirmations_passed": confirmations,
            "check_results":       check_results,
        }

        return confirmations >= MIN_CONFIRMATIONS, confirmations, token_data

    except Exception as e:
        print(f"[Confirm Error] {e}")
        return False, 0, {}


def analyze_pair(pair):
    """
    Pre-filter → 12 confirmations → strict AI check (85%+ confidence required).
    Very selective — only the very best setups get through.
    """
    base       = pair.get("baseToken", {})
    quote      = pair.get("quoteToken", {})
    symbol     = base.get("symbol", "?")

    if quote.get("symbol") != "SOL":
        return None

    token_addr = base.get("address")
    if not token_addr or token_addr in seen_tokens or token_addr in positions or token_addr in traded_coins:
        return None

    # Age filter
    age_hours = _pair_age_hours(pair)
    if age_hours > NEW_COIN_MAX_AGE_HOURS:
        return None

    # Hard pump rejection — don't buy already-pumped coins
    try:
        pc1h_raw = float(pair.get("priceChange", {}).get("h1") or 0)
        if pc1h_raw > MAX_PRICE_CHANGE_1H:
            return None
    except:
        pass

    # Pre-filter: fast rejection before expensive API calls
    try:
        liquidity    = float(pair.get("liquidity", {}).get("usd") or 0)
        vol_5m       = float(pair.get("volume", {}).get("m5") or 0)
        mc           = float(pair.get("fdv") or pair.get("marketCap") or 0)
        txns_5m      = pair.get("txns", {}).get("m5", {})
        buys_5m      = int(txns_5m.get("buys") or 0)
        sells_5m     = int(txns_5m.get("sells") or 1)
        buy_ratio_5m = buys_5m / sells_5m if sells_5m > 0 else 0

        fail_reasons = []
        if liquidity < MIN_LIQUIDITY:    fail_reasons.append(f"Liq ${liquidity:,.0f}<${MIN_LIQUIDITY:,}")
        if liquidity > MAX_LIQUIDITY:    fail_reasons.append(f"Liq ${liquidity:,.0f}>${MAX_LIQUIDITY:,}")
        if vol_5m    < MIN_5M_VOLUME:    fail_reasons.append(f"Vol5m ${vol_5m:,.0f}<${MIN_5M_VOLUME:,}")
        if mc        < MIN_MC:           fail_reasons.append(f"FDV ${mc:,.0f}<${MIN_MC:,}")
        if mc        > MAX_MC:           fail_reasons.append(f"FDV ${mc:,.0f}>${MAX_MC:,}")
        if buy_ratio_5m < MIN_BUY_RATIO_5M and buys_5m > 3:
            fail_reasons.append(f"BuyPress {buy_ratio_5m:.2f}x<{MIN_BUY_RATIO_5M}x")

        if fail_reasons:
            print(f"[PreFilter ❌] {symbol}: {' | '.join(fail_reasons)}")
            return None
    except:
        return None

    print(f"[PreFilter ✅] {symbol} passed — checking rugcheck...")
    rug_data = rugcheck_token(token_addr)
    passed, score, token_data = run_12_confirmations(pair, rug_data)

    if not passed:
        fails = [r for r in token_data.get("check_results", []) if "❌" in r]
        print(f"[Skip] {symbol} — {score}/12 checks (need {MIN_CONFIRMATIONS}) | Failed: {', '.join(fails[:3])}")
        seen_tokens.add(token_addr)   # Mark as seen to skip on next scan
        return None

    print(f"\n[🔍 AI Check] {symbol} passed {score}/12 — asking AI (need {MIN_AI_CONFIDENCE}%+ confidence)...")
    ai_result  = ask_ai_brain(token_data)
    decision   = ai_result.get("decision", "SKIP")
    confidence = ai_result.get("confidence", 0)
    reason     = ai_result.get("reason", "")

    print(f"[🤖 AI] {decision} | {confidence}% | {reason}")

    # STRICT: minimum 85% confidence required — no exceptions
    if decision != "BUY" or confidence < MIN_AI_CONFIDENCE:
        print(f"[Skip] AI rejected {symbol} ({confidence}% < {MIN_AI_CONFIDENCE}% required): {reason}")
        seen_tokens.add(token_addr)
        return None

    token_data["ai_confidence"] = confidence
    token_data["ai_reason"]     = reason
    token_data["score"]         = score
    token_data["pair_age_hours"] = age_hours
    return token_data


# ==================== TRADING ====================

def simulate_buy(token_data):
    global balance_usd

    if balance_usd < MIN_TRADE_USD:
        return False
    if len(positions) >= MAX_POSITIONS:
        return False

    token_addr   = token_data["address"]
    symbol       = token_data["symbol"]
    price_usd    = token_data["price"]
    amount_usd   = balance_usd
    tokens_bought = amount_usd / price_usd
    entry_mc     = token_data.get("mc", 0)

    positions[token_addr] = {
        "symbol":        symbol,
        "entry_price":   price_usd,
        "amount_tokens": tokens_bought,
        "amount_usd":    amount_usd,
        "tp_price":      price_usd * (1 + TP_PERCENT / 100),
        "sl_price":      price_usd * (1 - SL_PERCENT / 100),
        "entry_mc":      entry_mc,
        "current_mc":    entry_mc,
        "current_price": price_usd,
        "entry_time":    datetime.now(),
        "ai_confidence": token_data.get("ai_confidence", 0),
        "ai_reason":     token_data.get("ai_reason", ""),
        "score":         token_data.get("score", 0),
        # ---- Profit protection tracking ----
        "peak_pnl_usd":  0.0,   # Highest $ profit seen — trailing stop uses this
        "peak_price":    price_usd,
        "last_price":    price_usd,   # Price at last check — for fast momentum detection
        "last_check_ts": time.time(),
        # ---- Loss tracking ----
        "low_pnl":       0.0,
        "was_in_loss":   False,
        "ai_exit_count": 0,     # How many times we asked AI to exit (avoid spam)
    }

    balance_usd  -= amount_usd
    traded_coins.add(token_addr)
    seen_tokens.add(token_addr)

    age_str = f"{token_data.get('pair_age_hours', 999):.1f}h" if token_data.get('pair_age_hours', 999) < 999 else "?"
    send_telegram(f"""🚀 <b>SIMULATED BUY — STRICT MODE</b>
━━━━━━━━━━━━━━━━━━━━
🪙 Token: <b>{symbol}</b> (🆕 {age_str} old)
🔗 <code>{token_addr}</code>

💵 Entry Price: ${price_usd:.10f}
💰 Amount: <b>${amount_usd:.2f}</b> (full balance)
📊 Entry MC: ${entry_mc:,.0f}

🎯 TP: +{TP_PERCENT}% | 🛑 SL: -{SL_PERCENT}% (max -$1)
⚡ Early Exit: {EARLY_EXIT_PCT}% | Hard Cut: {HARD_FAST_CUT_PCT}%
📉 Trail: Exit if profit drops $1 from peak

🤖 AI Confidence: <b>{token_data.get('ai_confidence', 0)}%</b> ✅ (min {MIN_AI_CONFIDENCE}%)
📊 Confirmations: {token_data.get('score', 0)}/12
💡 {token_data.get('ai_reason', 'N/A')}

💧 Liq: ${token_data.get('liquidity', 0):,.0f}
📊 Vol 5m: ${token_data.get('vol_5m', 0):,.0f}
🔄 Buy Ratio: {token_data.get('buy_ratio_5m', 0):.2f}x
━━━━━━━━━━━━━━━━━━━━""")

    print(f"[BUY ✅] {symbol} ({age_str}) @ ${price_usd:.10f} | ${amount_usd:.2f} | AI: {token_data.get('ai_confidence')}%")
    return True


def _close_position(addr, pos, current_price, pnl_percent, pnl_usd, exit_value, result_label, reason=""):
    global balance_usd
    balance_usd += exit_value
    trade_history.append({
        "symbol":  pos["symbol"],
        "pnl_usd": pnl_usd,
        "pnl_pct": pnl_percent,
        "result":  result_label,
        "time":    datetime.now(),
    })
    del positions[addr]

    icons  = {"TP": "✅", "SL": "❌", "AI-EXIT": "🤖", "TRAIL": "📉", "FASTCUT": "✂️", "RECOVERY": "🔄"}
    titles = {
        "TP":       "TAKE PROFIT HIT!",
        "SL":       "HARD STOP LOSS",
        "AI-EXIT":  "AI EARLY EXIT — Capital Protected",
        "TRAIL":    "TRAILING STOP — Profit Locked!",
        "FASTCUT":  "FAST CUT — Loss Stopped Immediately",
        "RECOVERY": "RECOVERY EXIT — Smart Sell",
    }
    icon  = icons.get(result_label, "⚠️")
    title = titles.get(result_label, "POSITION CLOSED")

    sign   = "+" if pnl_usd >= 0 else ""
    pnl_str = f"{sign}${pnl_usd:.2f} ({sign}{pnl_percent:.1f}%)"
    peak    = pos.get("peak_pnl_usd", 0)
    peak_line = f"\n🏔 Peak was: +${peak:.2f}" if peak > 0.3 else ""
    reason_line = f"\n💡 {reason}" if reason else ""

    send_telegram(f"""{icon} <b>{title}</b>
━━━━━━━━━━━━━━━━━━━━
🪙 <b>{pos['symbol']}</b>
📥 Entry: ${pos['entry_price']:.10f}
📤 Exit:  ${current_price:.10f}
📊 PnL: <b>{pnl_str}</b>{peak_line}{reason_line}
💼 New Balance: <b>${balance_usd:.2f}</b>
━━━━━━━━━━━━━━━━━━━━""")
    print(f"[{result_label}] {pos['symbol']} {pnl_percent:+.2f}% | ${pnl_usd:+.2f} | Balance: ${balance_usd:.2f}")


# ==================== CHECK POSITIONS — AGGRESSIVE PROFIT PROTECTION ====================

def check_positions():
    """
    Core position monitor — runs every CHECK_INTERVAL seconds.
    
    Priority order of checks:
    1. TRAIL STOP     — profit dropped $1 from peak → protect profits NOW
    2. TP             — take profit target hit
    3. HARD SL        — absolute stop loss
    4. FAST CUT       — loss > HARD_FAST_CUT_PCT without waiting for AI
    5. EARLY AI EXIT  — at EARLY_EXIT_PCT loss → ask AI immediately
    6. PROFIT DROP    — in profit but fast price drop → exit to save profit
    7. RECOVERY       — was in loss, recovered to green → lock it in
    8. HOLD           — everything is fine, report and hold
    """
    for addr, pos in list(positions.items()):
        try:
            resp = requests.get(
                f"https://api.dexscreener.com/latest/dex/tokens/{addr}",
                timeout=7
            )
            data  = resp.json()
            pairs = data.get("pairs", [])
            if not pairs:
                continue

            current_price = float(pairs[0]["priceUsd"])
            current_mc    = float(pairs[0].get("fdv") or pairs[0].get("marketCap") or pos.get("entry_mc", 0))
            pnl_percent   = (current_price - pos["entry_price"]) / pos["entry_price"] * 100
            pnl_usd       = (current_price - pos["entry_price"]) * pos["amount_tokens"]
            exit_value    = pos["amount_tokens"] * current_price

            # ---- Update live tracking ----
            last_price = pos.get("last_price", pos["entry_price"])
            positions[addr]["last_price"]    = current_price
            positions[addr]["last_check_ts"] = time.time()
            positions[addr]["current_price"] = current_price
            positions[addr]["current_mc"]    = current_mc

            # Update loss tracking
            if pnl_percent < positions[addr]["low_pnl"]:
                positions[addr]["low_pnl"] = pnl_percent
            if pnl_percent < -0.5:
                positions[addr]["was_in_loss"] = True

            # Update peak profit tracking — this drives the trailing stop
            if pnl_usd > positions[addr]["peak_pnl_usd"]:
                positions[addr]["peak_pnl_usd"] = pnl_usd
                positions[addr]["peak_price"]    = current_price

            peak_pnl_usd   = positions[addr]["peak_pnl_usd"]
            drop_from_peak = peak_pnl_usd - pnl_usd   # How much profit we've given back

            # Calculate price change since last check (momentum signal)
            price_change_since_last = ((current_price - last_price) / last_price * 100) if last_price > 0 else 0

            print(f"[Monitor] {pos['symbol']} | PnL: {pnl_percent:+.2f}% (${pnl_usd:+.2f}) | "
                  f"Peak: +${peak_pnl_usd:.2f} | Drop from peak: ${drop_from_peak:.2f} | "
                  f"Since last: {price_change_since_last:+.2f}%")

            # ==============================================================
            # 1. TRAIL STOP — protect profits aggressively
            #    Trigger: we had $1+ profit AND now gave back $1 from peak
            # ==============================================================
            if peak_pnl_usd >= TRAIL_TRIGGER_PROFIT_USD and drop_from_peak >= TRAIL_DROP_USD:
                reason = (
                    f"Peak profit was +${peak_pnl_usd:.2f} — "
                    f"dropped ${drop_from_peak:.2f} → trailing stop triggered. "
                    f"Locking in ${pnl_usd:.2f} profit."
                )
                print(f"[📉 TRAIL STOP] {pos['symbol']} — {reason}")
                _close_position(addr, pos, current_price, pnl_percent, pnl_usd, exit_value, "TRAIL", reason)
                continue

            # ==============================================================
            # 2. TP — take profit
            # ==============================================================
            if current_price >= pos["tp_price"]:
                _close_position(addr, pos, current_price, pnl_percent, pnl_usd, exit_value, "TP")
                continue

            # ==============================================================
            # 3. HARD STOP LOSS — absolute floor (rarely hit due to early exits)
            # ==============================================================
            if current_price <= pos["sl_price"]:
                _close_position(addr, pos, current_price, pnl_percent, pnl_usd, exit_value, "SL")
                continue

            # ==============================================================
            # 4. FAST CUT — loss exceeds hard cut threshold, NO AI WAIT
            #    This is the psychological "rip the bandaid" exit.
            #    At -1.5% with $20 = $0.30 loss. Exit immediately.
            # ==============================================================
            if pnl_percent <= HARD_FAST_CUT_PCT:
                reason = (
                    f"Loss hit {pnl_percent:.2f}% — hard cut threshold {HARD_FAST_CUT_PCT}%. "
                    f"Exiting immediately without waiting for AI. Capital protected."
                )
                print(f"[✂️ FAST CUT] {pos['symbol']} — {reason}")
                _close_position(addr, pos, current_price, pnl_percent, pnl_usd, exit_value, "FASTCUT", reason)
                continue

            # ==============================================================
            # 5. EARLY AI EXIT — at -0.8% loss → consult AI urgently
            #    Much earlier than old -2.0% trigger.
            # ==============================================================
            if pnl_percent <= EARLY_EXIT_PCT:
                ai_exit_count = pos.get("ai_exit_count", 0)
                # Don't spam AI more than every 2 checks — but always exit if AI said so
                if ai_exit_count == 0 or ai_exit_count % 2 == 0:
                    trigger = f"Loss at {pnl_percent:.2f}% (threshold: {EARLY_EXIT_PCT}%)"
                    print(f"[⚠️ Early Exit Trigger] {pos['symbol']} at {pnl_percent:.2f}% — consulting AI...")
                    ai_exit  = _ask_ai_exit(pos, current_price, current_mc, pnl_percent, pnl_usd, trigger)
                    action   = ai_exit.get("action", "EXIT")
                    reason   = ai_exit.get("reason", "")
                    urgency  = ai_exit.get("urgency", "NORMAL")
                    positions[addr]["ai_exit_count"] = ai_exit_count + 1

                    print(f"[AI Exit] {action} ({urgency}) — {reason}")

                    if action == "EXIT" or urgency == "HIGH":
                        _close_position(addr, pos, current_price, pnl_percent, pnl_usd, exit_value, "AI-EXIT", reason)
                        continue
                    else:
                        print(f"[Hold] {pos['symbol']} | AI says HOLD but monitoring closely | PnL: {pnl_percent:.2f}%")
                else:
                    positions[addr]["ai_exit_count"] = ai_exit_count + 1
                    print(f"[⚠️ Loss] {pos['symbol']} at {pnl_percent:.2f}% — AI asked recently, monitoring...")
                continue

            # ==============================================================
            # 6. PROFIT MOMENTUM CHECK — in profit but dropping FAST
            #    If we're in profit but price dropped 2%+ since last check
            #    and we have meaningful profit at stake → ask AI now
            # ==============================================================
            if pnl_usd > 0.5 and price_change_since_last <= -TRAIL_FAST_DROP_PCT:
                trigger = (
                    f"In profit (${pnl_usd:.2f}) but price dropped {price_change_since_last:.2f}% "
                    f"since last check — fast reversal detected"
                )
                print(f"[⚡ Profit Momentum Drop] {pos['symbol']} — {trigger}")
                ai_exit = _ask_ai_exit(pos, current_price, current_mc, pnl_percent, pnl_usd, trigger)
                action  = ai_exit.get("action", "EXIT")
                reason  = ai_exit.get("reason", "")
                if action == "EXIT":
                    _close_position(addr, pos, current_price, pnl_percent, pnl_usd, exit_value, "AI-EXIT", reason)
                    continue
                else:
                    print(f"[Hold] {pos['symbol']} | Momentum drop but AI says hold | PnL: {pnl_percent:.2f}%")

            # ==============================================================
            # 7. RECOVERY — was in loss but now recovered to green → lock it
            # ==============================================================
            elif pos.get("was_in_loss") and pnl_percent >= 0.5:
                reason = f"Recovered from {pos.get('low_pnl', 0):.1f}% low → now {pnl_percent:+.1f}% — locking recovery profit"
                print(f"[🔄 Recovery] {pos['symbol']} — {reason}")
                _close_position(addr, pos, current_price, pnl_percent, pnl_usd, exit_value, "RECOVERY", reason)
                continue

            # ==============================================================
            # 8. HOLD — everything nominal
            # ==============================================================
            else:
                status = "🟢 PROFIT" if pnl_usd > 0 else "🟡 SMALL LOSS"
                print(f"[Hold {status}] {pos['symbol']} | {pnl_percent:+.2f}% | MC: ${current_mc:,.0f}")

        except Exception as e:
            print(f"[Position Error] {addr}: {e}")


# ==================== MAIN LOOP ====================

def main_loop():
    global scan_count

    print(f"[BOT START] Balance: ${balance_usd:.2f} | Target: ${TARGET_BALANCE_USD:.2f}")
    print(f"[CONFIG] TP: +{TP_PERCENT}% | SL: -{SL_PERCENT}% | Early Exit: {EARLY_EXIT_PCT}% | Hard Cut: {HARD_FAST_CUT_PCT}%")
    print(f"[CONFIG] Min AI Confidence: {MIN_AI_CONFIDENCE}% | Trail Drop: -${TRAIL_DROP_USD} from peak")
    print(f"[CONFIG] Check Interval: {CHECK_INTERVAL}s | Min Confirmations: {MIN_CONFIRMATIONS}/12")
    print(f"[AI] Groq keys: {len(GROQ_KEYS)} | Slots: {len(GROQ_SLOTS)} ({len(GROQ_KEYS)} keys × {len(GROQ_MODELS)} models)")

    t_cmd = threading.Thread(target=command_listener, daemon=True)
    t_cmd.start()

    send_telegram(f"""🤖 <b>AI TRADING BOT STARTED — STRICT MODE</b>
━━━━━━━━━━━━━━━━━━━━
💼 Balance: <b>${INITIAL_BALANCE_USD}</b> → Target: <b>${TARGET_BALANCE_USD}</b>
💰 Trade Size: FULL BALANCE | Max: {MAX_POSITIONS} position

📈 TP: +{TP_PERCENT}% | 🛑 SL: -{SL_PERCENT}% (max -$1 loss)
⚡ Early Exit: {EARLY_EXIT_PCT}% | Hard Cut: {HARD_FAST_CUT_PCT}%
📉 Trail: exits if profit drops $1 from peak
🔒 Min AI Confidence: <b>{MIN_AI_CONFIDENCE}%</b> (was 70%)
📊 Min Confirmations: <b>{MIN_CONFIRMATIONS}/12</b> (was 8/12)

🤖 AI: Groq <b>{len(GROQ_SLOTS)} slots</b> ({len(GROQ_KEYS)} keys × {len(GROQ_MODELS)} models)
⏱ Check every: {CHECK_INTERVAL}s (was 30s)
📡 Mode: PAPER TRADING (STRICT — Real Money Mindset)

<b>Commands:</b> /status /balance /positions /history /pause /help
━━━━━━━━━━━━━━━━━━━━""")

    last_status_time = time.time()
    last_seen_reset  = time.time()

    while True:
        # Reset seen_tokens every 20 min — allow re-evaluation of passed tokens
        if time.time() - last_seen_reset >= 1200:
            seen_tokens.clear()
            last_seen_reset = time.time()
            print("[Reset] seen_tokens cleared — fresh scan window")

        # Target reached check
        if balance_usd >= TARGET_BALANCE_USD:
            send_telegram(
                f"🏆 <b>TARGET REACHED!</b> Balance: ${balance_usd:.2f} / Goal: ${TARGET_BALANCE_USD:.2f} | "
                f"Trades: {len(trade_history)}"
            )
            print(f"[🏆 GOAL REACHED] ${balance_usd:.2f}")

        # ---- Always check open positions first (highest priority) ----
        if positions:
            check_positions()

        scan_count += 1
        print(f"\n[Scan #{scan_count}] Balance: ${balance_usd:.2f} | "
              f"Positions: {len(positions)}/{MAX_POSITIONS} | Paused: {bot_paused}")

        # ---- Scan for new trades only when slot is open ----
        if not bot_paused and len(positions) < MAX_POSITIONS and balance_usd >= MIN_TRADE_USD:
            pairs = get_dexscreener_pairs()
            print(f"[Scan] Analyzing {len(pairs)} pairs (need {MIN_CONFIRMATIONS}/12 + {MIN_AI_CONFIDENCE}%+ AI)...")

            for pair in pairs:
                if len(positions) >= MAX_POSITIONS or bot_paused:
                    break
                token_data = analyze_pair(pair)
                if token_data and token_data["address"] not in positions:
                    simulate_buy(token_data)
                    break   # One trade at a time — don't stack quick entries
        else:
            if bot_paused:
                reason = "bot paused"
            elif len(positions) >= MAX_POSITIONS:
                sym    = next(iter(positions.values()), {}).get("symbol", "?")
                reason = f"position open ({sym})"
            else:
                reason = "low balance"
            print(f"[Wait] Skipping scan — {reason}")

        # Auto status update every 30 min
        if time.time() - last_status_time >= 1800:
            wins      = [t for t in trade_history if t["result"] == "TP"]
            losses    = [t for t in trade_history if t["result"] in ("SL", "FASTCUT", "AI-EXIT")]
            total_pnl = sum(t["pnl_usd"] for t in trade_history)
            send_telegram(f"""📊 <b>AUTO STATUS — STRICT MODE</b>
Balance: <b>${balance_usd:.2f}</b> / ${TARGET_BALANCE_USD:.2f}
PnL: ${total_pnl:+.2f} | W: {len(wins)} L: {len(losses)}
Position: {len(positions)}/{MAX_POSITIONS}
Type /status for full details.""")
            last_status_time = time.time()

        print(f"[Sleep] {CHECK_INTERVAL}s until next check...")
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    t_web = threading.Thread(target=run_web, daemon=True)
    t_web.start()
    print(f"[Web] Dashboard running on port {PORT}")
    main_loop()
