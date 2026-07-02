import os
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/Chicago")

_BUY_SIGNALS = ("STRONG_BUY", "BUY")
_SELL_SIGNALS = ("STRONG_SELL", "SELL")


def is_configured() -> bool:
    return bool(os.environ.get("RESEND_API_KEY") and os.environ.get("ALERT_EMAIL"))


def send_email(subject: str, html_body: str) -> None:
    import resend
    resend.api_key = os.environ.get("RESEND_API_KEY", "")
    alert_email = os.environ.get("ALERT_EMAIL", "")
    from_email = os.environ.get("RESEND_FROM_EMAIL", "onboarding@resend.dev")

    if not resend.api_key or not alert_email:
        raise RuntimeError("Email not configured. Set RESEND_API_KEY and ALERT_EMAIL.")

    resend.Emails.send({
        "from": from_email,
        "to": [alert_email],
        "subject": subject,
        "html": html_body,
    })


def format_morning_email(
    recs: list[dict],
    holdings_tickers: set[str] | None = None,
    screener_results: list[dict] | None = None,
    decision: dict | None = None,
) -> tuple[str, str]:
    """Returns (subject, html_body) for the daily morning report.

    When an AI daily decision is available it leads the email; the formula
    sections follow as supporting detail. Always sends.
    """
    date_str = datetime.now(ET).strftime("%A, %B %d, %Y")
    now_utc = datetime.now(ET).strftime("%I:%M %p CT")

    # Split into portfolio vs watchlist if holdings_tickers provided
    if holdings_tickers is not None:
        portfolio_recs  = [r for r in recs if r.get("ticker") in holdings_tickers]
        watchlist_recs  = [r for r in recs if r.get("ticker") not in holdings_tickers]
    else:
        portfolio_recs  = recs
        watchlist_recs  = []

    buys  = [r for r in portfolio_recs if r.get("signal") in _BUY_SIGNALS]
    sells = [r for r in portfolio_recs if r.get("signal") in _SELL_SIGNALS]
    holds = [r for r in portfolio_recs if r.get("signal") == "HOLD"]

    watchlist_buys = [r for r in watchlist_recs if r.get("signal") in _BUY_SIGNALS]

    buy_count  = len(buys)
    sell_count = len(sells)
    wl_count   = len(watchlist_buys)

    # Subject: prefer the AI decision counts when available
    if decision is not None:
        d_buys, d_sells = len(decision.get("buys", [])), len(decision.get("sells", []))
        if d_buys == 0 and d_sells == 0:
            subject = f"Stock Analyzer — AI: No Action Today ({date_str})"
        else:
            parts = []
            if d_buys:
                parts.append(f"{d_buys} Buy")
            if d_sells:
                parts.append(f"{d_sells} Sell")
            subject = f"Stock Analyzer — AI: {', '.join(parts)} ({date_str})"
    elif buy_count == 0 and sell_count == 0 and wl_count == 0:
        subject = f"Stock Analyzer — No Action Needed ({date_str})"
    elif buy_count > 0 and sell_count > 0:
        subject = f"Stock Analyzer — {buy_count} Buy, {sell_count} Sell ({date_str})"
    elif buy_count > 0:
        subject = f"Stock Analyzer — {buy_count} Buy Signal{'s' if buy_count > 1 else ''} ({date_str})"
    elif sell_count > 0:
        subject = f"Stock Analyzer — {sell_count} Sell Alert{'s' if sell_count > 1 else ''} ({date_str})"
    else:
        subject = f"Stock Analyzer — {wl_count} Watchlist Opportunit{'ies' if wl_count > 1 else 'y'} ({date_str})"

    decision_section  = _render_decision_section(decision)
    buy_section       = _render_buy_section(buys)
    sell_section      = _render_sell_section(sells)
    hold_section      = _render_hold_summary(holds)
    watchlist_section = _render_watchlist_section(watchlist_buys)
    discover_section  = _render_discover_section(screener_results or [])

    html = f"""<!DOCTYPE html>
<html><body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
                   max-width:680px;margin:0 auto;padding:20px;color:#1f2937">
  <h2 style="margin-bottom:4px">Daily Report — {date_str}</h2>
  <p style="color:#6b7280;font-size:13px;margin-top:0">
    Market opens 8:30 AM CT &nbsp;·&nbsp; Generated {now_utc}
  </p>
  {decision_section}
  {buy_section}
  {sell_section}
  {hold_section}
  {watchlist_section}
  {discover_section}
  <p style="color:#9ca3af;font-size:11px;margin-top:32px;border-top:1px solid #e5e7eb;padding-top:12px">
    Not financial advice. Scores combine price momentum (15%), technical indicators (30%),
    news sentiment (25%), analyst consensus (20%), and earnings (10%).
    Discover section uses lightweight screening (price momentum + cached sentiment only).
  </p>
</body></html>"""

    return subject, html


