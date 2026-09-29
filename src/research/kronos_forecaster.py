"""Kronos as a `Forecaster` for the calibration harness. Measurement only.

Kronos (github.com/shiyu-coder/Kronos, MIT, AAAI 2026) is a foundation model for
candlestick data. This wraps it in the one interface
`interval_calibration.evaluate_calibration` understands, so it can be scored
against `naive_empirical` on identical windows — which is the comparison the
upstream repository never makes.

**Optional dependency, by design.** `torch` and the Kronos package are not in
`requirements.txt`; both are imported lazily and a missing install returns a
`Band`-less forecaster with a stated reason rather than raising. Nothing in the
scan, signal or execution path imports this module.

Three deliberate differences from the walkthrough that popularised it:

1. **`top_p=1.0`, not 0.9.** Nucleus sampling at 0.9 discards the bottom tenth
   of probability mass at every token, so the sampled paths are drawn from an
   already-truncated distribution — and then their 5th/95th percentiles are
   presented as the model's uncertainty. That understates the band in exactly
   the tails a risk interval exists to describe. Truncation is a decoding
   convenience for text; here it silently biases the measurement.

2. **`n_paths` defaults to 200, not 20.** At n=20 the 5th and 95th percentiles
   are essentially the sample minimum and maximum — the highest-variance
   statistics available — so the band width moves substantially run to run. 200
   is affordable because of point 3.

3. **One batched call, not N sequential ones.** `sample_count` looks like the
   parameter for this and is not: `model/kronos.py` averages those paths
   internally (`preds = np.mean(preds, axis=1)`), so `sample_count=20` collapses
   to a single mean path and destroys the distribution. Passing the same window
   N times to `predict_batch` with `sample_count=1` yields N independently
   sampled paths in one forward pass instead.

The band is taken from the distribution of the close at the **final** horizon
step, because that is what the harness compares against: the realised close
`horizon` bars after the origin.
"""

from __future__ import annotations

import math
from typing import Optional

import numpy as np
import pandas as pd

from src.research.interval_calibration import Band
from src.utils.logger import get_logger

log = get_logger(__name__)

__all__ = ["KronosUnavailable", "load_kronos", "make_kronos_forecaster"]

_OHLCV = ["open", "high", "low", "close", "volume"]


class KronosUnavailable(RuntimeError):
    """Kronos or torch is not installed, or the weights could not be fetched."""


def load_kronos(
    *,
    model_name: str = "NeoQuasar/Kronos-small",
    tokenizer_name: str = "NeoQuasar/Kronos-Tokenizer-base",
    max_context: int = 512,
    device: Optional[str] = None,
    kronos_path: Optional[str] = None,
):
    """Build a `KronosPredictor`, or raise `KronosUnavailable` with the reason.

    `kronos_path` is the local clone of the Kronos repository; its `model`
    package is not pip-installable, so the path is prepended to `sys.path`.

    Device: CUDA when present, else CPU. Apple `mps` is deliberately never
    chosen — this model's attention calls `scaled_dot_product_attention` with
    dropout, which the MPS backend does not implement, so it crashes rather than
    running slowly.
    """
    import sys

    if kronos_path:
        if kronos_path not in sys.path:
            sys.path.insert(0, kronos_path)
    try:
        import torch
    except Exception as exc:
        raise KronosUnavailable(
            f"torch is not installed ({type(exc).__name__}). It is an optional "
            f"dependency: pip install torch"
        ) from exc
    try:
        from model import Kronos, KronosPredictor, KronosTokenizer
    except Exception as exc:
        raise KronosUnavailable(
            f"the Kronos `model` package is not importable ({type(exc).__name__}). "
            f"Clone github.com/shiyu-coder/Kronos and pass kronos_path=<clone>"
        ) from exc

    if device is None:
        device = "cuda:0" if torch.cuda.is_available() else "cpu"
    try:
        tokenizer = KronosTokenizer.from_pretrained(tokenizer_name)
        model = Kronos.from_pretrained(model_name)
    except Exception as exc:
        raise KronosUnavailable(
            f"could not load weights for {model_name}/{tokenizer_name} "
            f"({type(exc).__name__}). They download from huggingface.co on first "
            f"use, so this also fails when that host is unreachable"
        ) from exc
    log.info(f"Kronos loaded: {model_name} on {device}, max_context={max_context}")
    return KronosPredictor(model, tokenizer, device=device, max_context=max_context)


