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
    "STRONG_BUY":  {"color": "#16a34a", "bg": "#f0fdf4", "emoji": "🚀"},
    "BUY":         {"color": "#22c55e", "bg": "#f0fdf4", "emoji": "📈"},
    "HOLD":        {"color": "#d97706", "bg": "#fffbeb", "emoji": "⏸️"},
    "SELL":        {"color": "#ef4444", "bg": "#fef2f2", "emoji": "📉"},
    "STRONG_SELL": {"color": "#dc2626", "bg": "#fef2f2", "emoji": "🔴"},
}


def _render_user_section(user_id: int, name: str, generated_at, recs, holdings_count: int) -> str:
    ts_str = generated_at.replace("T", " ")[:16] if generated_at else None

    buys  = [r for r in recs if r["signal"] in ("STRONG_BUY", "BUY")]
    sells = [r for r in recs if r["signal"] in ("STRONG_SELL", "SELL")]
    holds = [r for r in recs if r["signal"] == "HOLD"]

    summary_pills = f"""
    <div class="pill-row">
      <span class="pill pill-buy">🚀 {len(buys)} Buy</span>
      <span class="pill pill-sell">📉 {len(sells)} Sell</span>
      <span class="pill pill-hold">⏸️ {len(holds)} Hold</span>
      <span class="pill pill-neutral">🗂️ {holdings_count} Holdings</span>
    </div>"""

    broken_tickers = [
        r["ticker"] for r in recs
        if (r["technical_score"] or 0) == 0 and "Insufficient historical data" in (r["reason"] or "")
    ]
    data_warning = f"""
    <div style="background:#fef3c7;border-left:4px solid #f59e0b;border-radius:6px;
                padding:10px 14px;margin-bottom:12px;font-size:13px;color:#92400e">
      ⚠️ <strong>Price history unavailable for: {', '.join(broken_tickers)}</strong> —
      yfinance may be down or rate-limiting. Technical analysis (30% of score) was skipped for these tickers.
      Signals are based on sentiment and analyst data only.
    </div>""" if broken_tickers else ""

    if not recs:
        body = f"""
        {summary_pills}
        <p class="muted" style="margin-top:12px">No recommendations yet — run the analysis below.</p>"""
    else:
        rows = ""
        for r in recs:
            meta = _SIGNAL_META.get(r["signal"], {"color": "#6b7280", "bg": "#f9fafb", "emoji": ""})
            price = f"${r['current_price']:.2f}" if r["current_price"] else "—"
            score = f"{r['combined_score']:+.3f}"

            gl = r["unrealized_gain_loss"]
            gl_str = f"${gl:+.2f}" if gl is not None else "—"
            gl_color = "#16a34a" if (gl or 0) >= 0 else "#dc2626"

            affordable = f"{r['affordable_shares']:.2f} sh" if r["affordable_shares"] else "—"

            reason_short = _plain_summary(r)

            rows += f"""
            <tr class="rec-row" data-reason="{r['reason'] or ''}">
              <td><strong style="font-size:15px">{r['ticker']}</strong></td>
              <td>
                <span class="badge" style="background:{meta['color']}">{meta['emoji']} {r['signal']}</span>
              </td>
              <td>{price}</td>
              <td style="font-family:monospace;font-size:13px">{score}</td>
              <td style="color:{gl_color};font-weight:600">{gl_str}</td>
              <td style="color:#2563eb">{affordable}</td>
              <td class="reason-cell">{reason_short}</td>
            </tr>"""

        body = f"""
        {summary_pills}
        {data_warning}
        <div style="overflow-x:auto;margin-top:12px">
        <table>
          <thead><tr>
            <th>Ticker</th><th>Signal</th><th>Price</th>
            <th>Score</th><th>P&amp;L</th><th>Affordable</th><th>Reason</th>
          </tr></thead>
          <tbody>{rows}</tbody>
        </table>
        </div>"""

    ts_label = f'<span class="muted" style="font-size:12px">Last run: {ts_str}</span>' if ts_str else '<span class="muted" style="font-size:12px">Never run</span>'

    return f"""
    <div class="card">
      <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px;margin-bottom:12px">
        <div>
          <h2 style="font-size:18px">{name}</h2>
          {ts_label}
        </div>
        <div style="display:flex;gap:8px;flex-wrap:wrap">
          <button class="run-btn" style="background:#10b981" onclick="openAddStock({user_id})">+ Add Stock</button>
          <button class="run-btn" style="background:#6b7280" onclick="openManageHoldings({user_id})">✎ Manage Holdings</button>
          <button class="run-btn" onclick="runDaily({user_id}, this)">▶ Run Analysis</button>
          <button class="run-btn" style="background:#7c3aed" onclick="sendEmail({user_id}, this)">✉ Send Email</button>
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
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      background: #0f172a;
      color: #1f2937;
      min-height: 100vh;
    }}
    header {{
      background: linear-gradient(135deg, #1e3a5f 0%, #0f172a 100%);
      padding: 20px 20px 16px;
      color: white;
      border-bottom: 1px solid rgba(255,255,255,.08);
    }}
    header h1 {{ font-size: 22px; font-weight: 700; letter-spacing: -.3px; }}
    header p {{ font-size: 13px; color: #94a3b8; margin-top: 2px; }}
    .main {{ padding: 16px; max-width: 900px; margin: 0 auto; }}
    .card {{
      background: white;
      border-radius: 14px;
      padding: 18px;
      margin-bottom: 16px;
      box-shadow: 0 4px 16px rgba(0,0,0,.15);
    }}
    h2 {{ font-size: 17px; font-weight: 700; color: #111827; }}
    .muted {{ color: #6b7280; font-size: 13px; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
    th {{
      text-align: left; padding: 8px 8px; color: #6b7280;
      font-size: 11px; font-weight: 600; text-transform: uppercase;
      letter-spacing: .5px; border-bottom: 2px solid #e5e7eb;
    }}
    td {{ padding: 11px 8px; border-bottom: 1px solid #f3f4f6; vertical-align: top; }}
    tr:last-child td {{ border-bottom: none; }}
    .badge {{
      color: white; padding: 4px 9px; border-radius: 6px;
      font-size: 11px; font-weight: 700; white-space: nowrap; display: inline-block;
    }}
    .pill-row {{ display: flex; flex-wrap: wrap; gap: 6px; }}
    .pill {{
      padding: 4px 10px; border-radius: 20px; font-size: 12px;
      font-weight: 600; white-space: nowrap;
    }}
    .pill-buy   {{ background: #dcfce7; color: #15803d; }}
    .pill-sell  {{ background: #fee2e2; color: #b91c1c; }}
    .pill-hold  {{ background: #fef3c7; color: #b45309; }}
    .pill-neutral {{ background: #e0e7ff; color: #4338ca; }}
    .run-btn {{
      background: #2563eb; color: white; border: none; border-radius: 8px;
      padding: 9px 18px; font-size: 13px; font-weight: 600;
      cursor: pointer; transition: background .15s;
    }}
    .run-btn:hover {{ background: #1d4ed8; }}
    .run-btn:disabled {{ background: #93c5fd; cursor: not-allowed; }}
    .reason-cell {{ color: #6b7280; font-size: 12px; max-width: 260px; }}
    .toast {{
      position: fixed; bottom: 20px; right: 20px; left: 20px; max-width: 400px;
      margin: 0 auto; padding: 12px 16px; border-radius: 10px; color: white;
      font-size: 14px; font-weight: 500; z-index: 999;
      box-shadow: 0 8px 24px rgba(0,0,0,.3); display: none;
    }}
    .toast.ok  {{ background: #16a34a; }}
    .toast.err {{ background: #dc2626; }}
    .empty-state {{ text-align: center; padding: 40px 20px; }}
    .empty-icon {{ font-size: 48px; margin-bottom: 12px; }}
    .empty-state h2 {{ color: #374151; margin-bottom: 8px; }}
    .empty-state p {{ color: #6b7280; font-size: 14px; }}
    .empty-state a {{ color: #2563eb; }}
    .footer {{
      text-align: center; font-size: 11px; color: #475569;
      padding: 20px; margin-top: 8px;
    }}
  </style>
</head>
<body>
  <header style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px">
    <div>
      <h1>📊 Stock Analyzer</h1>
      <p>Daily recommendations · Not financial advice</p>
    </div>
    <button class="run-btn" onclick="openModal()" style="background:#10b981">+ Add User</button>
  </header>
  <div class="main">
    {content}
  </div>
  <p class="footer">Scores: price momentum 20% · technical indicators 40% · news sentiment 40%</p>

  <!-- Manage Holdings Modal -->
  <div id="manage-overlay" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:100;overflow-y:auto;padding:20px">
    <div style="background:white;border-radius:16px;max-width:480px;margin:40px auto;padding:24px;position:relative">
      <button onclick="closeManageHoldings()" style="position:absolute;top:16px;right:16px;background:none;border:none;font-size:20px;cursor:pointer;color:#6b7280">✕</button>
      <h2 style="margin-bottom:4px">Manage Holdings</h2>
      <p class="muted" style="margin-bottom:20px">Remove stocks you have sold</p>
      <div id="manage-list">
        <p class="muted">Loading…</p>
      </div>
    </div>
  </div>

  <!-- Add Stock Modal -->
  <div id="add-stock-overlay" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:100;overflow-y:auto;padding:20px">
    <div style="background:white;border-radius:16px;max-width:420px;margin:40px auto;padding:24px;position:relative">
      <button onclick="closeAddStock()" style="position:absolute;top:16px;right:16px;background:none;border:none;font-size:20px;cursor:pointer;color:#6b7280">✕</button>
      <h2 style="margin-bottom:4px">Add Stock</h2>
      <p class="muted" style="margin-bottom:20px">Add a stock you already own to your portfolio</p>

      <label class="field-label">Ticker Symbol</label>
      <input id="as-ticker" class="field-input" placeholder="AAPL" style="text-transform:uppercase" oninput="this.value=this.value.toUpperCase()" />

      <label class="field-label" style="margin-top:12px">Number of Shares</label>
      <input id="as-shares" class="field-input" type="number" placeholder="10" min="0.0001" step="any" />

      <label class="field-label" style="margin-top:12px">Average Cost Per Share ($)</label>
      <input id="as-cost" class="field-input" type="number" placeholder="150.00" min="0" step="any" />

      <label class="field-label" style="margin-top:12px">Date Acquired</label>
      <input id="as-date" class="field-input" type="date" />

      <div id="as-err" class="form-err"></div>
      <button class="run-btn" style="margin-top:20px;width:100%" onclick="doAddStock()">Add to Portfolio</button>
    </div>
  </div>

  <!-- Add User Modal -->
  <div id="modal-overlay" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:100;overflow-y:auto;padding:20px">
    <div style="background:white;border-radius:16px;max-width:520px;margin:40px auto;padding:24px;position:relative">
      <button onclick="closeModal()" style="position:absolute;top:16px;right:16px;background:none;border:none;font-size:20px;cursor:pointer;color:#6b7280">✕</button>

      <!-- Step 1: Register -->
      <div id="step1">
        <h2 style="margin-bottom:4px">Create Account</h2>
        <p class="muted" style="margin-bottom:20px">Step 1 of 2 — Your details</p>
        <label class="field-label">Full Name</label>
        <input id="reg-name" class="field-input" placeholder="Jane Smith" />
        <div id="step1-err" class="form-err"></div>
        <button class="run-btn" style="margin-top:20px;width:100%" onclick="doRegister()">Continue →</button>
      </div>

      <!-- Step 2: Onboard -->
      <div id="step2" style="display:none">
        <h2 style="margin-bottom:4px">Your Portfolio</h2>
        <p class="muted" style="margin-bottom:20px">Step 2 of 2 — Add existing holdings (optional)</p>

        <label style="display:flex;align-items:center;gap:10px;font-size:14px;margin-bottom:16px;cursor:pointer">
          <input type="checkbox" id="has-stocks" onchange="toggleHoldings(this.checked)"
                 style="width:16px;height:16px;accent-color:#2563eb">
          I already own stocks I want to track
        </label>

        <div id="holdings-section" style="display:none">
          <div style="display:grid;grid-template-columns:1fr 1fr 1fr 1fr auto;gap:6px;margin-bottom:6px">
            <span style="font-size:11px;font-weight:600;color:#6b7280;text-transform:uppercase">Ticker</span>
            <span style="font-size:11px;font-weight:600;color:#6b7280;text-transform:uppercase">Shares</span>
            <span style="font-size:11px;font-weight:600;color:#6b7280;text-transform:uppercase">Avg Cost</span>
            <span style="font-size:11px;font-weight:600;color:#6b7280;text-transform:uppercase">Date</span>
            <span></span>
          </div>
          <div id="holdings-rows"></div>
          <button onclick="addHoldingRow()" style="background:none;border:1px dashed #d1d5db;border-radius:8px;padding:8px;width:100%;color:#6b7280;font-size:13px;cursor:pointer;margin-top:6px">+ Add stock</button>
        </div>

        <div id="step2-err" class="form-err"></div>
        <button class="run-btn" style="margin-top:20px;width:100%" onclick="doOnboard()">Finish Setup ✓</button>
      </div>

      <!-- Done -->
      <div id="step-done" style="display:none;text-align:center;padding:20px 0">
        <div style="font-size:48px;margin-bottom:12px">🎉</div>
        <h2>You're all set!</h2>
        <p class="muted" style="margin-top:6px">Reloading dashboard…</p>
      </div>
    </div>
  </div>

  <div id="toast" class="toast"></div>

  <style>
    .field-label {{ display:block;font-size:13px;font-weight:600;color:#374151;margin-bottom:4px }}
    .field-input {{
      width:100%;padding:10px 12px;border:1.5px solid #d1d5db;border-radius:8px;
      font-size:14px;outline:none;transition:border .15s
    }}
    .field-input:focus {{ border-color:#2563eb }}
    .form-err {{ color:#dc2626;font-size:13px;margin-top:8px;min-height:18px }}
    .holding-row {{
      display:grid;grid-template-columns:1fr 1fr 1fr 1fr auto;
      gap:6px;margin-bottom:6px;align-items:center
    }}
    .holding-row input {{
      padding:8px;border:1.5px solid #d1d5db;border-radius:6px;
      font-size:13px;width:100%;outline:none
    }}
    .holding-row input:focus {{ border-color:#2563eb }}
    .del-btn {{
      background:none;border:none;color:#ef4444;font-size:18px;
      cursor:pointer;padding:0 4px;line-height:1
    }}
  </style>

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
          <div style="display:flex;align-items:center;justify-content:space-between;
                      padding:10px 0;border-bottom:1px solid #f3f4f6">
            <div>
              <strong style="font-size:15px">${{h.ticker}}</strong>
              <span class="muted" style="margin-left:8px">${{h.shares}} sh @ $${{h.avg_cost_basis.toFixed(2)}}</span>
            </div>
            <button onclick="removeHolding(${{h.id}}, '${{h.ticker}}')"
                    style="background:#fee2e2;color:#dc2626;border:none;border-radius:6px;
                           padding:6px 12px;font-size:13px;font-weight:600;cursor:pointer">
              Remove
            </button>
          </div>`;
        }}
        document.getElementById('manage-list').innerHTML = rows;
      }} catch(e) {{
        document.getElementById('manage-list').innerHTML = `<p style="color:#dc2626">${{e.message}}</p>`;
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
      document.getElementById('as-date').value = new Date().toISOString().slice(0,10);
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
      const date   = document.getElementById('as-date').value.trim();
      const err    = document.getElementById('as-err');
      if (!ticker || !shares || !cost || !date) {{ err.textContent = 'All fields are required.'; return; }}
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
      const today = new Date().toISOString().slice(0,10);
      const row = document.createElement('div');
      row.className = 'holding-row';
      row.innerHTML = `
        <input placeholder="AAPL" style="text-transform:uppercase" oninput="this.value=this.value.toUpperCase()">
        <input type="number" placeholder="10" min="0.0001" step="any">
        <input type="number" placeholder="150.00" min="0" step="any">
        <input type="date" value="${{today}}">
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
          const [ticker, shares, cost, date] = [...row.querySelectorAll('input')].map(i => i.value.trim());
          if (!ticker || !shares || !cost || !date) {{ err.textContent = 'Fill in all fields for each holding.'; return; }}
          holdings.push({{ ticker, shares: parseFloat(shares), avg_cost_basis: parseFloat(cost), acquired_date: date }});
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
      btn.textContent = '⏳ Sending…';
      try {{
        const res = await fetch(`/messaging/send/${{userId}}`, {{ method: 'POST', headers: _authHeaders }});
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || 'Unknown error');
        showToast(`✅ Email sent — ${{data.subject}}`, 'ok');
      }} catch (e) {{
        showToast('❌ ' + e.message, 'err');
      }} finally {{
        btn.disabled = false;
        btn.textContent = '✉ Send Email';
      }}
    }}

    async function runDaily(userId, btn) {{
      btn.disabled = true;
      btn.textContent = '⏳ Running…';
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
        btn.textContent = '▶ Run Analysis';
      }}
    }}

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
