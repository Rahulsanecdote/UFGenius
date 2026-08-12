"""Validated-backtest baseline + paper-vs-backtest tolerance check.

Closes the `docs/UPGRADE_PLAN.md` acceptance criterion:

    "Live **paper** performance matches the validated backtest within tolerance
     before any real-money flag is enabled."

The P0.4 scorecard gate (`src/alpaca/scorecard.py`) checks realized paper trades
against **absolute floors** — useful, but it never asks the question the
acceptance criterion actually poses: *does the live paper edge look like the
edge that was validated out-of-sample?* A strategy can clear a 1.2 profit-factor
floor while delivering half the validated backtest's profit factor; that is a
degraded edge, and it should not graduate to real money.

This module supplies the missing reference point:

* `save_baseline()` — `python bot.py --mode validate --save-baseline` persists the
  **held-out out-of-sample** metrics of a validation run, together with the
  provenance needed to judge whether the reference still applies (span, ticker
  count, capital, seed, timestamp, and the run's `validated` verdict).
* `compare_paper_to_baseline()` — scores the paper scorecard against that
  reference within a configured **relative tolerance**.

### What is compared, and why only these two metrics

Only `win_rate_pct` and `profit_factor`. Both are **trade-level** (the paper
ledger has trades, not a daily equity curve, so Sharpe/drawdown have no
like-for-like paper counterpart) and both are **capital-invariant**, so a paper
account sized differently from the backtest does not skew the comparison.
`total_return_pct` is deliberately excluded for exactly that reason.

### The comparison is one-sided, on purpose

The gate fails only when paper **underperforms** the baseline beyond tolerance.
Paper outperforming is not blocked: paper fills carry no real slippage or queue
position, so paper-better-than-backtest is the *expected* direction of error, and
vetoing it would be a false veto on the money path. A large overshoot is still
surfaced (`paper_exceeds_baseline`) because it more often means a data or
accounting bug than a real edge — advisory only, it never fails the gate.

Fail-closed by construction: when the check is enabled, anything that prevents a
real comparison — no baseline, an unreadable one, a baseline whose own verdict
was NOT VALIDATED, one older than `baseline_max_age_days`, or one with no
comparable metric — returns `all_pass=False`. This is the opposite of the
advisory Phase-4 risk engine's fail-open stance, and deliberately so: this is the
last gate before real capital, and "we could not check" must never read as "you
may proceed".

Nothing here assumes profitability, and a passing comparison is not a prediction
— it only says the paper record has not visibly decayed relative to the run that
was validated out-of-sample.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from src.utils import config
from src.utils.logger import get_logger

log = get_logger(__name__)

# Trade-level, capital-invariant metrics produced by BOTH the backtest engine and
# the paper scorecard. Higher is better for both, so the tolerance is a floor.
TOLERANCE_METRICS: tuple[str, ...] = ("win_rate_pct", "profit_factor")

# Overshoot factor above which paper is flagged as suspiciously better than the
# validated backtest. Advisory only — never fails the gate.
OVERSHOOT_FLAG_MULTIPLE = 2.0

_SCHEMA_VERSION = 1


def _finite(value: Any) -> Optional[float]:
    """Coerce to a finite float, else None. Never raises."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(value: Any) -> Optional[datetime]:
    """Parse a stored ISO-8601 timestamp; naive values are read as UTC."""
    if not isinstance(value, str) or not value:
        return None
    try:
        ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts


def _path(path: Optional[str] = None) -> str:
    return str(path or config.PAPER_SCORECARD_BASELINE_PATH)


# ── persistence ─────────────────────────────────────────────────────────────── #


def build_baseline(
    validation_result: dict,
    *,
    tickers: Optional[list[str]] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    initial_capital: Optional[float] = None,
    seed: Optional[int] = None,
    now: Optional[datetime] = None,
) -> dict:
    """Extract the persistable reference block from a `validate_strategy` result.

    Records the **held-out out-of-sample** metrics only — the in-sample and
    walk-forward numbers were used for fitting/selection, so comparing live paper
    against them would compare against the part of the record most likely to be
    optimistic.
    """
    result = validation_result or {}
    oos = result.get("out_of_sample") or {}
    verdict = result.get("verdict") or {}
    split = result.get("split") or {}
    trade_boot = (result.get("bootstrap_out_of_sample") or {}).get("trade_level") or {}

    return {
        "schema_version": _SCHEMA_VERSION,
        "generated_at": (now or _utcnow()).isoformat(),
        "validated": bool(verdict.get("validated")),
        "metrics": {
            "win_rate_pct": _finite(oos.get("win_rate_pct")),
            "profit_factor": _finite(oos.get("profit_factor")),
            # Context only — not compared (see module docstring).
            "sharpe_ratio": _finite(oos.get("sharpe_ratio")),
            "total_return_pct": _finite(oos.get("total_return_pct")),
            "max_drawdown_pct": _finite(oos.get("max_drawdown_pct")),
            "prob_profitable": _finite(trade_boot.get("prob_profitable")),
            "total_trades": int(_finite(oos.get("total_trades")) or 0),
        },
        "provenance": {
            "in_sample": split.get("in_sample"),
            "out_of_sample": split.get("out_of_sample"),
            "requested_start": start,
            "requested_end": end,
            "n_tickers": len(tickers) if tickers is not None else None,
            # A sample of the universe, not the whole list — enough to spot a
            # baseline built on a different universe without bloating the file.
            "tickers_sample": sorted(tickers)[:25] if tickers else None,
            "initial_capital": _finite(initial_capital),
            "seed": seed,
        },
        "disclaimer": (
            "Out-of-sample backtest metrics. A validated baseline reduces the "
            "risk of promoting a noise-driven strategy; it does not predict "
            "future results."
        ),
    }


