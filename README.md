# Hyperliquid Copy Trading Bot

A production-ready copy trading bot for [Hyperliquid](https://hyperliquid.xyz) perpetual futures, fully controllable via Discord slash commands. Monitors a leader wallet in real-time via WebSocket and mirrors every trade to your follower wallet with proportional sizing.

```
Leader buys 1 BTC ($87,000)       →  You buy 0.013 BTC ($1,131)
Leader sells 500 SOL ($67,000)    →  You sell 6.5 SOL ($871)
Leader closes ETH position        →  You close ETH position
```

Your position sizes are automatically scaled by the ratio of your equity to the leader's equity.

---

## Table of Contents

- [Architecture](#architecture)
- [How Fill Copying Works](#how-fill-copying-works)
- [Position Sizing & Scaling](#position-sizing--scaling)
- [Risk Management](#risk-management)
- [Discord Control Interface](#discord-control-interface)
- [Quick Start](#quick-start)
- [Configuration Reference](#configuration-reference)
- [Deployment](#deployment)
- [Project Structure](#project-structure)

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         Main Event Loop                         │
│                                                                 │
│  ┌────────────────────────────────────────────────────────────┐ │
│  │                    PRIMARY PATH (~50ms)                     │ │
│  │                                                            │ │
│  │  Hyperliquid WS ──▶ Leader Fill ──▶ FillCopier ──▶ Execute │ │
│  │  (userEvents)       detected        scale & go     paper/  │ │
│  │                                                    live    │ │
│  └────────────────────────────────────────────────────────────┘ │
│                                                                 │
│  ┌────────────────────────────────────────────────────────────┐ │
│  │                   BACKUP PATH (every 30s)                  │ │
│  │                                                            │ │
│  │  REST Poller ──▶ Decision Engine ──▶ Risk Controller ──▶   │ │
│  │  fetch state     compute deltas     check limits       Exe │ │
│  │                                                            │ │
│  └────────────────────────────────────────────────────────────┘ │
│                                                                 │
│  ┌──────────────┐   ┌──────────────┐   ┌──────────────────┐   │
│  │ Discord Bot  │◀──│ BotController│──▶│ All Components   │   │
│  │ /commands    │   │ (bridge)     │   │ (pause, kill..)  │   │
│  └──────────────┘   └──────────────┘   └──────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

### Two Execution Paths

| | Primary (FillCopier) | Backup (Reconciliation Loop) |
|---|---|---|
| **Trigger** | WebSocket fill event from leader | 30-second timer |
| **Latency** | ~50ms (no REST call needed) | ~500ms (fetches state via REST) |
| **Mechanism** | Scales the leader's fill directly | Compares full position state, computes delta |
| **When it fires** | Every time the leader's order is filled | Catches anything the WebSocket missed |
| **False trades** | Never — only fires on real fills | Protected by 2% drift threshold |

The **primary path** handles 99% of trades. The **backup path** is a safety net for edge cases like WebSocket disconnections.

---

## How Fill Copying Works

When the leader places a trade and it fills, Hyperliquid pushes a WebSocket event containing:

```json
{
  "coin": "BTC",
  "side": "Buy",
  "sz": "0.5",
  "px": "87431.2"
}
```

The bot processes this in 4 steps:

### Step 1: Receive Fill (WebSocket Listener)
The `WebSocketListener` subscribes to the leader's `userEvents` channel. When a fill arrives, it's parsed into a `LeaderFill` object and passed directly to the `FillCopier`.

```python
fill = LeaderFill(coin="BTC", side="Buy", size=0.5, price=87431.2)
await fill_copier.copy_fill(fill)
```

### Step 2: Scale the Size (FillCopier)
The fill size is scaled proportionally based on equity ratio:

```
scale_factor = (follower_equity / leader_equity) × multiplier
our_size     = floor(leader_fill_size × scale_factor, szDecimals)
```

Example with $10,000 follower vs $300,000 leader at 1× multiplier:
```
scale_factor = ($10,000 / $300,000) × 1.0 = 0.0333
our_size     = floor(0.5 × 0.0333, 5) = 0.01666 → 0.01666 BTC
```

### Step 3: Validate
Before executing, the bot checks:
- Is the bot paused? (via Discord `/pause`)
- Is the coin whitelisted?
- Is the scaled size above the minimum order size?
- Is the notional value above Hyperliquid's $10 USD minimum?

### Step 4: Execute
- **Paper mode**: The `PaperExecutionEngine` simulates the fill at the leader's price and updates virtual positions in memory.
- **Live mode**: The `ExecutionEngine` submits an IOC (Immediate-or-Cancel) order to Hyperliquid via the SDK at an aggressive price (mid ± slippage tolerance).

A Discord notification is sent with the leader's fill details and your copied fill.

### Deduplication
The WebSocket listener hashes every event and maintains a dedup cache of 5,000 events. If the same fill arrives twice (which can happen with reconnections), it's silently skipped.

---

## Position Sizing & Scaling

### Core Formula

The bot scales ALL sizes by the equity ratio between follower and leader:

```
scale_factor = (follower_equity / leader_equity) × multiplier
```

| Your Equity | Leader Equity | Multiplier | Scale Factor | Leader buys 1 BTC → You buy |
|------------|--------------|-----------|-------------|----------------------------|
| $10,000 | $300,000 | 1.0× | 0.033 | 0.033 BTC ($2,880) |
| $10,000 | $300,000 | 0.5× | 0.017 | 0.017 BTC ($1,480) |
| $50,000 | $300,000 | 1.0× | 0.167 | 0.167 BTC ($14,530) |
| $10,000 | $10,000 | 1.0× | 1.000 | 1.000 BTC ($87,000) |

### Floor Truncation

All sizes are **truncated downward** to the asset's `szDecimals` (precision defined by Hyperliquid). This ensures you never accidentally exceed your balance:

```
raw_size = 0.016667 BTC
szDecimals = 5 for BTC
truncated = 0.01666 BTC  (NOT rounded to 0.01667)
```

### Equity Refresh

The equity values used for scaling are refreshed every 30 seconds by the reconciliation loop. This means if the leader gains/loses a lot of equity between refreshes, the scale factor might be slightly stale — but this is negligible in practice.

### Startup Behavior

When the bot starts:
1. It fetches the leader's current positions
2. Pre-seeds the paper trader with scaled versions of those positions as a **baseline**
3. Only copies **new changes** from that point forward

> If the leader already has 5 open positions when you start the bot, those 5 positions are NOT copied. Only new fills are.

---

## Risk Management

### Kill Switch
Automatically closes ALL positions if session losses exceed thresholds:

```yaml
kill_switch:
  loss_usd: -5000.0    # Absolute: close everything at -$5,000
  loss_pct: -0.10       # Relative: close everything at -10% of starting equity
```

Once triggered, the kill switch latches — trading will NOT resume until you manually run `/reset` in Discord.

### Position Cap
Per-symbol notional limit. If a leader opens a massive position, your order is capped:

```yaml
max_position_usd: 50000.0   # Your BTC position will never exceed $50k notional
```

If the cap would be exceeded, the order is automatically resized downward. If you're already at the cap, the order is rejected entirely.

### Total Exposure Cap
Sum of all |notional| across all positions cannot exceed this:

```yaml
max_total_exposure_usd: 200000.0
```

### Leverage Cap
Even if the leader uses 50× leverage, your position will be capped at:

```yaml
max_leverage: 20
```

### Consecutive Failure Breaker
After N consecutive failed order executions, the bot pauses to prevent runaway errors:

```yaml
max_consecutive_failures: 5
```

### Symbol Whitelist
Restrict which coins the bot will copy. Set to `"ALL"` to copy everything, or specify a list:

```yaml
symbol_whitelist: ["BTC", "ETH", "SOL"]   # Only copy these
# symbol_whitelist: "ALL"                  # Copy everything
```

### Direction Flip Safety
When the leader flips from long → short (or vice versa), the bot splits this into two separate orders:
1. **Close** the existing position (reduce-only)
2. **Open** the new direction

This prevents a single order from crossing zero and triggering unexpected margin behavior.

---

## Discord Control Interface

The bot runs a full slash command interface inside Discord. Type `/` in any channel to see the command dropdown.

### Setup

1. Go to [discord.com/developers/applications](https://discord.com/developers/applications)
2. Create **New Application** → name it anything (e.g. "HL CopyBot")
3. **Bot** tab → **Reset Token** → copy it → add to your `.env` file
4. Enable **Message Content Intent** under Privileged Gateway Intents
5. **OAuth2 → URL Generator** → scope: `bot` → permissions: `Send Messages`, `Read Messages`, `Embed Links`
6. Open the generated invite URL → add the bot to your Discord server

### Commands

#### Monitoring
| Command | Description |
|---------|-------------|
| `/status` | Uptime, mode (paper/live), WebSocket connection state, trade count |
| `/balance` | Leader and follower equity with position counts |
| `/positions` | Side-by-side view of all open positions (leader vs follower) |
| `/pnl` | Session PnL in USD and percentage, starting vs current equity |
| `/trades [count]` | Last N trade history with coin, side, size, price, status |

#### Control
| Command | Description |
|---------|-------------|
| `/pause` | Stops all trading immediately. WebSocket monitoring continues so no fills are missed — they're just not copied. |
| `/resume` | Resumes trading after a pause. |
| `/kill` | Emergency: initiates closure of ALL positions. Requires `/confirm_kill` within 30 seconds. |
| `/confirm_kill` | Confirms the kill switch. All positions are closed and trading halts. |
| `/reset` | Resets the kill switch after manual review, allowing trading to resume. |

#### Configuration
| Command | Description |
|---------|-------------|
| `/config` | Shows current settings: mode, multiplier, position caps, thresholds |
| `/set multiplier 0.5` | Change the scaling multiplier live (no restart needed) |
| `/set max_position 25000` | Change the per-symbol position cap live |
| `/help` | Shows all available commands with descriptions |

### Authorization
Only Discord user IDs listed in `settings.yaml` can execute commands:

```yaml
discord:
  authorized_user_ids:
    - 811339626496131119   # Your Discord user ID
```

To find your ID: Discord Settings → Advanced → Enable Developer Mode → Right-click your name → Copy User ID.

### Notifications
The bot sends real-time Discord webhook notifications for:
- ✅ **Startup** — mode, number of pairs
- 📝 **Paper trades** / 💰 **Live trades** — leader fill details, your copied fill, scale factor
- 🚨 **Kill switch** — reason, session PnL, equity
- ⚠️ **Errors** — execution failures, consecutive failures

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
nano .env   # Fill in your values
```

```env
# Required
DISCORD_BOT_TOKEN=your_discord_bot_token
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
HL_PAIR_0_FOLLOWER_ADDRESS=0xyour_wallet_address

# Only for live mode
# HL_PAIR_0_AGENT_PRIVATE_KEY=your_agent_wallet_key
```

The `.env` file is loaded automatically on startup — no manual `export` needed. It is gitignored and never committed.

### 3. Configure Settings

Edit `copybot/config/settings.yaml` to set the leader address, scaling parameters, and risk limits. See [Configuration Reference](#configuration-reference) below.

### 4. Run

```bash
# Paper mode (simulated — no real orders)
python -m copybot.main --mode paper

# Live mode (real orders — requires agent wallet key)
python -m copybot.main --mode live
```

> ⚠️ **Paper trade for at least 72 hours** before going live. Watch the Discord notifications to verify the bot is copying correctly.

---

## Configuration Reference

All settings live in `copybot/config/settings.yaml`.

### Pairs

```yaml
pairs:
  - name: "leader_alpha"                              # Identifier for logs and Discord
    leader_address: "0x..."                           # The wallet you're copying
    follower_address: ""                              # Your wallet (or set via env var)
    agent_private_key_env: "HL_PAIR_0_AGENT_PRIVATE_KEY"  # Env var name for agent key
```

The bot supports multiple pairs running concurrently — add more entries to the list.

### Scaling

```yaml
scaling:
  multiplier: 1.0              # 1.0 = proportional to equity ratio. 0.5 = half.
  min_order_notional: 11.0     # USD — Hyperliquid minimum is ~$10, we use $11 for safety
  drift_threshold_pct: 0.02    # 2% — reconciliation ignores drifts smaller than this
  paper_equity: 10000.0        # Starting equity for paper mode simulation ($)
```

### Risk Controls

```yaml
risk:
  symbol_whitelist: "ALL"         # "ALL" or list like ["BTC", "ETH", "SOL"]
  max_position_usd: 50000.0      # Per-symbol cap
  max_total_exposure_usd: 200000  # Portfolio-wide cap
  max_leverage: 20                # Leverage ceiling
  slippage_tolerance_bps: 50      # 0.5% — aggressive price offset for IOC orders
  max_consecutive_failures: 5     # Pause after this many failures in a row
  max_open_orders: 20             # Max concurrent open orders
  kill_switch:
    loss_usd: -5000.0            # Absolute loss limit
    loss_pct: -0.10              # Percentage loss limit (of starting equity)
```

### Polling & WebSocket

```yaml
polling:
  reconciliation_interval_s: 30   # Backup reconciliation frequency
  equity_refresh_interval_s: 10   # How often to refresh equity values
  metadata_refresh_interval_s: 300 # Asset metadata refresh (szDecimals, etc.)

websocket:
  reconnect_delay_s: 1            # Initial reconnect delay
  max_reconnect_delay_s: 60       # Max delay with exponential backoff
  heartbeat_interval_s: 15        # WebSocket ping interval
```

### Discord

```yaml
discord:
  bot_token_env: "DISCORD_BOT_TOKEN"     # Env var name for the bot token
  command_channel: ""                      # Restrict commands to this channel (empty = any)
  authorized_user_ids:
    - 811339626496131119                   # Only these users can run commands
```

### Operating Mode

```yaml
mode: "paper"       # "paper" (simulated) or "live" (real orders)
network: "mainnet"  # "mainnet" or "testnet"
log_level: "INFO"   # DEBUG, INFO, WARNING, ERROR, CRITICAL
```

---

## Deployment

### Option 1: tmux (Simple)

```bash
ssh -i ~/.ssh/hyper-key.pem ubuntu@your-ec2-ip

cd ~/hyperliquid_bot
source venv/bin/activate
tmux new -s copybot
python -m copybot.main --mode paper

# Detach: Ctrl+B, then D
# Reattach later: tmux attach -t copybot
```

### Option 2: systemd (Auto-restart, Survives Reboot)

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
sudo systemctl enable hl-copybot    # Start on boot
sudo systemctl start hl-copybot     # Start now
sudo journalctl -u hl-copybot -f    # Tail logs
sudo systemctl restart hl-copybot   # Restart after config change
```

### Updating the Bot

```bash
cd ~/hyperliquid_bot
git pull
pip install -r requirements.txt
sudo systemctl restart hl-copybot   # or: Ctrl+C and restart in tmux
```

---

## Project Structure

```
hyperliquid_bot/
├── copybot/
│   ├── config/
│   │   ├── loader.py              # YAML + env var parsing into typed dataclasses
│   │   └── settings.yaml          # All bot configuration
│   │
│   ├── engine/
│   │   ├── fill_copier.py         # PRIMARY: scales and copies leader fills directly
│   │   ├── decision.py            # BACKUP: computes position deltas for reconciliation
│   │   ├── execution.py           # Live order execution via Hyperliquid SDK (IOC orders)
│   │   ├── paper_trader.py        # Simulated execution with virtual positions
│   │   ├── reconciliation.py      # 30s backup loop + equity refresh for FillCopier
│   │   └── risk.py                # Kill switch, position caps, exposure limits
│   │
│   ├── ingestion/
│   │   ├── ws_listener.py         # WebSocket: leader fill detection + direct callback
│   │   └── rest_poller.py         # REST: state fetching + mid price queries
│   │
│   ├── state/
│   │   ├── models.py              # Core types: LeaderFill, PositionInfo, OrderIntent, etc.
│   │   ├── metadata.py            # Asset metadata cache (szDecimals, min sizes)
│   │   └── store.py               # SQLite persistence (WAL mode) for state + order log
│   │
│   ├── utils/
│   │   ├── alerting.py            # Discord webhook notifications (embeds)
│   │   ├── logging.py             # Structured JSON logging via structlog
│   │   └── math.py                # Floor truncation, price rounding, equity scaling
│   │
│   ├── controller.py              # Bridges Discord commands ↔ trading components
│   ├── discord_bot.py             # Slash command interface (/status, /kill, etc.)
│   └── main.py                    # Entry point: wires everything, runs async loop
│
├── tests/
│   └── unit/                      # 73 unit tests (scaling, math, decision engine)
│
├── .env.example                   # Template for secrets
├── requirements.txt               # Python dependencies
├── pyproject.toml                 # Project metadata
└── Makefile                       # Dev shortcuts (test, lint)
```

### Data Flow

```
settings.yaml + .env
        │
        ▼
    loader.py ──▶ BotConfig (typed dataclasses)
        │
        ▼
    main.py (wires all components)
        │
   ┌────┴─────────────────────────────────┐
   │                                      │
   ▼                                      ▼
WebSocketListener                   ReconciliationLoop
   │  (subscribes to leader)             │  (every 30s)
   │                                      │
   │  on fill event:                      │  fetches leader + follower state
   ▼                                      ▼
FillCopier                          DecisionEngine
   │  scale by equity ratio              │  compare positions, compute deltas
   │                                      │
   ▼                                      ▼
PaperExecution / LiveExecution      RiskController
   │                                      │  check caps, kill switch
   │                                      ▼
   │                                PaperExecution / LiveExecution
   │                                      │
   ▼                                      ▼
StateStore (SQLite)                 StateStore (SQLite)
   │                                      │
   ▼                                      ▼
DiscordAlerter (webhook)            Update FillCopier equities
```

---

## Safety Checklist

Before going live, verify:

- [ ] Paper traded for 72+ hours with no unexpected behavior
- [ ] Kill switch tested with `/kill` → `/confirm_kill` → `/reset`
- [ ] `/pause` and `/resume` verified to stop/start trading
- [ ] Discord notifications arriving for every trade
- [ ] `multiplier` set appropriately for your account size
- [ ] `max_position_usd` and `max_total_exposure_usd` set conservatively
- [ ] `symbol_whitelist` configured (start with a small list, not `"ALL"`)
- [ ] Agent wallet has only the minimum necessary permissions
- [ ] `.env` file permissions restricted (`chmod 600 .env`)

---

## License

MIT
