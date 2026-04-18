"""Unit tests for the risk controller."""

from __future__ import annotations

from decimal import Decimal

import pytest

from copybot.config.loader import BotConfig, KillSwitchConfig, RiskConfig, ScalingConfig
from copybot.engine.risk import KillSwitch, RiskController
from copybot.state.models import (
    AccountState,
    LeverageInfo,
    OrderIntent,
    PositionInfo,
)


def _make_config(**overrides) -> BotConfig:
    defaults = dict(
        risk=RiskConfig(
            symbol_whitelist=["BTC", "ETH", "SOL"],
            max_position_usd=Decimal("50000"),
            max_total_exposure_usd=Decimal("200000"),
            max_leverage=20,
            slippage_tolerance_bps=50,
            max_consecutive_failures=3,
            kill_switch=KillSwitchConfig(
                loss_usd=Decimal("-5000"),
                loss_pct=Decimal("-0.10"),
            ),
        ),
    )
    defaults.update(overrides)
    return BotConfig(**defaults)


def _make_follower(positions: dict[str, str] | None = None, equity: str = "10000") -> AccountState:
    pos_dict = {}
    if positions:
        for coin, szi in positions.items():
            pos_dict[coin] = PositionInfo(
                coin=coin,
                szi=Decimal(szi),
                entry_px=Decimal("50000") if coin == "BTC" else Decimal("3000"),
                leverage=LeverageInfo("cross", 10),
                unrealized_pnl=Decimal("0"),
            )
    return AccountState(address="0xfollower", positions=pos_dict, account_value=Decimal(equity))


class TestKillSwitch:
    def test_inactive_by_default(self):
        ks = KillSwitch(Decimal("-5000"), Decimal("-0.10"))
        assert ks.active is False
        assert ks.check(Decimal("0"), Decimal("10000")) is False

    def test_triggers_on_absolute_loss(self):
        ks = KillSwitch(Decimal("-5000"), Decimal("-0.10"))
        assert ks.check(Decimal("-5001"), Decimal("100000")) is True
        assert ks.active is True

    def test_triggers_on_percentage_loss(self):
        ks = KillSwitch(Decimal("-50000"), Decimal("-0.10"))
        # -11% loss on 10000 starting equity
        assert ks.check(Decimal("-1100"), Decimal("10000")) is True
        assert ks.active is True

    def test_does_not_trigger_within_limits(self):
        ks = KillSwitch(Decimal("-5000"), Decimal("-0.10"))
        assert ks.check(Decimal("-4999"), Decimal("100000")) is False
        assert ks.active is False

    def test_stays_active_once_triggered(self):
        ks = KillSwitch(Decimal("-5000"), Decimal("-0.10"))
        ks.check(Decimal("-6000"), Decimal("100000"))
        # Even if PnL recovers, stays active
        assert ks.check(Decimal("0"), Decimal("100000")) is True

    def test_reset(self):
        ks = KillSwitch(Decimal("-5000"), Decimal("-0.10"))
        ks.activate("test")
        ks.reset()
        assert ks.active is False

    def test_zero_starting_equity_skips_pct_check(self):
        ks = KillSwitch(Decimal("-5000"), Decimal("-0.10"))
        # Zero equity should not cause division error
        assert ks.check(Decimal("-100"), Decimal("0")) is False


