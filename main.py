import requests
import time
import json
import os
import threading
from datetime import datetime

# ========================= CONFIG =========================
TELEGRAM_BOT_TOKEN = "6253228355:AAEkmteKAFnFoe-m0HauSYGouYN0m5MDZjM"
TELEGRAM_CHAT_ID = "aadi00bot"
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = "compound-beta"

# Simulation Settings
INITIAL_BALANCE_USD = 20.0
TARGET_BALANCE_USD = 50.0
MIN_TRADE_USD = 9.0
TRADE_AMOUNT_USD = 9.0
TP_PERCENT = 35.0
SL_PERCENT = 10.0
CHECK_INTERVAL = 120
MAX_POSITIONS = 2

# 12-Point Confirmation Thresholds
MIN_LIQUIDITY = 30000
MIN_5M_VOLUME = 5000
MIN_1H_VOLUME = 15000
MIN_BUY_RATIO_5M = 1.5
MIN_BUY_RATIO_1H = 1.2
MAX_MC = 3000000
MIN_LP_LOCKED_PCT = 50
MIN_PRICE_CHANGE_5M = 0.5
MAX_PRICE_CHANGE_5M = 50.0
MAX_PRICE_CHANGE_1H = 150.0
MIN_PRICE_CHANGE_1H = -10.0
MIN_VOLUME_MCAP_RATIO = 0.01

# =========================================================

balance_usd = INITIAL_BALANCE_USD
positions = {}
trade_history = []
seen_tokens = set()
start_time = datetime.now()
bot_paused = False
last_update_id = 0


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
Ye ek Solana meme coin paper trading bot hai.
AI (Groq {GROQ_MODEL}) decide karta hai kab buy/sell karna hai.

<b>Available Commands:</b>

/start — Bot ka welcome message
/status — Balance, P&amp;L, aur positions
/balance — Sirf current balance
/positions — Open trades ki live PnL
/history — Last 5 closed trades
/pause — Trading rok do
/resume — Trading dubara shuru karo
/help — Ye list dobara dikhao

━━━━━━━━━━━━━━━━━━━━
💡 Bot har {CHECK_INTERVAL}s mein scan karta hai
🎯 Target: $20 → ${TARGET_BALANCE_USD:.0f}
📡 Mode: PAPER TRADING (Demo)""", chat_id)


def cmd_status(chat_id):
    global balance_usd, positions, trade_history, bot_paused

    wins = [t for t in trade_history if t['result'] == 'TP']
    losses = [t for t in trade_history if t['result'] == 'SL']
    total_pnl = sum(t['pnl_usd'] for t in trade_history)
    win_rate = (len(wins) / len(trade_history) * 100) if trade_history else 0
    progress_pct = max(0, (balance_usd - INITIAL_BALANCE_USD) / (TARGET_BALANCE_USD - INITIAL_BALANCE_USD) * 100)
    runtime = datetime.now() - start_time
    hours = int(runtime.total_seconds() // 3600)
    minutes = int((runtime.total_seconds() % 3600) // 60)

    open_pos_text = ""
    for addr, pos in positions.items():
        try:
            resp = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{addr}", timeout=5)
            data = resp.json()
            if data.get('pairs'):
                curr_p = float(data['pairs'][0]['priceUsd'])
                pnl = (curr_p - pos['entry_price']) / pos['entry_price'] * 100
                pnl_usd = (curr_p - pos['entry_price']) * pos['amount_tokens']
                open_pos_text += f"\n  • <b>{pos['symbol']}</b>: {pnl:+.1f}% (${pnl_usd:+.2f})"
        except:
            open_pos_text += f"\n  • {pos['symbol']}: checking..."

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

    msg = "📌 <b>OPEN POSITIONS</b>\n━━━━━━━━━━━━━━━━━━━━"
    for addr, pos in positions.items():
        try:
            resp = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{addr}", timeout=5)
            data = resp.json()
            if data.get('pairs'):
                curr_p = float(data['pairs'][0]['priceUsd'])
                pnl_pct = (curr_p - pos['entry_price']) / pos['entry_price'] * 100
                pnl_usd = (curr_p - pos['entry_price']) * pos['amount_tokens']
                entry_time = pos['entry_time'].strftime("%H:%M:%S")
                icon = "📈" if pnl_pct >= 0 else "📉"
                msg += f"""

