#!/usr/bin/env python3
"""
Hermes Arena — agent_v2.py — alternative deterministic-strategy starter.

NOTE: this is NOT the Hermes-Decides path. A hand-rolled strategy layer
in this file picks every trade. Hermes Agent — when configured — is only
called at the END of each cycle to rewrite the `reason` text in your
BOT_PERSONA voice. The model never chooses LONG / SHORT / FLAT, position
size, or which symbol to enter. If you want Hermes to be the decision
engine, use the primary `agent.py` instead. Both starters submit to the
same arena server with identical wire protocol.

Three-layer architecture, each with its own failure mode:

  1. STRATEGY (deterministic). Reads coins[SYM].analysis from the
     snapshot — trend, volatility, multi-timeframe momentum — and
     generates entry, exit, and FLAT decisions with sized positions.
     No LLM in this layer. The math here decides what trades happen.

  2. NARRATION (optional LLM). Rewrites each decision's `reason` in
     your BOT_PERSONA voice so the public chat stream has personality.
     Failure here NEVER blocks a submission — falls back to a template
     reason ("$BTC strong-up trend, vol 1.2%, opening 12% long") that
     still reads like a human wrote it.

  3. SUBMISSION (deadline-aware). Tracks server.nextCycleAt, budgets
     each step against the cycle close, skips narration when the
     budget gets tight, and posts decisions before the cycle ticks.

Telemetry: a rolling action histogram + named counters dump to stderr
every N cycles so silent failure modes (gateway down, malformed JSON,
"all FLAT" stuck loop) surface immediately instead of looking like
quiet activity.

Heartbeat behavior is fundamentally different from v1:
  - Heartbeat fires only when the strategy layer found NO actionable
    signals — it's a truthful "no setups" notice, not a fallback for
    plumbing failures.
  - Plumbing failures (snapshot 5xx, submit 5xx, narration error) are
    logged + counted + the cycle is SKIPPED. The agent never
    pretends to be active when it's broken.

Quickstart:
    pip install -r requirements.txt
    cp .env.example .env
    # Set ARENA_BASE_URL, ARENA_AGENT_ID, and ARENA_AGENT_BEARER_TOKEN
    python agent_v2.py            # run forever
    python agent_v2.py --once     # one cycle then exit

Tunable env vars (all optional, sensible defaults):
    ARENA_BASE_URL                 https://api.hermesarena.live
    ARENA_AGENT_ID                 (required)
    ARENA_AGENT_BEARER_TOKEN       (or ARENA_AGENT_API_KEY)

    AGENT_INTERVAL_SEC             60       fallback poll cadence when
                                            cycle timing isn't returned
    AGENT_DEADLINE_SAFETY_SEC      5        skip submit at this many
                                            seconds before cycle close
    AGENT_NARRATION_BUDGET_SEC     6        minimum time left to
                                            attempt LLM narration
    AGENT_TELEMETRY_EVERY          10       cycles between health dumps
    AGENT_TRADES_HISTOGRAM_DEPTH   50       rolling window for action mix

    STRATEGY_MAX_TOTAL_EXPOSURE    50       strategy-side cap. Server has
                                            no exposure ceiling — cash is
                                            the only constraint — but
                                            holding some cash buffer makes
                                            re-entry/sizing flexible.
    STRATEGY_MIN_VOLATILITY        0.4      % below this = chop, skip
    STRATEGY_REENTRY_COOLDOWN      5        cycles before re-entering
                                            same symbol after exit
    STRATEGY_DRAWDOWN_LIMIT        10       % drawdown that triggers
                                            risk-off (close largest
                                            underwater position)

    HERMES_BASE_URL                (optional) http://127.0.0.1:8642
    HERMES_MODEL                   hermes-agent
    HERMES_API_KEY                 (optional) bearer for the gateway
    BOT_PERSONA                    (optional) used by narration layer
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import requests
from dotenv import load_dotenv

from hermes_parse import safe_json_parse

load_dotenv()

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("hermes-agent-v2")


# ─── Config ────────────────────────────────────────────────────────────────


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, str(default)))
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class Config:
    base_url: str
    agent_id: str
    bearer_token: Optional[str]
    api_key: Optional[str]

    interval_sec: int
    deadline_safety_sec: int
    narration_budget_sec: int
    telemetry_every: int
    histogram_depth: int

    max_total_exposure_pct: float
    min_volatility_pct: float
    reentry_cooldown_cycles: int
    drawdown_limit_pct: float

    hermes_base_url: Optional[str]
    hermes_model: str
    hermes_api_key: Optional[str]
    bot_persona: str

    @classmethod
    def from_env(cls) -> "Config":
        base_url = (
            os.environ.get("ARENA_BASE_URL")
            or "https://api.hermesarena.live"
        ).rstrip("/")
        agent_id = os.environ.get("ARENA_AGENT_ID") or ""
        bearer = os.environ.get("ARENA_AGENT_BEARER_TOKEN") or None
        api_key = os.environ.get("ARENA_AGENT_API_KEY") or None
        if not agent_id:
            sys.exit("ERROR: ARENA_AGENT_ID is required (see .env.example).")
        if not bearer and not api_key:
            sys.exit(
                "ERROR: provide ARENA_AGENT_BEARER_TOKEN or ARENA_AGENT_API_KEY."
            )
        return cls(
            base_url=base_url,
            agent_id=agent_id,
            bearer_token=bearer,
            api_key=api_key,
            interval_sec=_env_int("AGENT_INTERVAL_SEC", 60),
            deadline_safety_sec=_env_int("AGENT_DEADLINE_SAFETY_SEC", 5),
            narration_budget_sec=_env_int("AGENT_NARRATION_BUDGET_SEC", 6),
            telemetry_every=_env_int("AGENT_TELEMETRY_EVERY", 10),
            histogram_depth=_env_int("AGENT_TRADES_HISTOGRAM_DEPTH", 50),
            max_total_exposure_pct=_env_float("STRATEGY_MAX_TOTAL_EXPOSURE", 50.0),
            min_volatility_pct=_env_float("STRATEGY_MIN_VOLATILITY", 0.4),
            reentry_cooldown_cycles=_env_int("STRATEGY_REENTRY_COOLDOWN", 5),
            drawdown_limit_pct=_env_float("STRATEGY_DRAWDOWN_LIMIT", 10.0),
            hermes_base_url=(os.environ.get("HERMES_BASE_URL") or "").rstrip("/") or None,
            hermes_model=os.environ.get("HERMES_MODEL", "hermes-agent"),
            hermes_api_key=os.environ.get("HERMES_API_KEY") or None,
            bot_persona=os.environ.get(
                "BOT_PERSONA",
                "You are a sharp, no-nonsense crypto trader. Trade with "
                "conviction, speak in short blunt sentences, drop a bit of "
                "trader slang.",
            ),
        )


# ─── Telemetry ─────────────────────────────────────────────────────────────


@dataclass
class Telemetry:
    histogram_depth: int
    counters: dict[str, int] = field(default_factory=dict)
    actions: deque = field(default_factory=deque)
    last_dump_cycle: int = -1

    def __post_init__(self) -> None:
        self.actions = deque(maxlen=self.histogram_depth)

    def incr(self, key: str, by: int = 1) -> None:
        self.counters[key] = self.counters.get(key, 0) + by

    def record_action(self, action: str) -> None:
        self.actions.append(action.upper())

    def action_mix(self) -> dict[str, float]:
        if not self.actions:
            return {}
        total = len(self.actions)
        out: dict[str, float] = {}
        for a in self.actions:
            out[a] = out.get(a, 0) + 1
        return {k: round((v / total) * 100, 1) for k, v in out.items()}

    def maybe_dump(self, cycle: int, every: int) -> None:
        if cycle == self.last_dump_cycle:
            return
        if cycle <= 0 or cycle % every != 0:
            return
        self.dump(cycle)
        self.last_dump_cycle = cycle

    def dump(self, cycle: int) -> None:
        mix = self.action_mix() or {"-": "no submissions yet"}
        # Print to stderr so it's separable from regular log output if
        # operators want to grep telemetry separately.
        print(
            f"\n[telemetry @ cycle {cycle}] "
            f"counters={json.dumps(self.counters, sort_keys=True)} "
            f"action_mix(last_{len(self.actions)})={mix}",
            file=sys.stderr,
            flush=True,
        )
        # If the action mix is dominated by FLAT, that's a strong signal
        # the agent is stuck in heartbeat mode — surface it as a warning.
        if isinstance(mix, dict) and mix.get("FLAT", 0) >= 90 and len(self.actions) >= 20:
            print(
                f"[telemetry warning] last {len(self.actions)} submissions "
                f"are >=90% FLAT — agent is likely stuck. Check counters "
                f"for gateway_failures / parse_failures / no_signals_this_cycle.",
                file=sys.stderr,
                flush=True,
            )


# ─── Arena client ─────────────────────────────────────────────────────────


class ArenaClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.session = requests.Session()
        if cfg.bearer_token:
            self.session.headers["Authorization"] = f"Bearer {cfg.bearer_token}"
        elif cfg.api_key:
            self.session.headers["x-agent-key"] = cfg.api_key

    def snapshot(self, timeout: float = 10.0) -> dict[str, Any]:
        r = self.session.get(
            f"{self.cfg.base_url}/api/arena/agent/{self.cfg.agent_id}/snapshot",
            params={"include": "analysis"},
            timeout=timeout,
        )
        r.raise_for_status()
        return r.json()

    def submit(
        self, decisions: list[dict[str, Any]], timeout: float = 10.0
    ) -> dict[str, Any]:
        r = self.session.post(
            f"{self.cfg.base_url}/api/arena/agent/{self.cfg.agent_id}/decision",
            json={"decisions": decisions},
            timeout=timeout,
        )
        if r.status_code >= 400:
            log.warning("submit failed [%s]: %s", r.status_code, r.text[:200])
        r.raise_for_status()
        return r.json()


# ─── Strategy layer ───────────────────────────────────────────────────────
#
# Pure-function, deterministic. Reads coins[SYM].analysis; emits a
# ranked list of Decision objects. No network calls in this layer.


@dataclass
class Decision:
    symbol: str
    action: str  # 'LONG' | 'SHORT' | 'FLAT'
    position_size_percent: float
    reason: str
    # Internal — not sent to the server. Used to rank candidates and
    # to drive the narration layer.
    conviction: int = 0  # higher = stronger signal (1 weak ... 4 strong)
    kind: str = "entry"  # 'entry' | 'exit' | 'risk_off' | 'no_op'

    def to_payload(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "action": self.action,
            "positionSizePercent": (
                0 if self.action == "FLAT" else round(self.position_size_percent, 2)
            ),
            "reason": self.reason[:280],
        }


# Trend labels emitted by the server's analysis block. Sourced from
# services/priceHistoryService.ts in the backend.
_TREND_BULL = {"STRONG_UP", "UP"}
_TREND_BEAR = {"STRONG_DOWN", "DOWN"}


def _conviction_for(trend: str, volatility: float) -> int:
    """Map trend strength + volatility into a 1-4 conviction tier."""
    if trend in {"STRONG_UP", "STRONG_DOWN"}:
        return 4 if volatility >= 1.5 else 3
    if trend in {"UP", "DOWN"}:
        return 2 if volatility >= 1.0 else 1
    return 0


def _size_for_conviction(conviction: int) -> float:
    """Position size in percent, by conviction tier. The server has no
    per-trade cap — cash availability is the only constraint — these
    sizes are this strategy's deliberate budget, not a server limit."""
    return {4: 15.0, 3: 12.0, 2: 8.0, 1: 6.0}.get(conviction, 0.0)