_CONVICTION_COLORS = {"HIGH": "#16a34a", "MEDIUM": "#22c55e", "LOW": "#86efac"}
_URGENCY_COLORS = {"HIGH": "#dc2626", "MEDIUM": "#ef4444", "LOW": "#fca5a5"}


def _render_decision_section(decision: dict | None) -> str:
    """Lead section of the email: the single daily AI portfolio decision."""
    if decision is None:
        return ""

    buys = decision.get("buys", [])
    sells = decision.get("sells", [])
    summary = decision.get("summary", "")

    buy_rows = ""
    for b in buys:
        color = _CONVICTION_COLORS.get(b.get("conviction", ""), "#22c55e")
        buy_rows += f"""
        <tr>
          <td style="{_td}font-weight:600;font-size:15px">{b['ticker']}</td>
          <td style="{_td}">
            <span style="background:{color};color:white;padding:3px 8px;border-radius:4px;
                         font-size:11px;font-weight:600">BUY · {b.get('conviction', '')}</span>
          </td>
          <td style="{_td}">${b.get('allocation_usd', 0):.2f}</td>
          <td style="{_td};font-size:12px;color:#374151">{b.get('reason', '')}</td>
        </tr>"""

    sell_rows = ""
    for s in sells:
        color = _URGENCY_COLORS.get(s.get("urgency", ""), "#ef4444")
        sell_rows += f"""
        <tr>
          <td style="{_td}font-weight:600;font-size:15px">{s['ticker']}</td>
          <td style="{_td}">
            <span style="background:{color};color:white;padding:3px 8px;border-radius:4px;
                         font-size:11px;font-weight:600">SELL · {s.get('urgency', '')}</span>
          </td>
          <td style="{_td};font-size:12px;color:#374151" colspan="2">{s.get('reason', '')}</td>
        </tr>"""

    if buy_rows or sell_rows:
        table = f"""
      <table style="width:100%;border-collapse:collapse;margin-top:10px">
        <thead><tr style="background:#f5f3ff">
          <th style="{_th}">Ticker</th>
          <th style="{_th}">Action</th>
          <th style="{_th}">Allocate</th>
          <th style="{_th}">Why</th>
        </tr></thead>
        <tbody>{buy_rows}{sell_rows}</tbody>
      </table>"""
    else:
        table = """
      <p style="font-size:13px;color:#374151;margin:10px 0 0">
        <strong>No trades recommended today</strong> — hold everything.
      </p>"""

    holds = decision.get("holds", [])
    holds_line = ""
    if holds:
        holds_line = f"""
      <p style="font-size:12px;color:#6b7280;margin:10px 0 0">
        Holding: {', '.join(h['ticker'] for h in holds)}
      </p>"""

    return f"""
    <div style="background:#faf5ff;border:1px solid #ddd6fe;border-radius:8px;
                padding:16px;margin:16px 0">
      <h3 style="color:#6d28d9;margin:0 0 6px">
        🤖 Today's Decision
        <span style="font-weight:400;font-size:11px;color:#8b5cf6">Claude portfolio analysis</span>
      </h3>
      <p style="font-size:13px;color:#374151;margin:0">{summary}</p>
      {table}
      {holds_line}
    </div>"""


