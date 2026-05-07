"""
Strategy unit tests for agent_v2.py — runs against synthetic snapshot
dicts that exercise each branch of generate_decisions(), the telemetry
layer, and the cycle-deadline parser.

Zero new dependencies — uses stdlib unittest. To run:

    python -m unittest tests.test_strategy

CI integrate with:

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import pathlib
import sys
import unittest
from datetime import datetime, timedelta, timezone

# Make agent_v2.py importable as a module from this tests/ dir.
_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

# Required env so Config.from_env() doesn't sys.exit when the strategy
# module is imported (Config is constructed at runtime, not import time,
# but having the env present prevents accidental traps in helpers).
os.environ.setdefault("ARENA_AGENT_ID", "test-agent")
os.environ.setdefault("ARENA_AGENT_BEARER_TOKEN", "test-bearer")

import agent_v2  # noqa: E402


# ─── Snapshot builder helpers ──────────────────────────────────────────


def _server_block(
    current_cycle: int = 100,
    next_in_sec: float = 60,
) -> dict:
    now = datetime.now(timezone.utc)
    cycle_started = now - timedelta(seconds=max(0.0, 60 - next_in_sec))
    next_cycle_at = now + timedelta(seconds=next_in_sec)
    return {
        "currentCycle": current_cycle,
        "acceptingDecisionsForCycle": current_cycle + 1,
        "cycleStartedAt": cycle_started.isoformat().replace("+00:00", "Z"),
        "cycleAgeMs": int((now - cycle_started).total_seconds() * 1000),
        "nextCycleAt": next_cycle_at.isoformat().replace("+00:00", "Z"),
        "decisionWindowSec": 30,
        "decisionWindowOpen": True,
        "timestamp": now.isoformat().replace("+00:00", "Z"),
    }


def _coin(
    *,
    price: float,
    trend: str = "NEUTRAL",
    volatility: float = 0.5,
    chg1m: float = 0,
    chg5m: float = 0,
    chg15m: float = 0,
    chg30m: float = 0,
    chg1h: float = 0,
    recent_high: float | None = None,
    recent_low: float | None = None,
) -> dict:
    return {
        "price": price,
        "analysis": {
            "currentPrice": price,
            "priceChange1m": chg1m,
            "priceChange5m": chg5m,
            "priceChange15m": chg15m,
            "priceChange30m": chg30m,
            "priceChange1h": chg1h,
            "volatility": volatility,
            "trend": trend,
            "recentHigh": recent_high if recent_high is not None else price * 1.02,
            "recentLow": recent_low if recent_low is not None else price * 0.98,
        },
    }


def _portfolio(
    cash: float = 10000,
    portfolio_value: float = 10000,
    drawdown_pct: float = 0,
    open_trades: dict | None = None,
    peak_value: float | None = None,
) -> dict:
    return {
        "cash": cash,
        "portfolioValue": portfolio_value,
        "unrealizedPnl": portfolio_value - cash,
        "peakValue": peak_value if peak_value is not None else portfolio_value,
        "currentDrawdownPercent": drawdown_pct,
        "openTrades": open_trades or {},
        "portfolioHistory": [],
    }


def _build_snap(
    *,
    coins: dict | None = None,
    portfolio: dict | None = None,
    server: dict | None = None,
    status: str = "ACTIVE",
) -> dict:
    return {
        "agentId": "test-agent",
        "name": "TestBot",
        "tier": "free",
        "status": status,
        "preferredIntervalSec": 60,
        "server": server or _server_block(),
        "rateLimit": {"limit": 120, "used": 1, "remaining": 119},
        "coins": coins or {},
        "portfolio": portfolio or _portfolio(),
        "recentDecisions": [],
    }


def _default_cfg(**overrides) -> agent_v2.Config:
    base = dict(
        base_url="https://test.local",
        agent_id="test-agent",
        bearer_token="x",
        api_key=None,
        interval_sec=60,
        deadline_safety_sec=5,
        narration_budget_sec=6,
        telemetry_every=10,
        histogram_depth=50,
        max_total_exposure_pct=50.0,
        min_volatility_pct=0.4,
        reentry_cooldown_cycles=5,
        drawdown_limit_pct=10.0,
        hermes_base_url=None,
        hermes_model="hermes-agent",
        hermes_api_key=None,
        bot_persona="Test persona.",
    )
    base.update(overrides)
    return agent_v2.Config(**base)


# ─── Tests ─────────────────────────────────────────────────────────────


class StrategyEntryTests(unittest.TestCase):
    def test_strong_up_trend_with_aligned_momentum_opens_long(self) -> None:
        snap = _build_snap(
            coins={
                "BTC": _coin(
                    price=100_000,
                    trend="STRONG_UP",
                    volatility=1.5,
                    chg1m=0.3,
                    chg5m=0.8,
                    chg15m=1.2,
                    chg30m=2.0,
                ),
            },
        )
        decisions = agent_v2.generate_decisions(snap, agent_v2.StrategyState(), _default_cfg())
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0].symbol, "BTC")
        self.assertEqual(decisions[0].action, "LONG")
        self.assertEqual(decisions[0].kind, "entry")
        self.assertEqual(decisions[0].conviction, 4)
        self.assertEqual(decisions[0].position_size_percent, 15.0)

    def test_strong_down_trend_opens_short(self) -> None:
        snap = _build_snap(
            coins={
                "ETH": _coin(
                    price=3000,
                    trend="STRONG_DOWN",
                    volatility=2.0,
                    chg1m=-0.2,
                    chg5m=-0.5,
                ),
            },
        )
        decisions = agent_v2.generate_decisions(snap, agent_v2.StrategyState(), _default_cfg())
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0].action, "SHORT")
        self.assertEqual(decisions[0].position_size_percent, 15.0)

    def test_weak_up_trend_with_low_volatility_opens_smaller_long(self) -> None:
        snap = _build_snap(
            coins={
                "SOL": _coin(
                    price=200,
                    trend="UP",
                    volatility=0.6,
                    chg1m=0.1,
                    chg5m=0.3,
                ),
            },
        )
        decisions = agent_v2.generate_decisions(snap, agent_v2.StrategyState(), _default_cfg())
        self.assertEqual(decisions[0].action, "LONG")
        # UP + volatility < 1.0 → conviction 1 → size 6%
        self.assertEqual(decisions[0].conviction, 1)
        self.assertEqual(decisions[0].position_size_percent, 6.0)

    def test_chop_floor_blocks_entry(self) -> None:
        # volatility below the chop floor (0.4 default) — even a STRONG_UP
        # trend gets filtered out.
        snap = _build_snap(
            coins={
                "BTC": _coin(price=100_000, trend="STRONG_UP", volatility=0.2, chg1m=0.3, chg5m=0.5),
            },
        )
        decisions = agent_v2.generate_decisions(snap, agent_v2.StrategyState(), _default_cfg())
        self.assertEqual(len(decisions), 1)
        # Falls through to no-op heartbeat (single FLAT on alphabetical first symbol).
        self.assertEqual(decisions[0].action, "FLAT")
        self.assertEqual(decisions[0].kind, "no_op")

    def test_unaligned_momentum_blocks_entry(self) -> None:
        # Trend is STRONG_UP but 1m or 5m is negative — momentum check rejects.
        snap = _build_snap(
            coins={
                "BTC": _coin(
                    price=100_000,
                    trend="STRONG_UP",
                    volatility=1.5,
                    chg1m=-0.3,  # ⚠ unaligned
                    chg5m=0.5,
                ),
            },
        )
        decisions = agent_v2.generate_decisions(snap, agent_v2.StrategyState(), _default_cfg())
        self.assertEqual(decisions[0].action, "FLAT")
        self.assertEqual(decisions[0].kind, "no_op")


class StrategyExitTests(unittest.TestCase):
    def test_long_position_with_trend_reversal_exits(self) -> None:
        snap = _build_snap(
            coins={
                "BTC": _coin(
                    price=99_000,
                    trend="STRONG_DOWN",
                    volatility=1.0,
                    chg1m=-0.2,
                    chg5m=-1.0,
                ),
            },
            portfolio=_portfolio(
                open_trades={
                    "BTC": {
                        "symbol": "BTC",
                        "action": "LONG",
                        "entryPrice": 100_000,
                        "quantity": 0.1,
                        "reason": "earlier",
                        "entryTimestamp": "2026-01-01T00:00:00Z",
                    }
                }
            ),
        )
        decisions = agent_v2.generate_decisions(snap, agent_v2.StrategyState(), _default_cfg())
        flats = [d for d in decisions if d.action == "FLAT" and d.kind == "exit"]
        self.assertEqual(len(flats), 1)
        self.assertEqual(flats[0].symbol, "BTC")

    def test_long_position_with_neutral_trend_does_not_exit(self) -> None:
        snap = _build_snap(
            coins={"BTC": _coin(price=100_000, trend="NEUTRAL", volatility=0.5)},
            portfolio=_portfolio(
                open_trades={
                    "BTC": {
                        "symbol": "BTC",
                        "action": "LONG",
                        "entryPrice": 100_000,
                        "quantity": 0.1,
                        "reason": "earlier",
                        "entryTimestamp": "2026-01-01T00:00:00Z",
                    }
                }
            ),
        )
        decisions = agent_v2.generate_decisions(snap, agent_v2.StrategyState(), _default_cfg())
        # Should NOT close the LONG and SHOULD heartbeat instead.
        self.assertNotIn("BTC", [d.symbol for d in decisions if d.action == "FLAT" and d.kind == "exit"])


class StrategyRiskOffTests(unittest.TestCase):
    def test_drawdown_limit_triggers_close_of_worst_loser(self) -> None:
        snap = _build_snap(
            coins={
                "BTC": _coin(price=98_000, trend="UP", volatility=1.0, chg1m=0.1, chg5m=0.2),
                "ETH": _coin(price=2_900, trend="DOWN", volatility=1.0, chg1m=-0.1, chg5m=-0.3),
            },
            portfolio=_portfolio(
                portfolio_value=8_900,
                peak_value=10_000,
                drawdown_pct=11.0,  # over the 10% default limit
                open_trades={
                    "BTC": {
                        "symbol": "BTC",
                        "action": "LONG",
                        "entryPrice": 100_000,
                        "quantity": 0.05,  # -$100 unrealized
                        "reason": "earlier",
                        "entryTimestamp": "2026-01-01T00:00:00Z",
                    },
                    "ETH": {
                        "symbol": "ETH",
                        "action": "LONG",
                        "entryPrice": 3_000,
                        "quantity": 1.0,  # -$100 unrealized
                        "reason": "earlier",
                        "entryTimestamp": "2026-01-01T00:00:00Z",
                    },
                },
            ),
        )
        decisions = agent_v2.generate_decisions(snap, agent_v2.StrategyState(), _default_cfg())
        risk_offs = [d for d in decisions if d.kind == "risk_off"]
        self.assertEqual(len(risk_offs), 1)
        self.assertEqual(risk_offs[0].action, "FLAT")
        # BTC LONG entry 100k, current 98k, qty 0.05 → -$100 vs ETH -$100; tie
        # broken by iteration order. The test just verifies one of the losers
        # was selected, not the specific tiebreaker behavior.
        self.assertIn(risk_offs[0].symbol, {"BTC", "ETH"})


class StrategyExposureTests(unittest.TestCase):
    def test_total_exposure_cap_blocks_third_entry(self) -> None:
        # Two strong setups already filling the budget — a third strong setup
        # should be blocked by the total-exposure cap.
        snap = _build_snap(
            coins={
                "BTC": _coin(price=100_000, trend="STRONG_UP", volatility=2.0, chg1m=0.3, chg5m=0.8),
                "ETH": _coin(price=3000, trend="STRONG_UP", volatility=2.0, chg1m=0.3, chg5m=0.8),
                "SOL": _coin(price=200, trend="STRONG_UP", volatility=2.0, chg1m=0.3, chg5m=0.8),
                "BNB": _coin(price=600, trend="STRONG_UP", volatility=2.0, chg1m=0.3, chg5m=0.8),
            },
        )
        # Cap at 25% so 2 strong (15%+12% = 27%, but second gets scaled to 10)
        # cleanly demonstrates the limit. Three setups → at most 2 fit.
        cfg = _default_cfg(max_total_exposure_pct=25.0)
        decisions = agent_v2.generate_decisions(snap, agent_v2.StrategyState(), cfg)
        self.assertLessEqual(len(decisions), 3)
        total = sum(d.position_size_percent for d in decisions if d.action != "FLAT")
        self.assertLessEqual(total, 25.0 + 0.01)

    def test_max_three_decisions_per_cycle(self) -> None:
        # Five strong candidates → server caps at 3, strategy must respect.
        coins = {}
        for sym in ["BTC", "ETH", "SOL", "BNB", "XRP"]:
            coins[sym] = _coin(
                price=1000,
                trend="STRONG_UP",
                volatility=2.0,
                chg1m=0.3,
                chg5m=0.8,
            )
        snap = _build_snap(coins=coins)
        decisions = agent_v2.generate_decisions(snap, agent_v2.StrategyState(), _default_cfg())
        self.assertLessEqual(len(decisions), 3)


class StrategyCooldownTests(unittest.TestCase):
    def test_recent_exit_blocks_re_entry(self) -> None:
        snap = _build_snap(
            coins={
                "BTC": _coin(
                    price=100_000,
                    trend="STRONG_UP",
                    volatility=2.0,
                    chg1m=0.3,
                    chg5m=0.8,
                ),
            },
            server=_server_block(current_cycle=10),
        )
        state = agent_v2.StrategyState()
        # Mark BTC exited 2 cycles ago. Default cooldown = 5.
        state.last_exit_cycle["BTC"] = 8
        decisions = agent_v2.generate_decisions(snap, state, _default_cfg())
        # Heartbeat fires instead of re-entry.
        self.assertEqual(decisions[0].kind, "no_op")

    def test_after_cooldown_can_re_enter(self) -> None:
        snap = _build_snap(
            coins={
                "BTC": _coin(
                    price=100_000,
                    trend="STRONG_UP",
                    volatility=2.0,
                    chg1m=0.3,
                    chg5m=0.8,
                ),
            },
            server=_server_block(current_cycle=20),
        )
        state = agent_v2.StrategyState()
        # 6 cycles passed (default cooldown is 5).
        state.last_exit_cycle["BTC"] = 14
        decisions = agent_v2.generate_decisions(snap, state, _default_cfg())
        self.assertEqual(decisions[0].action, "LONG")


class StrategyHeartbeatTests(unittest.TestCase):
    def test_no_signals_fires_one_flat_heartbeat(self) -> None:
        snap = _build_snap(
            coins={
                "BTC": _coin(price=100_000, trend="NEUTRAL", volatility=0.5),
                "ETH": _coin(price=3000, trend="NEUTRAL", volatility=0.5),
            },
        )
        decisions = agent_v2.generate_decisions(snap, agent_v2.StrategyState(), _default_cfg())
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0].action, "FLAT")
        self.assertEqual(decisions[0].kind, "no_op")
        self.assertEqual(decisions[0].position_size_percent, 0)

    def test_heartbeat_picks_unheld_symbol(self) -> None:
        snap = _build_snap(
            coins={
                "BTC": _coin(price=100_000, trend="NEUTRAL", volatility=0.5),
                "ETH": _coin(price=3000, trend="NEUTRAL", volatility=0.5),
            },
            portfolio=_portfolio(
                open_trades={
                    "BTC": {
                        "symbol": "BTC",
                        "action": "LONG",
                        "entryPrice": 99_000,
                        "quantity": 0.1,
                        "reason": "earlier",
                        "entryTimestamp": "2026-01-01T00:00:00Z",
                    }
                }
            ),
        )
        decisions = agent_v2.generate_decisions(snap, agent_v2.StrategyState(), _default_cfg())
        # Heartbeat should NOT pick BTC (held). ETH is the only other symbol.
        self.assertEqual(decisions[0].symbol, "ETH")

    def test_heartbeat_payload_is_zero_sized_flat(self) -> None:
        snap = _build_snap(
            coins={"BTC": _coin(price=100_000, trend="NEUTRAL", volatility=0.5)},
        )
        decisions = agent_v2.generate_decisions(snap, agent_v2.StrategyState(), _default_cfg())
        payload = decisions[0].to_payload()
        self.assertEqual(payload["action"], "FLAT")
        self.assertEqual(payload["positionSizePercent"], 0)


class TelemetryTests(unittest.TestCase):
    def test_action_mix_calculation(self) -> None:
        t = agent_v2.Telemetry(histogram_depth=10)
        for _ in range(7):
            t.record_action("LONG")
        for _ in range(3):
            t.record_action("FLAT")
        mix = t.action_mix()
        self.assertEqual(mix["LONG"], 70.0)
        self.assertEqual(mix["FLAT"], 30.0)

    def test_histogram_depth_is_a_ring_buffer(self) -> None:
        t = agent_v2.Telemetry(histogram_depth=5)
        for _ in range(20):
            t.record_action("LONG")
        # Only the last 5 should remain.
        self.assertEqual(len(t.actions), 5)

    def test_counters_increment_independently(self) -> None:
        t = agent_v2.Telemetry(histogram_depth=10)
        t.incr("a")
        t.incr("a")
        t.incr("b", by=5)
        self.assertEqual(t.counters["a"], 2)
        self.assertEqual(t.counters["b"], 5)


class CycleDeadlineTests(unittest.TestCase):
    def test_parse_iso_handles_z_suffix(self) -> None:
        ts = "2026-05-07T12:00:00Z"
        result = agent_v2.parse_iso(ts)
        self.assertIsNotNone(result)
        # Sanity: timestamp lies in the expected ballpark for a 2026 date.
        self.assertGreater(result, 1_700_000_000)

    def test_parse_iso_handles_offset_suffix(self) -> None:
        ts = "2026-05-07T12:00:00+00:00"
        self.assertIsNotNone(agent_v2.parse_iso(ts))

    def test_parse_iso_returns_none_for_garbage(self) -> None:
        self.assertIsNone(agent_v2.parse_iso(None))
        self.assertIsNone(agent_v2.parse_iso(""))
        self.assertIsNone(agent_v2.parse_iso("not a date"))

    def test_compute_cycle_deadline_subtracts_safety_margin(self) -> None:
        snap = _build_snap(server=_server_block(next_in_sec=30))
        cfg = _default_cfg(deadline_safety_sec=5)
        deadline = agent_v2.compute_cycle_deadline(snap, cfg)
        self.assertIsNotNone(deadline)
        # Deadline should be ~25 seconds in the future (30 next_in - 5 safety).
        delta = deadline - datetime.now(timezone.utc).timestamp()
        self.assertGreater(delta, 20)
        self.assertLess(delta, 30)

    def test_compute_cycle_deadline_returns_none_without_server_timing(self) -> None:
        snap = _build_snap(server={"currentCycle": 1, "acceptingDecisionsForCycle": 2})
        self.assertIsNone(agent_v2.compute_cycle_deadline(snap, _default_cfg()))


class DecisionPayloadTests(unittest.TestCase):
    def test_flat_payload_size_is_zero(self) -> None:
        d = agent_v2.Decision(
            symbol="BTC",
            action="FLAT",
            position_size_percent=15,  # would be ignored
            reason="closing",
        )
        self.assertEqual(d.to_payload()["positionSizePercent"], 0)

    def test_reason_truncates_at_280_chars(self) -> None:
        long_reason = "x" * 500
        d = agent_v2.Decision(
            symbol="BTC", action="LONG", position_size_percent=10, reason=long_reason
        )
        self.assertLessEqual(len(d.to_payload()["reason"]), 280)

    def test_payload_size_rounded(self) -> None:
        d = agent_v2.Decision(
            symbol="BTC", action="LONG", position_size_percent=12.345, reason="r"
        )
        self.assertEqual(d.to_payload()["positionSizePercent"], 12.35)


if __name__ == "__main__":
    unittest.main()
