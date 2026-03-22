"""Weekly report: portfolio performance, signal summary, and risk alerts."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/Chicago")

_BUY_SIGNALS  = ("STRONG_BUY", "BUY")
_SELL_SIGNALS = ("STRONG_SELL", "SELL")


# ---------------------------------------------------------------------------
# Data generation
# ---------------------------------------------------------------------------

def generate_weekly_data(user_id: int) -> dict:
    """Pull all data needed for the weekly report from the DB."""
    from app.database import get_db

    now_ct = datetime.now(ET)
    week_end   = now_ct.date()
    week_start = week_end - timedelta(days=7)

    with get_db() as conn:
        user = conn.execute(
            "SELECT id, name FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if not user:
            return {}

        holdings = conn.execute(
            "SELECT * FROM holdings WHERE user_id = ?", (user_id,)
        ).fetchall()

        tickers = list({h["ticker"] for h in holdings})

        # ── Portfolio performance ──────────────────────────────────────────
        performance = []
        for h in holdings:
            ticker = h["ticker"]

            # Latest price in DB
            latest = conn.execute(
                """SELECT current_price FROM price_snapshots
                   WHERE ticker = ? ORDER BY fetched_at DESC LIMIT 1""",
                (ticker,),
            ).fetchone()

            # Price ~7 days ago (closest snapshot to week_start)
            week_ago = conn.execute(
                """SELECT current_price FROM price_snapshots
                   WHERE ticker = ? AND date(fetched_at) <= ?
                   ORDER BY fetched_at DESC LIMIT 1""",
                (ticker, week_start.isoformat()),
            ).fetchone()

            current_price = latest["current_price"] if latest else None
            old_price     = week_ago["current_price"] if week_ago else None

            week_chg_pct  = ((current_price - old_price) / old_price * 100) if (current_price and old_price) else None
            week_chg_usd  = ((current_price - old_price) * h["shares"])     if (current_price and old_price) else None

            cost           = h["avg_cost_basis"]
            unreal_pl_pct  = ((current_price - cost) / cost * 100) if (current_price and cost) else None
            unreal_pl_usd  = ((current_price - cost) * h["shares"]) if (current_price and cost) else None

            performance.append({
                "ticker":         ticker,
                "shares":         h["shares"],
                "cost_basis":     cost,
                "current_price":  current_price,
                "old_price":      old_price,
                "week_chg_pct":   week_chg_pct,
                "week_chg_usd":   week_chg_usd,
                "unreal_pl_pct":  unreal_pl_pct,
                "unreal_pl_usd":  unreal_pl_usd,
            })

        # Sort: worst week performance first, then best
        performance.sort(key=lambda x: (x["week_chg_pct"] or 0))

        # ── Signal summary (recommendations generated this week) ──────────
        week_recs = conn.execute(
            """SELECT ticker, signal, combined_score, generated_at
               FROM recommendations
               WHERE user_id = ? AND date(generated_at) >= ?
               ORDER BY generated_at""",
            (user_id, week_start.isoformat()),
        ).fetchall()

        buy_count  = sum(1 for r in week_recs if r["signal"] in _BUY_SIGNALS)
        sell_count = sum(1 for r in week_recs if r["signal"] in _SELL_SIGNALS)
        hold_count = sum(1 for r in week_recs if r["signal"] == "HOLD")

        # Strongest conviction: highest avg |score| that had a buy signal
        ticker_scores: dict[str, list[float]] = {}
        ticker_latest_signal: dict[str, str] = {}
        ticker_first_signal:  dict[str, str] = {}
        for r in week_recs:
            t = r["ticker"]
            ticker_scores.setdefault(t, []).append(r["combined_score"] or 0)
            ticker_latest_signal[t] = r["signal"]
            if t not in ticker_first_signal:
                ticker_first_signal[t] = r["signal"]

        strongest_buy  = None
        strongest_sell = None
        best_buy_score = -1
        best_sell_score = 1

        for t, scores in ticker_scores.items():
            avg = sum(scores) / len(scores)
            if ticker_latest_signal.get(t) in _BUY_SIGNALS and avg > best_buy_score:
                best_buy_score = avg
                strongest_buy  = t
            if ticker_latest_signal.get(t) in _SELL_SIGNALS and avg < best_sell_score:
                best_sell_score = avg
                strongest_sell  = t

        # Flipped signals: first signal this week != latest signal this week
        flipped = []
        for t in ticker_first_signal:
            first  = ticker_first_signal[t]
            latest = ticker_latest_signal[t]
            if first != latest:
                flipped.append({"ticker": t, "from_signal": first, "to_signal": latest})

        signal_summary = {
            "buy_count":    buy_count,
            "sell_count":   sell_count,
            "hold_count":   hold_count,
            "strongest_buy":  strongest_buy,
            "strongest_sell": strongest_sell,
            "flipped":      flipped,
        }

        # ── Risk alerts ───────────────────────────────────────────────────
        risk_alerts = []
        for p in performance:
            pl = p["unreal_pl_pct"]
            if pl is None:
                continue
            if pl <= -10:
                risk_alerts.append({
                    "ticker":   p["ticker"],
                    "type":     "near_stop_loss",
                    "pl_pct":   pl,
                    "current_price": p["current_price"],
                    "cost_basis":    p["cost_basis"],
                })
            elif pl >= 20:
                risk_alerts.append({
                    "ticker":   p["ticker"],
                    "type":     "near_take_profit",
                    "pl_pct":   pl,
                    "current_price": p["current_price"],
                    "cost_basis":    p["cost_basis"],
                })

    return {
        "user_id":        user_id,
        "user_name":      user["name"],
        "week_start":     week_start.strftime("%b %d"),
        "week_end":       week_end.strftime("%b %d, %Y"),
        "performance":    performance,
        "signal_summary": signal_summary,
        "risk_alerts":    risk_alerts,
    }


# ---------------------------------------------------------------------------
# Email formatting
# ---------------------------------------------------------------------------

def format_weekly_email(data: dict) -> tuple[str, str]:
    """Returns (subject, html_body) for the weekly report."""
    name       = data["user_name"]
    week_start = data["week_start"]
    week_end   = data["week_end"]
    perf       = data["performance"]
    sig        = data["signal_summary"]
    risks      = data["risk_alerts"]

    generated = datetime.now(ET).strftime("%I:%M %p CT")
    subject   = f"Weekly Report — {week_start}–{week_end} | {name}"

    perf_section   = _render_performance(perf)
    signal_section = _render_signal_summary(sig)
    risk_section   = _render_risk_alerts(risks)

    html = f"""<!DOCTYPE html>
