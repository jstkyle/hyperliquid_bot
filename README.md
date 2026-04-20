# Hyperliquid Copy Trading Bot

A production-ready copy trading bot for [Hyperliquid](https://hyperliquid.xyz) perpetual futures. Monitors a leader wallet's positions in real-time via WebSocket and automatically mirrors trades to your follower wallet with proportional sizing.

## How It Works

```
Leader opens 1 BTC long ($75,000)     Your wallet opens 0.013 BTC long ($975)
Leader increases to 2 BTC             Your wallet increases to 0.026 BTC
Leader closes position                Your wallet closes position
```

### Architecture

```
┌─────────────────────────────────────────────────────┐
│                    Main Event Loop                   │
│                                                      │
│  ┌──────────────┐   ┌────────────────────────────┐  │
│  │  WebSocket    │──▶│   Reconciliation Loop       │  │
│  │  Listener     │   │   (event + 30s timer)       │  │
│  │  (real-time)  │   └────────┬───────────────────┘  │
│  └──────────────┘            │                       │
│                    ┌─────────▼─────────┐             │
│                    │  Decision Engine   │             │
│                    │  (compute deltas)  │             │
│                    └─────────┬─────────┘             │
│                    ┌─────────▼─────────┐             │
│                    │  Risk Controller   │             │
│                    │  (limits & kills)  │             │
│                    └─────────┬─────────┘             │
│                    ┌─────────▼─────────┐             │
│                    │ Execution Engine   │             │
│                    │ (paper / live)     │             │
│  ┌──────────────┐ └───────────────────┘             │
│  │ Discord Bot  │◀─── BotController ──▶ All above   │
│  │ (/ commands) │                                    │
│  └──────────────┘                                    │
└─────────────────────────────────────────────────────┘
```

### Execution Flow

1. **WebSocket** detects a leader fill → triggers reconciliation in ~100ms
2. **REST Poller** fetches fresh leader & follower state
3. **Decision Engine** computes target positions:
   ```
   target = leader_size × (follower_equity / leader_equity) × multiplier
   ```
4. **Risk Controller** checks kill switch, exposure limits, position caps
5. **Execution Engine** places IOC orders (paper simulates, live submits to exchange)
6. **30s backup loop** catches any missed WebSocket events

### Key Design Decisions

- **Copies positions, not orders** — avoids partial fill issues and ensures convergence
- **Ignores pre-existing positions** — on startup, takes a snapshot of the leader's state as baseline and only copies NEW changes
- **Floor-truncation** — sizes are always truncated down to `szDecimals` to prevent balance overflows
- **Drift threshold** — ignores micro-drifts (<2%) to avoid unnecessary orders

---

## Quick Start

### 1. Clone & Install

```bash
git clone https://github.com/jstkyle/hyperliquid_bot.git
cd hyperliquid_bot
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure Secrets

```bash
cp .env.example .env
nano .env
```

```env
DISCORD_BOT_TOKEN=your_discord_bot_token
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
HL_PAIR_0_FOLLOWER_ADDRESS=0xyour_wallet_address
# Only for live mode:
# HL_PAIR_0_AGENT_PRIVATE_KEY=your_agent_wallet_key
```

Secrets are loaded automatically from `.env` on startup — no manual exports needed.

### 3. Run (Paper Mode)

```bash
python -m copybot.main --mode paper
```

Paper mode uses simulated equity ($10,000 by default) and doesn't touch the exchange. All trades are logged and visible via Discord.

### 4. Run (Live Mode)

```bash
python -m copybot.main --mode live
```

> ⚠️ Live mode requires an **agent wallet** private key and real funds. Paper trade for 72+ hours before going live.

---

## Discord Control Interface

The bot includes a full Discord slash command interface. Type `/` in any channel to see all commands.

### Setup Discord Bot

1. Go to [discord.com/developers/applications](https://discord.com/developers/applications)
2. Create **New Application** → name it "HL CopyBot"
3. **Bot** tab → Reset Token → copy it → add to `.env`
4. Enable **Message Content Intent** under Privileged Gateway Intents
5. **OAuth2 → URL Generator** → scope: `bot` → permissions: `Send Messages`, `Read Messages`, `Embed Links`
6. Open the URL → add bot to your server

### Commands

| Command | Description |
|---------|-------------|
| `/status` | Bot uptime, mode, WebSocket connection status |
| `/balance` | Leader & follower equity and position count |
| `/positions` | All open positions side-by-side (leader vs follower) |
| `/pnl` | Session profit/loss (USD and %) |
| `/trades` | Recent trade history with coin, side, size, price |
| `/pause` | Pause all trading (monitoring continues) |
| `/resume` | Resume trading |
| `/kill` | ⚠️ Emergency close all positions (requires `/confirm_kill`) |
| `/reset` | Reset kill switch after review |
| `/config` | View current configuration |
| `/set` | Change multiplier or max position cap live |
| `/help` | Show all commands |

Only authorized Discord user IDs can run control commands.

---

## Configuration

All settings are in `copybot/config/settings.yaml`:

### Pairs
```yaml
pairs:
  - name: "leader_alpha"
    leader_address: "0x..."           # Leader wallet to copy
    follower_address: ""              # Set via env var
    agent_private_key_env: "HL_PAIR_0_AGENT_PRIVATE_KEY"
```

### Scaling
```yaml
scaling:
  multiplier: 1.0              # Extra scaling factor (0.5 = half leader size)
  min_order_notional: 11.0     # USD minimum (above HL's $10 min)
  drift_threshold_pct: 0.02    # 2% — ignore micro-drifts
  paper_equity: 10000.0        # Simulated equity for paper mode
```

### Risk Controls
```yaml
risk:
  symbol_whitelist: "ALL"         # Or ["BTC", "ETH", "SOL"]
  max_position_usd: 50000.0      # Per-symbol notional cap
  max_total_exposure_usd: 200000  # Total portfolio exposure cap
  max_leverage: 20                # Won't exceed even if leader does
  kill_switch:
    loss_usd: -5000.0            # Auto-close all at this session loss
    loss_pct: -0.10              # Or 10% of starting equity
```

---

## Deployment (AWS EC2)

### Initial Setup
```bash
# SSH into your instance
ssh -i ~/.ssh/hyper-key.pem ubuntu@your-ec2-ip

# Clone and install
git clone https://github.com/jstkyle/hyperliquid_bot.git
cd hyperliquid_bot
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Configure secrets
cp .env.example .env
nano .env
```

### Run with tmux (persists across SSH disconnects)
```bash
tmux new -s copybot
source venv/bin/activate
python -m copybot.main --mode paper

# Detach: Ctrl+B, then D
# Reattach: tmux attach -t copybot
```

### Run with systemd (auto-restart, auto-start on reboot)

Create `/etc/systemd/system/hl-copybot.service`:
```ini
[Unit]
Description=Hyperliquid Copy Trading Bot
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/hyperliquid_bot
EnvironmentFile=/home/ubuntu/hyperliquid_bot/.env
ExecStart=/home/ubuntu/hyperliquid_bot/venv/bin/python -m copybot.main
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable hl-copybot
sudo systemctl start hl-copybot
sudo journalctl -u hl-copybot -f   # View logs
```

---

## Project Structure

```
hyperliquid_bot/
├── copybot/
│   ├── config/
│   │   ├── loader.py          # YAML + env var config parsing
│   │   └── settings.yaml      # All bot settings
│   ├── engine/
│   │   ├── decision.py        # Position delta computation
│   │   ├── execution.py       # Live order execution (IOC via SDK)
│   │   ├── paper_trader.py    # Simulated execution for paper mode
│   │   ├── reconciliation.py  # Main loop (event + timer driven)
│   │   └── risk.py            # Kill switch, exposure limits, caps
│   ├── ingestion/
│   │   ├── rest_poller.py     # REST API for state + mid prices
│   │   └── ws_listener.py     # WebSocket for real-time fill detection
│   ├── state/
│   │   ├── metadata.py        # Asset metadata cache (szDecimals, etc.)
│   │   ├── models.py          # Core dataclasses (positions, orders)
│   │   └── store.py           # SQLite state persistence (WAL mode)
│   ├── utils/
│   │   ├── alerting.py        # Discord webhook notifications
│   │   ├── logging.py         # Structured JSON logging (structlog)
│   │   └── math.py            # Floor-truncation, price rounding
│   ├── controller.py          # Bridges Discord commands ↔ trading
│   ├── discord_bot.py         # Slash command interface
│   └── main.py                # Entry point, wires everything
├── tests/
│   └── unit/                  # 73 unit tests
├── .env.example               # Environment variable template
├── requirements.txt
└── pyproject.toml
```

---

## Safety Features

| Feature | Description |
|---------|-------------|
| **Kill Switch** | Auto-closes all positions if session loss exceeds threshold |
| **Position Cap** | Per-symbol notional limit prevents oversized positions |
| **Exposure Cap** | Total portfolio exposure limit across all positions |
| **Leverage Cap** | Won't exceed configured max even if leader uses higher |
| **Consecutive Failures** | Pauses after N consecutive execution failures |
| **Drift Threshold** | Ignores noise — only acts on meaningful position changes |
| **Direction Flip Safety** | Splits long→short into close + open (never crosses zero in one order) |
| **Paper Mode** | Full simulation with no exchange interaction |
| **Discord Kill** | `/kill` command requires `/confirm_kill` within 30 seconds |

---

## License

MIT
