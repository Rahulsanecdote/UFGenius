"""Warrants, rights and units out of discovery — without losing any stock.

Fixtures are the real Alpaca asset names from the 2026-09-30 audit of all
14,389 active US equity assets. The first cut of the rule flagged every
partnership whose name contains "units", which would have dropped Energy
Transfer and Plains All American from discovery with nothing to show for it;
those names are pinned here so the rule cannot regress onto them.
"""

from __future__ import annotations

import pytest

import src.utils.config as cfg
from src.data import security_type as st

# ---------------------------------------------------------------- the rule --

STOCK = {
    "ET": "Energy Transfer LP Common Units representing limited partner interests",
    "PAA": "Plains All American Pipeline, L.P. Common Units representing Limited Partner Interests",
    "ARLP": "Alliance Resource Partners, L.P. Common Units representing Limited Partners Interest",
    "CAPL": "CrossAmerica Partners LP Common units representing limited partner interests",
    "PAGP": "Plains GP Holdings, L.P. Class A Units representing Limited Partner Interests",
    "USDP": "USD PARTNERS LP COM UNIT REPSTG LTD PARTNER INTS",
    "UNG": "United States Natural Gas Fund, LP Unit",
    "GAVA": "Grayscale Avalanche Staking ETF Common Units of Fractional Undivided Beneficial Interest",
    "BEP.PRA": "Brookfield Renewable Partners L.P. 5.25% Class A Preferred Limited Partnership Units",
    "NIVF": "NewGenIvf Group Limited Class A Ordinary Shares",
    "RETO": "ReTo Eco-Solutions, Inc. Class A Shares",
    "FNGR": "FingerMotion, Inc. Common Stock",
    "SNOW": "Snowflake Inc.",
    "UAL": "United Airlines Holdings, Inc. Common Stock",
    "EPD": "Enterprise Products Partners L.P.",
}
DERIVATIVE = {
    "NIVFW": "NewGenIvf Group Limited Warrants",
    "ASTLW": "Algoma Steel Group Inc. Warrant",
    "ASGI.RT": "abrdn Global Infrastructure Income Fund Rights (expiring October 2026)",
    "OPENZ": "Opendoor Technologies Inc Series Z Warrants, each whole warrant exercisable",
    "TGE.WS": "The Generation Essentials Group Warrants, each whole warrant exercisable",
    "AACPR": "Apogee Acquisition Corp Rights",
    "HCVIU": "HENNESSY CAP INVT CORP VI UNIT 1 CL A & 1/3 WT",
    "DMYYU": "DMY SQUARED TECHNOLOGY GROUP INC UNIT 1 CL A COM & 1/2 WT EXP",
    "AMACU": "AMR Resources Acquisition Corp Units",
    "ATIIU": "Archimedes Tech SPAC Partners II Co. Unit",
    "FLDDU": "FTAC EMERALD ACQUISITION CORP Unit   10/19/2028",
    "PRSTW": "PRESTO AUTOMATION INC Warrant   09/21/2027",
}


@pytest.mark.parametrize("symbol,name", sorted(STOCK.items()))
def test_stock_is_never_called_a_derivative(symbol, name):
    assert st.name_is_derivative(name) is False, symbol


@pytest.mark.parametrize("symbol,name", sorted(DERIVATIVE.items()))
def test_warrants_rights_and_spac_units_are_caught(symbol, name):
    assert st.name_is_derivative(name) is True, symbol


def test_partnership_units_are_the_regression_this_file_exists_for():
    """ET and PAA were flagged by the first cut. They are among the most
    liquid names in the market; dropping them would be invisible."""
    assert not st.name_is_derivative(STOCK["ET"])
    assert not st.name_is_derivative(STOCK["PAA"])


def test_a_suffix_rule_alone_would_have_been_wrong_both_ways():
    """Why the name is primary: OPENZ is a warrant a W/R/U suffix misses,
    and SNOW ends in W and is Snowflake."""
    assert st._SUFFIX_RE.search("OPENZ") is None and st.name_is_derivative(DERIVATIVE["OPENZ"])
    assert not st.name_is_derivative(STOCK["SNOW"])