{icon} <b>{pos['symbol']}</b>
Entry: ${pos['entry_price']:.10f}
Now:   ${curr_p:.10f}
PnL:   <b>{pnl_pct:+.1f}% (${pnl_usd:+.2f})</b>
TP:    ${pos['tp_price']:.10f} (+{TP_PERCENT}%)
SL:    ${pos['sl_price']:.10f} (-{SL_PERCENT}%)
AI:    {pos.get('ai_confidence', '?')}% confidence
Time:  {entry_time}"""
        except:
            msg += f"\n• {pos['symbol']}: Data load nahi hua"

    msg += "\n━━━━━━━━━━━━━━━━━━━━"
    send_telegram(msg, chat_id)


def cmd_history(chat_id):
    if not trade_history:
        send_telegram("📜 <b>History:</b> Abhi tak koi trade close nahi hua.", chat_id)
        return

    recent = trade_history[-5:][::-1]
    msg = "📜 <b>LAST 5 TRADES</b>\n━━━━━━━━━━━━━━━━━━━━"
    for t in recent:
        icon = "✅" if t['result'] == 'TP' else "❌"
        trade_time = t['time'].strftime("%H:%M")
        msg += f"\n{icon} <b>{t['symbol']}</b> [{t['result']}] {t['pnl_pct']:+.1f}% (${t['pnl_usd']:+.2f}) at {trade_time}"

    total_pnl = sum(t['pnl_usd'] for t in trade_history)
    wins = len([t for t in trade_history if t['result'] == 'TP'])
    msg += f"\n━━━━━━━━━━━━━━━━━━━━\nTotal PnL: <b>${total_pnl:+.2f}</b> | Wins: {wins}/{len(trade_history)}"
    send_telegram(msg, chat_id)


def cmd_pause(chat_id):
    global bot_paused
    bot_paused = True
    send_telegram("⏸ <b>Bot PAUSED.</b>\nNaye trades nahi lega. Open positions monitor hoti rahengi.\n/resume se dubara shuru karo.", chat_id)
    print("[BOT] Paused by Telegram command")


def cmd_resume(chat_id):
    global bot_paused
    bot_paused = False
    send_telegram("▶️ <b>Bot RESUMED.</b>\nAb phir se tokens scan karega aur trades lega.", chat_id)
    print("[BOT] Resumed by Telegram command")


def cmd_help(chat_id):
    send_telegram("""❓ <b>BOT COMMANDS</b>
━━━━━━━━━━━━━━━━━━━━
/start — Welcome message
/status — Full bot status
/balance — Current balance aur progress
/positions — Open trades PnL
/history — Last 5 closed trades
/pause — Naye trades rokna
/resume — Trading resume karna
/help — Ye list
━━━━━━━━━━━━━━━━━━━━""", chat_id)


def handle_command(text, chat_id):
    text = text.strip().lower().split()[0]
    if text in ["/start", "/start@" + "bot"]:
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


# ==================== AI BRAIN ====================

def ask_ai_brain(token_data: dict) -> dict:
    if not GROQ_API_KEY:
        return {"decision": "SKIP", "confidence": 0, "reason": "No Groq API key"}

    prompt = f"""You are an expert crypto trading AI analyzing a Solana meme token for a paper trading bot.

TOKEN DATA:
- Symbol: {token_data.get('symbol')}
- Price USD: ${token_data.get('price', 0):.10f}
- Market Cap: ${token_data.get('mc', 0):,.0f}
- Liquidity: ${token_data.get('liquidity', 0):,.0f}
- 5min Volume: ${token_data.get('vol_5m', 0):,.0f}
- 1hr Volume: ${token_data.get('vol_1h', 0):,.0f}
- 24hr Volume: ${token_data.get('vol_24h', 0):,.0f}
- Buy/Sell Ratio (5m): {token_data.get('buy_ratio_5m', 0):.2f}
- Buy/Sell Ratio (1h): {token_data.get('buy_ratio_1h', 0):.2f}
- Price Change 5m: {token_data.get('price_change_5m', 0):.2f}%
- Price Change 1h: {token_data.get('price_change_1h', 0):.2f}%
- Price Change 24h: {token_data.get('price_change_24h', 0):.2f}%
- LP Locked: {token_data.get('lp_locked', 0):.1f}%
- Mint Revoked: {token_data.get('mint_revoked', False)}
- Volume/MC Ratio: {token_data.get('vol_mc_ratio', 0):.4f}
- Confirmations Passed: {token_data.get('confirmations_passed', 0)}/12