def _template_reason_entry(symbol: str, action: str, analysis: dict[str, Any], size: float) -> str:
    trend = analysis.get("trend", "?")
    vol = float(analysis.get("volatility", 0) or 0)
    chg5 = float(analysis.get("priceChange5m", 0) or 0)
    chg30 = float(analysis.get("priceChange30m", 0) or 0)
    return (
        f"${symbol} {action.lower()} {size:.0f}% — trend {trend.lower().replace('_', ' ')}, "
        f"vol {vol:.1f}%, 5m {chg5:+.2f}%, 30m {chg30:+.2f}%."
    )


def _template_reason_exit(symbol: str, analysis: dict[str, Any], reason_kind: str) -> str:
    trend = analysis.get("trend", "?")
    if reason_kind == "trend_reversal":
        return f"${symbol} trend flipped to {trend.lower().replace('_', ' ')} — closing."
    if reason_kind == "risk_off":
        return f"${symbol} drawdown ceiling — risk-off close."
    return f"${symbol} closing position."


def _template_reason_no_op(snap: dict[str, Any]) -> str:
    coins = snap.get("coins", {})
    trend_counts: dict[str, int] = {}
    for data in coins.values():
        analysis = (data or {}).get("analysis") or {}
        t = analysis.get("trend", "NEUTRAL")
        trend_counts[t] = trend_counts.get(t, 0) + 1
    if not trend_counts:
        return "No analysis available — holding cash."
    summary = ", ".join(f"{n} {t.lower().replace('_', ' ')}" for t, n in trend_counts.items())
    return f"No setups passing filters this cycle. Market: {summary}. Holding."


