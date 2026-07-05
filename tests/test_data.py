from __future__ import annotations

from datetime import date

import pytest

from src.data.polygon_options import PolygonError, PolygonOptions


def test_option_ticker_format():
    t = PolygonOptions.option_ticker(580.0, "C", date(2026, 7, 2))
    assert t == "O:SPY260702C00580000"
    assert PolygonOptions.option_ticker(500.5, "P", date(2026, 1, 5)) == "O:SPY260105P00500500"


def test_missing_api_key_raises():
    with pytest.raises(PolygonError):
        PolygonOptions(api_key="")


def test_list_and_nearest_contract(tmp_path, monkeypatch):
    poly = PolygonOptions(api_key="dummy", cache_dir=tmp_path)
    sample = [
        {"ticker": "O:SPY260702C00498000", "strike_price": 498, "contract_type": "call"},
        {"ticker": "O:SPY260702C00500000", "strike_price": 500, "contract_type": "call"},
        {"ticker": "O:SPY260702C00502000", "strike_price": 502, "contract_type": "call"},
        {"ticker": "O:SPY260702P00500000", "strike_price": 500, "contract_type": "put"},
    ]
    monkeypatch.setattr(poly, "_get", lambda url, params=None: sample)

    chain = poly.list_contracts(date(2026, 7, 2))
    assert set(chain["type"]) == {"C", "P"}

    near = poly.nearest_contract(date(2026, 7, 2), "C", spot=500.4)
    assert near["strike"] == 500.0
    near_otm = poly.nearest_contract(date(2026, 7, 2), "C", spot=500.4, strike_offset=1)
    assert near_otm["strike"] == 502.0


def test_option_bars_parsing(tmp_path, monkeypatch):
    poly = PolygonOptions(api_key="dummy", cache_dir=tmp_path)
    rows = [
        {"t": 1_751_000_000_000, "o": 1.0, "h": 1.2, "l": 0.9, "c": 1.1, "v": 500, "vw": 1.05},
        {"t": 1_751_000_060_000, "o": 1.1, "h": 1.3, "l": 1.0, "c": 1.25, "v": 800, "vw": 1.18},
    ]
    monkeypatch.setattr(poly, "_get", lambda url, params=None: rows)
    df = poly.option_bars("O:SPY260702C00500000", date(2026, 7, 2))
    assert list(df.columns) == ["open", "high", "low", "close", "volume", "vwap"]
    assert str(df.index.tz) == "UTC"
    assert df["close"].iloc[-1] == 1.25
