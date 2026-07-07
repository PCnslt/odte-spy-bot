"""Intraday SPY session chart with the bot's events marked on it.

Turns "4 anomaly halts at 13:19/13:21/13:58/14:17" into a picture: the day's SPY price line
with a marker wherever the bot did or saw something — anomaly HALTs, gap guards, and trade
opens/closes. Read-only; renders a self-contained SVG for the dashboard.

Data sources (all local, no new entitlements):
  * SPY intraday: logs/spy_intraday_YYYYMMDD.csv — pulled from IBKR at EOD (Polygon Starter
    won't serve same-day SPY). `--pull-spy` writes it.
  * Events: the day's session log (ANOMALY / GAP GUARD lines) + trades.db (opens/closes).

    python -m src.session_chart --pull-spy                # EOD: save today's SPY bars
    python -m src.session_chart --svg > /tmp/spy.svg      # render today's chart
"""
from __future__ import annotations

import argparse
import re
import sqlite3
from datetime import date, datetime
from pathlib import Path

ET = "America/New_York"
COL = {"spy": "#45c4b8", "halt": "#e0533c", "gap": "#d9a430",
       "open": "#49b65f", "close": "#6ea8ff", "grid": "#27323f",
       "axis": "#5b6773", "ref": "#3a4655"}
SESSION_MIN = 390.0  # 09:30 -> 16:00


def _mins(dt) -> float:
    """Minutes since 09:30 ET for an ET-localized datetime-like."""
    return (dt.hour - 9) * 60 + dt.minute - 30 + dt.second / 60.0


# --- data loading --------------------------------------------------------------------------
def load_spy(csv_path: str) -> list[tuple[float, float]]:
    """[(minutes_since_open, close)] from the saved SPY intraday CSV. [] if missing."""
    p = Path(csv_path)
    if not p.exists():
        return []
    import pandas as pd
    df = pd.read_csv(p, index_col=0)
    idx = pd.to_datetime(df.index, utc=True, errors="coerce").tz_convert(ET)
    out = []
    for ts, close in zip(idx, df["close"]):
        if ts is not None and not pd.isna(close):
            out.append((_mins(ts), float(close)))
    return [pt for pt in out if 0 <= pt[0] <= SESSION_MIN]


def parse_log_events(log_text: str) -> list[tuple[float, str, str]]:
    """[(minutes_since_open, type, label)] for ANOMALY halts and GAP guards in a day's log."""
    events = []
    for m in re.finditer(r"(\d{2}):(\d{2}):(\d{2})\D.*?ANOMALY \[([^\]]*)\]", log_text):
        h, mm, _s, kinds = int(m[1]), int(m[2]), int(m[3]), m[4]
        label = ", ".join(k.strip().strip("'\"").replace("_", " ").title()
                           for k in kinds.split(",") if k.strip())
        events.append(((h - 9) * 60 + mm - 30, "halt", f"HALT · {label} · {h:02d}:{mm:02d}"))
    for m in re.finditer(r"(\d{2}):(\d{2}):(\d{2})\D.*?GAP GUARD", log_text):
        h, mm = int(m[1]), int(m[2])
        events.append(((h - 9) * 60 + mm - 30, "gap", f"GAP GUARD · {h:02d}:{mm:02d}"))
    return events


def tail_activity(log_text: str, n: int = 30) -> list[tuple[str, str]]:
    """Human-readable tail of a session log as [(HH:MM:SS, message)] — the bot's narrative
    (starts, health-checks, entries, halts, skips, session end), with ib_insync noise dropped."""
    out: list[tuple[str, str]] = []
    for ln in log_text.splitlines():
        s = ln.strip()
        if not s or "ib_insync" in ln or "Warning " in ln:
            continue
        m = re.match(r"\d{4}-\d{2}-\d{2} (\d{2}:\d{2}:\d{2})\D+?(?:alert: \[\w+\] |\| )?(.+)", ln)
        if m:
            out.append((m.group(1), m.group(2).strip()))
        elif re.match(r"^\d{2}:\d{2} ", s):        # the runner's own "09:25 ..." echoes
            out.append((s[:5], s[6:]))
        elif s.startswith("==="):                  # section banners
            out.append(("", s.strip("= ")))
    return out[-n:]


def trade_events(db_path: str, day: date) -> list[tuple[float, str, str]]:
    """Opens/closes from trades.db for `day` as chart events."""
    if not Path(db_path).exists():
        return []
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT opened_at, closed_at, kind, pnl FROM trades WHERE opened_at LIKE ?",
            (f"{day.isoformat()}%",))]
    except sqlite3.OperationalError:
        rows = []
    finally:
        conn.close()
    ev = []
    for r in rows:
        try:
            o = datetime.fromisoformat(r["opened_at"])
            ev.append((_mins(o), "open", f"OPEN {r['kind']} · {o:%H:%M}"))
        except Exception:
            pass
        if r["closed_at"]:
            try:
                c = datetime.fromisoformat(r["closed_at"])
                pnl = r["pnl"] or 0.0
                ev.append((_mins(c), "close", f"CLOSE {r['kind']} · ${pnl:+.0f} · {c:%H:%M}"))
            except Exception:
                pass
    return ev