def _future_stamps(last: pd.Timestamp, n: int, freq) -> pd.Series:
    """Future bar timestamps.

    The model consumes calendar features (minute/hour/weekday/day/month), so
    these are real covariates, not labels — a business-day offset that ignores
    market holidays feeds it a slightly wrong calendar on long horizons. Passing
    an explicit `freq` is how a caller supplies a real trading calendar.
    """
    if callable(freq):
        return pd.Series([freq(last, i + 1) for i in range(n)])
    return pd.Series([last + freq * (i + 1) for i in range(n)])


def make_kronos_forecaster(
    predictor,
    *,
    n_paths: int = 200,
    temperature: float = 1.0,
    top_p: float = 1.0,
    top_k: int = 0,
    lookback: int = 512,
    batch_size: int = 25,
    freq=pd.offsets.BDay(1),
):
    """Wrap a loaded predictor as a `Forecaster`.

    `batch_size` chunks the `n_paths` copies so a large path count does not have
    to fit in memory at once; the chunks are concatenated before quantiles are
    taken, so the band is over all `n_paths` regardless of chunking.
    """
    if not (0.0 < top_p <= 1.0):
        raise ValueError(f"top_p must be in (0, 1]; got {top_p}")
    if n_paths < 2:
        raise ValueError(f"n_paths must be at least 2 to form a band; got {n_paths}")
    if n_paths < 100:
        log.warning(
            f"n_paths={n_paths}: a 90%% band's tail quantiles from fewer than "
            f"~100 paths are close to the sample min/max and will move "
            f"substantially between runs"
        )

    def forecast(bars: pd.DataFrame, horizon: int, level: float) -> Optional[Band]:
        try:
            h = max(1, int(horizon))
            window = bars.tail(int(lookback))
            if len(window) < 64:
                return None

            df = pd.DataFrame({
                "open": window["Open"].astype(float).to_numpy(),
                "high": window["High"].astype(float).to_numpy(),
                "low": window["Low"].astype(float).to_numpy(),
                "close": window["Close"].astype(float).to_numpy(),
                "volume": window["Volume"].astype(float).to_numpy(),
            })
            if not np.isfinite(df.to_numpy()).all():
                return None

            idx = pd.to_datetime(pd.Series(window.index))
            # Convert, never strip: `tz_localize(None)` keeps the wall clock, so
            # an ET-indexed frame would hand the model an hour-of-day feature
            # shifted by the UTC offset.
            if getattr(idx.dt, "tz", None) is not None:
                idx = idx.dt.tz_convert("UTC").dt.tz_localize(None)
            x_ts = idx.reset_index(drop=True)
            y_ts = _future_stamps(x_ts.iloc[-1], h, freq)

            finals: list[float] = []
            remaining = int(n_paths)
            while remaining > 0:
                k = min(int(batch_size), remaining)
                preds = predictor.predict_batch(
                    df_list=[df] * k,
                    x_timestamp_list=[x_ts] * k,
                    y_timestamp_list=[y_ts] * k,
                    pred_len=h,
                    T=float(temperature), top_k=int(top_k), top_p=float(top_p),
                    sample_count=1,          # >1 averages internally — see module docstring
                    verbose=False,
                )
                for p in preds:
                    v = float(p["close"].iloc[-1])
                    if math.isfinite(v) and v > 0:
                        finals.append(v)
                remaining -= k

            if len(finals) < max(10, n_paths // 4):
                # Too many paths came back unusable to trust a quantile.
                return None
            arr = np.asarray(finals, dtype=float)
            alpha = 1.0 - float(level)
            lo, hi = np.quantile(arr, [alpha / 2.0, 1.0 - alpha / 2.0])
            return Band(float(lo), float(hi), float(np.median(arr)))
        except Exception as exc:
            log.debug(f"kronos forecast failed ({type(exc).__name__}: {exc})")
            return None

    return forecast
