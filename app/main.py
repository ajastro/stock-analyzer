import os
import secrets
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from app.database import init_db
from app.routers import users, portfolio, budget, stocks, sentiment, recommendations, messaging, run_daily
from app.scheduler import start_scheduler, stop_scheduler

# --- Auth config (set these as env vars) ---
_DASHBOARD_USER = os.environ.get("DASHBOARD_USER", "admin")
_DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")
_API_SECRET_KEY = os.environ.get("API_SECRET_KEY", "")

_basic_auth = HTTPBasic()


def _require_dashboard_auth(credentials: HTTPBasicCredentials = Depends(_basic_auth)):
    """HTTP Basic Auth for the browser dashboard."""
    if not _DASHBOARD_PASSWORD:
        return  # auth disabled locally if no password set
    ok = (
        secrets.compare_digest(credentials.username.encode(), _DASHBOARD_USER.encode())
        and secrets.compare_digest(credentials.password.encode(), _DASHBOARD_PASSWORD.encode())
    )
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )


_BROWSER_PATHS = {"/", "/dashboard", "/healthz", "/docs", "/openapi.json", "/redoc"}
_BROWSER_PREFIXES = ("/messaging/preview/",)

def _require_api_key(request: Request):
    """Bearer token check for all API routes. Skips browser-facing paths."""
    path = request.url.path
    if not _API_SECRET_KEY or path in _BROWSER_PATHS or any(path.startswith(p) for p in _BROWSER_PREFIXES):
        return
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer ") or not secrets.compare_digest(
        auth_header[len("Bearer "):], _API_SECRET_KEY
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key. Set Authorization: Bearer <API_SECRET_KEY>",
        )


@asynccontextmanager
async def lifespan(application: FastAPI):
    init_db()
    start_scheduler()
    yield
    stop_scheduler()


_allowed_origins = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "*").split(",")]