# --- rendering (pure) ----------------------------------------------------------------------
def render_svg(spy: list[tuple[float, float]], events: list[tuple[float, str, str]],
               width: int = 760, height: int = 280) -> str:
    """Self-contained SVG: SPY line + event markers. '' if no SPY data."""
    if not spy:
        return ""
    L, R, T, B = 46, 16, 16, 28
    ps = [p for _, p in spy]
    lo, hi = min(ps), max(ps)
    span = (hi - lo) or 1.0
    lo -= span * 0.08
    hi += span * 0.08
    span = hi - lo

    def X(m):
        return L + (width - L - R) * max(0.0, min(m, SESSION_MIN)) / SESSION_MIN

    def Y(p):
        return height - B - (height - T - B) * (p - lo) / span

    def price_at(m):
        return min(spy, key=lambda kp: abs(kp[0] - m))[1]

    open_px = spy[0][1]
    line = " ".join(f"{X(m):.1f},{Y(p):.1f}" for m, p in spy)

    # gridlines + time ticks (09:30, 12:00, 15:30, 16:00) + price min/max
    grid = []
    for m, lbl in [(0, "9:30"), (150, "12:00"), (360, "15:30"), (390, "16:00")]:
        x = X(m)
        grid.append(f'<line x1="{x:.1f}" y1="{T}" x2="{x:.1f}" y2="{height-B}" '
                    f'stroke="{COL["grid"]}" stroke-width="1"/>')
        grid.append(f'<text x="{x:.1f}" y="{height-9}" fill="{COL["axis"]}" font-size="10" '
                    f'font-family="ui-monospace,Menlo,monospace" text-anchor="middle">{lbl}</text>')
    real_lo, real_hi = min(ps), max(ps)
    for p in (real_lo, real_hi):
        grid.append(f'<text x="{L-6}" y="{Y(p)+3:.1f}" fill="{COL["axis"]}" font-size="10" '
                    f'font-family="ui-monospace,Menlo,monospace" text-anchor="end">{p:.2f}</text>')
    ref = (f'<line x1="{L}" y1="{Y(open_px):.1f}" x2="{width-R}" y2="{Y(open_px):.1f}" '
           f'stroke="{COL["ref"]}" stroke-width="1" stroke-dasharray="3 3"/>')

    marks = []
    for m, typ, label in sorted(events):
        x, y = X(m), Y(price_at(m))
        c = COL.get(typ, COL["halt"])
        marks.append(
            f'<g><title>{label}</title>'
            f'<line x1="{x:.1f}" y1="{T}" x2="{x:.1f}" y2="{height-B}" stroke="{c}" '
            f'stroke-width="1" stroke-dasharray="2 3" opacity="0.55"/>'
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="{c}" stroke="#0e141b" '
            f'stroke-width="1.5"/></g>')

    n_halt = sum(1 for _, t, _ in events if t == "halt")
    n_open = sum(1 for _, t, _ in events if t == "open")
    legend = (f'<text x="{L}" y="{T+2}" fill="{COL["axis"]}" font-size="10.5" '
              f'font-family="ui-monospace,Menlo,monospace">'
              f'<tspan fill="{COL["spy"]}">— SPY</tspan>'
              f'<tspan dx="14" fill="{COL["halt"]}">● {n_halt} halt(s)</tspan>'
              f'<tspan dx="12" fill="{COL["open"]}">● {n_open} trade(s)</tspan></text>')

    return (f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" '
            f'style="width:100%;height:auto;display:block">'
            + "".join(grid) + ref
            + f'<polyline points="{line}" fill="none" stroke="{COL["spy"]}" stroke-width="1.7"/>'
            + "".join(marks) + legend + "</svg>")


def build_session_svg(day: date | None = None, log_dir: str = "logs",
                      db_path: str = "trades.db") -> str:
    """Assemble today's chart from local files. '' if the SPY series isn't available."""
    day = day or datetime.now().date()
    spy = load_spy(f"{log_dir}/spy_intraday_{day:%Y%m%d}.csv")
    if not spy:
        return ""
    log_path = Path(f"{log_dir}/daily_{day:%Y%m%d}.log")
    events = parse_log_events(log_path.read_text()) if log_path.exists() else []
    events += trade_events(db_path, day)
    return render_svg(spy, events)


# --- EOD SPY pull (IBKR) -------------------------------------------------------------------
def pull_spy(day: date | None = None, host="127.0.0.1", port=4002,
             out_dir="logs") -> str | None:
    """Save today's RTH SPY 1-min bars from IBKR (Gateway must be logged in). Path or None."""
    from .data.ibkr_feed import IBKRFeed
    day = day or datetime.now().date()
    feed = IBKRFeed(host=host, port=port, client_id=44, symbol="SPY")
    try:
        feed.connect()
        bars = feed.latest_bars(lookback_minutes=400)
        if bars.empty:
            return None
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        out = f"{out_dir}/spy_intraday_{day:%Y%m%d}.csv"
        bars.to_csv(out)
        return out
    except Exception:
        return None
    finally:
        try:
            feed.disconnect()
        except Exception:
            pass


def main() -> None:
    ap = argparse.ArgumentParser(description="SPY session chart / EOD SPY pull")
    ap.add_argument("--pull-spy", action="store_true", help="save today's SPY bars from IBKR")
    ap.add_argument("--svg", action="store_true", help="print today's chart SVG")
    ap.add_argument("--db", default="trades.db")
    a = ap.parse_args()
    if a.pull_spy:
        print(f"SPY intraday saved: {pull_spy()}")
    if a.svg:
        print(build_session_svg(db_path=a.db) or "<!-- no SPY data for today -->")


if __name__ == "__main__":
    main()
