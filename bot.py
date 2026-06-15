import asyncio
import json
import logging
import os
import re
import time
from decimal import Decimal
from pathlib import Path

import httpx
import websockets
from websockets.asyncio.client import connect as ws_connect
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

NETWORK = os.environ.get("NETWORK", "mainnet").lower()
SUBSCRIPTIONS_WS = (
    "wss://gateway.prod.nado.xyz/v1/subscribe"
    if NETWORK == "mainnet"
    else "wss://gateway.test.nado.xyz/v1/subscribe"
)

PING_INTERVAL = 25
RECONNECT_DELAY = 5

GATEWAY_REST = (
    "https://gateway.prod.nado.xyz/v1"
    if NETWORK == "mainnet"
    else "https://gateway.test.nado.xyz/v1"
)


HEALTH_CHECK_INTERVAL = int(os.environ.get("HEALTH_CHECK_INTERVAL", "60"))       # seconds between checks
MARGIN_ALERT_THRESHOLD = float(os.environ.get("MARGIN_ALERT_THRESHOLD", "10.0")) # % of assets — alert below
MARGIN_ALERT_COOLDOWN = int(os.environ.get("MARGIN_ALERT_COOLDOWN", "3600"))      # seconds between repeated alerts


PRODUCT_NAMES: dict[int, str] = {}


user_sessions: dict[int, dict] = {}

SESSIONS_FILE = Path(__file__).parent / "sessions.json"


def save_sessions() -> None:
    data = {str(cid): s["wallet"] for cid, s in user_sessions.items()}
    SESSIONS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_sessions_from_file() -> dict[int, str]:
    if not SESSIONS_FILE.exists():
        return {}
    try:
        data = json.loads(SESSIONS_FILE.read_text(encoding="utf-8"))
        return {int(k): v for k, v in data.items()}
    except Exception as e:
        log.error("Failed to load sessions: %s", e)
        return {}


async def load_product_names() -> None:
    url = f"{GATEWAY_REST}/query?type=symbols"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, headers={"Accept-Encoding": "gzip"}, timeout=10)
            resp.raise_for_status()
            data = resp.json()
        symbols = data.get("data", {}).get("symbols", {})
        for symbol, info in symbols.items():
            pid = info.get("product_id")
            if pid is not None:
                PRODUCT_NAMES[pid] = symbol
        log.info("Loaded %d tokens: %s", len(PRODUCT_NAMES), list(PRODUCT_NAMES.values()))
    except Exception as e:
        log.error("Failed to fetch the token list: %s", e)



async def fetch_subaccount_info(subaccount: str) -> dict | None:
    url = f"{GATEWAY_REST}/query?type=subaccount_info&subaccount={subaccount}"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, headers={"Accept-Encoding": "gzip"}, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            if data.get("status") == "success":
                return data.get("data")
    except Exception as e:
        log.error("fetch_subaccount_info error: %s", e)
    return None


async def fetch_isolated_positions(subaccount: str) -> list | None:
    url = f"{GATEWAY_REST}/query?type=isolated_positions&subaccount={subaccount}"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, headers={"Accept-Encoding": "gzip"}, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            if data.get("status") == "success":
                return data.get("data", {}).get("isolated_positions", [])
    except Exception as e:
        log.error("fetch_isolated_positions error: %s", e)
    return None



def build_subaccount(wallet: str, name: str = "default") -> str:
    addr = wallet.lower().removeprefix("0x")
    name_bytes = name.encode("utf-8")
    name_hex = name_bytes.hex().ljust(24, "0")
    return "0x" + addr + name_hex


def x18(value: str) -> Decimal:
    """Convert x18-scaled integer string to Decimal."""
    try:
        return Decimal(value) / Decimal("1e18")
    except Exception:
        return Decimal("0")


def x18_to_human(value: str, decimals: int = 6) -> str:
    try:
        result = Decimal(value) / Decimal("1e18")
        return f"{result:.{decimals}f}"
    except Exception:
        return value


def is_valid_address(addr: str) -> bool:
    return bool(re.match(r"^0x[0-9a-fA-F]{40}$", addr))