@pytest.mark.parametrize("junk", [None, "", 123, object()])
def test_junk_names_never_raise(junk):
    assert st.name_is_derivative(junk) is False


# ------------------------------------------------------ fetch, cache, backoff --

class _Resp:
    def __init__(self, rows, ok=True):
        self._rows, self.ok = rows, ok

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError("HTTP 500")

    def json(self):
        return self._rows


class _Session:
    def __init__(self, rows=None, fail=False):
        self.calls = 0
        self.rows = rows if rows is not None else [
            {"symbol": s, "name": n} for s, n in {**STOCK, **DERIVATIVE}.items()]
        self.fail = fail

    def get(self, url, **kw):
        self.calls += 1
        assert url.endswith("/v2/assets")
        return _Resp(self.rows, ok=not self.fail)


class _Cache(dict):
    def get(self, key, default=None):          # noqa: D401 - cache API shape
        return super().get(key, default)

    def set(self, key, value, ttl=None):
        self[key] = value


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(st, "_retry_after", 0.0)
    monkeypatch.setattr(st, "_last_fetch_ok", False)
    monkeypatch.setattr(st, "cache", _Cache())


def _keys(monkeypatch):
    monkeypatch.setattr(cfg, "ALPACA_API_KEY", "k")
    monkeypatch.setattr(cfg, "ALPACA_SECRET_KEY", "s")


def test_with_the_asset_list_it_decides_by_name(monkeypatch):
    _keys(monkeypatch)
    sess = _Session()
    monkeypatch.setattr(st, "get_retry_session", lambda: sess)
    assert st.is_derivative("NIVFW") is True
    assert st.is_derivative("NIVF") is False
    assert st.is_derivative("ET") is False
    assert st.last_fetch_ok() is True


def test_the_list_is_fetched_once_then_cached(monkeypatch):
    _keys(monkeypatch)
    sess = _Session()
    monkeypatch.setattr(st, "get_retry_session", lambda: sess)
    for sym in ("NIVF", "NIVFW", "ET", "OPENZ", "PAA"):
        st.is_derivative(sym)
    assert sess.calls == 1


def test_without_credentials_it_never_touches_the_cache_or_network(monkeypatch):
    """Credentials first: a keyless caller — every unit test — must not read a
    map some live run left on disk."""
    poisoned = _Cache({st._CACHE_KEY: {"NIVF": True}})
    monkeypatch.setattr(st, "cache", poisoned)
    monkeypatch.setattr(st, "get_retry_session",
                        lambda: (_ for _ in ()).throw(AssertionError("network touched")))
    assert st.is_derivative("NIVF") is False       # suffix fallback, not the poisoned map
    assert st.is_derivative("NIVFW") is True       # suffix fallback still works
    assert st.last_fetch_ok() is False


def test_an_unlisted_symbol_falls_back_to_the_suffix(monkeypatch):
    _keys(monkeypatch)
    monkeypatch.setattr(st, "get_retry_session", lambda: _Session())
    assert st.is_derivative("ZZZZW") is True       # not in the map
    assert st.is_derivative("ZZZZ") is False


def test_a_failed_fetch_backs_off_instead_of_retrying_every_call(monkeypatch):
    _keys(monkeypatch)
    sess = _Session(fail=True)
    monkeypatch.setattr(st, "get_retry_session", lambda: sess)
    for _ in range(5):
        st.is_derivative("NIVF")
    assert sess.calls == 1
    assert st.last_fetch_ok() is False


def test_an_empty_asset_list_is_a_failure_not_an_answer(monkeypatch):
    _keys(monkeypatch)
    monkeypatch.setattr(st, "get_retry_session", lambda: _Session(rows=[]))
    st.is_derivative("NIVF")
    assert st.last_fetch_ok() is False


@pytest.mark.parametrize("junk", [None, "", "   "])
def test_empty_symbols_are_not_derivatives(junk):
    assert st.is_derivative(junk) is False