@dataclass
class StrategyState:
    """Persists between cycles. Held in-process — not durable across
    restarts on purpose. The server is the source of truth for actual
    open positions; this is just for cooldown tracking."""

    last_exit_cycle: dict[str, int] = field(default_factory=dict)


def generate_decisions(
    snap: dict[str, Any],
    state: StrategyState,
    cfg: Config,
) -> list[Decision]:
    """Translate snapshot → list of decisions, ranked by conviction.
    Server caps the submission at 3, so we never return more than 3."""

    server = snap.get("server") or {}
    cycle = int(server.get("currentCycle", 0))
    portfolio = snap.get("portfolio") or {}
    open_trades = portfolio.get("openTrades") or {}
    drawdown_pct = float(portfolio.get("currentDrawdownPercent", 0) or 0)
    portfolio_value = float(portfolio.get("portfolioValue", 0) or 0)

    coins = snap.get("coins") or {}

    # Compute current exposure as the sum of |position size| per coin.
    # The server's openTrade objects expose entryPrice + quantity; we
    # mark to current price for an honest exposure number.
    current_exposure_pct = 0.0
    for sym, pos in open_trades.items():
        price = (coins.get(sym) or {}).get("price") or 0
        qty = float((pos or {}).get("quantity", 0) or 0)
        notional = abs(price * qty)
        if portfolio_value > 0:
            current_exposure_pct += (notional / portfolio_value) * 100

    decisions: list[Decision] = []

    # ── Risk-off: drawdown ceiling ────────────────────────────────────
    if drawdown_pct >= cfg.drawdown_limit_pct and open_trades:
        # Close the position whose unrealized PnL is most negative —
        # i.e. our biggest losing trade. The server will compute final
        # PnL on close; here we just rank by mark-to-market.
        worst_sym: Optional[str] = None
        worst_loss = 0.0
        for sym, pos in open_trades.items():
            price = (coins.get(sym) or {}).get("price") or 0
            entry = float((pos or {}).get("entryPrice", 0) or 0)
            qty = float((pos or {}).get("quantity", 0) or 0)
            action = (pos or {}).get("action", "LONG")
            if entry <= 0 or qty <= 0:
                continue
            direction = 1 if action == "LONG" else -1
            pnl = (price - entry) * qty * direction
            if pnl < worst_loss:
                worst_loss = pnl
                worst_sym = sym
        if worst_sym:
            analysis = (coins.get(worst_sym) or {}).get("analysis") or {}
            decisions.append(
                Decision(
                    symbol=worst_sym,
                    action="FLAT",
                    position_size_percent=0,
                    reason=_template_reason_exit(worst_sym, analysis, "risk_off"),
                    conviction=4,
                    kind="risk_off",
                )
            )
            state.last_exit_cycle[worst_sym] = cycle

    # ── Exits: trend reversal on a position we're holding ────────────
    # A LONG with trend now bearish = exit. A SHORT with trend now
    # bullish = exit. Skip symbols we've already queued for risk-off.
    queued = {d.symbol for d in decisions}
    for sym, pos in open_trades.items():
        if sym in queued:
            continue
        analysis = (coins.get(sym) or {}).get("analysis") or {}
        trend = analysis.get("trend", "NEUTRAL")
        action = (pos or {}).get("action", "LONG")
        chg5m = float(analysis.get("priceChange5m", 0) or 0)
        reverse = (
            (action == "LONG" and trend in _TREND_BEAR and chg5m <= -0.5)
            or (action == "SHORT" and trend in _TREND_BULL and chg5m >= 0.5)
        )
        if reverse:
            decisions.append(
                Decision(
                    symbol=sym,
                    action="FLAT",
                    position_size_percent=0,
                    reason=_template_reason_exit(sym, analysis, "trend_reversal"),
                    conviction=3,
                    kind="exit",
                )
            )
            state.last_exit_cycle[sym] = cycle

    # ── Entries: rank candidates by conviction × volatility ──────────
    # Build a list, then pick the top however-many we can fit under
    # the exposure budget (and the 3-decision cycle cap).
    queued = {d.symbol for d in decisions}
    candidates: list[tuple[int, Decision]] = []
    for sym, data in coins.items():
        analysis = (data or {}).get("analysis") or {}
        if not analysis:
            continue
        if sym in open_trades or sym in queued:
            continue  # don't double-add or churn an existing position
        last_exit = state.last_exit_cycle.get(sym)
        if last_exit is not None and (cycle - last_exit) < cfg.reentry_cooldown_cycles:
            continue  # cooldown still active

        trend = analysis.get("trend", "NEUTRAL")
        volatility = float(analysis.get("volatility", 0) or 0)
        if volatility < cfg.min_volatility_pct:
            continue  # chop floor — no edge in flat tape

        is_bull = trend in _TREND_BULL
        is_bear = trend in _TREND_BEAR
        if not (is_bull or is_bear):
            continue

        # Confirmation: require recent momentum aligned with the trend
        # so we're not buying the very top of an exhausted move. The
        # server's analysis already factors all five timeframes into
        # `trend`; we add a 5m and 1m momentum check as a finer gate.
        chg1m = float(analysis.get("priceChange1m", 0) or 0)
        chg5m = float(analysis.get("priceChange5m", 0) or 0)
        if is_bull and (chg1m < 0 or chg5m < 0):
            continue
        if is_bear and (chg1m > 0 or chg5m > 0):
            continue

        conviction = _conviction_for(trend, volatility)
        if conviction == 0:
            continue
        size = _size_for_conviction(conviction)
        if size <= 0:
            continue

        action = "LONG" if is_bull else "SHORT"
        decisions_reason = _template_reason_entry(sym, action, analysis, size)
        candidates.append(
            (
                conviction,
                Decision(
                    symbol=sym,
                    action=action,
                    position_size_percent=size,
                    reason=decisions_reason,
                    conviction=conviction,
                    kind="entry",
                ),
            )
        )

    candidates.sort(key=lambda x: (-x[0], x[1].symbol))

    remaining_exposure = max(0.0, cfg.max_total_exposure_pct - current_exposure_pct)
    for _, d in candidates:
        if len(decisions) >= 3:
            break
        if d.position_size_percent > remaining_exposure:
            # Try a smaller tier that still fits, otherwise skip.
            scaled = min(d.position_size_percent, remaining_exposure)
            if scaled < 4.0:
                continue
            d.position_size_percent = scaled
        decisions.append(d)
        remaining_exposure -= d.position_size_percent

    # ── Heartbeat: nothing actionable. One truthful FLAT no-op. ──────
    if not decisions:
        # Pick a symbol that the agent doesn't currently hold, so FLAT
        # is a true no-op. Stable choice keeps the chat tidy.
        held = set(open_trades.keys())
        candidates_for_noop = [
            sym for sym in coins.keys() if sym not in held
        ] or list(coins.keys())
        target = sorted(candidates_for_noop)[0] if candidates_for_noop else "BTC"
        decisions.append(
            Decision(
                symbol=target,
                action="FLAT",
                position_size_percent=0,
                reason=_template_reason_no_op(snap),
                conviction=0,
                kind="no_op",
            )
        )

    # Server caps at 3 decisions — already respected, but enforce here too.
    return decisions[:3]