def decode_order_type(appendix_str: str) -> str:
    try:
        appendix = int(appendix_str)
    except (ValueError, TypeError):
        return "📋 Limit"

    trigger_type = (appendix >> 12) & 0b11
    order_type   = (appendix >> 9)  & 0b11
    reduce_only  = bool((appendix >> 11) & 1)

    if trigger_type == 1:
        if reduce_only:
            return "⛔ Stop-Loss / Take-Profit"
        return "🎯 Trigger"
    if trigger_type in (2, 3):
        return "⏱ TWAP"
    if order_type == 1:
        return "⚡ Market"
    if order_type == 2:
        return "⚡ FOK"
    if order_type == 3:
        return "📌 Post-Only"
    return "📋 Limit"



def health_ratio_pct(health_obj: dict) -> float:
    """Maintenance margin ratio = health / assets * 100 (%).
    0 % means exactly at liquidation threshold, negative = already liquidatable."""
    try:
        assets = x18(health_obj.get("assets", "0"))
        health = x18(health_obj.get("health", "0"))
        if assets <= 0:
            return 100.0
        return float(health / assets * 100)
    except Exception:
        return 0.0


def health_status_line(ratio: float) -> str:
    if ratio < 0:
        return "🔴 <b>DANGER — Liquidation imminent!</b>"
    if ratio < MARGIN_ALERT_THRESHOLD:
        return "🟠 <b>WARNING — Low margin</b>"
    if ratio < 25:
        return "🟡 <b>Caution</b>"
    return "🟢 <b>Healthy</b>"


def calc_unrealized_pnl(amount: str, v_quote: str, oracle_price_x18: str) -> Decimal:
    """PnL = size * oracle_price + v_quote_balance (all x18-scaled)."""
    try:
        return x18(amount) * x18(oracle_price_x18) + x18(v_quote)
    except Exception:
        return Decimal("0")


def calc_leverage(
    amount: str,
    oracle_price_x18: str,
    long_weight_initial_x18: str,
    short_weight_initial_x18: str,
    quote_balance: str | None = None,
    account_equity: str | None = None,
) -> float:
    """Leverage calculation.
    Isolated: notional / collateral (exact).
    Cross: notional / account_unweighted_equity (actual effective leverage)."""
    try:
        amt = x18(amount)
        if amt == 0:
            return 0.0
        notional = abs(amt) * x18(oracle_price_x18)
        if quote_balance is not None:
            # Isolated: actual leverage = notional / collateral
            collateral = x18(quote_balance)
            if collateral <= 0:
                return 0.0
            return float(notional / collateral)
        if account_equity is not None:
            # Cross: effective leverage = notional / account net equity
            equity = x18(account_equity)
            if equity <= 0:
                return 0.0
            return float(notional / equity)
        return 0.0
    except Exception:
        return 0.0


def calc_liq_price(
    amount: str,
    v_quote: str,
    oracle_price_x18: str,
    long_weight_maint_x18: str,
    short_weight_maint_x18: str,
    maint_health: str,
    quote_balance: str | None = None,
) -> Decimal | None:
    """Liquidation price estimation.
    Isolated: exact — solve health=0 for price.
    Cross: approximate — how far price can move before total health hits 0."""
    try:
        amt = x18(amount)
        if amt == 0:
            return None
        v_q   = x18(v_quote)
        lw    = x18(long_weight_maint_x18)
        sw    = x18(short_weight_maint_x18)

        if quote_balance is not None:
            # Isolated: quote_bal + amt * liq_price * weight + v_quote = 0
            q = x18(quote_balance)
            weight = lw if amt > 0 else sw
            if weight == 0:
                return None
            liq = -(q + v_q) / (amt * weight)
        else:
            # Cross: price moves until total maintenance health = 0
            price  = x18(oracle_price_x18)
            health = x18(maint_health)
            weight = lw if amt > 0 else sw
            if weight == 0:
                return None
            liq = price - health / (amt * weight)

        return max(Decimal("0"), liq)
    except Exception:
        return None



def format_fill(event: dict) -> str:
    product_id  = event.get("product_id", "?")
    symbol      = PRODUCT_NAMES.get(product_id, f"#{product_id}")
    filled_qty  = x18_to_human(event.get("filled_qty", "0"))
    price       = x18_to_human(event.get("price", "0"), decimals=2)
    remaining   = x18_to_human(event.get("remaining_qty", "0"))
    original    = x18_to_human(event.get("original_qty", "0"))
    fee         = x18_to_human(event.get("fee", "0"))
    is_bid      = event.get("is_bid", True)
    is_taker    = event.get("is_taker", True)
    order_label = decode_order_type(event.get("appendix", "0"))

    side = "🟢 BUY" if is_bid else "🔴 SELL"
    role = "taker" if is_taker else "maker"

    return (
        f"✅ <b>Order filled!</b>\n"
        f"\n"
        f"{side}  <b>{symbol}</b>\n"
        f"🏷 Type: {order_label}\n"
        f"\n"
        f"📊 Filled:  <b>{filled_qty}</b>\n"
        f"💵 Price:       <b>{price} USDT</b>\n"
        f"📦 Remaining:   <b>{remaining}</b> / {original}\n"
        f"💸 Fee:   {fee} USDT ({role})"
    )