def _render_buy_section(buys: list[dict]) -> str:
    if not buys:
        return """
        <div style="background:#f0fdf4;border-left:4px solid #86efac;padding:12px 16px;
                    border-radius:4px;margin:16px 0">
          <strong style="color:#166534">No buy signals today</strong>
        </div>"""

    rows = ""
    for r in buys:
        is_strong = r.get("signal") == "STRONG_BUY"
        badge = "#16a34a" if is_strong else "#22c55e"
        price = f"${r['current_price']:.2f}" if r.get("current_price") else "—"
        score = f"{r['combined_score']:+.3f}"
        affordable = f"{r['affordable_shares']:.2f} shares" if r.get("affordable_shares") else "—"
        rows += f"""
        <tr>
          <td style="{_td}font-weight:600;font-size:15px">{r['ticker']}</td>
          <td style="{_td}">
            <span style="background:{badge};color:white;padding:3px 8px;border-radius:4px;
                         font-size:11px;font-weight:600">{r['signal']}</span>
          </td>
          <td style="{_td}">{price}</td>
          <td style="{_td};font-family:monospace">{score}</td>
          <td style="{_td}">{affordable}</td>
        </tr>"""

    return f"""
    <div style="margin:16px 0">
      <h3 style="color:#166534;margin-bottom:8px">Buy Signals</h3>
      <table style="width:100%;border-collapse:collapse">
        <thead><tr style="background:#f0fdf4">
          <th style="{_th}">Ticker</th>
          <th style="{_th}">Signal</th>
          <th style="{_th}">Price</th>
          <th style="{_th}">Score</th>
          <th style="{_th}">Affordable</th>
        </tr></thead>
        <tbody>{rows}</tbody>
      </table>
    </div>"""


def _render_sell_section(sells: list[dict]) -> str:
    if not sells:
        return """
        <div style="background:#fef2f2;border-left:4px solid #fca5a5;padding:12px 16px;
                    border-radius:4px;margin:16px 0">
          <strong style="color:#991b1b">No sell alerts today</strong>
        </div>"""

    rows = ""
    for r in sells:
        is_strong = r.get("signal") == "STRONG_SELL"
        badge = "#dc2626" if is_strong else "#ef4444"
        price = f"${r['current_price']:.2f}" if r.get("current_price") else "—"
        score = f"{r['combined_score']:+.3f}"
        gl = r.get("unrealized_gain_loss")
        gl_str = f"${gl:+.2f}" if gl is not None else "—"
        gl_color = "#16a34a" if (gl or 0) >= 0 else "#dc2626"

        # Show alert origin if this position was entered via a BUY alert
        origin = ""
        if r.get("alert_triggered") and r.get("alert_date"):
            origin = f'<br><span style="font-size:11px;color:#6b7280">Opened via BUY alert {r["alert_date"]}</span>'

        rows += f"""
        <tr>
          <td style="{_td}font-weight:600;font-size:15px">{r['ticker']}{origin}</td>
          <td style="{_td}">
            <span style="background:{badge};color:white;padding:3px 8px;border-radius:4px;
                         font-size:11px;font-weight:600">{r['signal']}</span>
          </td>
          <td style="{_td}">{price}</td>
          <td style="{_td};font-family:monospace">{score}</td>
          <td style="{_td};color:{gl_color};font-weight:600">{gl_str}</td>
        </tr>"""

    return f"""
    <div style="margin:16px 0">
      <h3 style="color:#991b1b;margin-bottom:8px">Sell Alerts</h3>
      <table style="width:100%;border-collapse:collapse">
        <thead><tr style="background:#fef2f2">
          <th style="{_th}">Ticker</th>
          <th style="{_th}">Signal</th>
          <th style="{_th}">Price</th>
          <th style="{_th}">Score</th>
          <th style="{_th}">Unrealized P&L</th>
        </tr></thead>
        <tbody>{rows}</tbody>
      </table>
    </div>"""