# ─── Narration layer (optional LLM rewrite) ────────────────────────────
#
# Takes a list of strategy decisions, asks Hermes to rewrite each
# `reason` in BOT_PERSONA voice. Returns the SAME list with reasons
# (potentially) replaced. Never raises — narration failure leaves
# the template reasons in place.


_NARRATION_SYSTEM = (
    "{persona}\n\nYou will receive a JSON list of trade decisions a "
    "deterministic strategy engine has already chosen. Your job is to "
    "rewrite the `reason` field of each — IN VOICE, under 240 chars, "
    "as ONE sentence — explaining the trade like a human trader would "
    "say it on a stream. Do NOT change symbol, action, or "
    "positionSizePercent. Output ONLY a JSON object of shape "
    '{"reasons": ["string", "string", ...]} where the i-th string is '
    "the new reason for the i-th decision. No prose, no markdown."
)


def narrate_reasons(
    decisions: list[Decision], cfg: Config, budget_sec: float, telemetry: Telemetry
) -> None:
    """In-place rewrite of `reason` fields. Falls back silently."""
    if not cfg.hermes_base_url or not decisions:
        return
    if budget_sec < 1.0:
        telemetry.incr("narration_skipped_no_budget")
        return

    payload = [
        {
            "symbol": d.symbol,
            "action": d.action,
            "positionSizePercent": (
                0 if d.action == "FLAT" else round(d.position_size_percent, 2)
            ),
            "kind": d.kind,
            "current_reason": d.reason,
        }
        for d in decisions
    ]

    headers = {"Content-Type": "application/json"}
    if cfg.hermes_api_key:
        headers["Authorization"] = f"Bearer {cfg.hermes_api_key}"

    try:
        r = requests.post(
            f"{cfg.hermes_base_url}/v1/chat/completions",
            headers=headers,
            json={
                "model": cfg.hermes_model,
                "messages": [
                    {"role": "system", "content": _NARRATION_SYSTEM.format(persona=cfg.bot_persona)},
                    {"role": "user", "content": json.dumps(payload)},
                ],
                "response_format": {"type": "json_object"},
                "temperature": 0.7,
            },
            timeout=max(2.0, budget_sec),
        )
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"]
        # Tolerant parse: handles fenced output, prose padding, trailing
        # commas, etc. so a slightly-malformed narration doesn't drop the
        # bot back to template reasons.
        parsed = safe_json_parse(content)
        if not isinstance(parsed, dict):
            telemetry.incr("narration_parse_failures")
            return
        reasons = parsed.get("reasons")
        if not isinstance(reasons, list):
            telemetry.incr("narration_parse_failures")
            return
        for d, new_reason in zip(decisions, reasons):
            if isinstance(new_reason, str) and new_reason.strip():
                d.reason = new_reason.strip()[:280]
        telemetry.incr("narration_ok")
    except requests.RequestException as exc:
        telemetry.incr("narration_gateway_failures")
        log.debug("narration gateway error (%s) — keeping template reasons", exc)
    except (ValueError, KeyError) as exc:
        telemetry.incr("narration_parse_failures")
        log.debug("narration parse error (%s) — keeping template reasons", exc)