def format_positions_msg(info: dict, isolated: list) -> str:
    lines = ["📊 <b>Open Positions</b>\n"]
    has_positions = False

    
    cross_healths      = info.get("healths", [{}, {}, {}])
    cross_maint_h      = cross_healths[1] if len(cross_healths) > 1 else {}
    cross_unweighted_h = cross_healths[2] if len(cross_healths) > 2 else {}
    cross_health_v     = cross_maint_h.get("health", "0")
    cross_equity_v     = cross_unweighted_h.get("health", "0")

    perp_products_map = {p["product_id"]: p for p in info.get("perp_products", [])}
    for bal in info.get("perp_balances", []):
        pid     = bal["product_id"]
        amount  = bal["balance"]["amount"]
        v_quote = bal["balance"].get("v_quote_balance", "0")
        if amount == "0":
            continue

        has_positions = True
        product    = perp_products_map.get(pid, {})
        risk       = product.get("risk", {})
        oracle_x18 = product.get("oracle_price_x18", "0")
        symbol     = PRODUCT_NAMES.get(pid, f"#{pid}")

        size       = x18(amount)
        oracle_usd = x18(oracle_x18)
        pnl        = calc_unrealized_pnl(amount, v_quote, oracle_x18)

        lw_init  = risk.get("long_weight_initial_x18", "0")
        sw_init  = risk.get("short_weight_initial_x18", "0")
        lw_maint = risk.get("long_weight_maintenance_x18", "0")
        sw_maint = risk.get("short_weight_maintenance_x18", "0")

        liq_p    = calc_liq_price(amount, v_quote, oracle_x18, lw_maint, sw_maint, cross_health_v)

        side     = "🟢 LONG" if size > 0 else "🔴 SHORT"
        pnl_icon = "📈" if pnl >= 0 else "📉"
        pnl_sign = "+" if pnl >= 0 else ""
        liq_str  = f"{liq_p:.2f} USDT" if liq_p is not None else "N/A"

        lines.append(
            f"<b>{symbol}</b>  <i>Cross</i>\n"
            f"  {side}  Size: <b>{abs(size):.4f}</b>\n"
            f"  💵 Oracle: <b>{oracle_usd:.2f} USDT</b>\n"
            f"  {pnl_icon} PnL: <b>{pnl_sign}{pnl:.2f} USDT</b>\n"
            f"  💀 Liq. price: <b>{liq_str}</b>\n"
        )

    
    for pos in (isolated or []):
        base_bal     = pos.get("base_balance", {})
        base_product = pos.get("base_product", {})
        pid          = base_bal.get("product_id") or base_product.get("product_id")
        amount       = base_bal["balance"]["amount"]
        v_quote      = base_bal["balance"].get("v_quote_balance", "0")
        oracle_x18   = base_product.get("oracle_price_x18", "0")
        quote_bal    = pos.get("quote_balance", {}).get("balance", {}).get("amount", "0")

        if amount == "0":
            continue

        has_positions = True
        risk       = base_product.get("risk", {})
        symbol     = PRODUCT_NAMES.get(pid, f"#{pid}")
        size       = x18(amount)
        oracle_usd = x18(oracle_x18)
        pnl        = calc_unrealized_pnl(amount, v_quote, oracle_x18)

        lw_init  = risk.get("long_weight_initial_x18", "0")
        sw_init  = risk.get("short_weight_initial_x18", "0")
        lw_maint = risk.get("long_weight_maintenance_x18", "0")
        sw_maint = risk.get("short_weight_maintenance_x18", "0")

        leverage = calc_leverage(amount, oracle_x18, lw_init, sw_init, quote_balance=quote_bal)
        liq_p    = calc_liq_price(amount, v_quote, oracle_x18, lw_maint, sw_maint,
                                  maint_health="0", quote_balance=quote_bal)

        iso_healths = pos.get("healths", [{}, {}])
        maint_h     = iso_healths[1] if len(iso_healths) > 1 else {}
        ratio       = health_ratio_pct(maint_h)

        side     = "🟢 LONG" if size > 0 else "🔴 SHORT"
        pnl_icon = "📈" if pnl >= 0 else "📉"
        pnl_sign = "+" if pnl >= 0 else ""
        liq_str  = f"{liq_p:.2f} USDT" if liq_p is not None else "N/A"

        lines.append(
            f"<b>{symbol}</b>  <i>Isolated</i>\n"
            f"  {side}  Size: <b>{abs(size):.4f}</b>\n"
            f"  💵 Oracle: <b>{oracle_usd:.2f} USDT</b>\n"
            f"  {pnl_icon} PnL: <b>{pnl_sign}{pnl:.2f} USDT</b>\n"
            f"  ⚡ Leverage: <b>{leverage:.1f}x</b>\n"
            f"  💀 Liq. price: <b>{liq_str}</b>\n"
            f"  🛡 Margin: <b>{ratio:.1f}%</b>  {health_status_line(ratio)}\n"
        )

    if not has_positions:
        return "📭 No open positions."

    return "\n".join(lines)