def save_baseline(
    validation_result: dict,
    *,
    path: Optional[str] = None,
    **kwargs: Any,
) -> dict:
    """Persist the validated-backtest reference. Returns the written baseline.

    Written atomically (temp file + `os.replace`) so a concurrent reader sees
    either the previous baseline or the new one, never a partial file. A single
    CLI process writes this, so no interprocess lock is needed.

    A NOT-VALIDATED run is still written — with `validated: false` — so the record
    of what was actually measured is kept; the comparison then refuses it rather
    than silently treating an unvalidated run as a reference.
    """
    baseline = build_baseline(validation_result, **kwargs)
    target = Path(_path(path))
    os.makedirs(target.parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".baseline-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(baseline, f, indent=2)
        os.replace(tmp, str(target))
    except Exception as exc:
        log.error(f"Failed to save validated baseline to {target}: {exc}", exc_info=True)
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        raise
    log.info(f"Validated baseline saved to {target} (validated={baseline['validated']})")
    return baseline


def load_baseline(path: Optional[str] = None) -> Optional[dict]:
    """Load the persisted baseline. Missing/malformed → None. Never raises."""
    p = Path(_path(path))
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        log.error(f"Validated baseline unreadable ({p}): {exc}")
        return None
    if not isinstance(data, dict) or not isinstance(data.get("metrics"), dict):
        log.error(f"Validated baseline malformed ({p}): missing metrics block")
        return None
    return data


# ── comparison ──────────────────────────────────────────────────────────────── #


def _fail(reason: str, **extra: Any) -> dict:
    return {"all_pass": False, "comparable": False, "reason": reason, "checks": {}, **extra}


def compare_paper_to_baseline(
    scorecard: dict,
    baseline: Optional[dict] = None,
    *,
    path: Optional[str] = None,
    tolerance_pct: Optional[float] = None,
    max_age_days: Optional[float] = None,
    now: Optional[datetime] = None,
) -> dict:
    """Score realized paper metrics against the validated backtest baseline.

    Returns a block with `all_pass`, per-metric `checks` (paper value, baseline
    value, computed floor), and a human-readable `reason`. `all_pass=False`
    whenever a real comparison could not be made — see the module docstring on
    fail-closed semantics.
    """
    tol = _finite(
        tolerance_pct if tolerance_pct is not None
        else config.PAPER_SCORECARD_BASELINE_TOLERANCE_PCT
    )
    # A nonsensical tolerance must not silently widen the gate: clamp to [0, 100].
    # 0 demands paper match the baseline exactly; 100 makes every floor 0.
    tol = 0.0 if tol is None else max(0.0, min(100.0, tol))
    max_age = _finite(
        max_age_days if max_age_days is not None
        else config.PAPER_SCORECARD_BASELINE_MAX_AGE_DAYS
    )

    card = scorecard or {}
    if baseline is None:
        baseline = load_baseline(path)
    if baseline is None:
        return _fail(
            "No validated backtest baseline on disk — run "
            "`python bot.py --mode validate --save-baseline` before enabling live trading."
        )
    if not baseline.get("validated"):
        return _fail(
            "The stored baseline's own verdict was NOT VALIDATED — there is no "
            "demonstrated out-of-sample edge to compare paper against."
        )

    generated_at = _parse_ts(baseline.get("generated_at"))
    age_days: Optional[float] = None
    if generated_at is not None:
        age_days = max(0.0, ((now or _utcnow()) - generated_at).total_seconds() / 86400.0)
    if max_age is not None and max_age > 0:
        if age_days is None:
            return _fail(
                "Baseline has no readable timestamp, so its age cannot be checked "
                f"against baseline_max_age_days={max_age:.0f} — re-run `--mode validate "
                "--save-baseline`."
            )
        if age_days > max_age:
            return _fail(
                f"Baseline is {age_days:.0f} days old (limit {max_age:.0f}) — re-validate "
                "before going live; a stale reference is not evidence of a current edge.",
                age_days=round(age_days, 1),
            )

    # Too few closed paper trades makes any comparison noise. Reuse the P0.4
    # min-trades floor so the two halves of the gate agree on "enough evidence".
    n_trades = int(_finite(card.get("n_trades")) or 0)
    min_trades = int(config.PAPER_SCORECARD_MIN_TRADES)
    if n_trades < min_trades:
        return _fail(
            f"Only {n_trades} closed paper trades (need {min_trades}) — not enough "
            "to compare against the validated backtest.",
            n_trades=n_trades,
            age_days=round(age_days, 1) if age_days is not None else None,
        )

    ref = baseline.get("metrics") or {}
    checks: dict[str, dict] = {}
    failures: list[str] = []
    overshoot: list[str] = []

    for metric in TOLERANCE_METRICS:
        base_val = _finite(ref.get(metric))
        paper_val = _finite(card.get(metric))

        if base_val is None:
            # No reference for this metric (e.g. a backtest with zero losing
            # trades yields profit_factor None). Skip rather than invent a floor.
            checks[metric] = {"comparable": False, "note": "no baseline value"}
            continue

        floor = base_val * (1.0 - tol / 100.0)

        if paper_val is None:
            # profit_factor is None on the paper side only when there are no
            # losses yet — strictly better than any finite baseline, so it passes
            # (mirroring the P0.4 floor check). Any other missing metric cannot
            # be compared and fails closed.
            if metric == "profit_factor":
                checks[metric] = {
                    "comparable": True, "pass": True, "paper": None,
                    "baseline": round(base_val, 4), "floor": round(floor, 4),
                    "note": "no losing paper trades yet",
                }
                continue
            checks[metric] = {"comparable": False, "note": "no paper value"}
            failures.append(f"{metric} missing from the paper scorecard")
            continue

        ok = paper_val >= floor
        checks[metric] = {
            "comparable": True,
            "pass": ok,
            "paper": round(paper_val, 4),
            "baseline": round(base_val, 4),
            "floor": round(floor, 4),
        }
        if not ok:
            failures.append(
                f"{metric} {paper_val:.4g} is below the tolerance floor "
                f"{floor:.4g} ({tol:.0f}% under the validated {base_val:.4g})"
            )
        elif base_val > 0 and paper_val > base_val * OVERSHOOT_FLAG_MULTIPLE:
            overshoot.append(
                f"{metric} {paper_val:.4g} is more than {OVERSHOOT_FLAG_MULTIPLE:g}x "
                f"the validated {base_val:.4g}"
            )

    comparable = [m for m, c in checks.items() if c.get("comparable")]
    if not comparable:
        return _fail(
            "No metric could be compared against the baseline (the stored "
            "baseline has no comparable trade-level metrics).",
            checks=checks,
        )

    all_pass = not failures
    result = {
        "all_pass": all_pass,
        "comparable": True,
        "checks": checks,
        "compared_metrics": comparable,
        "tolerance_pct": tol,
        "n_trades": n_trades,
        "baseline_generated_at": baseline.get("generated_at"),
        "baseline_age_days": round(age_days, 1) if age_days is not None else None,
        "baseline_out_of_sample": (baseline.get("provenance") or {}).get("out_of_sample"),
        "reason": (
            f"Paper metrics are within {tol:.0f}% of the validated out-of-sample "
            "backtest."
            if all_pass else
            "Paper underperforms the validated backtest: " + "; ".join(failures)
        ),
    }
    if overshoot:
        # Advisory only: paper beating the backtest never blocks the gate.
        result["paper_exceeds_baseline"] = overshoot
        result["divergence_note"] = (
            "Paper is far ABOVE the validated backtest (" + "; ".join(overshoot) + "). "
            "This does not block promotion, but paper fills carry no real slippage "
            "or queue position — check for a data or accounting error before "
            "treating it as a genuine edge."
        )
    return result


def baseline_gate(
    scorecard: dict, *, path: Optional[str] = None, now: Optional[datetime] = None
) -> tuple[bool, Optional[dict]]:
    """(passes, comparison) for the live paper-vs-backtest tolerance gate.

    Returns `(True, None)` when the check is disabled in config, so the gate is
    opt-in and a no-op until an operator turns it on.
    """
    if not config.PAPER_SCORECARD_BASELINE_GATE_ENABLED:
        return True, None
    comparison = compare_paper_to_baseline(scorecard, path=path, now=now)
    return bool(comparison.get("all_pass")), comparison
