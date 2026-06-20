![Uploading welcome.jpg…]()
# Nado AlertsBot

A Telegram bot for [Nado DEX](https://nado.xyz) that monitors your trades and positions in real time.

The bot is available on Telegram: @nado_alertstracker_bot

## Features

- **Order fill alerts** - instant notification when any of your orders is executed
- **Open positions** - view all your positions with unrealized PnL and liquidation price
- **Margin health** - check your account margin status at any time
- **Auto margin alerts** - automatic warning when your maintenance margin drops below a set threshold
- **Session persistence** - bot restores all sessions automatically after a restart

## Commands

| Command | Description |
|---|---|
| `/setwallet 0x...` | Link your wallet and start monitoring |
| `/positions` | Open positions with PnL and liquidation price |
| `/health` | Margin health and account overview |
| `/status` | Current bot connection status |
| `/stop` | Disable notifications |
| `/help` | Show all commands |

## Requirements

- Python 3.11+
- Telegram bot token from [@BotFather](https://t.me/BotFather)

## Installation

```bash
git clone https://github.com/your-username/nado-bot.git
cd nado-bot
pip install -r requirements.txt
```

Create a `.env` file:

```env
TELEGRAM_BOT_TOKEN=your_token_here
NETWORK=mainnet
```

Run the bot:

```bash
python bot.py
```

## Configuration

All settings are configured via environment variables in `.env`:

| Variable | Default | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | required | Token from @BotFather |
| `NETWORK` | `mainnet` | `mainnet` or `testnet` |
| `HEALTH_CHECK_INTERVAL` | `60` | Seconds between margin checks |
| `MARGIN_ALERT_THRESHOLD` | `10.0` | Alert when margin ratio falls below this % |
| `MARGIN_ALERT_COOLDOWN` | `3600` | Seconds between repeated alerts (1 hour) |

## How It Works

### Order Fill Alerts

The bot subscribes to a WebSocket stream for each linked wallet. When an order is filled on Nado, a notification is sent instantly with fill details: symbol, side, price, size, fee and order type.

### Positions and PnL

Queries the Nado REST API (`subaccount_info` and `isolated_positions`) to fetch all open perp positions. Unrealized PnL is calculated as:

```
PnL = position_size * oracle_price + v_quote_balance
```

### Liquidation Price

- **Isolated positions** - exact calculation: price at which isolated margin health = 0
- **Cross positions** - approximation: price move that would bring total account health to 0

### Margin Health

Uses maintenance health from the API (`healths[1]`). Margin ratio is calculated as:

```
margin_ratio = health / assets * 100%
```

Status levels:
- 🟢 Healthy - ratio >= 25%
- 🟡 Caution - ratio >= 10%
- 🟠 Warning - ratio < 10% (alert triggered)
- 🔴 Danger - ratio < 0% (liquidation imminent)

## Stack

- [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot) 21.9
- [websockets](https://github.com/python-websockets/websockets) 13.1
- [httpx](https://github.com/encode/httpx) 0.28.1
- [python-dotenv](https://github.com/theskumar/python-dotenv) 1.0.1

## License

MIT
