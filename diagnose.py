#!/usr/bin/env python3
"""
UFGenius — Data Connectivity Diagnostic

Run this to check if yfinance can fetch market data on your machine:

    python diagnose.py

If this fails, try:
    1. pip install --upgrade yfinance
    2. pip install yfinance==0.2.54   (known-good version)
    3. Check your internet/VPN/firewall
"""

import re
import sys
import time


def main():
    print("\n🔍 UFGenius Data Diagnostic\n" + "=" * 50)

    # ── Check imports ────────────────────────────────────────────────────────
    print("\n1. Checking imports...")
    try:
        import pandas as pd
        print(f"   ✅ pandas {pd.__version__}")
    except ImportError:
        print("   ❌ pandas not installed — run: pip install pandas")
        return

    try:
        import yfinance as yf
        print(f"   ✅ yfinance {yf.__version__}")
    except ImportError:
        print("   ❌ yfinance not installed — run: pip install yfinance")
        return

    try:
        import numpy as np
        print(f"   ✅ numpy {np.__version__}")
    except ImportError:
        print("   ❌ numpy not installed — run: pip install numpy")

    # ── Check yfinance version ───────────────────────────────────────────────
    def _parse_ver(v: str) -> tuple:
        """Parse 'X.Y.Z...' safely, ignoring non-numeric suffixes (e.g. rc1, .post1)."""
        parts = v.split(".")
        def _int(s: str) -> int:
            m = re.match(r"\d+", s)
            return int(m.group()) if m else 0
        return (
            _int(parts[0]) if len(parts) > 0 else 0,
            _int(parts[1]) if len(parts) > 1 else 0,
            _int(parts[2]) if len(parts) > 2 else 0,
        )

    yf_ver = yf.__version__
    major, minor, patch = _parse_ver(yf_ver)
    print(f"\n2. yfinance version: {yf_ver}")
    if major >= 1:
        print("   ⚠️  yfinance 1.x detected — using curl_cffi backend")
        print("   If fetches fail, try: pip install yfinance==0.2.54")
    else:
        if minor >= 2 and patch >= 40:
            print("   ℹ️  yfinance 0.2.40+ — MultiIndex columns enabled")
        print("   ✅ Compatible version")

    # ── Test Ticker.history() (preferred method) ─────────────────────────────
    print("\n3. Testing yf.Ticker().history() (preferred method)...")
    test_symbols = ["AAPL", "SPY", "^VIX"]

    for symbol in test_symbols:
        start = time.time()
        try:
            t = yf.Ticker(symbol)
            df = t.history(period="5d", auto_adjust=True)
            elapsed = round(time.time() - start, 2)

            if df is not None and not df.empty:
                last_close = round(float(df["Close"].iloc[-1]), 2)
                print(f"   ✅ {symbol:6s} — {len(df)} bars, last close: ${last_close}, took {elapsed}s")
                print(f"      Columns: {list(df.columns)}")
            else:
                print(f"   ❌ {symbol:6s} — empty response ({elapsed}s)")
        except Exception as e:
            elapsed = round(time.time() - start, 2)
            print(f"   ❌ {symbol:6s} — error ({elapsed}s): {e}")

    # ── Test yf.download() (fallback method) ─────────────────────────────────
    print("\n4. Testing yf.download() (fallback method)...")
    for symbol in ["AAPL"]:
        start = time.time()
        try:
            df = yf.download(symbol, period="5d", progress=False, auto_adjust=True)
            elapsed = round(time.time() - start, 2)

            if df is not None and not df.empty:
                # Check for MultiIndex
                if isinstance(df.columns, pd.MultiIndex):
                    print("   ℹ️  MultiIndex columns detected (normal for yfinance ≥0.2.40)")
                    flat_cols = list(df.columns.get_level_values(0).unique())
                    print(f"      Level 0: {flat_cols}")
                    # Flatten for display
                    df.columns = df.columns.get_level_values(0)
                    if df.columns.duplicated().any():
                        df = df.loc[:, ~df.columns.duplicated()]

                last_close = round(float(df["Close"].iloc[-1]), 2)
                print(f"   ✅ {symbol:6s} — {len(df)} bars, last close: ${last_close}, took {elapsed}s")
            else:
                print(f"   ❌ {symbol:6s} — empty response ({elapsed}s)")
        except Exception as e:
            elapsed = round(time.time() - start, 2)
            print(f"   ❌ {symbol:6s} — error ({elapsed}s): {e}")

    # ── Test ticker info ────────────────────────────────────────────────────
    print("\n5. Testing yf.Ticker().info...")
    start = time.time()
    try:
        t = yf.Ticker("AAPL")
        info = t.info
        elapsed = round(time.time() - start, 2)
        if info and isinstance(info, dict):
            name = info.get("longName", info.get("shortName", "?"))
            mcap = info.get("marketCap", 0)
            print(f"   ✅ AAPL — {name}, Market Cap: ${mcap:,.0f}, took {elapsed}s")
        else:
            print(f"   ❌ AAPL — empty info response ({elapsed}s)")
    except Exception as e:
        elapsed = round(time.time() - start, 2)
        print(f"   ❌ AAPL — error ({elapsed}s): {e}")

    # ── Check for stale cache ───────────────────────────────────────────────
    print("\n6. Checking for stale cache...")
    from pathlib import Path
    cache_dir = Path(__file__).parent / "data"
    if cache_dir.exists():
        pkl_files = list(cache_dir.glob("*.pkl"))
        if pkl_files:
            print(f"   ⚠️  Found {len(pkl_files)} cached files in {cache_dir}")
            print(f"   To clear: delete all .pkl files in {cache_dir}")
            print("   Or visit: http://localhost:5001/api/clear-cache")
        else:
            print("   ✅ No stale cache files")
    else:
        print("   ✅ No cache directory")

    # ── Summary ─────────────────────────────────────────────────────────────
    print("\n" + "=" * 50)
    print("TROUBLESHOOTING:")
    print("  If all fetches fail:")
    print("    1. Check internet connection")
    print("    2. Disable VPN (Yahoo Finance blocks some VPNs)")
    print("    3. Try: pip install yfinance==0.2.54")
    print("    4. Clear cache: rm -rf data/*.pkl")
    print("  If only some fail:")
    print("    5. Yahoo Finance may be rate-limiting you — wait 5 min")
    print("    6. Try again outside market hours (less congestion)")
    print("=" * 50 + "\n")


if __name__ == "__main__":
    main()
