"""
Read-only Alpaca portfolio integration.
"""

from src.utils import config
from src.utils.logger import get_logger

log = get_logger(__name__)


def get_portfolio_data() -> dict:
    """
    Fetch current portfolio holdings and buying power from Alpaca.

    Returns a dict with gracefully handled errors if alpaca-py is not installed
    or credentials are not configured.
    """
    api_key = config.env("ALPACA_API_KEY")
    api_secret = config.env("ALPACA_SECRET_KEY")
    is_paper = config.ALPACA_PAPER

    if not (api_key and api_secret):
        log.warning("Alpaca credentials not configured — portfolio data unavailable")
        return {"error": "Credentials not set. Add ALPACA_API_KEY and ALPACA_SECRET_KEY to .env"}

    try:
        from alpaca.trading.client import TradingClient
    except ImportError:
        log.warning("alpaca-py not installed — run: pip install alpaca-py")
        return {"error": "alpaca-py not installed"}

    try:
        trading_client = TradingClient(api_key, api_secret, paper=is_paper)
        
        # Load account details
        account = trading_client.get_account()
        buying_power = float(account.buying_power)
        total_equity = float(account.equity)

        # Load positions
        positions = trading_client.get_all_positions()
        
        holdings = []
        for pos in positions:
            try:
                ticker = pos.symbol
                shares = float(pos.qty)
                avg_cost = float(pos.avg_entry_price)
                curr_price = float(pos.current_price)
                
                # PNL Calculation
                pnl = float(pos.unrealized_pl)
                pnl_pct = float(pos.unrealized_plpc) * 100

                holdings.append({
                    "ticker":    ticker,
                    "shares":    shares,
                    "avg_cost":  round(avg_cost, 2),
                    "current":   round(curr_price, 2),
                    "pnl":       round(pnl, 2),
                    "pnl_pct":   round(pnl_pct, 2),
                })
            except Exception as e:
                log.debug(f"Position parse error: {e}")
                continue

        return {
            "buying_power": round(buying_power, 2),
            "total_equity": round(total_equity, 2),
            "holdings":     holdings,
            "position_count": len(holdings),
            "note": f"Broker: Alpaca ({'Paper' if is_paper else 'Live'}).",
        }

    except Exception as e:
        log.error(f"Alpaca portfolio fetch error: {e}")
        return {"error": str(e)}