# ─── Cycle deadline helper ─────────────────────────────────────────────


def parse_iso(ts: Optional[str]) -> Optional[float]:
    if not ts or not isinstance(ts, str):
        return None
    try:
        # Python 3.11+ accepts trailing Z. For older runtimes, normalize.
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        return datetime.fromisoformat(ts).timestamp()
    except ValueError:
        return None


def compute_cycle_deadline(snap: dict[str, Any], cfg: Config) -> Optional[float]:
    """Returns Unix epoch seconds at which we MUST have submitted by,
    or None if the snapshot doesn't expose cycle timing."""
    server = snap.get("server") or {}
    next_at = parse_iso(server.get("nextCycleAt"))
    if next_at is None:
        return None
    return next_at - cfg.deadline_safety_sec


# ─── Main loop ─────────────────────────────────────────────────────────


_running = True


def _stop(*_: Any) -> None:
    global _running
    _running = False
    log.info("shutdown signal received, exiting after current iteration")


def run_one_cycle(
    client: ArenaClient,
    cfg: Config,
    state: StrategyState,
    telemetry: Telemetry,
    last_submitted_cycle: int,
    dry_run: bool = False,
) -> int:
    """Returns the cycle number we submitted for, or last_submitted_cycle
    if we skipped (rate-limited, no new cycle, or any failure).

    When dry_run is True, every step (snapshot, strategy, narration) runs
    normally but the final POST to /decision is skipped. The would-be
    payload is logged so operators can see what v2 *would* have sent. The
    cycle counter still advances locally so dry-run loops behave like
    real ones — useful for safe end-to-end testing on a live agent.
    """

    telemetry.incr("cycles_seen")

    # 1. Snapshot.
    try:
        snap = client.snapshot(timeout=10.0)
    except requests.RequestException as exc:
        telemetry.incr("snapshot_failures")
        log.warning("snapshot failed: %s", exc)
        return last_submitted_cycle

    server = snap.get("server") or {}
    accepting = int(server.get("acceptingDecisionsForCycle", 0))
    if accepting <= last_submitted_cycle:
        # Already submitted for this cycle. Nothing to do until next.
        return last_submitted_cycle

    # Status gate — SUSPENDED agents can't submit; emit a counter so it's
    # obvious in telemetry rather than a silent reject loop.
    status = snap.get("status") or "ACTIVE"
    if status == "SUSPENDED":
        telemetry.incr("cycles_suspended")
        log.warning("agent SUSPENDED — skipping submit (server will reject)")
        return last_submitted_cycle

    # 2. Strategy.
    decisions = generate_decisions(snap, state, cfg)
    if any(d.kind == "no_op" for d in decisions):
        telemetry.incr("no_signals_this_cycle")

    # 3. Narration (optional, deadline-aware).
    deadline = compute_cycle_deadline(snap, cfg)
    if deadline is not None:
        budget = deadline - time.time() - 2.0  # leave 2s for the submit itself
        if budget >= cfg.narration_budget_sec:
            narrate_reasons(decisions, cfg, budget, telemetry)
        else:
            telemetry.incr("narration_skipped_no_budget")
    else:
        # No cycle timing in the snapshot — narrate optimistically with a
        # bounded budget. This shouldn't happen on the production backend
        # (it returns cycleStartedAt + nextCycleAt), but stay forward-compat.
        narrate_reasons(decisions, cfg, float(cfg.narration_budget_sec), telemetry)

    # 4. Submit.
    if deadline is not None and time.time() >= deadline:
        telemetry.incr("cycles_skipped_for_deadline")
        log.warning(
            "missed cycle %d deadline by %.1fs — skipping submit",
            accepting,
            time.time() - deadline,
        )
        return last_submitted_cycle

    submit_timeout = (
        max(3.0, deadline - time.time())
        if deadline is not None
        else 10.0
    )

    payload = [d.to_payload() for d in decisions]

    decision_summary = ", ".join(
        f"{d.symbol} {d.action}{'' if d.action == 'FLAT' else f' {d.position_size_percent:.0f}%'}"
        for d in decisions
    )
    portfolio_value = float((snap.get("portfolio") or {}).get("portfolioValue", 0) or 0)
    drawdown_percent = float(
        (snap.get("portfolio") or {}).get("currentDrawdownPercent", 0) or 0
    )

    if dry_run:
        # Tally the action mix as if we had submitted, so dry-run telemetry
        # matches what a live run would surface. Don't bump cycles_submitted —
        # use a separate counter so the two modes are distinguishable.
        telemetry.incr("cycles_dry_run")
        for d in decisions:
            telemetry.record_action(d.action)
        log.info(
            "cycle %d DRY-RUN (would submit): %s | NAV=$%.2f, dd=%.2f%%",
            accepting,
            decision_summary,
            portfolio_value,
            drawdown_percent,
        )
        for d in decisions:
            log.info("  → %s", json.dumps(d.to_payload()))
        return accepting

    try:
        resp = client.submit(payload, timeout=submit_timeout)
    except requests.RequestException as exc:
        telemetry.incr("submission_failures")
        log.warning("submit failed: %s — cycle %d not landed", exc, accepting)
        return last_submitted_cycle

    telemetry.incr("cycles_submitted")
    for d in decisions:
        telemetry.record_action(d.action)

    log.info(
        "cycle %s submitted: %s | NAV=$%.2f, dd=%.2f%%, replaced=%s",
        resp.get("targetCycle", accepting),
        decision_summary,
        portfolio_value,
        drawdown_percent,
        resp.get("replaced"),
    )
    return accepting


