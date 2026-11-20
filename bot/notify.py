"""Morning and night summaries sent to your phone through a Discord webhook.

Run:  python -m bot.notify morning     (or: night)
Needs the DISCORD_WEBHOOK_URL secret. Without it, the message is just printed.
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from bot import config as C
from bot import ledger, report

ET = ZoneInfo("America/New_York")


def market_line():
    """Ask Alpaca whether the stock market opens today (optional, needs keys)."""
    try:
        from bot.alpaca import AlpacaPaper
        clock = AlpacaPaper(os.environ.get("ALPACA_API_KEY"), os.environ.get("ALPACA_SECRET_KEY")).clock()
        nxt = datetime.fromisoformat(clock["next_open"].replace("Z", "+00:00")).astimezone(ET)
        if clock["is_open"]:
            return "Stock market is open now."
        if nxt.date() == datetime.now(ET).date():
            return f"Stock market opens today at {nxt:%-I:%M %p} ET."
        return f"Stock market closed today (weekend/holiday). Next open {nxt:%a %b %-d}. BTC still trades."
    except Exception:
        return None


def build_message(kind, now=None):
    now = now or datetime.now(timezone.utc)
    state = ledger.load_state()
    perf = ledger.load_performance()
    trades = ledger.load_trades()
    m, p = report.money, report.pct

    title = "☀️ Morning update" if kind == "morning" else "🌙 Night update"
    lines = [f"**{title} — {now.astimezone(ET):%a %b %-d}** (paper money)"]

    if len(perf):
        last = perf.iloc[-1]
        equity = float(last["equity"])
        lines.append(f"Account: **{m(equity)}** ({p(equity / C.STARTING_CAPITAL - 1)} since start)")
        if str(last.get("spy_hold_value", "")) not in ("", "nan"):
            spy_val = float(last["spy_hold_value"])
            lines.append(f"Holding SPY instead: {m(spy_val)} ({p(spy_val / C.STARTING_CAPITAL - 1)}) → "
                         f"bot is **{report.versus(equity, spy_val)}** SPY")
        if kind == "night":
            lines.append(f"Today: {float(last['day_pnl_pct']):+.2f}%")
    else:
        lines.append("No runs recorded yet.")

    if state["day"].get("halted"):
        lines.append("⛔ Down 3% today — no new trades until tomorrow.")
    if os.environ.get("TRADING_PAUSED", "").lower() == "true":
        lines.append("⏸️ TRADING_PAUSED is on — no new trades.")

    # Trades since the last summary (about 12 hours)
    if len(trades):
        t = trades.copy()
        t["time"] = pd.to_datetime(t["time_utc"]).dt.tz_localize("UTC")
        recent = t[t["time"] >= now - timedelta(hours=13)]
        label = "overnight" if kind == "morning" else "today"
        if len(recent):
            lines.append(f"\n**Trades {label}:**")
            for _, r in recent.tail(12).iterrows():
                pnl = "" if pd.isna(r["realized_pnl"]) or r["realized_pnl"] == "" else f" → {m(float(r['realized_pnl']))}"
                lines.append(f"• {r['side'].upper()} {r['qty']:g} {r['symbol']} @ {m(float(r['price']))} ({r['reason']}){pnl}")
        else:
            lines.append(f"No trades {label}.")

    if state["positions"]:
        lines.append("\n**Open positions:**")
        for sym, pos in state["positions"].items():
            lines.append(f"• {sym}: {pos['qty']:g} @ {m(pos['entry_price'])}, stop {m(pos['stop_price'])}")
    else:
        lines.append("Open positions: none (all cash).")

    if kind == "morning":
        line = market_line()
        if line:
            lines.append(line)
    return "\n".join(lines)[:1990]  # Discord limit is 2000 characters


def send(text, image_path=None):
    url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not url:
        print("DISCORD_WEBHOOK_URL not set, message not sent:\n" + text)
        return False
    payload = {"content": text, "username": "Paper Bot #2"}
    if image_path and os.path.exists(image_path):
        with open(image_path, "rb") as f:
            resp = requests.post(url, data={"payload_json": json.dumps(payload)},
                                 files={"file": ("bot_vs_spy.png", f, "image/png")}, timeout=30)
    else:
        resp = requests.post(url, json=payload, timeout=30)
    resp.raise_for_status()
    return True


def main():
    kind = sys.argv[1] if len(sys.argv) > 1 else "night"
    report.write_summary()
    chart = report.make_live_chart()
    text = build_message(kind)
    print(text)
    send(text, chart if kind == "night" else None)


if __name__ == "__main__":
    main()