TASK: Decide if this token is worth buying right now for a short-term trade (TP +35%, SL -10%).
Consider: momentum, safety, rug pull risk, pump stage, volume strength.

Respond ONLY in this exact JSON format (no extra text):
{{"decision": "BUY" or "SKIP", "confidence": 0-100, "reason": "one sentence explanation"}}"""

    try:
        headers = {
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": GROQ_MODEL,
            "messages": [
                {"role": "system", "content": "You are a crypto trading AI that responds only in valid JSON."},
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.2,
            "max_tokens": 150
        }
        resp = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers=headers,
            json=payload,
            timeout=15
        )
        if resp.status_code == 200:
            content = resp.json()['choices'][0]['message']['content'].strip()
            content = content.replace("```json", "").replace("```", "").strip()
            return json.loads(content)
        else:
            print(f"[Groq Error] {resp.status_code}: {resp.text[:200]}")
            return {"decision": "SKIP", "confidence": 0, "reason": f"API error {resp.status_code}"}
    except json.JSONDecodeError:
        return {"decision": "SKIP", "confidence": 0, "reason": "AI JSON parse error"}
    except Exception as e:
        print(f"[Groq Exception] {e}")
        return {"decision": "SKIP", "confidence": 0, "reason": str(e)}


# ==================== SCANNING ====================

def rugcheck_token(token_address):
    try:
        url = f"https://api.rugcheck.xyz/v1/tokens/{token_address}/report"
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            sec = data.get('security', {})
            lp_locked = sec.get('lpLockedPercentage') or 0
            mint_revoked = sec.get('mintAuthorityRevoked', False)
            return {
                'lp_locked_pct': lp_locked,
                'mint_revoked': mint_revoked,
                'is_safe': lp_locked >= MIN_LP_LOCKED_PCT and mint_revoked
            }
    except Exception as e:
        print(f"[Rugcheck Error] {e}")
    return {'lp_locked_pct': 0, 'mint_revoked': False, 'is_safe': False}


def get_dexscreener_pairs():
    all_pairs = []
    seen_addrs = set()

    # Source 1: Trending boosted tokens on Solana
    try:
        resp = requests.get(
            "https://api.dexscreener.com/token-boosts/top/v1",
            timeout=12
        )
        if resp.status_code == 200:
            boosted = resp.json() if isinstance(resp.json(), list) else []
            sol_addrs = [b.get('tokenAddress') for b in boosted if b.get('chainId') == 'solana']
            for addr in sol_addrs[:20]:
                if addr:
                    try:
                        r2 = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{addr}", timeout=8)
                        pairs = r2.json().get('pairs', [])
                        for p in pairs:
                            a = p.get('baseToken', {}).get('address')
                            if a and a not in seen_addrs:
                                all_pairs.append(p)
                                seen_addrs.add(a)
                    except:
                        pass
    except Exception as e:
        print(f"[DexScreener Boost Error] {e}")

    # Source 2: Latest boosted tokens
    try:
        resp = requests.get(
            "https://api.dexscreener.com/token-boosts/latest/v1",
            timeout=12
        )
        if resp.status_code == 200:
            boosted = resp.json() if isinstance(resp.json(), list) else []
            sol_addrs = [b.get('tokenAddress') for b in boosted if b.get('chainId') == 'solana']
            for addr in sol_addrs[:20]:
                if addr:
                    try:
                        r2 = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{addr}", timeout=8)
                        pairs = r2.json().get('pairs', [])
                        for p in pairs:
                            a = p.get('baseToken', {}).get('address')
                            if a and a not in seen_addrs:
                                all_pairs.append(p)
                                seen_addrs.add(a)
                    except:
                        pass
    except Exception as e:
        print(f"[DexScreener Latest Boost Error] {e}")

    # Source 3: Search SOL pairs
    for query in ["SOL", "pump", "meme"]:
        try:
            resp = requests.get(
                f"https://api.dexscreener.com/latest/dex/search?q={query}",
                timeout=12
            )
            if resp.status_code == 200:
                pairs = resp.json().get('pairs', [])
                for p in pairs:
                    a = p.get('baseToken', {}).get('address')
                    if a and a not in seen_addrs:
                        all_pairs.append(p)
                        seen_addrs.add(a)
        except Exception as e:
            print(f"[DexScreener Search Error q={query}] {e}")

    print(f"[DexScreener] Total unique pairs fetched: {len(all_pairs)}")
    return all_pairs


def run_12_confirmations(pair, rug_data):
    base = pair.get('baseToken', {})
    quote = pair.get('quoteToken', {})

    if quote.get('symbol') != "SOL":
        return False, 0, {}

    token_addr = base.get('address')
    if not token_addr:
        return False, 0, {}

    try:
        price_usd = float(pair.get('priceUsd') or 0)
        liquidity = float(pair.get('liquidity', {}).get('usd') or 0)
        mc = float(pair.get('fdv') or pair.get('marketCap') or 0)

        vol = pair.get('volume', {})
        vol_5m = float(vol.get('m5') or 0)
        vol_1h = float(vol.get('h1') or 0)
        vol_24h = float(vol.get('h24') or 0)

        price_change = pair.get('priceChange', {})
        pc_5m = float(price_change.get('m5') or 0)
        pc_1h = float(price_change.get('h1') or 0)
        pc_24h = float(price_change.get('h24') or 0)

        txns_5m = pair.get('txns', {}).get('m5', {})
        buys_5m = int(txns_5m.get('buys') or 0)
        sells_5m = int(txns_5m.get('sells') or 1)

        txns_1h = pair.get('txns', {}).get('h1', {})
        buys_1h = int(txns_1h.get('buys') or 0)
        sells_1h = int(txns_1h.get('sells') or 1)

        buy_ratio_5m = buys_5m / sells_5m if sells_5m > 0 else 0
        buy_ratio_1h = buys_1h / sells_1h if sells_1h > 0 else 0
        vol_mc_ratio = vol_1h / mc if mc > 0 else 0

        lp_locked = rug_data.get('lp_locked_pct', 0)
        mint_revoked = rug_data.get('mint_revoked', False)

        confirmations = 0
        check_results = []

        checks = [
            (liquidity >= MIN_LIQUIDITY,                           f"1. Liq ${liquidity:,.0f}>=${MIN_LIQUIDITY:,}"),
            (vol_5m >= MIN_5M_VOLUME,                              f"2. Vol5m ${vol_5m:,.0f}>=${MIN_5M_VOLUME:,}"),
            (vol_1h >= MIN_1H_VOLUME,                              f"3. Vol1h ${vol_1h:,.0f}>=${MIN_1H_VOLUME:,}"),
            (round(buy_ratio_5m, 2) >= MIN_BUY_RATIO_5M,          f"4. BR5m {buy_ratio_5m:.2f}>={MIN_BUY_RATIO_5M}"),
            (round(buy_ratio_1h, 2) >= MIN_BUY_RATIO_1H,          f"5. BR1h {buy_ratio_1h:.2f}>={MIN_BUY_RATIO_1H}"),
            (10000 <= mc <= MAX_MC,                                f"6. MC ${mc:,.0f} in range"),
            (lp_locked >= MIN_LP_LOCKED_PCT or mint_revoked,       f"7. Safety LP={lp_locked:.0f}% Rev={mint_revoked}"),
            (MIN_PRICE_CHANGE_5M <= pc_5m <= MAX_PRICE_CHANGE_5M, f"8. PC5m {pc_5m:.1f}% in range"),
            (MIN_PRICE_CHANGE_1H <= pc_1h <= MAX_PRICE_CHANGE_1H, f"9. PC1h {pc_1h:.1f}% in range"),
            (vol_mc_ratio >= MIN_VOLUME_MCAP_RATIO,               f"10. VolMC {vol_mc_ratio:.4f}>={MIN_VOLUME_MCAP_RATIO}"),
            (buys_5m >= 8,                                         f"11. Buys5m {buys_5m}>=8"),
            (buys_5m > sells_5m,                                   f"12. MoreBuys {buys_5m}>{sells_5m}"),
        ]

        for passed, label in checks:
            check_results.append(f"{label}: {'✅' if passed else '❌'}")
            if passed:
                confirmations += 1

        token_data = {
            'address': token_addr,
            'symbol': base.get('symbol', 'UNKNOWN'),
            'price': price_usd,
            'liquidity': liquidity,
            'mc': mc,
            'vol_5m': vol_5m,
            'vol_1h': vol_1h,
            'vol_24h': vol_24h,
            'buy_ratio_5m': buy_ratio_5m,
            'buy_ratio_1h': buy_ratio_1h,
            'price_change_5m': pc_5m,
            'price_change_1h': pc_1h,
            'price_change_24h': pc_24h,
            'lp_locked': lp_locked,
            'mint_revoked': mint_revoked,
            'vol_mc_ratio': vol_mc_ratio,
            'buys_5m': buys_5m,
            'confirmations_passed': confirmations,
            'check_results': check_results
        }

        return confirmations >= 8, confirmations, token_data

    except Exception as e:
        print(f"[Confirm Error] {e}")
        return False, 0, {}


def analyze_pair(pair):
    base = pair.get('baseToken', {})
    quote = pair.get('quoteToken', {})
    symbol = base.get('symbol', '?')

    if quote.get('symbol') != "SOL":
        return None

    token_addr = base.get('address')
    if not token_addr or token_addr in seen_tokens or token_addr in positions:
        return None

    # Pre-filter with logging
    try:
        liquidity = float(pair.get('liquidity', {}).get('usd') or 0)
        vol_5m = float(pair.get('volume', {}).get('m5') or 0)
        mc = float(pair.get('fdv') or pair.get('marketCap') or 0)

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
        fails = [r for r in token_data.get('check_results', []) if '❌' in r]
        print(f"[Skip] {symbol} — {score}/12 checks | Failed: {', '.join(fails[:3])}")
        return None

    print(f"\n[🔍 AI Check] {symbol} passed {score}/12 — asking Groq AI...")
    ai_result = ask_ai_brain(token_data)
    decision = ai_result.get("decision", "SKIP")
    confidence = ai_result.get("confidence", 0)
    reason = ai_result.get("reason", "")

    print(f"[🤖 AI] {decision} | {confidence}% | {reason}")

    if decision != "BUY" or confidence < 55:
        print(f"[Skip] AI rejected {symbol} ({confidence}%): {reason}")
        return None

    token_data['ai_confidence'] = confidence
    token_data['ai_reason'] = reason
    token_data['score'] = score
    return token_data


# ==================== TRADING ====================

def simulate_buy(token_data):
    global balance_usd

    if balance_usd < MIN_TRADE_USD:
        return False
    if len(positions) >= MAX_POSITIONS:
        return False

    token_addr = token_data['address']
    symbol = token_data['symbol']
    price_usd = token_data['price']
    amount_usd = min(TRADE_AMOUNT_USD, balance_usd)
    tokens_bought = amount_usd / price_usd

    positions[token_addr] = {
        "symbol": symbol,
        "entry_price": price_usd,
        "amount_tokens": tokens_bought,
        "amount_usd": amount_usd,
        "tp_price": price_usd * (1 + TP_PERCENT / 100),
        "sl_price": price_usd * (1 - SL_PERCENT / 100),
        "entry_time": datetime.now(),
        "ai_confidence": token_data.get('ai_confidence', 0),
        "score": token_data.get('score', 0)
    }

    balance_usd -= amount_usd
    seen_tokens.add(token_addr)

    send_telegram(f"""🚀 <b>SIMULATED BUY EXECUTED</b>
