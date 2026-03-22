"""
Lightweight daily stock screener.

Scores ~50 stocks sampled from the current S&P 500 using:
  - Price momentum   (Finnhub quote — 1 API call per stock)
  - Cached sentiment (news_articles table — no API call)

The S&P 500 universe is fetched from Wikipedia once and cached for 24 hours.
This keeps total API usage to ~50 calls, well within Finnhub free tier limits.
Returns the top N candidates that aren't already in the user's holdings/watchlist.
"""
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# --- S&P 500 universe cache ---
_universe_cache: list[str] = []
_universe_fetched_at: datetime | None = None
_CACHE_TTL_HOURS = 24

# --- Screener results cache (populated by pre-warm, consumed by email job) ---
_results_cache: list[dict] = []
_results_cached_at: datetime | None = None
_RESULTS_TTL_HOURS = 2  # results older than 2 hours are considered stale


def _fetch_sp500_universe() -> list[str]:
    """
    Fetch the current S&P 500 constituents from Wikipedia and return a
    representative sample of ~50 tickers spread across all sectors.
    Cached for 24 hours so we don't hit Wikipedia on every run.
    """
    global _universe_cache, _universe_fetched_at

    now = datetime.utcnow()
    if (
        _universe_cache
        and _universe_fetched_at
        and now - _universe_fetched_at < timedelta(hours=_CACHE_TTL_HOURS)
        and len(_universe_cache) > 20  # guard against stale partial cache
    ):
        return _universe_cache

    try:
        import io
        import pandas as pd
        import urllib.request

        req = urllib.request.Request(
            "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
            headers={"User-Agent": "Mozilla/5.0 (compatible; stock-analyzer/1.0)"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8")

        tables = pd.read_html(io.StringIO(html), attrs={"id": "constituents"})
        df = tables[0]

        # Normalize ticker column (Wikipedia uses '.' for BRK.B etc — yfinance uses '-')
        df["Symbol"] = df["Symbol"].str.replace(".", "-", regex=False)

        tickers = df["Symbol"].tolist()

        _universe_cache = tickers
        _universe_fetched_at = now
        logger.info(f"S&P 500 universe loaded: {len(tickers)} tickers across {df['GICS Sector'].nunique()} sectors")
        return tickers

    except Exception as e:
        logger.warning(f"Could not fetch S&P 500 from Wikipedia ({e}) — using cached or fallback list")
        if _universe_cache:
            return _universe_cache
        # Minimal fallback if Wikipedia is unreachable
        return [
            "AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "JPM", "BAC",
            "JNJ", "UNH", "XOM", "CVX", "WMT", "COST", "CAT", "BA",
        ]


def run_deep_screener() -> None:
    """
    Run the full 5-component analysis on the entire S&P 500 universe and
    store results in the screener_results table. Designed to run at 6 AM CT
    so fresh data is ready when the 7:30 AM email fires.

    Rate-limited to ~60 Finnhub calls/minute using a 3-stock batch with 3s delays.
    Each stock uses ~5 API calls → ~440 calls total → ~7 minutes to complete.
    """
    from app.database import get_db
    from app.recommendation_engine import score_ticker_standalone

    universe = _fetch_sp500_universe()
    logger.info(f"Deep screener starting — {len(universe)} stocks to analyze")

    quote_cache    = {}
    candle_cache   = {}
    analyst_cache  = {}
    earnings_cache = {}

    BATCH_SIZE = 3
    BATCH_DELAY = 3.0

    results = []
    batches = [universe[i:i + BATCH_SIZE] for i in range(0, len(universe), BATCH_SIZE)]

    for batch_idx, batch in enumerate(batches):
        if batch_idx > 0:
            time.sleep(BATCH_DELAY)
        for ticker in batch:
            try:
                # Use a fresh connection per ticker to avoid long-held write locks
                with get_db() as conn:
                    result = score_ticker_standalone(
                        ticker, quote_cache, candle_cache,
                        analyst_cache, earnings_cache, conn
                    )
                if result:
                    results.append(result)
            except Exception as e:
                logger.warning(f"Deep screener failed for {ticker}: {e}")

    if not results:
        logger.warning("Deep screener returned no results — check Finnhub API key and rate limits")
        return

    # Store results — clear old rows first then insert fresh batch
    with get_db() as conn:
        conn.execute("DELETE FROM screener_results")
        conn.executemany(
            """INSERT INTO screener_results
               (ticker, signal, combined_score, price_score, technical_score,
                sentiment_score, analyst_score, earnings_score, current_price, reason)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    r["ticker"], r["signal"], r["combined_score"],
                    r["price_score"], r["technical_score"], r["sentiment_score"],
                    r["analyst_score"], r["earnings_score"],
                    r["current_price"], r["reason"],
                )
                for r in results
            ],
        )

    logger.info(f"Deep screener complete — {len(results)} stocks scored and stored")


def warm_screener(exclude_tickers: set[str], top_n: int = 5) -> None:
    """No-op — kept for backward compatibility. Deep screener runs at 6 AM CT."""
    logger.info("warm_screener called — deep screener already ran at 6 AM CT")


def get_cached_results(exclude_tickers: set[str], top_n: int = 5) -> list[dict]:
    """
    Read the top buy candidates from screener_results (populated by the 6 AM job).
    Excludes tickers already in holdings or watchlist.
    Falls back to empty list if the deep screener hasn't run yet today.
    """
    from app.database import get_db

    with get_db() as conn:
        rows = conn.execute(
            """SELECT ticker, signal, combined_score, price_score, technical_score,
                      sentiment_score, analyst_score, earnings_score, current_price, reason,
                      analyzed_at
               FROM screener_results
               WHERE signal IN ('STRONG_BUY', 'BUY')
                 AND analyzed_at >= datetime('now', '-20 hours')
               ORDER BY combined_score DESC""",
        ).fetchall()

    results = [
        {
            "ticker":          r["ticker"],
            "signal":          r["signal"],
            "combined_score":  r["combined_score"],
            "price_score":     r["price_score"],
            "technical_score": r["technical_score"],
            "sentiment_score": r["sentiment_score"],
            "analyst_score":   r["analyst_score"],
            "earnings_score":  r["earnings_score"],
            "current_price":   r["current_price"],
            "reason":          r["reason"],
        }
        for r in rows
        if r["ticker"] not in exclude_tickers
    ]
    return results[:top_n]