app = FastAPI(
    title="Stock Analyzer",
    version="0.1.0",
    lifespan=lifespan,
    dependencies=[Depends(_require_api_key)],
    swagger_ui_init_oauth={},
    swagger_ui_parameters={"persistAuthorization": True},
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(users.router)
app.include_router(portfolio.router)
app.include_router(budget.router)
app.include_router(stocks.router)
app.include_router(sentiment.router)
app.include_router(recommendations.router)
app.include_router(messaging.router)
app.include_router(run_daily.router)


@app.get("/")
async def root():
    return RedirectResponse(url="/dashboard")


@app.get("/healthz")  # public — Railway uses this for health checks
async def healthz():
    return {"status": "ok"}


@app.get("/dashboard", response_class=HTMLResponse, dependencies=[Depends(_require_dashboard_auth)])
async def dashboard():
    """Mobile-friendly dashboard showing the latest recommendations for all users."""
    from app.database import get_db

    with get_db() as conn:
        users_rows = conn.execute(
            "SELECT id, name FROM users WHERE is_onboarded = 1"
        ).fetchall()

    if not users_rows:
        return HTMLResponse(_render_page("""
        <div class="card empty-state">
          <div class="empty-icon">📈</div>
          <h2>Welcome to Stock Analyzer</h2>
          <p style="margin-bottom:20px">Get started by creating your account below.</p>
          <button class="run-btn" onclick="openModal()">+ Add User</button>
        </div>""", _API_SECRET_KEY))

    sections = ""
    with get_db() as conn:
        for user in users_rows:
            latest_ts = conn.execute(
                "SELECT MAX(generated_at) as ts FROM recommendations WHERE user_id = ?",
                (user["id"],),
            ).fetchone()
            ts = latest_ts["ts"] if latest_ts else None

            recs = []
            if ts:
                recs = conn.execute(
                    """SELECT r.* FROM recommendations r
                       WHERE r.user_id = ? AND r.generated_at = ?
                       AND r.ticker IN (
                           SELECT ticker FROM holdings WHERE user_id = ?
                           UNION
                           SELECT ticker FROM watchlist WHERE user_id = ?
                       )
                       ORDER BY ABS(r.combined_score) DESC""",
                    (user["id"], ts, user["id"], user["id"]),
                ).fetchall()

            holdings_count = conn.execute(
                "SELECT COUNT(*) as cnt FROM holdings WHERE user_id = ?", (user["id"],)
            ).fetchone()["cnt"]

            sections += _render_user_section(user["id"], user["name"], ts, recs, holdings_count)

    return HTMLResponse(_render_page(sections, _API_SECRET_KEY))


def _plain_summary(rec) -> str:
    import re
    reason = rec["reason"] or ""
    parts = []

    # Price movement
    price_m = re.search(r'\(([+-][\d.]+)%\)', reason)
    if price_m:
        pct = float(price_m.group(1))
        if pct > 0.5:
            parts.append(f"Stock is up {pct:.1f}% today")
        elif pct < -0.5:
            parts.append(f"Stock is down {abs(pct):.1f}% today")

    # RSI
    rsi_m = re.search(r'RSI\(14\): ([\d.]+) \((\w+)\)', reason)
    if rsi_m:
        rsi, label = float(rsi_m.group(1)), rsi_m.group(2)
        if rsi < 30:
            parts.append(f"Heavily oversold (RSI {rsi:.0f}) — historically signals a bounce")
        elif rsi > 70:
            parts.append(f"Overbought (RSI {rsi:.0f}) — may be due for a pullback")
        else:
            parts.append(f"Neutral momentum (RSI {rsi:.0f})")

    # SMA trend
    sma_m = re.search(r'SMA20 .+ (above|below) SMA50', reason)
    if sma_m:
        parts.append("Short-term trend is upward" if sma_m.group(1) == "above" else "Short-term trend is downward")

    # Sentiment
    sent_m = re.search(r'(\d+)B/(\d+)S from (\d+) articles', reason)
    if sent_m:
        b, s, total = int(sent_m.group(1)), int(sent_m.group(2)), int(sent_m.group(3))
        if b > s * 1.5:
            parts.append(f"News is mostly positive ({b} bullish vs {s} bearish out of {total} articles)")
        elif s > b * 1.5:
            parts.append(f"News is mostly negative ({s} bearish vs {b} bullish out of {total} articles)")
        else:
            parts.append(f"News is mixed ({b} positive, {s} negative out of {total} articles)")

    # Analyst price target
    target_m = re.search(r'Price target \$(\d+) \(([+-][\d.]+)% upside, (\d+) analysts\)', reason)
    if target_m:
        target, upside, n = target_m.group(1), float(target_m.group(2)), target_m.group(3)
        if upside > 5:
            parts.append(f"{n} analysts have a ${target} target ({upside:+.0f}% upside from here)")
        elif upside < -5:
            parts.append(f"{n} analysts have a ${target} target ({upside:+.0f}% downside from here)")

    # Earnings
    eps_m = re.search(r'EPS: (\d+)/(\d+) beats, avg surprise ([+-][\d.]+)%', reason)
    if eps_m:
        beats, total_q, surprise = int(eps_m.group(1)), int(eps_m.group(2)), float(eps_m.group(3))
        if beats == total_q:
            parts.append(f"Beat earnings estimates all {total_q} recent quarters (avg {surprise:+.1f}%)")
        elif beats > total_q // 2:
            parts.append(f"Beat earnings {beats} of {total_q} recent quarters")
        else:
            parts.append(f"Missed earnings estimates {total_q - beats} of {total_q} recent quarters")

    # Personal P&L / stop-loss / take-profit
    stop_m = re.search(r'Stop-loss: down ([\d.]+)% from cost basis', reason)
    profit_m = re.search(r'Take-profit: up ([\d.]+)% from cost basis', reason)
    pl_m = re.search(r'Position P&L: ([+-][\d.]+)%', reason)
    if stop_m:
        parts.append(f"⚠️ You're down {stop_m.group(1)}% from your purchase price — stop-loss triggered")
    elif profit_m:
        parts.append(f"✅ You're up {profit_m.group(1)}% from your purchase price — consider taking profits")
    elif pl_m:
        pl = float(pl_m.group(1))
        parts.append(f"You're {'up' if pl >= 0 else 'down'} {abs(pl):.1f}% from your purchase price")

    return ". ".join(parts) + "." if parts else "Insufficient data for summary."


_SIGNAL_META = {
    "STRONG_BUY":  {"badge_cls": "badge-strong-buy"},
    "BUY":         {"badge_cls": "badge-buy"},
    "HOLD":        {"badge_cls": "badge-hold"},
    "SELL":        {"badge_cls": "badge-sell"},
    "STRONG_SELL": {"badge_cls": "badge-strong-sell"},
}


def _render_user_section(user_id: int, name: str, generated_at, recs, holdings_count: int) -> str:
    ts_str = generated_at.replace("T", " ")[:16] if generated_at else None

    buys  = [r for r in recs if r["signal"] in ("STRONG_BUY", "BUY")]
    sells = [r for r in recs if r["signal"] in ("STRONG_SELL", "SELL")]
    holds = [r for r in recs if r["signal"] == "HOLD"]

    summary_pills = f"""
    <div class="pill-row">
      <span class="pill pill-buy"><span class="pdot pdot-buy"></span>{len(buys)} Buy</span>
      <span class="pill pill-sell"><span class="pdot pdot-sell"></span>{len(sells)} Sell</span>
      <span class="pill pill-hold"><span class="pdot pdot-hold"></span>{len(holds)} Hold</span>
      <span class="pill pill-neutral">{holdings_count} Holdings</span>
    </div>"""

    broken_tickers = [
        r["ticker"] for r in recs
        if (r["technical_score"] or 0) == 0 and "Insufficient historical data" in (r["reason"] or "")
    ]
    data_warning = f"""
    <div class="data-warning">
      ⚠️ <strong>Price history unavailable for: {', '.join(broken_tickers)}</strong> —
      yfinance may be down or rate-limiting. Technical analysis (30% of score) was skipped for these tickers.
      Signals are based on sentiment and analyst data only.
    </div>""" if broken_tickers else ""

    if not recs:
        body = f"""
        {summary_pills}
        <p class="muted" style="margin-top:14px">No recommendations yet — run the analysis below.</p>"""
    else:
        rows = ""
        for r in recs:
            meta = _SIGNAL_META.get(r["signal"], {"badge_cls": ""})
            price = f"${r['current_price']:.2f}" if r["current_price"] else "—"

            # Score bar: map combined_score from [-1,+1] to a coloured bar
            raw_score = r["combined_score"] or 0
            score_label = f"{raw_score:+.3f}"
            bar_pct = int(min(abs(raw_score), 1.0) * 100)
            bar_color = "#22c55e" if raw_score >= 0 else "#ef4444"

            gl = r["unrealized_gain_loss"]
            if gl is None:
                gl_str, gl_cls = "—", "pl-neutral"
            elif gl >= 0:
                gl_str, gl_cls = f"${gl:+.2f}", "pl-positive"
            else:
                gl_str, gl_cls = f"${gl:+.2f}", "pl-negative"

            affordable = f"{r['affordable_shares']:.2f} sh" if r["affordable_shares"] else "—"

            reason_short = _plain_summary(r)

            rows += f"""
            <tr class="rec-row" data-reason="{r['reason'] or ''}">
              <td class="ticker-cell">{r['ticker']}</td>
              <td>
                <span class="badge {meta['badge_cls']}"><span class="badge-dot"></span>{r['signal'].replace('_', ' ')}</span>
              </td>
              <td style="font-variant-numeric:tabular-nums;white-space:nowrap">{price}</td>
              <td>
                <div class="score-wrap">
                  <div class="score-bar-bg">
                    <div class="score-bar-fill" style="width:{bar_pct}%;background:{bar_color}"></div>
                  </div>
                  <span class="score-num">{score_label}</span>
                </div>
              </td>
              <td class="{gl_cls}">{gl_str}</td>
              <td class="affordable-cell">{affordable}</td>
              <td class="reason-cell">{reason_short}</td>
            </tr>"""

        body = f"""
        {summary_pills}
        {data_warning}
        <div class="table-wrap">
        <table>
          <thead><tr>
            <th>Ticker</th><th>Signal</th><th>Price</th>
            <th>Score</th><th>P&amp;L</th>
            <th>Affordable<br><span style="font-weight:400;color:#4b5563;text-transform:none;font-size:10px;letter-spacing:0">$100/day budget</span></th>
            <th>Reason</th>
          </tr></thead>
          <tbody>{rows}</tbody>
        </table>
        </div>"""

    if ts_str:
        ts_label = f'<span class="ts-label"><span class="ts-dot"></span>Last run: {ts_str}</span>'
    else:
        ts_label = '<span class="ts-label" style="color:#4b5563">Never run</span>'

    return f"""
    <div class="card">
      <div class="card-header">
        <div>
          <h2>{name}</h2>
          {ts_label}
        </div>
        <div class="card-actions">
          <button class="btn-ghost" onclick="openAddStock({user_id})">+ Add Stock</button>
          <button class="btn-ghost" onclick="openImport({user_id})">&#8679; Import</button>
          <button class="btn-ghost" onclick="openManageHoldings({user_id})">Holdings</button>
          <button class="btn-primary" onclick="runDaily({user_id}, this)">&#9654; Run Analysis</button>
          <button class="btn-ghost" onclick="sendEmail({user_id}, this)">&#9993; Send Email</button>
        </div>
      </div>
      {body}
    </div>"""


def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
    schema.setdefault("components", {}).setdefault("securitySchemes", {})["BearerAuth"] = {
        "type": "http", "scheme": "bearer"
    }
    for path in schema.get("paths", {}).values():
        for op in path.values():
            op.setdefault("security", [{"BearerAuth": []}])
    app.openapi_schema = schema
    return schema

app.openapi = custom_openapi  # type: ignore


def _render_page(content: str, api_key: str = "") -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Stock Analyzer</title>
  <link rel="icon" type="image/svg+xml" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><rect width='100' height='100' rx='22' fill='%2316a34a'/><polyline points='18,65 35,42 52,72 68,28 82,45' fill='none' stroke='white' stroke-width='9' stroke-linecap='round' stroke-linejoin='round'/></svg>">
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

    :root {{
      --bg:        #0b0f1a;
      --surface:   #111827;
      --surface2:  #1a2233;
      --border:    rgba(255,255,255,.07);
      --text:      #f1f5f9;
      --text-muted:#7d8fa8;
      --accent:    #3b82f6;
      --accent-dk: #2563eb;
      --green:     #22c55e;
      --green-dk:  #16a34a;
      --red:       #ef4444;
      --red-dk:    #dc2626;
      --amber:     #f59e0b;
      --purple:    #a78bfa;
      --radius-sm: 8px;
      --radius-md: 14px;
      --radius-lg: 20px;
      --shadow-card: 0 2px 12px rgba(0,0,0,.45), 0 0 0 1px rgba(255,255,255,.05);
      --shadow-btn:  0 2px 8px rgba(0,0,0,.35);
    }}

    body {{
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Inter', sans-serif;
      background: var(--bg);
      color: var(--text);
      min-height: 100vh;
      -webkit-font-smoothing: antialiased;
    }}

    /* ── Header ── */
    header {{
      background: linear-gradient(135deg, #0f2444 0%, #0b1629 50%, #0b0f1a 100%);
      border-bottom: 1px solid var(--border);
      padding: 0 24px;
      position: sticky;
      top: 0;
      z-index: 50;
      backdrop-filter: blur(12px);
    }}
    .header-inner {{
      max-width: 980px;
      margin: 0 auto;
      display: flex;
      align-items: center;
      justify-content: space-between;
      height: 64px;
      gap: 12px;
      flex-wrap: wrap;
      padding: 8px 0;
    }}
    .header-brand {{
      display: flex;
      align-items: center;
      gap: 10px;
    }}
    .header-logo {{
      width: 34px; height: 34px;
      background: linear-gradient(135deg, #3b82f6, #8b5cf6);
      border-radius: 9px;
      display: flex; align-items: center; justify-content: center;
      font-size: 18px;
      box-shadow: 0 2px 10px rgba(59,130,246,.4);
      flex-shrink: 0;
    }}
    header h1 {{
      font-size: 18px;
      font-weight: 700;
      color: var(--text);
      letter-spacing: -.4px;
    }}
    header p {{
      font-size: 11px;
      color: var(--text-muted);
      margin-top: 1px;
      letter-spacing: .2px;
    }}

    /* ── Main layout ── */
    .main {{
      padding: 24px 16px 48px;
      max-width: 980px;
      margin: 0 auto;
    }}

    /* ── Cards ── */
    .card {{
      background: var(--surface);
      border-radius: var(--radius-md);
      padding: 22px;
      margin-bottom: 20px;
      box-shadow: var(--shadow-card);
      border: 1px solid var(--border);
      transition: box-shadow .2s;
    }}
    .card:hover {{
      box-shadow: 0 4px 24px rgba(0,0,0,.55), 0 0 0 1px rgba(255,255,255,.09);
    }}

    /* ── Typography ── */
    h2 {{
      font-size: 20px;
      font-weight: 700;
      color: var(--text);
      letter-spacing: -.3px;
    }}
    .muted {{ color: var(--text-muted); font-size: 12px; }}
    .ts-label {{
      display: inline-flex;
      align-items: center;
      gap: 4px;
      font-size: 11px;
      color: var(--text-muted);
      margin-top: 3px;
    }}
    .ts-dot {{
      width: 6px; height: 6px;
      border-radius: 50%;
      background: var(--green);
      display: inline-block;
      box-shadow: 0 0 6px var(--green);
    }}

    /* ── Pill summary row ── */
    .pill-row {{
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      margin-bottom: 16px;
    }}
    .pill {{
      padding: 4px 12px;
      border-radius: 999px;
      font-size: 11px;
      font-weight: 600;
      white-space: nowrap;
      letter-spacing: .3px;
      border: 1px solid transparent;
      display: inline-flex;
      align-items: center;
      gap: 6px;
      text-transform: uppercase;
    }}
    .pill-buy   {{ background: rgba(34,197,94,.1);  color: #4ade80; border-color: rgba(34,197,94,.2); }}
    .pill-sell  {{ background: rgba(239,68,68,.1);  color: #f87171; border-color: rgba(239,68,68,.2); }}
    .pill-hold  {{ background: rgba(255,255,255,.05); color: var(--text-muted); border-color: rgba(255,255,255,.1); }}
    .pill-neutral {{ background: rgba(255,255,255,.05); color: var(--text-muted); border-color: rgba(255,255,255,.1); }}
    .pdot {{ width: 5px; height: 5px; border-radius: 50%; display: inline-block; flex-shrink: 0; }}
    .pdot-buy  {{ background: #4ade80; }}
    .pdot-sell {{ background: #f87171; }}
    .pdot-hold {{ background: var(--text-muted); }}

    /* ── Card header toolbar ── */
    .card-header {{
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      flex-wrap: wrap;
      gap: 10px;
      margin-bottom: 18px;
    }}
    .card-actions {{
      display: flex;
      gap: 7px;
      flex-wrap: wrap;
    }}

    /* ── Buttons — unified system ── */
    .btn-primary, .btn-ghost {{
      border-radius: var(--radius-sm);
      padding: 8px 14px;
      font-size: 12px;
      font-weight: 600;
      cursor: pointer;
      transition: all .15s;
      letter-spacing: .2px;
      white-space: nowrap;
    }}
    .btn-primary {{
      background: var(--green-dk);
      color: white;
      border: none;
      box-shadow: 0 2px 8px rgba(22,163,74,.3);
    }}
    .btn-primary:hover  {{ background: #15803d; box-shadow: 0 4px 14px rgba(22,163,74,.45); transform: translateY(-1px); }}
    .btn-primary:active {{ transform: translateY(0); }}
    .btn-primary:disabled {{ background: #1a3a27; color: #4b7a56; cursor: not-allowed; transform: none; box-shadow: none; }}
    .btn-ghost {{
      background: transparent;
      color: var(--text-muted);
      border: 1px solid rgba(255,255,255,.12);
    }}
    .btn-ghost:hover  {{ background: rgba(255,255,255,.06); color: var(--text); border-color: rgba(255,255,255,.22); transform: translateY(-1px); }}
    .btn-ghost:active {{ transform: translateY(0); }}
    .btn-ghost:disabled {{ opacity: .4; cursor: not-allowed; transform: none; }}

    /* ── Add User button in header ── */
    .btn-add-user {{
      background: var(--green-dk);
      color: white;
      border: none;
      border-radius: var(--radius-sm);
      padding: 9px 17px;
      font-size: 13px;
      font-weight: 600;
      cursor: pointer;
      box-shadow: 0 2px 10px rgba(22,163,74,.35);
      transition: all .15s;
      white-space: nowrap;
    }}
    .btn-add-user:hover {{ background: #15803d; box-shadow: 0 4px 16px rgba(22,163,74,.5); transform: translateY(-1px); }}
    .btn-add-user:active {{ transform: translateY(0); }}
    /* legacy aliases still used inside modals */
    .run-btn {{ display: inline-block; border-radius: var(--radius-sm); padding: 8px 14px; font-size: 12px; font-weight: 600; cursor: pointer; transition: all .15s; white-space: nowrap; background: var(--green-dk); color: white; border: none; }}
    .run-btn:hover {{ background: #15803d; transform: translateY(-1px); }}

    /* ── Table ── */
    .table-wrap {{
      overflow-x: auto;
      margin-top: 4px;
      border-radius: var(--radius-sm);
      border: 1px solid var(--border);
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
    }}
    thead {{ background: var(--surface2); }}
    th {{
      text-align: left;
      padding: 11px 14px;
      color: var(--text-muted);
      font-size: 10px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: .7px;
      white-space: nowrap;
      border-bottom: 1px solid var(--border);
    }}
    tbody tr {{
      border-bottom: 1px solid var(--border);
      transition: background .12s;
    }}
    tbody tr:last-child {{ border-bottom: none; }}
    tbody tr:nth-child(odd)  {{ background: rgba(255,255,255,.015); }}
    tbody tr:nth-child(even) {{ background: transparent; }}
    tbody tr:hover {{ background: rgba(59,130,246,.07); }}
    td {{
      padding: 13px 14px;
      vertical-align: middle;
    }}

    /* ── Ticker cell ── */
    .ticker-cell {{
      font-size: 15px;
      font-weight: 700;
      color: var(--text);
      letter-spacing: .5px;
      font-variant-numeric: tabular-nums;
    }}

    /* ── Signal badges (pill-shaped with dot) ── */
    .badge {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 4px 11px;
      border-radius: 999px;
      font-size: 11px;
      font-weight: 700;
      white-space: nowrap;
      letter-spacing: .3px;
      text-transform: uppercase;
      border: 1px solid transparent;
    }}
    .badge-dot {{
      width: 6px; height: 6px;
      border-radius: 50%;
      display: inline-block;
      flex-shrink: 0;
    }}
    .badge-strong-buy  {{ background: rgba(22,163,74,.15);  color: #4ade80; border-color: rgba(34,197,94,.25); }}
    .badge-strong-buy .badge-dot  {{ background: #22c55e; box-shadow: 0 0 5px rgba(34,197,94,.7); }}
    .badge-buy         {{ background: rgba(34,197,94,.1);   color: #86efac; border-color: rgba(34,197,94,.18); }}
    .badge-buy .badge-dot         {{ background: #86efac; }}
    .badge-hold        {{ background: rgba(255,255,255,.05); color: var(--text-muted); border-color: rgba(255,255,255,.1); }}
    .badge-hold .badge-dot        {{ background: #6b7280; }}
    .badge-sell        {{ background: rgba(239,68,68,.1);   color: #fca5a5; border-color: rgba(239,68,68,.18); }}
    .badge-sell .badge-dot        {{ background: #fca5a5; }}
    .badge-strong-sell {{ background: rgba(220,38,38,.17);  color: #f87171; border-color: rgba(220,38,38,.3); }}
    .badge-strong-sell .badge-dot {{ background: #ef4444; box-shadow: 0 0 5px rgba(239,68,68,.7); }}

    /* ── Score bar ── */
    .score-wrap {{
      display: flex;
      align-items: center;
      gap: 7px;
      min-width: 80px;
    }}
    .score-bar-bg {{
      flex: 1;
      height: 5px;
      background: rgba(255,255,255,.1);
      border-radius: 3px;
      overflow: hidden;
    }}
    .score-bar-fill {{
      height: 100%;
      border-radius: 3px;
      transition: width .3s;
    }}
    .score-num {{
      font-size: 11px;
      font-family: 'SF Mono', 'Fira Code', monospace;
      color: var(--text-muted);
      white-space: nowrap;
      min-width: 46px;
      text-align: right;
    }}

    /* ── P&L ── */
    .pl-positive {{ color: #4ade80; font-weight: 700; font-variant-numeric: tabular-nums; }}
    .pl-negative {{ color: #f87171; font-weight: 700; font-variant-numeric: tabular-nums; }}
    .pl-neutral  {{ color: var(--text-muted); font-variant-numeric: tabular-nums; }}

    /* ── Affordable shares ── */
    .affordable-cell {{ color: #93c5fd; font-size: 12px; font-variant-numeric: tabular-nums; }}

    /* ── Reason cell ── */
    .reason-cell {{
      color: var(--text-muted);
      font-size: 12px;
      line-height: 1.5;
      max-width: 260px;
      display: -webkit-box;
      -webkit-line-clamp: 3;
      -webkit-box-orient: vertical;
      overflow: hidden;
    }}
    .reason-cell.expanded {{
      display: block;
      overflow: visible;
      -webkit-line-clamp: unset;
    }}
    .reason-cell:not(.expanded):after {{
      content: ' \2026';
      color: var(--accent);
    }}

    /* ── Data warning banner ── */
    .data-warning {{
      background: rgba(245,158,11,.1);
      border-left: 3px solid var(--amber);
      border-radius: var(--radius-sm);
      padding: 10px 14px;
      margin-bottom: 14px;
      font-size: 12px;
      color: #fcd34d;
    }}

    /* ── Empty state ── */
    .empty-state {{
      text-align: center;
      padding: 56px 20px;
    }}
    .empty-icon {{
      font-size: 52px;
      margin-bottom: 16px;
      opacity: .85;
    }}
    .empty-state h2 {{
      font-size: 20px;
      color: var(--text);
      margin-bottom: 10px;
    }}
    .empty-state p {{
      color: var(--text-muted);
      font-size: 14px;
      margin-bottom: 24px;
    }}

    /* ── Toast ── */
    .toast {{
      position: fixed;
      bottom: 24px; right: 24px; left: 24px;
      max-width: 420px;
      margin: 0 auto;
      padding: 13px 18px;
      border-radius: var(--radius-sm);
      color: white;
      font-size: 13px;
      font-weight: 500;
      z-index: 999;
      box-shadow: 0 8px 30px rgba(0,0,0,.5);
      display: none;
      backdrop-filter: blur(8px);
      border: 1px solid rgba(255,255,255,.1);
    }}
    .toast.ok  {{ background: rgba(22,163,74,.92); }}
    .toast.err {{ background: rgba(220,38,38,.92); }}

    /* ── Footer ── */
    .footer {{
      font-size: 11px;
      color: #374151;
      padding: 18px 0 4px;
      margin-top: 8px;
      letter-spacing: .2px;
      border-top: 1px solid var(--border);
    }}

    /* ── Modal backdrop & box ── */
    .modal-backdrop {{
      display: none;
      position: fixed;
      inset: 0;
      background: rgba(0,0,0,.7);
      backdrop-filter: blur(4px);
      z-index: 100;
      overflow-y: auto;
      padding: 20px;
    }}
    .modal-box {{
      background: var(--surface);
      border: 1px solid rgba(255,255,255,.1);
      border-radius: var(--radius-lg);
      max-width: 480px;
      margin: 48px auto;
      padding: 28px;
      position: relative;
      box-shadow: 0 24px 60px rgba(0,0,0,.7);
    }}
    .modal-box h2 {{ color: var(--text); margin-bottom: 4px; font-size: 18px; }}
    .modal-close {{
      position: absolute;
      top: 18px; right: 18px;
      background: rgba(255,255,255,.07);
      border: none;
      border-radius: 6px;
      width: 30px; height: 30px;
      font-size: 15px;
      cursor: pointer;
      color: var(--text-muted);
      display: flex; align-items: center; justify-content: center;
      transition: background .15s, color .15s;
    }}
    .modal-close:hover {{ background: rgba(255,255,255,.14); color: var(--text); }}

    /* ── Form elements ── */
    .field-label {{
      display: block;
      font-size: 12px;
      font-weight: 600;
      color: var(--text-muted);
      margin-bottom: 5px;
      text-transform: uppercase;
      letter-spacing: .5px;
    }}
    .field-input {{
      width: 100%;
      padding: 10px 13px;
      background: var(--surface2);
      border: 1.5px solid rgba(255,255,255,.1);
      border-radius: var(--radius-sm);
      font-size: 14px;
      color: var(--text);
      outline: none;
      transition: border .15s, box-shadow .15s;
    }}
    .field-input::placeholder {{ color: #4b5563; }}
    .field-input:focus {{
      border-color: var(--accent);
      box-shadow: 0 0 0 3px rgba(59,130,246,.15);
    }}
    .form-err {{ color: #f87171; font-size: 12px; margin-top: 8px; min-height: 18px; }}

    .holding-row {{
      display: grid;
      grid-template-columns: 1fr 1fr 1fr auto;
      gap: 6px;
      margin-bottom: 6px;
      align-items: center;
    }}
    .holding-row input {{
      padding: 8px 10px;
      background: var(--surface2);
      border: 1.5px solid rgba(255,255,255,.1);
      border-radius: 6px;
      font-size: 13px;
      color: var(--text);
      width: 100%;
      outline: none;
    }}
    .holding-row input:focus {{ border-color: var(--accent); }}
    .del-btn {{
      background: rgba(239,68,68,.15);
      border: 1px solid rgba(239,68,68,.25);
      border-radius: 6px;
      color: #f87171;
      font-size: 14px;
      cursor: pointer;
      padding: 4px 8px;
      line-height: 1;
      transition: background .15s;
    }}
    .del-btn:hover {{ background: rgba(239,68,68,.3); }}

    .manage-holding-row {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 12px 0;
      border-bottom: 1px solid var(--border);
    }}
    .manage-holding-row:last-child {{ border-bottom: none; }}
    .manage-remove-btn {{
      background: rgba(239,68,68,.15);
      color: #f87171;
      border: 1px solid rgba(239,68,68,.25);
      border-radius: 6px;
      padding: 6px 14px;
      font-size: 12px;
      font-weight: 600;
      cursor: pointer;
      transition: background .15s;
    }}
    .manage-remove-btn:hover {{ background: rgba(239,68,68,.3); }}

    /* ── Mobile ── */
    @media (max-width: 640px) {{
      header {{ padding: 0 16px; }}
      .main  {{ padding: 16px 12px 32px; }}
      .card  {{ padding: 16px; border-radius: var(--radius-sm); }}
      .card-actions {{ gap: 5px; }}
      .btn-primary, .btn-ghost {{ padding: 7px 10px; font-size: 11px; }}
      th, td {{ padding: 10px 10px; }}
      .reason-cell {{ display: none; }}
      .score-wrap  {{ min-width: 60px; }}
      .modal-box {{ margin: 20px auto; padding: 20px; border-radius: var(--radius-md); }}
    }}
  </style>
</head>
<body>
  <header>
    <div class="header-inner">
      <div class="header-brand">
        <div class="header-logo">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#4ade80" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/>
          </svg>
        </div>
        <div>
          <h1>Stock Analyzer</h1>
          <p>Daily recommendations &middot; Not financial advice</p>
        </div>
      </div>
      <button class="btn-add-user" onclick="openModal()">+ Add User</button>
    </div>
  </header>

  <div class="main">
    {content}
    <p class="footer">Not financial advice &nbsp;&middot;&nbsp; Scores: price momentum 15% &nbsp;&middot;&nbsp; technical 30% &nbsp;&middot;&nbsp; sentiment 25% &nbsp;&middot;&nbsp; analyst 20% &nbsp;&middot;&nbsp; earnings 10%</p>
  </div>

  <!-- Manage Holdings Modal -->
  <div id="manage-overlay" class="modal-backdrop">
    <div class="modal-box" style="max-width:500px">
      <button class="modal-close" onclick="closeManageHoldings()">✕</button>
      <h2>Manage Holdings</h2>
      <p class="muted" style="margin-bottom:20px">Remove stocks you have sold</p>
      <div id="manage-list">
        <p class="muted">Loading…</p>
      </div>
    </div>
  </div>

  <!-- Add Stock Modal -->
  <div id="add-stock-overlay" class="modal-backdrop">
    <div class="modal-box" style="max-width:440px">
      <button class="modal-close" onclick="closeAddStock()">✕</button>
      <h2>Add Stock</h2>
      <p class="muted" style="margin-bottom:22px">Add a stock you already own to your portfolio</p>

      <label class="field-label">Ticker Symbol</label>
      <input id="as-ticker" class="field-input" placeholder="AAPL" style="text-transform:uppercase" oninput="this.value=this.value.toUpperCase()" />

      <label class="field-label" style="margin-top:14px">Number of Shares</label>
      <input id="as-shares" class="field-input" type="number" placeholder="10" min="0.0001" step="any" />

      <label class="field-label" style="margin-top:14px">Average Cost Per Share ($)</label>
      <input id="as-cost" class="field-input" type="number" placeholder="150.00" min="0" step="any" />

      <div id="as-err" class="form-err"></div>
      <button class="run-btn btn-green" style="margin-top:22px;width:100%;padding:12px" onclick="doAddStock()">Add to Portfolio</button>
    </div>
  </div>

  <!-- Import Modal -->
  <div id="import-overlay" class="modal-backdrop">
    <div class="modal-box" style="max-width:460px">
      <button class="modal-close" onclick="closeImport()">&#x2715;</button>
      <h2>Bulk Import</h2>
      <p class="muted" style="margin-bottom:20px">Download the template, fill in your stocks, then upload it.</p>

      <div style="display:flex;flex-direction:column;gap:12px">
        <button class="btn-ghost" style="padding:12px;text-align:left;border-radius:var(--radius-sm);display:flex;align-items:center;gap:10px" onclick="downloadTemplate()">
          <span style="font-size:20px">&#8659;</span>
          <div>
            <div style="font-weight:600;color:var(--text);font-size:13px">Download Template</div>
            <div style="font-size:11px;color:var(--text-muted);margin-top:2px">portfolio_template.xlsx — fill in Ticker, Shares, Avg Cost</div>
          </div>
        </button>

        <label for="import-file" style="display:block;border:1.5px dashed rgba(255,255,255,.15);border-radius:var(--radius-sm);padding:20px;text-align:center;cursor:pointer;transition:border-color .15s" id="import-drop" ondragover="event.preventDefault()" ondrop="handleDrop(event)">
          <input type="file" id="import-file" accept=".xlsx" style="display:none" onchange="handleFileSelect(this)">
          <div id="import-file-label">
            <div style="font-size:28px;margin-bottom:8px">&#128190;</div>
            <div style="font-size:13px;color:var(--text-muted)">Click to choose .xlsx file or drag &amp; drop</div>
          </div>
        </label>
      </div>

      <div id="import-err" class="form-err" style="margin-top:10px"></div>
      <button class="btn-primary" id="import-submit-btn" style="margin-top:18px;width:100%;padding:12px;display:none" onclick="doImport()">Upload &amp; Add Stocks</button>
    </div>
  </div>

  <!-- Add User Modal -->
  <div id="modal-overlay" class="modal-backdrop">
    <div class="modal-box" style="max-width:540px">
      <button class="modal-close" onclick="closeModal()">✕</button>

      <!-- Step 1: Register -->
      <div id="step1">
        <h2 style="margin-bottom:4px">Create Account</h2>
        <p class="muted" style="margin-bottom:22px">Step 1 of 2 — Your details</p>
        <label class="field-label">Full Name</label>
        <input id="reg-name" class="field-input" placeholder="Jane Smith" />
        <div id="step1-err" class="form-err"></div>
        <button class="run-btn" style="margin-top:22px;width:100%;padding:12px" onclick="doRegister()">Continue →</button>
      </div>

      <!-- Step 2: Onboard -->
      <div id="step2" style="display:none">
        <h2 style="margin-bottom:4px">Your Portfolio</h2>
        <p class="muted" style="margin-bottom:22px">Step 2 of 2 — Add existing holdings (optional)</p>

        <label style="display:flex;align-items:center;gap:10px;font-size:14px;margin-bottom:18px;cursor:pointer;color:var(--text)">
          <input type="checkbox" id="has-stocks" onchange="toggleHoldings(this.checked)"
                 style="width:16px;height:16px;accent-color:#3b82f6">
          I already own stocks I want to track
        </label>

        <div id="holdings-section" style="display:none">
          <div style="display:grid;grid-template-columns:1fr 1fr 1fr auto;gap:6px;margin-bottom:8px">
            <span style="font-size:10px;font-weight:700;color:var(--text-muted);text-transform:uppercase;letter-spacing:.5px">Ticker</span>
            <span style="font-size:10px;font-weight:700;color:var(--text-muted);text-transform:uppercase;letter-spacing:.5px">Shares</span>
            <span style="font-size:10px;font-weight:700;color:var(--text-muted);text-transform:uppercase;letter-spacing:.5px">Avg Cost</span>
            <span></span>
          </div>
          <div id="holdings-rows"></div>
          <button onclick="addHoldingRow()"
                  style="background:none;border:1px dashed rgba(255,255,255,.15);border-radius:8px;
                         padding:9px;width:100%;color:var(--text-muted);font-size:13px;
                         cursor:pointer;margin-top:8px;transition:border-color .15s,color .15s">
            + Add stock
          </button>
        </div>

        <div id="step2-err" class="form-err"></div>
        <button class="run-btn btn-green" style="margin-top:22px;width:100%;padding:12px" onclick="doOnboard()">Finish Setup ✓</button>
      </div>

      <!-- Done -->
      <div id="step-done" style="display:none;text-align:center;padding:24px 0">
        <div style="font-size:52px;margin-bottom:14px">🎉</div>
        <h2>You're all set!</h2>
        <p class="muted" style="margin-top:8px">Reloading dashboard…</p>
      </div>
    </div>
  </div>

  <div id="toast" class="toast"></div>

  <script>
    const _apiKey = "{api_key}";
    const _authHeaders = _apiKey
      ? {{'Content-Type': 'application/json', 'Authorization': 'Bearer ' + _apiKey}}
      : {{'Content-Type': 'application/json'}};

    let _newUserId = null;
    let _addStockUserId = null;

    let _manageUserId = null;

    async function openManageHoldings(userId) {{
      _manageUserId = userId;
      document.getElementById('manage-overlay').style.display = 'block';
      document.body.style.overflow = 'hidden';
      document.getElementById('manage-list').innerHTML = '<p class="muted">Loading…</p>';
      try {{
        const res = await fetch(`/users/${{userId}}/portfolio`, {{ headers: _authHeaders }});
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || 'Failed to load holdings');
        if (!data.length) {{
          document.getElementById('manage-list').innerHTML = '<p class="muted">No holdings found.</p>';
          return;
        }}
        let rows = '';
        for (const h of data) {{
          rows += `
          <div class="manage-holding-row">
            <div>
              <strong style="font-size:15px;color:var(--text)">${{h.ticker}}</strong>
              <span class="muted" style="margin-left:8px">${{h.shares}} sh @ $${{h.avg_cost_basis.toFixed(2)}}</span>
            </div>
            <button class="manage-remove-btn" onclick="removeHolding(${{h.id}}, '${{h.ticker}}')">Remove</button>
          </div>`;
        }}
        document.getElementById('manage-list').innerHTML = rows;
      }} catch(e) {{
        document.getElementById('manage-list').innerHTML = `<p style="color:#f87171">${{e.message}}</p>`;
      }}
    }}

    function closeManageHoldings() {{
      document.getElementById('manage-overlay').style.display = 'none';
      document.body.style.overflow = '';
    }}

    async function removeHolding(holdingId, ticker) {{
      if (!confirm(`Remove ${{ticker}} from your portfolio?`)) return;
      try {{
        const res = await fetch(`/users/${{_manageUserId}}/portfolio/${{holdingId}}`, {{
          method: 'DELETE', headers: _authHeaders
        }});
        if (!res.ok) {{
          const data = await res.json();
          throw new Error(data.detail || 'Failed to remove');
        }}
        showToast(`✅ ${{ticker}} removed`, 'ok');
        setTimeout(() => location.reload(), 800);
      }} catch(e) {{
        showToast('❌ ' + e.message, 'err');
      }}
    }}

    function openAddStock(userId) {{
      _addStockUserId = userId;
      document.getElementById('as-ticker').value = '';
      document.getElementById('as-shares').value = '';
      document.getElementById('as-cost').value = '';
      document.getElementById('as-err').textContent = '';
      document.getElementById('add-stock-overlay').style.display = 'block';
      document.body.style.overflow = 'hidden';
      document.getElementById('as-ticker').focus();
    }}

    function closeAddStock() {{
      document.getElementById('add-stock-overlay').style.display = 'none';
      document.body.style.overflow = '';
    }}

    async function doAddStock() {{
      const ticker = document.getElementById('as-ticker').value.trim();
      const shares = document.getElementById('as-shares').value.trim();
      const cost   = document.getElementById('as-cost').value.trim();
      const date   = new Date().toISOString().slice(0, 10);
      const err    = document.getElementById('as-err');
      if (!ticker || !shares || !cost) {{ err.textContent = 'All fields are required.'; return; }}
      err.textContent = '';
      try {{
        const res = await fetch(`/users/${{_addStockUserId}}/portfolio`, {{
          method: 'POST',
          headers: _authHeaders,
          body: JSON.stringify({{ ticker, shares: parseFloat(shares), avg_cost_basis: parseFloat(cost), acquired_date: date }}),
        }});
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || 'Failed to add stock');
        closeAddStock();
        showToast(`✅ ${{ticker}} added to portfolio`, 'ok');
        setTimeout(() => location.reload(), 1200);
      }} catch(e) {{
        err.textContent = '❌ ' + e.message;
      }}
    }}


    function openModal() {{
      document.getElementById('modal-overlay').style.display = 'block';
      document.body.style.overflow = 'hidden';
    }}
    function closeModal() {{
      document.getElementById('modal-overlay').style.display = 'none';
      document.body.style.overflow = '';
    }}

    async function doRegister() {{
      const name  = document.getElementById('reg-name').value.trim();
      const err   = document.getElementById('step1-err');
      if (!name) {{ err.textContent = 'Name is required.'; return; }}
      err.textContent = '';
      try {{
        const res  = await fetch('/users/register', {{
          method: 'POST',
          headers: _authHeaders,
          body: JSON.stringify({{ name }}),
        }});
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || 'Registration failed');
        _newUserId = data.id;
        document.getElementById('step1').style.display = 'none';
        document.getElementById('step2').style.display = 'block';
        addHoldingRow();
      }} catch(e) {{
        err.textContent = '❌ ' + e.message;
      }}
    }}

    function toggleHoldings(checked) {{
      document.getElementById('holdings-section').style.display = checked ? 'block' : 'none';
    }}

    function addHoldingRow() {{
      const row = document.createElement('div');
      row.className = 'holding-row';
      row.innerHTML = `
        <input placeholder="AAPL" style="text-transform:uppercase" oninput="this.value=this.value.toUpperCase()">
        <input type="number" placeholder="10" min="0.0001" step="any">
        <input type="number" placeholder="150.00" min="0" step="any">
        <button class="del-btn" onclick="this.parentElement.remove()">✕</button>`;
      document.getElementById('holdings-rows').appendChild(row);
    }}

    async function doOnboard() {{
      const hasStocks = document.getElementById('has-stocks').checked;
      const err = document.getElementById('step2-err');
      err.textContent = '';
      let holdings = [];
      if (hasStocks) {{
        const rows = document.querySelectorAll('#holdings-rows .holding-row');
        for (const row of rows) {{
          const [ticker, shares, cost] = [...row.querySelectorAll('input')].map(i => i.value.trim());
          if (!ticker || !shares || !cost) {{ err.textContent = 'Fill in all fields for each holding.'; return; }}
          holdings.push({{ ticker, shares: parseFloat(shares), avg_cost_basis: parseFloat(cost), acquired_date: new Date().toISOString().slice(0,10) }});
        }}
      }}
      try {{
        const res = await fetch(`/users/${{_newUserId}}/onboard`, {{
          method: 'POST',
          headers: _authHeaders,
          body: JSON.stringify({{ has_prior_stocks: hasStocks, holdings }}),
        }});
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || 'Onboarding failed');
        document.getElementById('step2').style.display = 'none';
        document.getElementById('step-done').style.display = 'block';
        setTimeout(() => location.reload(), 1500);
      }} catch(e) {{
        err.textContent = '❌ ' + e.message;
      }}
    }}

    async function sendEmail(userId, btn) {{
      btn.disabled = true;
      btn.textContent = 'Sending\u2026';
      try {{
        const res = await fetch(`/messaging/send/${{userId}}`, {{ method: 'POST', headers: _authHeaders }});
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || 'Unknown error');
        showToast(`\u2705 Email sent \u2014 ${{data.subject}}`, 'ok');
      }} catch (e) {{
        showToast('\u274c ' + e.message, 'err');
      }} finally {{
        btn.disabled = false;
        btn.textContent = '\u2709 Send Email';
      }}
    }}

    async function runDaily(userId, btn) {{
      btn.disabled = true;
      btn.textContent = 'Running\u2026';
      try {{
        const res = await fetch(`/run-daily/${{userId}}`, {{ method: 'POST', headers: _authHeaders }});
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || 'Unknown error');
        const summary = `✅ Analysis done — ${{data.recommendations_generated}} recs · ${{data.prices_refreshed}} prices · ${{data.articles_analyzed}} articles`;
        const errors = data.errors && data.errors.length ? ` ⚠️ ${{data.errors.join(' | ')}}` : '';
        showToast(summary + errors, data.errors && data.errors.length ? 'err' : 'ok');
        setTimeout(() => location.reload(), 3000);
      }} catch (e) {{
        showToast('❌ ' + e.message, 'err');
        btn.disabled = false;
        btn.textContent = '\u25b6 Run Analysis';
      }}
    }}

    let _importUserId = null;
    let _importFile = null;

    function openImport(userId) {{
      _importUserId = userId;
      _importFile = null;
      document.getElementById('import-file').value = '';
      document.getElementById('import-file-label').innerHTML = '<div style="font-size:28px;margin-bottom:8px">\U0001F4BE</div><div style="font-size:13px;color:var(--text-muted)">Click to choose .xlsx file or drag &amp; drop</div>';
      document.getElementById('import-err').textContent = '';
      document.getElementById('import-submit-btn').style.display = 'none';
      document.getElementById('import-overlay').style.display = 'block';
      document.body.style.overflow = 'hidden';
    }}
    function closeImport() {{
      document.getElementById('import-overlay').style.display = 'none';
      document.body.style.overflow = '';
    }}
    function downloadTemplate() {{
      window.location.href = `/users/${{_importUserId}}/portfolio/template`;
    }}
    function handleFileSelect(input) {{
      if (input.files[0]) setImportFile(input.files[0]);
    }}
    function handleDrop(event) {{
      event.preventDefault();
      const f = event.dataTransfer.files[0];
      if (f) setImportFile(f);
    }}
    function setImportFile(f) {{
      _importFile = f;
      document.getElementById('import-file-label').innerHTML =
        `<div style="font-size:20px;margin-bottom:6px">\u2705</div><div style="font-size:13px;color:var(--text)">${{f.name}}</div>`;
      document.getElementById('import-submit-btn').style.display = 'block';
      document.getElementById('import-err').textContent = '';
    }}
    async function doImport() {{
      if (!_importFile) return;
      const btn = document.getElementById('import-submit-btn');
      btn.disabled = true;
      btn.textContent = 'Uploading\u2026';
      const form = new FormData();
      form.append('file', _importFile);
      const headers = _apiKey ? {{'Authorization': 'Bearer ' + _apiKey}} : {{}};
      try {{
        const res = await fetch(`/users/${{_importUserId}}/portfolio/bulk`, {{
          method: 'POST', headers, body: form,
        }});
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || 'Upload failed');
        closeImport();
        let msg = `\u2705 Added ${{data.added}} stock${{data.added !== 1 ? 's' : ''}}`;
        if (data.skipped) msg += ` \u00b7 ${{data.skipped}} skipped`;
        if (data.errors && data.errors.length) msg += ` \u2014 ${{data.errors[0]}}`;
        showToast(msg, data.skipped ? 'err' : 'ok');
        setTimeout(() => location.reload(), 1500);
      }} catch(e) {{
        document.getElementById('import-err').textContent = '\u274c ' + e.message;
        btn.disabled = false;
        btn.textContent = 'Upload & Add Stocks';
      }}
    }}

    document.querySelectorAll('tr.rec-row').forEach(row => {{
      row.style.cursor = 'pointer';
      row.addEventListener('click', () => {{
        const cell = row.querySelector('.reason-cell');
        if (cell) cell.classList.toggle('expanded');
      }});
    }});

    function showToast(msg, type) {{
      const t = document.getElementById('toast');
      t.textContent = msg;
      t.className = 'toast ' + type;
      t.style.display = 'block';
      setTimeout(() => {{ t.style.display = 'none'; }}, 4000);
    }}
  </script>
</body>
</html>"""