def format_health_msg(info: dict) -> str:
    healths = info.get("healths", [{}, {}, {}])
    initial_h  = healths[0] if len(healths) > 0 else {}
    maint_h    = healths[1] if len(healths) > 1 else {}

    initial_ratio = health_ratio_pct(initial_h)
    maint_ratio   = health_ratio_pct(maint_h)
    status        = health_status_line(maint_ratio)

    maint_assets = x18_to_human(maint_h.get("assets", "0"), decimals=2)
    maint_health = x18_to_human(maint_h.get("health", "0"), decimals=2)
    maint_liabs  = x18_to_human(maint_h.get("liabilities", "0"), decimals=2)

    return (
        f"🏥 <b>Margin Health</b>\n\n"
        f"Status: {status}\n\n"
        f"📐 Initial margin:     <b>{initial_ratio:.1f}%</b>\n"
        f"🛡 Maintenance margin: <b>{maint_ratio:.1f}%</b>\n\n"
        f"💰 Assets:      <b>{maint_assets} USDT</b>\n"
        f"📋 Liabilities: <b>{maint_liabs} USDT</b>\n"
        f"💚 Net health:  <b>{maint_health} USDT</b>\n\n"
        f"<i>ℹ️ Checked every {HEALTH_CHECK_INTERVAL}s, "
        f"alert below {MARGIN_ALERT_THRESHOLD:.0f}%</i>"
    )




async def nado_listener(app: Application, chat_id: int, wallet: str) -> None:
    subaccount = build_subaccount(wallet)

    async def send(text: str) -> None:
        try:
            await app.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
        except Exception as e:
            log.error("Error send Telegram: %s", e)

    while True:
        try:
            log.info("[%d] Connecting to Nado...", chat_id)
            async with ws_connect(
                SUBSCRIPTIONS_WS,
                additional_headers={"Sec-WebSocket-Extensions": "permessage-deflate"},
                open_timeout=15,
            ) as ws:
                log.info("[%d] Connected successfully", chat_id)

                await ws.send(json.dumps({
                    "method": "subscribe",
                    "stream": {
                        "type": "fill",
                        "product_id": None,
                        "subaccount": subaccount,
                    },
                    "id": 1,
                }))

                ping_task = asyncio.create_task(_ping_loop(ws))

                async for raw in ws:
                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    if data.get("id") == 1 and "result" in data:
                        if data["result"] is None:
                            log.info("[%d] Subscription activated", chat_id)
                        continue

                    if data.get("type") == "fill":
                        log.info("[%d] Fill: %s", chat_id, data)
                        await send(format_fill(data))

                ping_task.cancel()

        except asyncio.CancelledError:
            log.info("[%d] Listener stopped", chat_id)
            return
        except websockets.exceptions.ConnectionClosed as e:
            log.warning("[%d] Connection closed: %s", chat_id, e)
        except OSError as e:
            log.error("[%d] Error: %s", chat_id, e)
        except Exception as e:
            log.exception("[%d] Unexpected error: %s", chat_id, e)

        log.info("[%d] Reconnecting %d sec...", chat_id, RECONNECT_DELAY)
        await asyncio.sleep(RECONNECT_DELAY)


async def _ping_loop(ws) -> None:
    while True:
        await asyncio.sleep(PING_INTERVAL)
        try:
            await ws.ping()
        except Exception:
            break




