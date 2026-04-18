"""Unit tests for the decision engine — delta computation and order planning."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from copybot.config.loader import BotConfig, RiskConfig, ScalingConfig
from copybot.engine.decision import DecisionEngine
from copybot.state.metadata import MetadataCache
from copybot.state.models import (
    AccountState,
    AssetMeta,
    LeverageInfo,
    OrderIntent,
    PositionInfo,
)


def _make_config(**overrides) -> BotConfig:
    """Create a BotConfig with test defaults."""
    defaults = dict(
        scaling=ScalingConfig(
            multiplier=Decimal("1.0"),
            min_order_notional=Decimal("11.0"),
            drift_threshold_pct=Decimal("0.02"),
        ),
        risk=RiskConfig(symbol_whitelist="ALL"),
    )
    defaults.update(overrides)
    return BotConfig(**defaults)


def _make_metadata(coins: dict[str, int] | None = None) -> MetadataCache:
    """Create a mock metadata cache."""
    meta = MetadataCache.__new__(MetadataCache)
    meta._assets = {}
    meta._last_refresh = 999999999.0
    meta.refresh_interval_s = 300

    if coins is None:
        coins = {"BTC": 5, "ETH": 4, "SOL": 2}

    for i, (name, sz_dec) in enumerate(coins.items()):
        meta._assets[name] = AssetMeta(
            name=name, sz_decimals=sz_dec, asset_index=i
        )
    return meta


def _make_state(address: str, equity: str, positions: dict[str, str] | None = None) -> AccountState:
    """Create a test AccountState."""
    pos_dict = {}
    if positions:
        for coin, szi in positions.items():
            pos_dict[coin] = PositionInfo(
                coin=coin,
                szi=Decimal(szi),
                entry_px=Decimal("50000"),
                leverage=LeverageInfo("cross", 10),
                unrealized_pnl=Decimal("0"),
            )

    return AccountState(
        address=address,
        positions=pos_dict,
        account_value=Decimal(equity),
        timestamp=1000.0,
    )


class TestDecisionEngine:
    """Tests for the decision engine's compute_intents method."""

    def test_leader_opens_long(self):
        """Leader opens BTC long, follower should open proportional long."""
        config = _make_config()
        metadata = _make_metadata()
        engine = DecisionEngine(config, metadata)

        leader = _make_state("0xleader", "100000", {"BTC": "1.0"})
        follower = _make_state("0xfollower", "10000")

        intents = engine.compute_intents(leader, follower, force=True)

        assert len(intents) == 1
        assert intents[0].coin == "BTC"
        assert intents[0].is_buy is True
        assert intents[0].delta == Decimal("0.10000")  # 1.0 * (10k/100k)
        assert intents[0].is_reduce_only is False

    def test_leader_opens_short(self):
        """Leader opens short → follower should sell."""
        config = _make_config()
        metadata = _make_metadata()
        engine = DecisionEngine(config, metadata)

        leader = _make_state("0xleader", "100000", {"ETH": "-5.0"})
        follower = _make_state("0xfollower", "10000")

        intents = engine.compute_intents(leader, follower, force=True)

        assert len(intents) == 1
        assert intents[0].coin == "ETH"
        assert intents[0].is_buy is False
        assert intents[0].delta == Decimal("-0.5000")
        assert intents[0].is_reduce_only is False

    def test_leader_closes_position(self):
        """Leader closes position → follower should close (reduce-only)."""
        config = _make_config()
        metadata = _make_metadata()
        engine = DecisionEngine(config, metadata)

        leader = _make_state("0xleader", "100000")  # No positions
        follower = _make_state("0xfollower", "10000", {"BTC": "0.10000"})

        intents = engine.compute_intents(leader, follower, force=True)

        assert len(intents) == 1
        assert intents[0].coin == "BTC"
        assert intents[0].delta == Decimal("-0.10000")
        assert intents[0].is_reduce_only is True

    def test_leader_increases_position(self):
        """Leader doubles BTC position → follower adds."""
        config = _make_config()
        metadata = _make_metadata()
        engine = DecisionEngine(config, metadata)

        leader = _make_state("0xleader", "100000", {"BTC": "2.0"})
        follower = _make_state("0xfollower", "10000", {"BTC": "0.10000"})

        intents = engine.compute_intents(leader, follower, force=True)

        assert len(intents) == 1
        assert intents[0].delta == Decimal("0.10000")  # Need 0.2 total, have 0.1
        assert intents[0].is_reduce_only is False

    def test_leader_reduces_position(self):
        """Leader reduces position → follower should reduce (reduce-only)."""
        config = _make_config()
        metadata = _make_metadata()
        engine = DecisionEngine(config, metadata)

        leader = _make_state("0xleader", "100000", {"BTC": "0.5"})
        follower = _make_state("0xfollower", "10000", {"BTC": "0.10000"})

        intents = engine.compute_intents(leader, follower, force=True)

        assert len(intents) == 1
        assert intents[0].delta == Decimal("-0.05000")  # Target 0.05, have 0.1
        assert intents[0].is_reduce_only is True

    def test_direction_flip_long_to_short(self):
        """Leader flips from long to short → two orders: close + reverse."""
        config = _make_config()
        metadata = _make_metadata()
        engine = DecisionEngine(config, metadata)

        leader = _make_state("0xleader", "100000", {"ETH": "-5.0"})
        follower = _make_state("0xfollower", "10000", {"ETH": "0.5000"})

        intents = engine.compute_intents(leader, follower, force=True)

        assert len(intents) == 2

        # First: close existing long (reduce-only)
        assert intents[0].delta == Decimal("-0.5000")
        assert intents[0].is_reduce_only is True
        assert intents[0].is_buy is False

        # Second: open short
        assert intents[1].delta == Decimal("-0.5000")
        assert intents[1].is_reduce_only is False
        assert intents[1].is_buy is False

    def test_no_change_needed(self):
        """No delta → no intents."""
        config = _make_config()
        metadata = _make_metadata()
        engine = DecisionEngine(config, metadata)

        leader = _make_state("0xleader", "100000", {"BTC": "1.0"})
        follower = _make_state("0xfollower", "10000", {"BTC": "0.10000"})

        intents = engine.compute_intents(leader, follower, force=True)
        assert len(intents) == 0

    def test_zero_leader_equity_skips(self):
        """Zero/negative leader equity → no intents."""
        config = _make_config()
        metadata = _make_metadata()
        engine = DecisionEngine(config, metadata)

        leader = _make_state("0xleader", "0", {"BTC": "1.0"})
        follower = _make_state("0xfollower", "10000")

        intents = engine.compute_intents(leader, follower, force=True)
        assert len(intents) == 0

    def test_whitelist_filters(self):
        """Non-whitelisted coins are skipped."""
        config = _make_config(
            risk=RiskConfig(symbol_whitelist=["BTC", "ETH"])
        )
        metadata = _make_metadata()
        engine = DecisionEngine(config, metadata)

        leader = _make_state("0xleader", "100000", {"SOL": "100"})
        follower = _make_state("0xfollower", "10000")

        intents = engine.compute_intents(leader, follower, force=True)
        assert len(intents) == 0

    def test_drift_threshold_skips_small_changes(self):
        """Small drifts below threshold are skipped in normal (non-force) mode."""
        config = _make_config(
            scaling=ScalingConfig(drift_threshold_pct=Decimal("0.05"))
        )
        metadata = _make_metadata()
        engine = DecisionEngine(config, metadata)

        # 1% drift: target=0.10 have=0.099 → drift=0.001/0.1=1% < 5% threshold
        leader = _make_state("0xleader", "100000", {"BTC": "1.0"})
        follower = _make_state("0xfollower", "10000", {"BTC": "0.09900"})

        intents = engine.compute_intents(leader, follower, force=False)
        assert len(intents) == 0

    def test_drift_threshold_overridden_by_force(self):
        """Force mode ignores drift threshold."""
        config = _make_config(
            scaling=ScalingConfig(drift_threshold_pct=Decimal("0.05"))
        )
        metadata = _make_metadata()
        engine = DecisionEngine(config, metadata)

        leader = _make_state("0xleader", "100000", {"BTC": "1.0"})
        follower = _make_state("0xfollower", "10000", {"BTC": "0.09900"})

        intents = engine.compute_intents(leader, follower, force=True)
        assert len(intents) == 1

    def test_multiple_coins(self):
        """Engine handles multiple coins simultaneously."""
        config = _make_config()
        metadata = _make_metadata()
        engine = DecisionEngine(config, metadata)

        leader = _make_state("0xleader", "100000", {"BTC": "1.0", "ETH": "10.0"})
        follower = _make_state("0xfollower", "10000")

        intents = engine.compute_intents(leader, follower, force=True)
        coins = {i.coin for i in intents}
        assert coins == {"BTC", "ETH"}

    def test_orphaned_follower_position(self):
        """Follower has position that leader doesn't → should close it."""
        config = _make_config()
        metadata = _make_metadata()
        engine = DecisionEngine(config, metadata)

        leader = _make_state("0xleader", "100000")  # No positions
        follower = _make_state("0xfollower", "10000", {"BTC": "0.05000"})

        intents = engine.compute_intents(leader, follower, force=True)

        assert len(intents) == 1
        assert intents[0].coin == "BTC"
        assert intents[0].is_reduce_only is True
        assert intents[0].delta == Decimal("-0.05000")
