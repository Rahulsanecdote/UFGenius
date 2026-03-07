"""
Read-only Robinhood portfolio integration via robin_stocks.

IMPORTANT: This module is READ-ONLY. No order placement is ever performed.
Automated trading via the Robinhood unofficial API violates their Terms of Service.
Use the signal output to manually enter trades in the Robinhood app.
"""

from src.utils import config
from src.utils.logger import get_logger

log = get_logger(__name__)


def get_portfolio_data(username: str | None = None, password: str | None = None) -> dict:
    """
    Fetch current portfolio holdings and buying power from Robinhood.

    Returns an empty dict with a warning if robin_stocks is not installed
    or credentials are not configured.

    This is STRICTLY READ-ONLY. No trading logic here.
    """
    if username is None:
        username = config.env("ROBINHOOD_USERNAME")
    if password is None:
        password = config.env("ROBINHOOD_PASSWORD")

    if not (username and password):
        log.warning("Robinhood credentials not configured — portfolio data unavailable")
        return {"error": "Credentials not set. Add ROBINHOOD_USERNAME and ROBINHOOD_PASSWORD to .env"}

    try:
        import robin_stocks.robinhood as r
    except ImportError:
        log.warning("robin_stocks not installed — run: pip install robin_stocks")
        return {"error": "robin_stocks not installed"}

    try:
        r.login(username, password)

        account  = r.profiles.load_account_profile() or {}
        portfolio = r.profiles.load_portfolio_profile() or {}
        positions = r.account.get_open_stock_positions() or []

        buying_power  = float(account.get("buying_power", 0))
        total_equity  = float(portfolio.get("equity", 0))

        holdings = []
        for pos in positions:
            try:
                instrument  = r.stocks.get_instrument_by_url(pos["instrument"])
                ticker      = instrument.get("symbol", "?")
                shares      = float(pos.get("quantity", 0))
                avg_cost    = float(pos.get("average_buy_price", 0))
                curr_price  = float(r.stocks.get_latest_price(ticker)[0] or 0)
                pnl         = (curr_price - avg_cost) * shares
                pnl_pct     = ((curr_price / avg_cost) - 1) * 100 if avg_cost > 0 else 0

                holdings.append({
                    "ticker":    ticker,
                    "shares":    shares,
                    "avg_cost":  avg_cost,
                    "current":   curr_price,
                    "pnl":       round(pnl, 2),
                    "pnl_pct":   round(pnl_pct, 2),
                })
            except Exception as e:
                log.debug(f"Position parse error: {e}")
                continue

        r.logout()

        return {
            "buying_power": buying_power,
            "total_equity": total_equity,
            "holdings":     holdings,
            "position_count": len(holdings),
            "note": "READ-ONLY. All trades must be entered manually in the Robinhood app.",
        }

    except Exception as e:
        log.error(f"Robinhood portfolio fetch error: {e}")
        return {"error": str(e)}