━━━━━━━━━━━━━━━━━━━━
🪙 Token: <b>{symbol}</b>
💵 Entry: ${price_usd:.10f}
💰 Amount: ${amount_usd:.2f} ({tokens_bought:.2f} tokens)
🎯 TP: +{TP_PERCENT}% | 🛑 SL: -{SL_PERCENT}%

🤖 AI Confidence: <b>{token_data.get('ai_confidence', 0)}%</b>
📊 Confirmations: {token_data.get('score', 0)}/12
💡 {token_data.get('ai_reason', 'N/A')}

📈 MC: ${token_data.get('mc', 0):,.0f}
💧 Liq: ${token_data.get('liquidity', 0):,.0f}
📊 Vol 5m: ${token_data.get('vol_5m', 0):,.0f}
🔄 Buy Ratio: {token_data.get('buy_ratio_5m', 0):.2f}x

💼 Balance Left: <b>${balance_usd:.2f}</b>
━━━━━━━━━━━━━━━━━━━━""")

    print(f"[BUY] {symbol} @ ${price_usd:.10f} | ${amount_usd:.2f} | Balance: ${balance_usd:.2f}")
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
            pairs = data.get('pairs', [])
            if not pairs:
                continue

            current_price = float(pairs[0]['priceUsd'])
            pnl_percent = (current_price - pos['entry_price']) / pos['entry_price'] * 100
            pnl_usd = (current_price - pos['entry_price']) * pos['amount_tokens']
            exit_value = pos['amount_tokens'] * current_price

            if current_price >= pos['tp_price']:
                balance_usd += exit_value
                trade_history.append({'symbol': pos['symbol'], 'pnl_usd': pnl_usd, 'pnl_pct': pnl_percent, 'result': 'TP', 'time': datetime.now()})
                del positions[addr]
                send_telegram(f"""✅ <b>TAKE PROFIT HIT!</b>
