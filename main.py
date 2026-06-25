import requests
import time
import json
import os
import threading
from datetime import datetime
from flask import Flask, render_template_string, jsonify

# ========================= CONFIG =========================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "aadi00bot")

# Multiple Groq API keys — loaded from environment
_RAW_KEYS = [
    os.environ.get("GROQ_API_KEY", ""),
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

# Simulation Settings
INITIAL_BALANCE_USD = 20.0
TARGET_BALANCE_USD = 50.0
MIN_TRADE_USD = 5.0
TP_PERCENT = 20.0
SL_PERCENT = 7.0
CHECK_INTERVAL = 120
MAX_POSITIONS = 1

# 12-Point Confirmation Thresholds (relaxed to actually find tokens)
MIN_LIQUIDITY = 8000
MIN_5M_VOLUME = 1000
MIN_1H_VOLUME = 5000
MIN_BUY_RATIO_5M = 1.3
MIN_BUY_RATIO_1H = 1.1
MAX_MC = 5000000
MIN_LP_LOCKED_PCT = 50
MIN_PRICE_CHANGE_5M = 0.3
MAX_PRICE_CHANGE_5M = 60.0
MAX_PRICE_CHANGE_1H = 200.0
MIN_PRICE_CHANGE_1H = -15.0
MIN_VOLUME_MCAP_RATIO = 0.005

# Web server port
PORT = int(os.environ.get("PORT", 5000))
# =========================================================

balance_usd = INITIAL_BALANCE_USD
positions = {}
trade_history = []
seen_tokens = set()
start_time = datetime.now()
bot_paused = False
last_update_id = 0
scan_count = 0


# ==================== FLASK WEB DASHBOARD ====================

app = Flask(__name__)

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AI Trading Bot Dashboard</title>
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
  .card-value.green { color: #48bb78; }
  .card-value.red   { color: #fc8181; }
  .card-value.blue  { color: #63b3ed; }
  .card-value.yellow{ color: #f6ad55; }
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
  .tp-box, .sl-box {
    flex: 1;
    padding: 6px 10px;
    border-radius: 8px;
    font-size: 0.75rem;
    font-weight: 600;
    text-align: center;
  }
  .tp-box { background: rgba(72,187,120,0.12); color: #48bb78; border: 1px solid rgba(72,187,120,0.3); }
  .sl-box { background: rgba(252,129,129,0.12); color: #fc8181; border: 1px solid rgba(252,129,129,0.3); }
  .confidence-bar {
    margin-top: 10px;
  }
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
  .badge-tp { background: rgba(72,187,120,0.15); color: #48bb78; padding: 3px 8px; border-radius: 6px; font-size: 0.7rem; font-weight: 700; }
  .badge-sl { background: rgba(252,129,129,0.15); color: #fc8181; padding: 3px 8px; border-radius: 6px; font-size: 0.7rem; font-weight: 700; }
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
</style>
</head>
<body>
<div class="header">
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

<div class="refresh-note" id="last-updated">Auto-refreshing every 15s</div>

<script>
async function fetchData() {
  try {
    const r = await fetch('/api/status');
    const d = await r.json();

    // Status
    const dot = d.paused ? '<span class="status-dot paused"></span>' : '<span class="status-dot running"></span>';
    document.getElementById('bot-status').innerHTML = dot + (d.paused ? 'PAUSED' : 'RUNNING') + ' &nbsp;|&nbsp; Scan #' + d.scan_count + ' &nbsp;|&nbsp; Runtime: ' + d.runtime;

    // Stats
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

    // Open positions
    const posEl = document.getElementById('open-positions');
    if (d.positions.length === 0) {
      posEl.innerHTML = '<div class="no-data">No open positions</div>';
    } else {
      posEl.innerHTML = d.positions.map(p => {
        const pnlClass = p.pnl_pct >= 0 ? 'pos' : 'neg';
        const pnlSign = p.pnl_pct >= 0 ? '+' : '';
        const shortAddr = p.address.slice(0, 6) + '...' + p.address.slice(-6);
        return `<div class="position-card">
          <div class="position-header">
            <div>
              <div class="coin-name">${p.symbol}</div>
              <div class="contract" title="${p.address}">${p.address}</div>
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
            <div class="tp-box">🎯 TP +20% &nbsp; $${p.tp_price.toFixed(8)}</div>
            <div class="sl-box">🛑 SL -7% &nbsp; $${p.sl_price.toFixed(8)}</div>
          </div>
          <div class="confidence-bar">
            <div class="conf-label"><span>🤖 AI Confidence</span><span>${p.ai_confidence}%</span></div>
            <div style="background:#2d3748;border-radius:999px;height:6px;overflow:hidden;">
              <div class="conf-fill" style="width:${p.ai_confidence}%"></div>
            </div>
          </div>
          ${p.ai_reason ? '<div class="ai-reason">💡 ' + p.ai_reason + '</div>' : ''}
        </div>`;
      }).join('');
    }

    // Trade history
    const histEl = document.getElementById('trade-history');
    if (d.history.length === 0) {
      histEl.innerHTML = '<div class="no-data">No trades yet</div>';
    } else {
      histEl.innerHTML = d.history.slice().reverse().map(t => {
        const pnlClass = t.pnl_usd >= 0 ? 'pos' : 'neg';
        const pnlSign = t.pnl_usd >= 0 ? '+' : '';
        return `<div class="trade-row">
          <div>
            <div class="trade-symbol">${t.symbol}</div>
            <div class="trade-detail">${t.time}</div>
          </div>
          <div><span class="badge-${t.result.toLowerCase()}">${t.result}</span></div>
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
  if (n >= 1000) return (n/1000).toFixed(1) + 'K';
  return n.toFixed(0);
}

fetchData();
setInterval(fetchData, 15000);
</script>
</body>
</html>"""


@app.route("/")
def dashboard():
    return render_template_string(DASHBOARD_HTML)


@app.route("/api/status")
def api_status():
    wins = [t for t in trade_history if t["result"] == "TP"]
    losses = [t for t in trade_history if t["result"] == "SL"]
    total_pnl = sum(t["pnl_usd"] for t in trade_history)
    win_rate = (len(wins) / len(trade_history) * 100) if trade_history else 0
    runtime = datetime.now() - start_time
    hours = int(runtime.total_seconds() // 3600)
    minutes = int((runtime.total_seconds() % 3600) // 60)

    positions_data = []
    for addr, pos in positions.items():
        current_price = pos.get("current_price", pos["entry_price"])
        current_mc = pos.get("current_mc", pos.get("entry_mc", 0))
        pnl_pct = (current_price - pos["entry_price"]) / pos["entry_price"] * 100
        pnl_usd = (current_price - pos["entry_price"]) * pos["amount_tokens"]
        positions_data.append({
            "address": addr,
            "symbol": pos["symbol"],
            "entry_price": pos["entry_price"],
            "current_price": current_price,
            "entry_mc": pos.get("entry_mc", 0),
            "current_mc": current_mc,
            "amount_usd": pos["amount_usd"],
            "amount_tokens": pos["amount_tokens"],
            "tp_price": pos["tp_price"],
            "sl_price": pos["sl_price"],
            "pnl_pct": pnl_pct,
            "pnl_usd": pnl_usd,
            "ai_confidence": pos.get("ai_confidence", 0),
            "ai_reason": pos.get("ai_reason", ""),
            "entry_time": pos["entry_time"].strftime("%H:%M:%S"),
        })

    history_data = []
    for t in trade_history:
        history_data.append({
            "symbol": t["symbol"],
            "result": t["result"],
            "pnl_usd": t["pnl_usd"],
            "pnl_pct": t["pnl_pct"],
            "time": t["time"].strftime("%d %b %H:%M"),
        })

    return jsonify({
        "balance": round(balance_usd, 4),
        "target": TARGET_BALANCE_USD,
        "total_pnl": round(total_pnl, 4),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(win_rate, 1),
        "paused": bot_paused,
        "scan_count": scan_count,
        "runtime": f"{hours}h {minutes}m",
        "positions": positions_data,
        "history": history_data,
    })


def run_web():
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)


# ==================== TELEGRAM ====================

def send_telegram(message, chat_id=None):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, json={
            "chat_id": chat_id or TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        }, timeout=10)
    except Exception as e:
        print(f"[Telegram Error] {e}")


def get_updates(offset=0):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
        resp = requests.get(url, params={
            "offset": offset,
            "timeout": 30,
            "allowed_updates": ["message"]
        }, timeout=35)
        if resp.status_code == 200:
            return resp.json().get("result", [])
    except Exception as e:
        print(f"[getUpdates Error] {e}")
    return []


# ==================== COMMANDS ====================

def cmd_start(chat_id):
    send_telegram(f"""🤖 <b>AI TRADING BOT — ACTIVE</b>
━━━━━━━━━━━━━━━━━━━━
Solana meme coin paper trading bot.
AI (Groq llama-3.3-70b-versatile + {len(GROQ_KEYS)} keys) decide karta hai kab buy/sell karna hai.

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
📡 Mode: PAPER TRADING""", chat_id)


def cmd_status(chat_id):
    global balance_usd, positions, trade_history, bot_paused
    wins = [t for t in trade_history if t["result"] == "TP"]
    losses = [t for t in trade_history if t["result"] == "SL"]
    total_pnl = sum(t["pnl_usd"] for t in trade_history)
    win_rate = (len(wins) / len(trade_history) * 100) if trade_history else 0
    progress_pct = max(0, (balance_usd - INITIAL_BALANCE_USD) / (TARGET_BALANCE_USD - INITIAL_BALANCE_USD) * 100)
    runtime = datetime.now() - start_time
    hours = int(runtime.total_seconds() // 3600)
    minutes = int((runtime.total_seconds() % 3600) // 60)

    open_pos_text = ""
    for addr, pos in positions.items():
        cp = pos.get("current_price", pos["entry_price"])
        pnl = (cp - pos["entry_price"]) / pos["entry_price"] * 100
        pnl_usd = (cp - pos["entry_price"]) * pos["amount_tokens"]
        open_pos_text += f"\n  • <b>{pos['symbol']}</b>: {pnl:+.1f}% (${pnl_usd:+.2f})"

    status_icon = "⏸" if bot_paused else "▶️"
    send_telegram(f"""📊 <b>BOT STATUS</b>
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
━━━━━━━━━━━━━━━━━━━━""", chat_id)


def cmd_balance(chat_id):
    progress_pct = max(0, (balance_usd - INITIAL_BALANCE_USD) / (TARGET_BALANCE_USD - INITIAL_BALANCE_USD) * 100)
    bar_filled = int(progress_pct / 10)
    bar = "🟩" * bar_filled + "⬜" * (10 - bar_filled)
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
        cp = pos.get("current_price", pos["entry_price"])
        current_mc = pos.get("current_mc", pos.get("entry_mc", 0))
        pnl_pct = (cp - pos["entry_price"]) / pos["entry_price"] * 100
        pnl_usd = (cp - pos["entry_price"]) * pos["amount_tokens"]
        entry_time = pos["entry_time"].strftime("%d %b %H:%M:%S")
        icon = "📈" if pnl_pct >= 0 else "📉"

        def fmt_mc(v):
            if v >= 1_000_000:
                return f"${v/1_000_000:.2f}M"
            if v >= 1_000:
                return f"${v/1_000:.1f}K"
            return f"${v:.0f}"

        msg += f"""

{icon} <b>{pos['symbol']}</b>
🔗 <code>{addr}</code>

📊 Entry MC:   <b>{fmt_mc(pos.get('entry_mc',0))}</b>
📊 Now MC:     <b>{fmt_mc(current_mc)}</b>

💵 Entry:  <code>${pos['entry_price']:.10f}</code>
💵 Now:    <code>${cp:.10f}</code>

📈 Live PnL: <b>{pnl_pct:+.2f}% (${pnl_usd:+.2f})</b>
💰 Invested: ${pos['amount_usd']:.2f}

🎯 TP: <code>${pos['tp_price']:.10f}</code> (+{TP_PERCENT}%)
🛑 SL: <code>${pos['sl_price']:.10f}</code> (-{SL_PERCENT}%)

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
        icon = "✅" if t["result"] == "TP" else "❌"
        trade_time = t["time"].strftime("%d %b %H:%M")
        msg += f"\n{icon} <b>{t['symbol']}</b> [{t['result']}] {t['pnl_pct']:+.1f}% (${t['pnl_usd']:+.2f}) — {trade_time}"

    total_pnl = sum(t["pnl_usd"] for t in trade_history)
    wins = len([t for t in trade_history if t["result"] == "TP"])
    win_rate = wins / len(trade_history) * 100 if trade_history else 0
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
                msg = update.get("message", {})
                text = msg.get("text", "")
                chat_id = msg.get("chat", {}).get("id")
                if text and text.startswith("/") and chat_id:
                    print(f"[Command] Received: {text} from chat {chat_id}")
                    handle_command(text, chat_id)
        except Exception as e:
            print(f"[Command Listener Error] {e}")
            time.sleep(5)


# ==================== AI BRAIN (UPGRADED) ====================

# Track AI status so we only send one Telegram alert per outage
_ai_down = False
_ai_down_notified = False


def _build_prompt(token_data: dict) -> str:
    return f"""You are an expert Solana meme coin trader. Analyze this token for a SHORT-TERM paper trade (TP +20%, SL -7%).

TOKEN METRICS:
- Symbol: {token_data.get('symbol')}
- Price: ${token_data.get('price', 0):.10f}
- Market Cap: ${token_data.get('mc', 0):,.0f}
- Liquidity: ${token_data.get('liquidity', 0):,.0f}
- 5min Volume: ${token_data.get('vol_5m', 0):,.0f}
- 1hr Volume: ${token_data.get('vol_1h', 0):,.0f}
- Buy/Sell Ratio 5m: {token_data.get('buy_ratio_5m', 0):.2f}x
- Buy/Sell Ratio 1h: {token_data.get('buy_ratio_1h', 0):.2f}x
- Price Change 5m: {token_data.get('price_change_5m', 0):.2f}%
- Price Change 1h: {token_data.get('price_change_1h', 0):.2f}%
- LP Locked: {token_data.get('lp_locked', 0):.1f}%
- Mint Revoked: {token_data.get('mint_revoked', False)}
- Confirmations: {token_data.get('confirmations_passed', 0)}/12

BUY if: price moving up, buy ratio >= 1.3, MC < $5M, not already pumped >150% in 1h.
SKIP if: sellers dominating (BR < 1.0), already pumped >180% in 1h, liquidity < $5K.
This is paper trading — take calculated risks. Give BUY at 55%+ confidence.

Respond ONLY in this exact JSON:
{{"decision": "BUY" or "SKIP", "confidence": 0-100, "reason": "one sentence"}}"""


def ask_ai_brain(token_data: dict) -> dict:
    global _ai_down, _ai_down_notified

    if not GROQ_SLOTS:
        return _rule_based_fallback(token_data, notify=True)

    prompt = _build_prompt(token_data)
    payload_base = {
        "messages": [
            {"role": "system", "content": "You are a crypto trading AI. Respond ONLY in valid JSON."},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.2,
        "max_tokens": 200,
    }

    for idx, (api_key, model) in enumerate(GROQ_SLOTS):
        try:
            payload = {**payload_base, "model": model}
            resp = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=payload,
                timeout=20,
            )
            if resp.status_code == 200:
                content = resp.json()["choices"][0]["message"]["content"].strip()
                content = content.replace("```json", "").replace("```", "").strip()
                result = json.loads(content)
                # AI recovered — notify once
                if _ai_down:
                    _ai_down = False
                    _ai_down_notified = False
                    key_num = GROQ_KEYS.index(api_key) + 1
                    send_telegram(f"✅ <b>AI Brain BACK ONLINE</b>\nKey #{key_num} | Model: <code>{model}</code>")
                print(f"[🤖 AI Key#{GROQ_KEYS.index(api_key)+1} {model}] {result.get('decision')} {result.get('confidence')}%")
                return result
            else:
                err_body = resp.text[:120]
                print(f"[Groq Slot {idx+1}] Error {resp.status_code} key#{GROQ_KEYS.index(api_key)+1} {model}: {err_body}")
        except json.JSONDecodeError:
            print(f"[Groq Slot {idx+1}] JSON parse error — {model}")
        except Exception as e:
            print(f"[Groq Slot {idx+1}] Exception {model}: {e}")

    # All slots exhausted
    _ai_down = True
    return _rule_based_fallback(token_data, notify=True)


def _rule_based_fallback(token_data: dict, notify: bool = False) -> dict:
    """Rule-based decision when all Groq slots fail."""
    global _ai_down_notified

    br5m  = token_data.get("buy_ratio_5m", 0)
    pc5m  = token_data.get("price_change_5m", 0)
    pc1h  = token_data.get("price_change_1h", 0)
    liq   = token_data.get("liquidity", 0)
    vol5m = token_data.get("vol_5m", 0)
    confs = token_data.get("confirmations_passed", 0)
    sym   = token_data.get("symbol", "?")

    # Hard rejects
    if br5m < 1.0:
        result = {"decision": "SKIP", "confidence": 0, "reason": f"[NoAI] Sellers dominating BR={br5m:.2f}"}
    elif pc1h > 180:
        result = {"decision": "SKIP", "confidence": 0, "reason": f"[NoAI] Overextended {pc1h:.0f}% in 1h"}
    elif liq < 5000:
        result = {"decision": "SKIP", "confidence": 0, "reason": f"[NoAI] Low liquidity ${liq:.0f}"}
    else:
        score = 0
        if br5m >= 1.5:        score += 20
        elif br5m >= 1.3:      score += 12
        if 1 <= pc5m <= 40:    score += 20
        elif pc5m > 0:         score += 10
        if pc1h < 80:          score += 15
        if liq >= 15000:       score += 15
        elif liq >= 8000:      score += 8
        if vol5m >= 3000:      score += 15
        elif vol5m >= 1000:    score += 8
        if confs >= 10:        score += 15
        elif confs >= 8:       score += 10
        confidence = min(score, 88)
        if confidence >= 55:
            result = {"decision": "BUY", "confidence": confidence,
                      "reason": f"[NoAI] BR={br5m:.2f} PC5m={pc5m:.1f}% Liq=${liq:,.0f} {confs}/12"}
        else:
            result = {"decision": "SKIP", "confidence": confidence,
                      "reason": f"[NoAI] Score too low {confidence}%"}

    # Send one Telegram alert per outage
    if notify and not _ai_down_notified:
        _ai_down_notified = True
        decision_icon = "🟡 Trading with rules" if result["decision"] == "BUY" else "⏸ Skipping token"
        send_telegram(
            f"⚠️ <b>AI Brain DOWN — All Groq APIs failed</b>\n"
            f"Bot switching to <b>Rule-Based mode</b> automatically.\n"
            f"{decision_icon}: <b>{sym}</b>\n"
            f"Reason: {result['reason']}\n\n"
            f"📌 {len(GROQ_SLOTS)} slots tried ({len(GROQ_KEYS)} keys × {len(GROQ_MODELS)} models)\n"
            f"Bot will keep trading and auto-resume AI when available."
        )
    return result


# ==================== SCANNING ====================

def rugcheck_token(token_address):
    try:
        url = f"https://api.rugcheck.xyz/v1/tokens/{token_address}/report"
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            sec = data.get("security", {})
            lp_locked = sec.get("lpLockedPercentage") or 0
            mint_revoked = sec.get("mintAuthorityRevoked", False)
            return {
                "lp_locked_pct": lp_locked,
                "mint_revoked": mint_revoked,
                "is_safe": lp_locked >= MIN_LP_LOCKED_PCT and mint_revoked
            }
    except Exception as e:
        print(f"[Rugcheck Error] {e}")
    return {"lp_locked_pct": 0, "mint_revoked": False, "is_safe": False}


def get_dexscreener_pairs():
    all_pairs = []
    seen_addrs = set()

    for endpoint in [
        "https://api.dexscreener.com/token-boosts/top/v1",
        "https://api.dexscreener.com/token-boosts/latest/v1",
    ]:
        try:
            resp = requests.get(endpoint, timeout=12)
            if resp.status_code == 200:
                boosted = resp.json() if isinstance(resp.json(), list) else []
                sol_addrs = [b.get("tokenAddress") for b in boosted if b.get("chainId") == "solana"]
                for addr in sol_addrs[:20]:
                    if addr:
                        try:
                            r2 = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{addr}", timeout=8)
                            pairs = r2.json().get("pairs", [])
                            for p in pairs:
                                a = p.get("baseToken", {}).get("address")
                                if a and a not in seen_addrs:
                                    all_pairs.append(p)
                                    seen_addrs.add(a)
                        except:
                            pass
        except Exception as e:
            print(f"[DexScreener Error] {e}")

    for query in ["SOL", "pump", "meme"]:
        try:
            resp = requests.get(
                f"https://api.dexscreener.com/latest/dex/search?q={query}",
                timeout=12
            )
            if resp.status_code == 200:
                pairs = resp.json().get("pairs", [])
                for p in pairs:
                    a = p.get("baseToken", {}).get("address")
                    if a and a not in seen_addrs:
                        all_pairs.append(p)
                        seen_addrs.add(a)
        except Exception as e:
            print(f"[DexScreener Search Error q={query}] {e}")

    print(f"[DexScreener] Total unique pairs fetched: {len(all_pairs)}")
    return all_pairs


def run_12_confirmations(pair, rug_data):
    base = pair.get("baseToken", {})
    quote = pair.get("quoteToken", {})

    if quote.get("symbol") != "SOL":
        return False, 0, {}

    token_addr = base.get("address")
    if not token_addr:
        return False, 0, {}

    try:
        price_usd = float(pair.get("priceUsd") or 0)
        liquidity = float(pair.get("liquidity", {}).get("usd") or 0)
        mc = float(pair.get("fdv") or pair.get("marketCap") or 0)

        vol = pair.get("volume", {})
        vol_5m = float(vol.get("m5") or 0)
        vol_1h = float(vol.get("h1") or 0)
        vol_24h = float(vol.get("h24") or 0)

        price_change = pair.get("priceChange", {})
        pc_5m = float(price_change.get("m5") or 0)
        pc_1h = float(price_change.get("h1") or 0)
        pc_24h = float(price_change.get("h24") or 0)

        txns_5m = pair.get("txns", {}).get("m5", {})
        buys_5m = int(txns_5m.get("buys") or 0)
        sells_5m = int(txns_5m.get("sells") or 1)

        txns_1h = pair.get("txns", {}).get("h1", {})
        buys_1h = int(txns_1h.get("buys") or 0)
        sells_1h = int(txns_1h.get("sells") or 1)

        buy_ratio_5m = buys_5m / sells_5m if sells_5m > 0 else 0
        buy_ratio_1h = buys_1h / sells_1h if sells_1h > 0 else 0
        vol_mc_ratio = vol_1h / mc if mc > 0 else 0

        lp_locked = rug_data.get("lp_locked_pct", 0)
        mint_revoked = rug_data.get("mint_revoked", False)

        confirmations = 0
        check_results = []

        checks = [
            (liquidity >= MIN_LIQUIDITY,                              f"1. Liq ${liquidity:,.0f}>=${MIN_LIQUIDITY:,}"),
            (vol_5m >= MIN_5M_VOLUME,                                 f"2. Vol5m ${vol_5m:,.0f}>=${MIN_5M_VOLUME:,}"),
            (vol_1h >= MIN_1H_VOLUME,                                 f"3. Vol1h ${vol_1h:,.0f}>=${MIN_1H_VOLUME:,}"),
            (round(buy_ratio_5m, 2) >= MIN_BUY_RATIO_5M,             f"4. BR5m {buy_ratio_5m:.2f}>={MIN_BUY_RATIO_5M}"),
            (round(buy_ratio_1h, 2) >= MIN_BUY_RATIO_1H,             f"5. BR1h {buy_ratio_1h:.2f}>={MIN_BUY_RATIO_1H}"),
            (10000 <= mc <= MAX_MC,                                   f"6. MC ${mc:,.0f} in range"),
            (lp_locked >= MIN_LP_LOCKED_PCT or mint_revoked,          f"7. Safety LP={lp_locked:.0f}% Rev={mint_revoked}"),
            (MIN_PRICE_CHANGE_5M <= pc_5m <= MAX_PRICE_CHANGE_5M,    f"8. PC5m {pc_5m:.1f}% in range"),
            (MIN_PRICE_CHANGE_1H <= pc_1h <= MAX_PRICE_CHANGE_1H,    f"9. PC1h {pc_1h:.1f}% in range"),
            (vol_mc_ratio >= MIN_VOLUME_MCAP_RATIO,                  f"10. VolMC {vol_mc_ratio:.4f}>={MIN_VOLUME_MCAP_RATIO}"),
            (buys_5m >= 8,                                            f"11. Buys5m {buys_5m}>=8"),
            (buys_5m > sells_5m,                                      f"12. MoreBuys {buys_5m}>{sells_5m}"),
        ]

        for passed, label in checks:
            check_results.append(f"{label}: {'✅' if passed else '❌'}")
            if passed:
                confirmations += 1

        token_data = {
            "address": token_addr,
            "symbol": base.get("symbol", "UNKNOWN"),
            "price": price_usd,
            "liquidity": liquidity,
            "mc": mc,
            "vol_5m": vol_5m,
            "vol_1h": vol_1h,
            "vol_24h": vol_24h,
            "buy_ratio_5m": buy_ratio_5m,
            "buy_ratio_1h": buy_ratio_1h,
            "price_change_5m": pc_5m,
            "price_change_1h": pc_1h,
            "price_change_24h": pc_24h,
            "lp_locked": lp_locked,
            "mint_revoked": mint_revoked,
            "vol_mc_ratio": vol_mc_ratio,
            "buys_5m": buys_5m,
            "confirmations_passed": confirmations,
            "check_results": check_results,
        }

        return confirmations >= 8, confirmations, token_data

    except Exception as e:
        print(f"[Confirm Error] {e}")
        return False, 0, {}


def analyze_pair(pair):
    base = pair.get("baseToken", {})
    quote = pair.get("quoteToken", {})
    symbol = base.get("symbol", "?")

    if quote.get("symbol") != "SOL":
        return None

    token_addr = base.get("address")
    if not token_addr or token_addr in seen_tokens or token_addr in positions:
        return None

    try:
        liquidity = float(pair.get("liquidity", {}).get("usd") or 0)
        vol_5m = float(pair.get("volume", {}).get("m5") or 0)
        mc = float(pair.get("fdv") or pair.get("marketCap") or 0)

        fail_reasons = []
        if liquidity < MIN_LIQUIDITY:
            fail_reasons.append(f"Liq ${liquidity:,.0f}<${MIN_LIQUIDITY:,}")
        if vol_5m < MIN_5M_VOLUME:
            fail_reasons.append(f"Vol5m ${vol_5m:,.0f}<${MIN_5M_VOLUME:,}")
        if mc > MAX_MC:
            fail_reasons.append(f"MC ${mc:,.0f}>${MAX_MC:,}")

        if fail_reasons:
            print(f"[PreFilter] {symbol}: {' | '.join(fail_reasons)}")
            return None
    except:
        return None

    print(f"[PreFilter ✅] {symbol} passed — checking rugcheck...")
    rug_data = rugcheck_token(token_addr)
    passed, score, token_data = run_12_confirmations(pair, rug_data)

    if not passed:
        fails = [r for r in token_data.get("check_results", []) if "❌" in r]
        print(f"[Skip] {symbol} — {score}/12 checks | Failed: {', '.join(fails[:3])}")
        return None

    print(f"\n[🔍 AI Check] {symbol} passed {score}/12 — asking Groq AI...")
    ai_result = ask_ai_brain(token_data)
    decision = ai_result.get("decision", "SKIP")
    confidence = ai_result.get("confidence", 0)
    reason = ai_result.get("reason", "")

    print(f"[🤖 AI] {decision} | {confidence}% | {reason}")

    if decision != "BUY" or confidence < 60:
        print(f"[Skip] AI rejected {symbol} ({confidence}%): {reason}")
        return None

    token_data["ai_confidence"] = confidence
    token_data["ai_reason"] = reason
    token_data["score"] = score
    return token_data


# ==================== TRADING ====================

def simulate_buy(token_data):
    global balance_usd

    if balance_usd < MIN_TRADE_USD:
        return False
    if len(positions) >= MAX_POSITIONS:
        return False

    token_addr = token_data["address"]
    symbol = token_data["symbol"]
    price_usd = token_data["price"]
    amount_usd = balance_usd  # Full balance in one trade
    tokens_bought = amount_usd / price_usd
    entry_mc = token_data.get("mc", 0)

    positions[token_addr] = {
        "symbol": symbol,
        "entry_price": price_usd,
        "amount_tokens": tokens_bought,
        "amount_usd": amount_usd,
        "tp_price": price_usd * (1 + TP_PERCENT / 100),
        "sl_price": price_usd * (1 - SL_PERCENT / 100),
        "entry_mc": entry_mc,
        "current_mc": entry_mc,
        "current_price": price_usd,
        "entry_time": datetime.now(),
        "ai_confidence": token_data.get("ai_confidence", 0),
        "ai_reason": token_data.get("ai_reason", ""),
        "score": token_data.get("score", 0),
    }

    balance_usd -= amount_usd

    send_telegram(f"""🚀 <b>SIMULATED BUY EXECUTED</b>
━━━━━━━━━━━━━━━━━━━━
🪙 Token: <b>{symbol}</b>
🔗 <code>{token_addr}</code>

💵 Entry Price: ${price_usd:.10f}
💰 Amount: <b>${amount_usd:.2f}</b> (full balance)
📊 Entry MC: ${entry_mc:,.0f}

🎯 TP: +{TP_PERCENT}% | 🛑 SL: -{SL_PERCENT}%

🤖 AI Confidence: <b>{token_data.get('ai_confidence', 0)}%</b>
📊 Confirmations: {token_data.get('score', 0)}/12
💡 {token_data.get('ai_reason', 'N/A')}

💧 Liq: ${token_data.get('liquidity', 0):,.0f}
📊 Vol 5m: ${token_data.get('vol_5m', 0):,.0f}
🔄 Buy Ratio: {token_data.get('buy_ratio_5m', 0):.2f}x
━━━━━━━━━━━━━━━━━━━━""")

    print(f"[BUY] {symbol} @ ${price_usd:.10f} | ${amount_usd:.2f} full balance")
    return True


def check_positions():
    global balance_usd

    for addr, pos in list(positions.items()):
        try:
            resp = requests.get(
                f"https://api.dexscreener.com/latest/dex/tokens/{addr}",
                timeout=8
            )
            data = resp.json()
            pairs = data.get("pairs", [])
            if not pairs:
                continue

            current_price = float(pairs[0]["priceUsd"])
            current_mc = float(pairs[0].get("fdv") or pairs[0].get("marketCap") or pos.get("entry_mc", 0))
            pnl_percent = (current_price - pos["entry_price"]) / pos["entry_price"] * 100
            pnl_usd = (current_price - pos["entry_price"]) * pos["amount_tokens"]
            exit_value = pos["amount_tokens"] * current_price

            # Update live price for dashboard
            positions[addr]["current_price"] = current_price
            positions[addr]["current_mc"] = current_mc

            if current_price >= pos["tp_price"]:
                balance_usd += exit_value
                trade_history.append({
                    "symbol": pos["symbol"],
                    "pnl_usd": pnl_usd,
                    "pnl_pct": pnl_percent,
                    "result": "TP",
                    "time": datetime.now()
                })
                del positions[addr]
                send_telegram(f"""✅ <b>TAKE PROFIT HIT!</b>
━━━━━━━━━━━━━━━━━━━━
🪙 <b>{pos['symbol']}</b>
📥 Entry: ${pos['entry_price']:.10f}
📤 Exit:  ${current_price:.10f}
📈 PnL: <b>+${pnl_usd:.2f} (+{pnl_percent:.1f}%)</b>
💼 New Balance: <b>${balance_usd:.2f}</b>
━━━━━━━━━━━━━━━━━━━━""")
                print(f"[TP] {pos['symbol']} +${pnl_usd:.2f} | Balance: ${balance_usd:.2f}")

            elif current_price <= pos["sl_price"]:
                balance_usd += exit_value
                trade_history.append({
                    "symbol": pos["symbol"],
                    "pnl_usd": pnl_usd,
                    "pnl_pct": pnl_percent,
                    "result": "SL",
                    "time": datetime.now()
                })
                del positions[addr]
                send_telegram(f"""❌ <b>STOP LOSS HIT</b>
━━━━━━━━━━━━━━━━━━━━
🪙 <b>{pos['symbol']}</b>
📥 Entry: ${pos['entry_price']:.10f}
📤 Exit:  ${current_price:.10f}
📉 PnL: <b>${pnl_usd:.2f} ({pnl_percent:.1f}%)</b>
💼 New Balance: <b>${balance_usd:.2f}</b>
━━━━━━━━━━━━━━━━━━━━""")
                print(f"[SL] {pos['symbol']} ${pnl_usd:.2f} | Balance: ${balance_usd:.2f}")

            else:
                print(f"[Hold] {pos['symbol']} | PnL: {pnl_percent:+.1f}% | MC: ${current_mc:,.0f}")

        except Exception as e:
            print(f"[Position Error] {addr}: {e}")


# ==================== MAIN LOOP ====================

def main_loop():
    global scan_count

    print(f"[BOT START] Balance: ${balance_usd:.2f} | Target: ${TARGET_BALANCE_USD:.2f}")
    print(f"[CONFIG] TP: +{TP_PERCENT}% | SL: -{SL_PERCENT}% | Max Positions: {MAX_POSITIONS}")
    print(f"[AI] Groq keys loaded: {len(GROQ_KEYS)} | Slots: {len(GROQ_SLOTS)} ({len(GROQ_KEYS)} keys × {len(GROQ_MODELS)} models)")

    t_cmd = threading.Thread(target=command_listener, daemon=True)
    t_cmd.start()

    send_telegram(f"""🤖 <b>AI TRADING BOT STARTED</b>
━━━━━━━━━━━━━━━━━━━━
💼 Balance: <b>${INITIAL_BALANCE_USD}</b> → Target: <b>${TARGET_BALANCE_USD}</b>
💰 Trade Size: FULL BALANCE | Max: {MAX_POSITIONS} position
📈 TP: +{TP_PERCENT}% | 🛑 SL: -{SL_PERCENT}%
🔍 Required: 8/12 checks + AI 70%+ confidence
🤖 AI: Groq <b>{len(GROQ_SLOTS)} slots</b> ({len(GROQ_KEYS)} keys × {len(GROQ_MODELS)} models)
📡 Mode: PAPER TRADING (Demo)

<b>Commands:</b> /status /balance /positions /history /pause /help
━━━━━━━━━━━━━━━━━━━━""")

    last_status_time = time.time()
    last_seen_reset = time.time()

    while True:
        # Reset seen_tokens every 30 min so previously skipped tokens can be re-evaluated
        if time.time() - last_seen_reset >= 1800:
            seen_tokens.clear()
            last_seen_reset = time.time()
            print("[Reset] seen_tokens cleared — fresh scan")

        if balance_usd >= TARGET_BALANCE_USD:
            send_telegram(f"🏆 <b>TARGET REACHED!</b> Balance: ${balance_usd:.2f} / Goal: ${TARGET_BALANCE_USD:.2f} | Trades: {len(trade_history)}")
            print(f"[🏆 GOAL REACHED] ${balance_usd:.2f}")

        if positions:
            check_positions()

        scan_count += 1
        print(f"\n[Scan #{scan_count}] Balance: ${balance_usd:.2f} | Positions: {len(positions)}/{MAX_POSITIONS} | Paused: {bot_paused}")

        if not bot_paused and len(positions) < MAX_POSITIONS and balance_usd >= MIN_TRADE_USD:
            pairs = get_dexscreener_pairs()
            print(f"[Scan] {len(pairs)} pairs from DexScreener")

            for pair in pairs:
                if len(positions) >= MAX_POSITIONS or bot_paused:
                    break
                token_data = analyze_pair(pair)
                if token_data and token_data["address"] not in positions:
                    simulate_buy(token_data)
                    time.sleep(2)
        else:
            reason = "paused" if bot_paused else ("position open" if len(positions) >= MAX_POSITIONS else "low balance")
            print(f"[Wait] Skipping scan — {reason}")

        if time.time() - last_status_time >= 1800:
            wins = [t for t in trade_history if t["result"] == "TP"]
            losses = [t for t in trade_history if t["result"] == "SL"]
            total_pnl = sum(t["pnl_usd"] for t in trade_history)
            send_telegram(f"""📊 <b>AUTO STATUS UPDATE</b>
Balance: <b>${balance_usd:.2f}</b> / ${TARGET_BALANCE_USD:.2f}
PnL: ${total_pnl:+.2f} | W: {len(wins)} L: {len(losses)}
Position: {len(positions)}/{MAX_POSITIONS}
Type /status for full details.""")
            last_status_time = time.time()

        print(f"[Sleep] {CHECK_INTERVAL}s...")
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    t_web = threading.Thread(target=run_web, daemon=True)
    t_web.start()
    print(f"[Web] Dashboard running on port {PORT}")
    main_loop()