async def margin_monitor(app: Application, chat_id: int, wallet: str) -> None:
    """Background task: checks margin health every HEALTH_CHECK_INTERVAL seconds.
    Sends an alert when maintenance margin ratio drops below MARGIN_ALERT_THRESHOLD."""
    subaccount = build_subaccount(wallet)
    last_alert_at: float = 0.0
    was_healthy: bool = True

    while True:
        try:
            await asyncio.sleep(HEALTH_CHECK_INTERVAL)

            info = await fetch_subaccount_info(subaccount)
            if info is None or not info.get("exists"):
                continue

            healths = info.get("healths", [{}, {}])
            maint_h = healths[1] if len(healths) > 1 else {}
            ratio   = health_ratio_pct(maint_h)

            now = time.monotonic()

            in_danger = ratio < MARGIN_ALERT_THRESHOLD
            cooldown_expired = (now - last_alert_at) >= MARGIN_ALERT_COOLDOWN

            if in_danger and (was_healthy or cooldown_expired):
                last_alert_at = now
                was_healthy   = False

                if ratio < 0:
                    urgency = "🔴 <b>CRITICAL — LIQUIDATION IS INEVITABLE!</b>"
                elif ratio < 5:
                    urgency = "🔴 <b>CRITICAL DANGER</b>"
                else:
                    urgency = "🟠 <b>WARNING</b>"

                maint_health = x18_to_human(maint_h.get("health", "0"), decimals=2)
                maint_assets = x18_to_human(maint_h.get("assets", "0"), decimals=2)

                await app.bot.send_message(
                    chat_id=chat_id,
                    parse_mode="HTML",
                    text=(
                        f"⚠️ {urgency}\n\n"
                        f"Margin dropped to <b>{ratio:.1f}%</b> "
                        f"(Threshold: {MARGIN_ALERT_THRESHOLD:.0f}%)\n\n"
                        f"💚 Health: <b>{maint_health} USDT</b>\n"
                        f"💰 Assets:   <b>{maint_assets} USDT</b>\n\n"
                        f"Check positions: /positions\n"
                        f"Full status:   /health"
                    ),
                )
            elif not in_danger:
                was_healthy = True

        except asyncio.CancelledError:
            log.info("[%d] Margin monitor stopped", chat_id)
            return
        except Exception as e:
            log.error("[%d] Margin monitor error: %s", chat_id, e)



WELCOME_IMAGE = os.path.join(os.path.dirname(__file__), "welcome.jpg")

WELCOME_TEXT = (
    "👋 <b>Welcome to Nado AlertsBot!</b>\n\n"
    "I monitor your trades and positions on Nado DEX in real time.\n\n"
    "🚀 <b>Get started:</b>\n"
    "<code>/setwallet 0x...</code> — link your wallet\n\n"
    "📋 <b>Commands:</b>\n"
    "📊 /positions — open positions with PnL &amp; liquidation price\n"
    "🏥 /health — margin health &amp; account overview\n"
    "🟢 /status — bot connection status\n"
    "🛑 /stop — disable notifications\n\n"
    "⚡ <b>Auto-alerts:</b> I'll notify you instantly when an order is filled "
    "and warn you if your margin drops to a dangerous level.\n\n"
    "🖥 <i>Running 24/7 on a dedicated server in Germany - no downtime, no interruptions.</i>"
)


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if os.path.exists(WELCOME_IMAGE):
        with open(WELCOME_IMAGE, "rb") as img:
            await update.message.reply_photo(photo=img, caption=WELCOME_TEXT, parse_mode="HTML")
    else:
        await update.message.reply_text(WELCOME_TEXT, parse_mode="HTML")


