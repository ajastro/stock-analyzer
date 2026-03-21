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


def format_morning_email(recs: list[dict]) -> tuple[str, str]:
    """Returns (subject, html_body) for the daily morning report.

    Always sends — includes buy signals, sell signals, and a hold summary.
    """
    date_str = datetime.now(ET).strftime("%A, %B %d, %Y")
    now_utc = datetime.now(ET).strftime("%I:%M %p CT")

    buys = [r for r in recs if r.get("signal") in _BUY_SIGNALS]
    sells = [r for r in recs if r.get("signal") in _SELL_SIGNALS]
    holds = [r for r in recs if r.get("signal") == "HOLD"]

    buy_count = len(buys)
    sell_count = len(sells)

    if buy_count == 0 and sell_count == 0:
        subject = f"Stock Analyzer — No Action Needed ({date_str})"
    elif buy_count > 0 and sell_count > 0:
        subject = f"Stock Analyzer — {buy_count} Buy, {sell_count} Sell ({date_str})"
    elif buy_count > 0:
        subject = f"Stock Analyzer — {buy_count} Buy Signal{'s' if buy_count > 1 else ''} ({date_str})"
    else:
        subject = f"Stock Analyzer — {sell_count} Sell Alert{'s' if sell_count > 1 else ''} ({date_str})"

    buy_section = _render_buy_section(buys)
    sell_section = _render_sell_section(sells)
    hold_section = _render_hold_summary(holds)

    html = f"""<!DOCTYPE html>
<html><body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
                   max-width:680px;margin:0 auto;padding:20px;color:#1f2937">
  <h2 style="margin-bottom:4px">Daily Report — {date_str}</h2>
  <p style="color:#6b7280;font-size:13px;margin-top:0">
    Market opens 8:30 AM CT &nbsp;·&nbsp; Generated {now_utc}
  </p>
  {buy_section}
  {sell_section}
  {hold_section}
  <p style="color:#9ca3af;font-size:11px;margin-top:32px;border-top:1px solid #e5e7eb;padding-top:12px">
    Not financial advice. Scores combine price momentum (20%), technical indicators (40%),
    and news sentiment (40%).
  </p>
</body></html>"""

    return subject, html


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


_td = "padding:10px 8px;border-bottom:1px solid #f3f4f6;vertical-align:top;"
_th = "padding:8px;text-align:left;font-size:12px;color:#374151;"