def _render_hold_summary(holds: list[dict]) -> str:
    if not holds:
        return ""
    tickers = ", ".join(r["ticker"] for r in holds)
    return f"""
    <div style="background:#f9fafb;border-radius:6px;padding:10px 14px;margin:16px 0;
                font-size:13px;color:#6b7280">
      <strong>Holding ({len(holds)}):</strong> {tickers}
    </div>"""


def _render_watchlist_section(buys: list[dict]) -> str:
    if not buys:
        return ""

    rows = ""
    for r in buys:
        is_strong = r.get("signal") == "STRONG_BUY"
        badge     = "#16a34a" if is_strong else "#22c55e"
        price     = f"${r['current_price']:.2f}" if r.get("current_price") else "—"
        score     = f"{r['combined_score']:+.3f}"
        affordable = f"{r['affordable_shares']:.2f} shares" if r.get("affordable_shares") else "—"
        rows += f"""
        <tr>
          <td style="{_td}font-weight:600;font-size:15px">{r['ticker']}</td>
          <td style="{_td}">
            <span style="background:{badge};color:white;padding:3px 8px;border-radius:4px;
                         font-size:11px;font-weight:600">{r['signal'].replace('_', ' ')}</span>
          </td>
          <td style="{_td}">{price}</td>
          <td style="{_td};font-family:monospace">{score}</td>
          <td style="{_td}">{affordable}</td>
        </tr>"""

    return f"""
    <div style="margin:16px 0">
      <h3 style="color:#1d4ed8;margin-bottom:4px">Watchlist Opportunities</h3>
      <p style="font-size:12px;color:#6b7280;margin:0 0 8px">
        These are stocks on your watchlist (not yet owned) with buy signals today.
      </p>
      <table style="width:100%;border-collapse:collapse">
        <thead><tr style="background:#eff6ff">
          <th style="{_th}">Ticker</th>
          <th style="{_th}">Signal</th>
          <th style="{_th}">Price</th>
          <th style="{_th}">Score</th>
          <th style="{_th}">Affordable ($100)</th>
        </tr></thead>
        <tbody>{rows}</tbody>
      </table>
    </div>"""


def _render_discover_section(candidates: list[dict]) -> str:
    if not candidates:
        return ""

    rows = ""
    for c in candidates:
        is_strong = c.get("signal") == "STRONG_BUY"
        badge     = "#16a34a" if is_strong else "#22c55e"
        price     = f"${c['current_price']:.2f}" if c.get("current_price") else "—"
        score_str = f"{c['combined_score']:+.3f}"
        rows += f"""
        <tr>
          <td style="{_td}font-weight:600;font-size:15px">{c['ticker']}</td>
          <td style="{_td}">
            <span style="background:{badge};color:white;padding:3px 8px;border-radius:4px;
                         font-size:11px;font-weight:600">{c['signal'].replace('_', ' ')}</span>
          </td>
          <td style="{_td}">{price}</td>
          <td style="{_td};font-family:monospace">{score_str}</td>
        </tr>"""

    return f"""
    <div style="margin:16px 0">
      <h3 style="color:#7c3aed;margin-bottom:4px">Discover — Today's Opportunities</h3>
      <p style="font-size:12px;color:#6b7280;margin:0 0 8px">
        Top picks from the S&P 500 that you don't currently own or watch.
        Full analysis run at 6 AM CT — same scoring as your portfolio.
      </p>
      <table style="width:100%;border-collapse:collapse">
        <thead><tr style="background:#f5f3ff">
          <th style="{_th}">Ticker</th>
          <th style="{_th}">Signal</th>
          <th style="{_th}">Price</th>
          <th style="{_th}">Score</th>
        </tr></thead>
        <tbody>{rows}</tbody>
      </table>
    </div>"""


_td = "padding:10px 8px;border-bottom:1px solid #f3f4f6;vertical-align:top;"
_th = "padding:8px;text-align:left;font-size:12px;color:#374151;"