async def cmd_setwallet(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id

    if not ctx.args:
        await update.message.reply_text(
            "❌ Enter your wallet address.\n"
            "Example: <code>/setwallet 0xAbCd...1234</code>",
            parse_mode="HTML",
        )
        return

    wallet = ctx.args[0].strip()
    if not is_valid_address(wallet):
        await update.message.reply_text(
            "❌ Invalid address, must start with <code>0x</code> и contain 40 hex-символов.",
            parse_mode="HTML",
        )
        return

    session = user_sessions.get(chat_id)
    if session:
        for task_key in ("task", "monitor_task"):
            t = session.get(task_key)
            if t and not t.done():
                t.cancel()

    task         = asyncio.create_task(nado_listener(ctx.application, chat_id, wallet))
    monitor_task = asyncio.create_task(margin_monitor(ctx.application, chat_id, wallet))
    user_sessions[chat_id] = {"wallet": wallet, "task": task, "monitor_task": monitor_task}
    save_sessions()

    await update.message.reply_text(
        f"✅ <b>Wallet linked!</b>\n\n"
        f"👛 <code>{wallet}</code>\n"
        f"🌐 Chain: <b>{NETWORK}</b>\n\n"
        f"Monitoring your orders. I'll notify you as soon as they update.\n"
        f"🛡 Margin alerts enabled (threshold: {MARGIN_ALERT_THRESHOLD:.0f}%)",
        parse_mode="HTML",
    )


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    session = user_sessions.get(chat_id)

    if not session or session["task"].done():
        await update.message.reply_text("😴 The bot is inactive. Enter /setwallet 0x... to get started.")
        return

    wallet = session["wallet"]
    await update.message.reply_text(
        f"🟢 <b>Active</b>\n\n"
        f"👛 Wallet: <code>{wallet}</code>\n"
        f"🌐 Chain: <b>{NETWORK}</b>",
        parse_mode="HTML",
    )


async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    session = user_sessions.pop(chat_id, None)

    if session:
        for task_key in ("task", "monitor_task"):
            t = session.get(task_key)
            if t and not t.done():
                t.cancel()
        save_sessions()
        await update.message.reply_text("🛑 Notifications are disabled.")
    else:
        await update.message.reply_text("Notifications were not enabled anyway.")


async def cmd_positions(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    session = user_sessions.get(chat_id)

    if not session:
        await update.message.reply_text("❌ Connect your wallet first: /setwallet 0x...")
        return

    msg = await update.message.reply_text("⏳ Loading positions...")
    wallet     = session["wallet"]
    subaccount = build_subaccount(wallet)

    info, isolated = await asyncio.gather(
        fetch_subaccount_info(subaccount),
        fetch_isolated_positions(subaccount),
    )

    if info is None:
        await msg.edit_text("❌ Failed to fetch data. Try again later.")
        return

    text = format_positions_msg(info, isolated or [])
    await msg.edit_text(text, parse_mode="HTML")


async def cmd_health(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    session = user_sessions.get(chat_id)

    if not session:
        await update.message.reply_text("❌ Connect your wallet first: /setwallet 0x...")
        return

    msg = await update.message.reply_text("⏳ Loading margin data...")
    wallet     = session["wallet"]
    subaccount = build_subaccount(wallet)

    info = await fetch_subaccount_info(subaccount)

    if info is None:
        await msg.edit_text("❌ Failed to fetch data. Try again later.")
        return

    text = format_health_msg(info)
    await msg.edit_text(text, parse_mode="HTML")


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "📖 <b>Commands:</b>\n\n"
        "/setwallet <code>0x...</code> — Connect wallet and enable notifications\n"
        "/status — Current bot status\n"
        "/positions — Open positions с PnL\n"
        "/health — Margin health\n"
        "/stop — Disable notifications\n"
        "/help — Show this message\n\n"
        f"<i>🛡 Auto-alert if margin &lt;{MARGIN_ALERT_THRESHOLD:.0f}% "
        f"(check every {HEALTH_CHECK_INTERVAL}с)</i>",
        parse_mode="HTML",
    )



async def main() -> None:
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",      cmd_start))
    app.add_handler(CommandHandler("setwallet",  cmd_setwallet))
    app.add_handler(CommandHandler("status",     cmd_status))
    app.add_handler(CommandHandler("positions",  cmd_positions))
    app.add_handler(CommandHandler("health",     cmd_health))
    app.add_handler(CommandHandler("stop",       cmd_stop))
    app.add_handler(CommandHandler("help",       cmd_help))

    await load_product_names()
    log.info("Bot started. Waiting for commands...")
    async with app:
        await app.start()

        saved = load_sessions_from_file()
        if saved:
            log.info("Restoring %d sessions from file...", len(saved))
            for chat_id, wallet in saved.items():
                task         = asyncio.create_task(nado_listener(app, chat_id, wallet))
                monitor_task = asyncio.create_task(margin_monitor(app, chat_id, wallet))
                user_sessions[chat_id] = {
                    "wallet":       wallet,
                    "task":         task,
                    "monitor_task": monitor_task,
                }
                log.info("  -> chat_id=%d wallet=%s", chat_id, wallet)

        await app.updater.start_polling(drop_pending_updates=True)
        await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
