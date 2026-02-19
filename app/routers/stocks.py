from fastapi import APIRouter, HTTPException, Query
from app.database import get_db
from app.finnhub_service import (
    get_quote,
    get_company_profile,
    search_symbol,
    get_candles,
    get_company_news,
)
from app.schemas import (
    StockQuoteResponse,
    CompanyProfileResponse,
    SymbolSearchResult,
    CandleResponse,
    NewsArticleResponse,
    PriceSnapshotResponse,
    WatchlistItemResponse,
    PortfolioSummaryItem,
    PortfolioSummaryResponse,
)

router = APIRouter(prefix="/stocks", tags=["stocks"])


@router.get("/quote/{ticker}", response_model=StockQuoteResponse)
def stock_quote(ticker: str):
    try:
        data = get_quote(ticker)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    if not data:
        raise HTTPException(status_code=404, detail=f"No quote data found for {ticker.upper()}")
    with get_db() as conn:
        conn.execute(
            """INSERT INTO price_snapshots
               (ticker, current_price, change, percent_change, high, low, open, previous_close)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                data["ticker"],
                data["current_price"],
                data.get("change"),
                data.get("percent_change"),
                data.get("high"),
                data.get("low"),
                data.get("open"),
                data.get("previous_close"),
            ),
        )
    return StockQuoteResponse(**data)


@router.get("/profile/{ticker}", response_model=CompanyProfileResponse)
def company_profile(ticker: str):
    try:
        data = get_company_profile(ticker)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    if not data:
        raise HTTPException(status_code=404, detail=f"No profile found for {ticker.upper()}")
    return CompanyProfileResponse(**data)


@router.get("/search", response_model=list[SymbolSearchResult])
def symbol_search(q: str = Query(..., min_length=1)):
    try:
        results = search_symbol(q)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return [SymbolSearchResult(**r) for r in results]


@router.get("/candles/{ticker}", response_model=list[CandleResponse])
def stock_candles(
    ticker: str,
    resolution: str = Query("D", pattern="^(1|5|15|30|60|D|W|M)$"),
    days: int = Query(30, ge=1, le=365),
):
    try:
        data = get_candles(ticker, resolution, days)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    if not data:
        raise HTTPException(status_code=404, detail=f"No candle data found for {ticker.upper()}")
    return [CandleResponse(**c) for c in data]


@router.get("/news/{ticker}", response_model=list[NewsArticleResponse])
def stock_news(ticker: str, days: int = Query(7, ge=1, le=30)):
    try:
        articles = get_company_news(ticker, days)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return [NewsArticleResponse(**a) for a in articles]


@router.get("/history/{ticker}", response_model=list[PriceSnapshotResponse])
def price_history(ticker: str, limit: int = Query(50, ge=1, le=500)):
    with get_db() as conn:
        rows = conn.execute(
            """SELECT * FROM price_snapshots
               WHERE ticker = ?
               ORDER BY fetched_at DESC
               LIMIT ?""",
            (ticker.upper(), limit),
        ).fetchall()
    if not rows:
        raise HTTPException(status_code=404, detail=f"No price history for {ticker.upper()}")
    return [_snapshot_from_row(r) for r in rows]


@router.get("/users/{user_id}/watchlist", response_model=list[WatchlistItemResponse])
def get_watchlist(user_id: int):
    with get_db() as conn:
        _require_user(conn, user_id)
        rows = conn.execute(
            "SELECT * FROM watchlist WHERE user_id = ? ORDER BY ticker",
            (user_id,),
        ).fetchall()
    return [
        WatchlistItemResponse(
            id=r["id"], user_id=r["user_id"], ticker=r["ticker"], added_at=r["added_at"]
        )
        for r in rows
    ]


@router.post("/users/{user_id}/watchlist/{ticker}", response_model=WatchlistItemResponse, status_code=201)
def add_to_watchlist(user_id: int, ticker: str):
    with get_db() as conn:
        _require_user(conn, user_id)
        existing = conn.execute(
            "SELECT id FROM watchlist WHERE user_id = ? AND ticker = ?",
            (user_id, ticker.upper()),
        ).fetchone()
        if existing:
            raise HTTPException(status_code=409, detail=f"{ticker.upper()} is already in watchlist")
        cursor = conn.execute(
            "INSERT INTO watchlist (user_id, ticker) VALUES (?, ?)",
            (user_id, ticker.upper()),
        )
        row = conn.execute(
            "SELECT * FROM watchlist WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
    return WatchlistItemResponse(
        id=row["id"], user_id=row["user_id"], ticker=row["ticker"], added_at=row["added_at"]
    )


@router.delete("/users/{user_id}/watchlist/{ticker}", status_code=204)
def remove_from_watchlist(user_id: int, ticker: str):
    with get_db() as conn:
        _require_user(conn, user_id)
        row = conn.execute(
            "SELECT id FROM watchlist WHERE user_id = ? AND ticker = ?",
            (user_id, ticker.upper()),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"{ticker.upper()} not in watchlist")
        conn.execute("DELETE FROM watchlist WHERE id = ?", (row["id"],))
    return None


@router.get("/users/{user_id}/portfolio-summary", response_model=PortfolioSummaryResponse)
def portfolio_summary(user_id: int):
    with get_db() as conn:
        _require_user(conn, user_id)
        holdings = conn.execute(
            "SELECT * FROM holdings WHERE user_id = ? ORDER BY ticker",
            (user_id,),
        ).fetchall()

    items: list[PortfolioSummaryItem] = []
    total_cost = 0.0
    total_market = 0.0
    has_prices = True

    for h in holdings:
        cost_basis = h["shares"] * h["avg_cost_basis"]
        total_cost += cost_basis
        try:
            quote = get_quote(h["ticker"])
        except Exception:
            quote = {}

        if quote and quote.get("current_price"):
            price = quote["current_price"]
            market_val = h["shares"] * price
            gl = market_val - cost_basis
            gl_pct = (gl / cost_basis * 100) if cost_basis > 0 else 0.0
            total_market += market_val
            items.append(PortfolioSummaryItem(
                ticker=h["ticker"],
                shares=h["shares"],
                avg_cost_basis=h["avg_cost_basis"],
                current_price=price,
                market_value=round(market_val, 2),
                gain_loss=round(gl, 2),
                gain_loss_percent=round(gl_pct, 2),
            ))
        else:
            has_prices = False
            items.append(PortfolioSummaryItem(
                ticker=h["ticker"],
                shares=h["shares"],
                avg_cost_basis=h["avg_cost_basis"],
                current_price=None,
                market_value=None,
                gain_loss=None,
                gain_loss_percent=None,
            ))

    return PortfolioSummaryResponse(
        user_id=user_id,
        holdings=items,
        total_cost_basis=round(total_cost, 2),
        total_market_value=round(total_market, 2) if has_prices else None,
        total_gain_loss=round(total_market - total_cost, 2) if has_prices else None,
    )


@router.post("/users/{user_id}/refresh-prices")
def refresh_portfolio_prices(user_id: int):
    with get_db() as conn:
        _require_user(conn, user_id)
        holdings = conn.execute(
            "SELECT DISTINCT ticker FROM holdings WHERE user_id = ?",
            (user_id,),
        ).fetchall()
        watchlist = conn.execute(
            "SELECT DISTINCT ticker FROM watchlist WHERE user_id = ?",
            (user_id,),
        ).fetchall()

    tickers = list({r["ticker"] for r in holdings} | {r["ticker"] for r in watchlist})
    results = []
    for ticker in sorted(tickers):
        try:
            data = get_quote(ticker)
            if data:
                with get_db() as conn:
                    conn.execute(
                        """INSERT INTO price_snapshots
                           (ticker, current_price, change, percent_change, high, low, open, previous_close)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            data["ticker"],
                            data["current_price"],
                            data.get("change"),
                            data.get("percent_change"),
                            data.get("high"),
                            data.get("low"),
                            data.get("open"),
                            data.get("previous_close"),
                        ),
                    )
                results.append({"ticker": ticker, "status": "ok", "price": data["current_price"]})
            else:
                results.append({"ticker": ticker, "status": "no_data"})
        except Exception as e:
            results.append({"ticker": ticker, "status": "error", "detail": str(e)})
    return {"refreshed": len(results), "results": results}


def _require_user(conn, user_id: int) -> None:
    row = conn.execute(
        "SELECT id FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="User not found")


def _snapshot_from_row(row) -> PriceSnapshotResponse:
    return PriceSnapshotResponse(
        id=row["id"],
        ticker=row["ticker"],
        current_price=row["current_price"],
        change=row["change"],
        percent_change=row["percent_change"],
        high=row["high"],
        low=row["low"],
        open=row["open"],
        previous_close=row["previous_close"],
        fetched_at=row["fetched_at"],
    )