def main() -> None:
    parser = argparse.ArgumentParser(description="Hermes Arena agent v2")
    parser.add_argument("--once", action="store_true", help="Run one cycle and exit")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Run normally (snapshot, strategy, narration) but skip the "
            "final POST to /decision. The would-be payload is logged. "
            "Combine with --once for a single-cycle smoke test."
        ),
    )
    args = parser.parse_args()

    cfg = Config.from_env()
    client = ArenaClient(cfg)
    state = StrategyState()
    telemetry = Telemetry(histogram_depth=cfg.histogram_depth)

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    log.info(
        "agent_v2 starting: agent=%s base=%s interval=%ds dd_limit=%.1f%% "
        "exposure_cap=%.1f%% narration=%s%s",
        cfg.agent_id,
        cfg.base_url,
        cfg.interval_sec,
        cfg.drawdown_limit_pct,
        cfg.max_total_exposure_pct,
        "on" if cfg.hermes_base_url else "off",
        " [DRY-RUN: no submissions will be posted]" if args.dry_run else "",
    )

    last_submitted_cycle = -1

    while _running:
        last_submitted_cycle = run_one_cycle(
            client, cfg, state, telemetry, last_submitted_cycle,
            dry_run=args.dry_run,
        )
        telemetry.maybe_dump(last_submitted_cycle, cfg.telemetry_every)

        if args.once:
            break

        # Sleep in 1s slices so signals are responsive. Cycle-aware
        # polling: if we know when the next cycle starts, sleep until
        # ~2s after that timestamp to catch the new accepting window
        # promptly. Otherwise fall back to interval_sec.
        try:
            snap = client.snapshot(timeout=5.0)
            next_at = parse_iso((snap.get("server") or {}).get("nextCycleAt"))
        except requests.RequestException:
            next_at = None

        if next_at is not None:
            sleep_for = max(2.0, next_at - time.time() + 2.0)
        else:
            sleep_for = float(cfg.interval_sec)

        # Cap the sleep so we still surface telemetry on a slow server.
        sleep_for = min(sleep_for, float(cfg.interval_sec) + 5.0)

        slept = 0.0
        while _running and slept < sleep_for:
            time.sleep(min(1.0, sleep_for - slept))
            slept += 1.0


if __name__ == "__main__":
    main()
