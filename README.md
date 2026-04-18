# Hyperliquid Copy Trading Bot

Production-ready copy trading bot for [Hyperliquid](https://hyperliquid.xyz). Monitors a leader wallet and replicates positions on your follower account using scaled position sizing.

## Architecture

```
WebSocket (leader fills) ──→ Decision Engine ──→ Risk Controller ──→ Execution Engine
REST Poller (state snapshots) ──↗    ↑                                      │
                                     │                                      ↓
                              Reconciliation Loop ←──── State Store (SQLite)
```

**Core principle**: Copies **positions**, not individual orders. Computes delta between leader and follower state, then issues the minimum set of orders to converge.

## Quick Start

### 1. Install

```bash
python -m venv venv
source venv/bin/activate
pip install -e ".[dev]"
```

### 2. Configure

Edit `copybot/config/settings.yaml`:
- Set your leader address
- Configure risk limits, scaling multiplier, symbol whitelist

Set environment variables:
```bash
export HL_PAIR_0_FOLLOWER_ADDRESS="0xyour_follower_wallet"
export HL_PAIR_0_AGENT_PRIVATE_KEY="your_agent_private_key"
export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
```

### 3. Paper Trading (recommended first)

```bash
python -m copybot.main --mode paper
```

### 4. Live Trading

```bash
python -m copybot.main --mode live
```

## Configuration

All settings are in `copybot/config/settings.yaml`. Secrets are loaded from environment variables — **never hardcode private keys**.

| Setting | Default | Description |
|---------|---------|-------------|
| `scaling.multiplier` | 1.0 | Additional scaling on top of equity ratio |
| `risk.max_leverage` | 20 | Maximum leverage (even if leader uses more) |
| `risk.max_position_usd` | 50,000 | Per-symbol notional cap |
| `risk.max_total_exposure_usd` | 200,000 | Total exposure limit |
| `risk.kill_switch.loss_usd` | -5,000 | Absolute loss to trigger emergency stop |
| `risk.kill_switch.loss_pct` | -10% | Percentage loss to trigger emergency stop |
| `polling.reconciliation_interval_s` | 30 | Drift correction frequency |

## Scaling Formula

```
target_size = leader_size × (follower_equity / leader_equity) × multiplier
```

Sizes are floor-truncated to the asset's `szDecimals` to avoid balance overflows.

## Testing

```bash
# All tests
make test

# Unit tests only
make test-unit

# With coverage
pytest tests/ -v --cov=copybot --cov-report=term-missing
```

## Deployment (AWS Tokyo)

```bash
# 1. Provision EC2
bash scripts/setup_ec2.sh

# 2. Deploy code
scp -r . copybot@your-ec2:/opt/hl-copybot/

# 3. Start
sudo systemctl start hl-copybot

# 4. Monitor
journalctl -u hl-copybot -f
```

## Project Structure

```
copybot/
├── config/          # YAML config + loader
├── ingestion/       # WebSocket listener + REST poller
├── state/           # Models, state store, metadata cache
├── engine/          # Decision, risk, execution, reconciliation
└── utils/           # Math, logging, Discord alerting
tests/
├── unit/            # Scaling, decision, risk tests
├── integration/     # Full scenario simulations
└── chaos/           # Failure mode tests
```

## Safety

- **Kill switch**: Auto-closes all positions on session loss threshold
- **Symbol whitelist**: Only trades specified assets
- **Slippage protection**: IOC orders with price limits
- **Reduce-only bypass**: Can always close positions regardless of limits
- **Paper trading**: Full simulation before risking real funds