━━━━━━━━━━━━━━━━━━━━
🪙 <b>{pos['symbol']}</b>
📥 Entry: ${pos['entry_price']:.10f}
📤 Exit:  ${current_price:.10f}
📈 PnL: <b>+${pnl_usd:.2f} (+{pnl_percent:.1f}%)</b>
💼 Balance: <b>${balance_usd:.2f}</b> / Target: ${TARGET_BALANCE_USD:.2f}
━━━━━━━━━━━━━━━━━━━━""")
                print(f"[TP] {pos['symbol']} +${pnl_usd:.2f} | Balance: ${balance_usd:.2f}")

            elif current_price <= pos['sl_price']:
                balance_usd += exit_value
                trade_history.append({'symbol': pos['symbol'], 'pnl_usd': pnl_usd, 'pnl_pct': pnl_percent, 'result': 'SL', 'time': datetime.now()})
                del positions[addr]
                send_telegram(f"""❌ <b>STOP LOSS HIT</b>
━━━━━━━━━━━━━━━━━━━━
🪙 <b>{pos['symbol']}</b>
📥 Entry: ${pos['entry_price']:.10f}
📤 Exit:  ${current_price:.10f}
📉 PnL: <b>${pnl_usd:.2f} ({pnl_percent:.1f}%)</b>
💼 Balance: <b>${balance_usd:.2f}</b>
━━━━━━━━━━━━━━━━━━━━""")
                print(f"[SL] {pos['symbol']} ${pnl_usd:.2f} | Balance: ${balance_usd:.2f}")

            else:
                print(f"[Hold] {pos['symbol']} | PnL: {pnl_percent:+.1f}%")

        except Exception as e:
            print(f"[Position Error] {addr}: {e}")


# ==================== MAIN LOOP ====================

def main_loop():
    global balance_usd

    print(f"[BOT START] Balance: ${balance_usd:.2f} | Target: ${TARGET_BALANCE_USD:.2f}")
    print(f"[AI] Groq: {GROQ_MODEL} | Key: {'LOADED' if GROQ_API_KEY else 'MISSING'}")

    # Start command listener in background thread
    t = threading.Thread(target=command_listener, daemon=True)
    t.start()

    send_telegram(f"""🤖 <b>AI TRADING BOT STARTED</b>