class TestRiskController:
    @pytest.mark.asyncio
    async def test_approve_normal_order(self):
        config = _make_config()
        rc = RiskController(config)

        intent = OrderIntent(
            coin="BTC", delta=Decimal("0.1"), is_buy=True,
            is_reduce_only=False, target_size=Decimal("0.1")
        )
        follower = _make_follower()
        mid_prices = {"BTC": Decimal("50000")}

        decision = await rc.check(intent, follower, Decimal("0"), Decimal("10000"), mid_prices)
        assert decision.approved is True

    @pytest.mark.asyncio
    async def test_reject_kill_switch_active(self):
        config = _make_config()
        rc = RiskController(config)
        rc.kill_switch.activate("test")

        intent = OrderIntent(
            coin="BTC", delta=Decimal("0.1"), is_buy=True,
            is_reduce_only=False, target_size=Decimal("0.1")
        )
        follower = _make_follower()

        decision = await rc.check(intent, follower, Decimal("0"), Decimal("10000"))
        assert decision.approved is False

    @pytest.mark.asyncio
    async def test_reduce_only_bypasses_kill_switch_check_but_not_kill_switch(self):
        """Kill switch rejects ALL orders including reduce-only."""
        config = _make_config()
        rc = RiskController(config)
        rc.kill_switch.activate("test")

        intent = OrderIntent(
            coin="BTC", delta=Decimal("-0.1"), is_buy=False,
            is_reduce_only=True, target_size=Decimal("0")
        )
        follower = _make_follower()

        decision = await rc.check(intent, follower, Decimal("0"), Decimal("10000"))
        # Kill switch blocks everything
        assert decision.approved is False

    @pytest.mark.asyncio
    async def test_reduce_only_bypasses_whitelist(self):
        """Reduce-only orders bypass most limits (except kill switch)."""
        config = _make_config()
        rc = RiskController(config)

        # DOGE not in whitelist, but reduce-only should pass
        intent = OrderIntent(
            coin="DOGE", delta=Decimal("100"), is_buy=True,
            is_reduce_only=True, target_size=Decimal("0")
        )
        follower = _make_follower()

        decision = await rc.check(intent, follower, Decimal("0"), Decimal("10000"))
        assert decision.approved is True

    @pytest.mark.asyncio
    async def test_reject_non_whitelisted_coin(self):
        config = _make_config()
        rc = RiskController(config)

        intent = OrderIntent(
            coin="DOGE", delta=Decimal("100"), is_buy=True,
            is_reduce_only=False, target_size=Decimal("100")
        )
        follower = _make_follower()

        decision = await rc.check(intent, follower, Decimal("0"), Decimal("10000"))
        assert decision.approved is False

    @pytest.mark.asyncio
    async def test_cap_max_position_size(self):
        """Position exceeding max_position_usd gets capped."""
        config = _make_config()
        rc = RiskController(config)

        # Target notional = 2.0 BTC * 50000 = 100k > 50k limit
        intent = OrderIntent(
            coin="BTC", delta=Decimal("2.0"), is_buy=True,
            is_reduce_only=False, target_size=Decimal("2.0")
        )
        follower = _make_follower()
        mid_prices = {"BTC": Decimal("50000")}

        decision = await rc.check(intent, follower, Decimal("0"), Decimal("10000"), mid_prices)
        assert decision.approved is True
        assert decision.modified_intent is not None
        # Capped to 50000/50000 = 1.0 BTC
        assert decision.modified_intent.target_size == Decimal("1")

    @pytest.mark.asyncio
    async def test_reject_total_exposure_exceeded(self):
        """Total exposure limit blocks new positions."""
        config = _make_config(
            risk=RiskConfig(
                symbol_whitelist="ALL",
                max_position_usd=Decimal("999999"),
                max_total_exposure_usd=Decimal("100000"),
                max_consecutive_failures=10,
                kill_switch=KillSwitchConfig(loss_usd=Decimal("-99999"), loss_pct=Decimal("-0.99")),
            ),
        )
        rc = RiskController(config)

        # Existing exposure: 1 BTC * 50000 = 50k
        follower = _make_follower({"BTC": "1.0"}, equity="100000")
        mid_prices = {"BTC": Decimal("50000"), "ETH": Decimal("3000")}

        # New order: 20 ETH * 3000 = 60k → total would be 110k > 100k
        intent = OrderIntent(
            coin="ETH", delta=Decimal("20"), is_buy=True,
            is_reduce_only=False, target_size=Decimal("20")
        )

        decision = await rc.check(intent, follower, Decimal("0"), Decimal("100000"), mid_prices)
        assert decision.approved is False

    @pytest.mark.asyncio
    async def test_consecutive_failures_blocks(self):
        config = _make_config()
        rc = RiskController(config)

        # Record 3 failures (max_consecutive_failures=3)
        for _ in range(3):
            rc.record_failure()

        intent = OrderIntent(
            coin="BTC", delta=Decimal("0.1"), is_buy=True,
            is_reduce_only=False, target_size=Decimal("0.1")
        )
        follower = _make_follower()

        decision = await rc.check(intent, follower, Decimal("0"), Decimal("10000"))
        assert decision.approved is False

    @pytest.mark.asyncio
    async def test_success_resets_failure_count(self):
        config = _make_config()
        rc = RiskController(config)

        rc.record_failure()
        rc.record_failure()
        rc.record_success()  # Reset

        intent = OrderIntent(
            coin="BTC", delta=Decimal("0.1"), is_buy=True,
            is_reduce_only=False, target_size=Decimal("0.1")
        )
        follower = _make_follower()
        mid_prices = {"BTC": Decimal("50000")}

        decision = await rc.check(intent, follower, Decimal("0"), Decimal("10000"), mid_prices)
        assert decision.approved is True