<html><body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
                   max-width:700px;margin:0 auto;padding:20px;color:#1f2937;background:#ffffff">

  <div style="border-bottom:3px solid #16a34a;padding-bottom:14px;margin-bottom:24px">
    <h1 style="margin:0;font-size:22px;color:#111827">Weekly Portfolio Report</h1>
    <p style="margin:4px 0 0;color:#6b7280;font-size:13px">
      {week_start} &ndash; {week_end} &nbsp;&middot;&nbsp; {name} &nbsp;&middot;&nbsp; Generated {generated}
    </p>
  </div>

  {perf_section}
  {signal_section}
  {risk_section}

  <p style="color:#9ca3af;font-size:11px;margin-top:32px;border-top:1px solid #e5e7eb;padding-top:12px">
    Not financial advice. Scores combine price momentum (15%), technical indicators (30%),
    news sentiment (25%), analyst consensus (20%), and earnings (10%).
  </p>
</body></html>"""

    return subject, html


def _render_performance(perf: list[dict]) -> str:
    if not perf:
        return ""

    total_week_usd = sum(p["week_chg_usd"] or 0 for p in perf)
    total_unreal   = sum(p["unreal_pl_usd"] or 0 for p in perf)
    total_color    = "#16a34a" if total_week_usd >= 0 else "#dc2626"
    unreal_color   = "#16a34a" if total_unreal >= 0 else "#dc2626"

    rows = ""
    for p in perf:
        price_str = f"${p['current_price']:.2f}" if p["current_price"] else "—"
        wk_pct    = f"{p['week_chg_pct']:+.2f}%" if p["week_chg_pct"] is not None else "—"
        wk_usd    = f"${p['week_chg_usd']:+.2f}" if p["week_chg_usd"] is not None else "—"
        pl_pct    = f"{p['unreal_pl_pct']:+.2f}%" if p["unreal_pl_pct"] is not None else "—"
        pl_usd    = f"${p['unreal_pl_usd']:+.2f}" if p["unreal_pl_usd"] is not None else "—"

        wk_color = "#16a34a" if (p["week_chg_pct"] or 0) >= 0 else "#dc2626"
        pl_color = "#16a34a" if (p["unreal_pl_pct"] or 0) >= 0 else "#dc2626"
        no_hist  = p["old_price"] is None

        rows += f"""
        <tr>
          <td style="{_td}font-weight:700;font-size:15px">{p['ticker']}</td>
          <td style="{_td}">{price_str}</td>
          <td style="{_td};color:{wk_color};font-weight:600;font-family:monospace">
            {wk_pct}{"&nbsp;<span style='font-size:10px;color:#9ca3af'>(no prior data)</span>" if no_hist else ""}
          </td>
          <td style="{_td};color:{wk_color};font-family:monospace">{wk_usd}</td>
          <td style="{_td};color:{pl_color};font-weight:600;font-family:monospace">{pl_pct}</td>
          <td style="{_td};color:{pl_color};font-family:monospace">{pl_usd}</td>
        </tr>"""

    return f"""
    <div style="margin-bottom:28px">
      <h2 style="font-size:16px;color:#111827;margin-bottom:4px">Portfolio Performance</h2>
      <p style="font-size:12px;color:#6b7280;margin:0 0 12px">
        Week total: <strong style="color:{total_color}">${total_week_usd:+.2f}</strong>
        &nbsp;&middot;&nbsp;
        Total unrealized P&L: <strong style="color:{unreal_color}">${total_unreal:+.2f}</strong>
      </p>
      <table style="width:100%;border-collapse:collapse;font-size:13px">
        <thead><tr style="background:#f9fafb">
          <th style="{_th}">Ticker</th>
          <th style="{_th}">Price</th>
          <th style="{_th}">Week %</th>
          <th style="{_th}">Week $</th>
          <th style="{_th}">Total P&L %</th>
          <th style="{_th}">Total P&L $</th>
        </tr></thead>
        <tbody>{rows}</tbody>
      </table>
    </div>"""


def _render_signal_summary(sig: dict) -> str:
    total = sig["buy_count"] + sig["sell_count"] + sig["hold_count"]
    if total == 0:
        return """
    <div style="background:#f9fafb;border-radius:8px;padding:16px;margin-bottom:28px">
      <h2 style="font-size:16px;color:#111827;margin:0 0 6px">Signal Summary</h2>
      <p style="color:#6b7280;font-size:13px;margin:0">No analysis was run this week.</p>
    </div>"""

    buy_bar  = int(sig["buy_count"]  / total * 100)
    sell_bar = int(sig["sell_count"] / total * 100)
    hold_bar = int(sig["hold_count"] / total * 100)

    strongest_buy_html  = f'<span style="font-weight:700;color:#16a34a">{sig["strongest_buy"]}</span>' if sig["strongest_buy"]  else "—"
    strongest_sell_html = f'<span style="font-weight:700;color:#dc2626">{sig["strongest_sell"]}</span>' if sig["strongest_sell"] else "—"

    flipped_html = ""
    if sig["flipped"]:
        items = ""
        for f in sig["flipped"]:
            from_color = "#16a34a" if f["from_signal"] in _BUY_SIGNALS else ("#dc2626" if f["from_signal"] in _SELL_SIGNALS else "#6b7280")
            to_color   = "#16a34a" if f["to_signal"]   in _BUY_SIGNALS else ("#dc2626" if f["to_signal"]   in _SELL_SIGNALS else "#6b7280")
            items += f"""
            <span style="background:#f3f4f6;border-radius:6px;padding:4px 10px;font-size:12px;display:inline-block;margin:3px">
              <strong>{f['ticker']}</strong>
              <span style="color:{from_color}">{f['from_signal'].replace('_',' ')}</span>
              &rarr;
              <span style="color:{to_color}">{f['to_signal'].replace('_',' ')}</span>
            </span>"""
        flipped_html = f"""
        <div style="margin-top:12px">
          <p style="font-size:12px;color:#374151;font-weight:600;margin:0 0 6px">Signal flips this week:</p>
          {items}
        </div>"""

    return f"""
    <div style="background:#f9fafb;border-radius:8px;padding:16px;margin-bottom:28px">
      <h2 style="font-size:16px;color:#111827;margin:0 0 12px">Signal Summary</h2>
      <div style="display:flex;gap:24px;flex-wrap:wrap;margin-bottom:12px">
        <div style="text-align:center">
          <div style="font-size:26px;font-weight:700;color:#16a34a">{sig['buy_count']}</div>
          <div style="font-size:11px;color:#6b7280;text-transform:uppercase;letter-spacing:.5px">Buy signals</div>
        </div>
        <div style="text-align:center">
          <div style="font-size:26px;font-weight:700;color:#dc2626">{sig['sell_count']}</div>
          <div style="font-size:11px;color:#6b7280;text-transform:uppercase;letter-spacing:.5px">Sell signals</div>
        </div>
        <div style="text-align:center">
          <div style="font-size:26px;font-weight:700;color:#6b7280">{sig['hold_count']}</div>
          <div style="font-size:11px;color:#6b7280;text-transform:uppercase;letter-spacing:.5px">Hold signals</div>
        </div>
        <div style="border-left:1px solid #e5e7eb;padding-left:24px">
          <div style="font-size:12px;color:#374151;margin-bottom:4px">
            Strongest buy conviction: {strongest_buy_html}
          </div>
          <div style="font-size:12px;color:#374151">
            Strongest sell conviction: {strongest_sell_html}
          </div>
        </div>
      </div>
      <div style="background:#e5e7eb;border-radius:4px;height:8px;overflow:hidden;display:flex">
        <div style="width:{buy_bar}%;background:#16a34a;height:100%"></div>
        <div style="width:{hold_bar}%;background:#d1d5db;height:100%"></div>
        <div style="width:{sell_bar}%;background:#dc2626;height:100%"></div>
      </div>
      <p style="font-size:10px;color:#9ca3af;margin:4px 0 0">{buy_bar}% buy &nbsp;·&nbsp; {hold_bar}% hold &nbsp;·&nbsp; {sell_bar}% sell &nbsp;·&nbsp; {total} total signals across the week</p>
      {flipped_html}
    </div>"""


def _render_risk_alerts(risks: list[dict]) -> str:
    if not risks:
        return """
    <div style="background:#f0fdf4;border-left:4px solid #86efac;border-radius:4px;padding:12px 16px;margin-bottom:28px">
      <strong style="color:#166534;font-size:14px">No risk alerts this week</strong>
      <p style="color:#166534;font-size:12px;margin:4px 0 0">All positions are within normal range.</p>
    </div>"""

    items = ""
    for r in risks:
        is_stop    = r["type"] == "near_stop_loss"
        bg         = "rgba(220,38,38,.07)"   if is_stop else "rgba(245,158,11,.07)"
        border     = "#dc2626"               if is_stop else "#d97706"
        label      = "Stop-Loss Warning"     if is_stop else "Take-Profit Zone"
        detail     = f"Down {abs(r['pl_pct']):.1f}% from cost — stop-loss triggers at -15%." if is_stop \
                else f"Up {r['pl_pct']:.1f}% from cost — take-profit triggers at +25%."
        icon       = "⚠️" if is_stop else "✅"
        price_str  = f"${r['current_price']:.2f}" if r["current_price"] else "—"
        cost_str   = f"${r['cost_basis']:.2f}"    if r["cost_basis"]    else "—"

        items += f"""
        <div style="background:{bg};border-left:4px solid {border};border-radius:4px;
                    padding:12px 16px;margin-bottom:10px">
          <div style="font-weight:700;font-size:14px;color:#111827">{icon} {r['ticker']} &mdash; {label}</div>
          <div style="font-size:12px;color:#374151;margin-top:4px">
            {detail}
            &nbsp;|&nbsp; Current: {price_str} &nbsp;|&nbsp; Avg cost: {cost_str}
          </div>
        </div>"""

    return f"""
    <div style="margin-bottom:28px">
      <h2 style="font-size:16px;color:#111827;margin-bottom:12px">Risk Alerts</h2>
      {items}
    </div>"""


_td = "padding:10px 8px;border-bottom:1px solid #f3f4f6;vertical-align:middle;"
_th = "padding:8px;text-align:left;font-size:11px;color:#6b7280;font-weight:600;text-transform:uppercase;letter-spacing:.5px;"

_BUY_SIGNALS  = ("STRONG_BUY", "BUY")
_SELL_SIGNALS = ("STRONG_SELL", "SELL")