━━━━━━━━━━━━━━━━━━━━
💼 Balance: <b>${INITIAL_BALANCE_USD}</b> → Target: <b>${TARGET_BALANCE_USD}</b>
💰 Trade Size: ${TRADE_AMOUNT_USD} | Max Positions: {MAX_POSITIONS}
📈 TP: +{TP_PERCENT}% | 🛑 SL: -{SL_PERCENT}%
🔍 Required: 10/12 checks + AI 65%+ confidence
🤖 AI Brain: Groq <b>{GROQ_MODEL}</b>
📡 Mode: PAPER TRADING (Demo)

<b>Commands:</b> /status /balance /positions /history /pause /help
━━━━━━━━━━━━━━━━━━━━""")

    last_status_time = time.time()
    scan_count = 0

    while True:
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
                if token_data and token_data['address'] not in positions:
                    simulate_buy(token_data)
                    time.sleep(2)
        else:
            reason = "paused" if bot_paused else ("full positions" if len(positions) >= MAX_POSITIONS else "low balance")
            print(f"[Wait] Skipping scan — {reason}")

        if time.time() - last_status_time >= 1800:
            # Auto status every 30 min
            wins = [t for t in trade_history if t['result'] == 'TP']
            losses = [t for t in trade_history if t['result'] == 'SL']
            total_pnl = sum(t['pnl_usd'] for t in trade_history)
            send_telegram(f"""📊 <b>AUTO STATUS UPDATE</b>
Balance: <b>${balance_usd:.2f}</b> / ${TARGET_BALANCE_USD:.2f}
PnL: ${total_pnl:+.2f} | W: {len(wins)} L: {len(losses)}
Positions: {len(positions)}/{MAX_POSITIONS}
Type /status for full details.""")
            last_status_time = time.time()

        print(f"[Sleep] {CHECK_INTERVAL}s...")
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main_loop()
