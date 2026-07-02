import sqlite3
from datetime import datetime

from app.database import get_db
from app.finnhub_service import get_quote, get_candles, _get_client
from app.sentiment_service import refresh_sentiment_for_ticker


PRICE_WEIGHT = 0.15
TECHNICAL_WEIGHT = 0.30
SENTIMENT_WEIGHT = 0.25
ANALYST_WEIGHT = 0.20
EARNINGS_WEIGHT = 0.10

STRONG_BUY_THRESHOLD = 0.3
BUY_THRESHOLD = 0.1
SELL_THRESHOLD = -0.1
STRONG_SELL_THRESHOLD = -0.3

STOP_LOSS_PCT = 15.0    # nudge toward SELL if down more than this %
TAKE_PROFIT_PCT = 25.0  # nudge toward SELL if up more than this %


def _fetch_quote_cached(ticker: str, cache: dict[str, dict]) -> dict:
    if ticker in cache:
        return cache[ticker]
    try:
        quote = get_quote(ticker)
    except Exception:
        quote = {}
    cache[ticker] = quote
    return quote


def _fetch_candles_cached(ticker: str, cache: dict[str, list]) -> list[dict]:
    if ticker in cache:
        return cache[ticker]
    try:
        candles = get_candles(ticker, resolution="D", days=60)
    except Exception:
        candles = []
    cache[ticker] = candles
    return candles


def _compute_technical_score(ticker: str, candle_cache: dict[str, list]) -> tuple[float, str]:
    candles = _fetch_candles_cached(ticker, candle_cache)

    if len(candles) < 20:
        return 0.0, "Insufficient historical data for technical analysis"

    closes = [c["close"] for c in candles]
    volumes = [c["volume"] for c in candles]
    reasons = []
    score_parts: list[tuple[float, float]] = []

    # --- SMA 20/50 crossover (weight 0.5) ---
    sma20 = sum(closes[-20:]) / 20
    sma_score = 0.0
    if len(closes) >= 50:
        sma50 = sum(closes[-50:]) / 50
        sma_diff_pct = (sma20 - sma50) / sma50
        sma_score = max(-1.0, min(1.0, sma_diff_pct * 10))
        direction = "above" if sma20 > sma50 else "below"
        reasons.append(f"SMA20 ${sma20:.2f} {direction} SMA50 ${sma50:.2f}")
    else:
        reasons.append(f"SMA20 ${sma20:.2f} (need 50 days for crossover)")
    score_parts.append((sma_score, 0.5))

    # --- RSI 14-day (weight 0.3) ---
    rsi_score = 0.0
    if len(closes) >= 15:
        deltas = [closes[i] - closes[i - 1] for i in range(len(closes) - 14, len(closes))]
        gains = [d for d in deltas if d > 0]
        losses = [-d for d in deltas if d < 0]
        avg_gain = sum(gains) / 14 if gains else 0.0
        avg_loss = sum(losses) / 14 if losses else 0.0
        if avg_loss == 0:
            rsi = 100.0
        else:
            rs = avg_gain / avg_loss
            rsi = 100 - (100 / (1 + rs))
        # Oversold (<30) → bullish (+1), overbought (>70) → bearish (-1), 50 → neutral
        rsi_score = max(-1.0, min(1.0, (50 - rsi) / 20))
        label = "oversold" if rsi < 30 else "overbought" if rsi > 70 else "neutral"
        reasons.append(f"RSI(14): {rsi:.1f} ({label})")
    score_parts.append((rsi_score, 0.3))

    # --- Volume spike confirmation (weight 0.2) ---
    vol_score = 0.0
    if len(volumes) >= 10:
        recent_vol = sum(volumes[-5:]) / 5
        avg_vol = sum(volumes[-20:]) / min(len(volumes), 20)
        if avg_vol > 0:
            vol_ratio = recent_vol / avg_vol
            price_direction = 1 if closes[-1] > closes[-6] else -1
            vol_score = max(-1.0, min(1.0, (vol_ratio - 1.0) * price_direction))
            arrow = "↑" if price_direction > 0 else "↓"
            reasons.append(f"Volume {vol_ratio:.1f}x avg {arrow}")
    score_parts.append((vol_score, 0.2))

    final = sum(s * w for s, w in score_parts)
    return round(final, 4), " | ".join(reasons)


def _compute_price_score(
    ticker: str, conn: sqlite3.Connection, quote_cache: dict[str, dict]
) -> tuple[float, float, str]:
    quote = _fetch_quote_cached(ticker, quote_cache)

    if not quote or not quote.get("current_price"):
        snapshot = conn.execute(
            """SELECT current_price, percent_change FROM price_snapshots
               WHERE ticker = ? ORDER BY fetched_at DESC LIMIT 1""",
            (ticker,),
        ).fetchone()
        if snapshot:
            pct = snapshot["percent_change"] or 0.0
            score = max(-1.0, min(1.0, pct / 5.0))
            return score, snapshot["current_price"], f"Price from snapshot: ${snapshot['current_price']:.2f} ({pct:+.2f}%)"
        return 0.0, 0.0, "No price data available"

    pct = quote.get("percent_change", 0.0) or 0.0
    score = max(-1.0, min(1.0, pct / 5.0))

    conn.execute(
        """INSERT INTO price_snapshots
           (ticker, current_price, change, percent_change, high, low, open, previous_close)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            quote["ticker"],
            quote["current_price"],
            quote.get("change"),
            quote.get("percent_change"),
            quote.get("high"),
            quote.get("low"),
            quote.get("open"),
            quote.get("previous_close"),
        ),
    )

    return score, quote["current_price"], f"Current: ${quote['current_price']:.2f} ({pct:+.2f}%)"


def _compute_sentiment_score(ticker: str, conn: sqlite3.Connection) -> tuple[float, str]:
    rows = conn.execute(
        """SELECT sentiment_compound, sentiment_label FROM news_articles
           WHERE ticker = ? ORDER BY analyzed_at DESC LIMIT 20""",
        (ticker,),
    ).fetchall()
    if not rows:
        return 0.0, "No sentiment data"

    compounds = [r["sentiment_compound"] for r in rows if r["sentiment_compound"] is not None]
    if not compounds:
        return 0.0, "No sentiment scores"

    avg = sum(compounds) / len(compounds)
    bullish = sum(1 for r in rows if r["sentiment_label"] == "bullish")
    bearish = sum(1 for r in rows if r["sentiment_label"] == "bearish")
    score = max(-1.0, min(1.0, avg * 2))
    return score, f"Avg sentiment: {avg:.3f} ({bullish}B/{bearish}S from {len(rows)} articles)"


def _compute_analyst_score(ticker: str, cache: dict[str, dict]) -> tuple[float, str]:
    if ticker in cache:
        return cache[ticker]

    try:
        import yfinance as yf
        client = _get_client()

        # Analyst buy/sell consensus from Finnhub
        trends = client.recommendation_trends(ticker)
        consensus_score = 0.0
        consensus_reason = ""
        if trends:
            t = trends[0]
            strong_buy = t.get("strongBuy", 0)
            buy = t.get("buy", 0)
            hold = t.get("hold", 0)
            sell = t.get("sell", 0)
            strong_sell = t.get("strongSell", 0)
            total = strong_buy + buy + hold + sell + strong_sell
            if total > 0:
                # Weighted score: strongBuy=+1, buy=+0.5, hold=0, sell=-0.5, strongSell=-1
                weighted = (strong_buy * 1.0 + buy * 0.5 - sell * 0.5 - strong_sell * 1.0) / total
                consensus_score = max(-1.0, min(1.0, weighted))
                consensus_reason = f"Analysts: {strong_buy}SB/{buy}B/{hold}H/{sell}S/{strong_sell}SS"

        # Price target upside from yfinance
        info = yf.Ticker(ticker).info
        current = info.get("currentPrice") or info.get("regularMarketPrice", 0)
        target = info.get("targetMeanPrice", 0)
        target_score = 0.0
        target_reason = ""
        if current and target:
            upside_pct = (target - current) / current * 100
            # +20% upside → +1.0, -20% downside → -1.0
            target_score = max(-1.0, min(1.0, upside_pct / 20.0))
            n = info.get("numberOfAnalystOpinions", "?")
            target_reason = f"Price target ${target:.0f} ({upside_pct:+.1f}% upside, {n} analysts)"

        # Blend: 60% consensus, 40% price target
        final = consensus_score * 0.6 + target_score * 0.4
        reason = " | ".join(filter(None, [consensus_reason, target_reason])) or "No analyst data"
        result = (round(final, 4), reason)
    except Exception as e:
        result = (0.0, f"Analyst data unavailable: {e}")

    cache[ticker] = result
    return result


def _compute_earnings_score(ticker: str, cache: dict[str, dict]) -> tuple[float, str]:
    if ticker in cache:
        return cache[ticker]

    try:
        client = _get_client()
        eps_list = client.company_earnings(ticker, limit=4)
        if not eps_list:
            result = (0.0, "No earnings data")
            cache[ticker] = result
            return result

        surprises = []
        for e in eps_list:
            actual = e.get("actual")
            estimate = e.get("estimate")
            if actual is not None and estimate and abs(estimate) > 0.001:
                surprises.append((actual - estimate) / abs(estimate) * 100)

        if not surprises:
            result = (0.0, "No EPS surprise data")
            cache[ticker] = result
            return result

        avg_surprise = sum(surprises) / len(surprises)
        beats = sum(1 for s in surprises if s > 0)
        # ±10% avg surprise maps to ±1.0
        score = max(-1.0, min(1.0, avg_surprise / 10.0))
        quarters = len(surprises)
        result = (
            round(score, 4),
            f"EPS: {beats}/{quarters} beats, avg surprise {avg_surprise:+.1f}%",
        )
    except Exception as e:
        result = (0.0, f"Earnings data unavailable: {e}")

    cache[ticker] = result
    return result


def _signal_from_score(score: float) -> str:
    if score >= STRONG_BUY_THRESHOLD:
        return "STRONG_BUY"
    if score >= BUY_THRESHOLD:
        return "BUY"
    if score <= STRONG_SELL_THRESHOLD:
        return "STRONG_SELL"
    if score <= SELL_THRESHOLD:
        return "SELL"
    return "HOLD"


def score_ticker_standalone(
    ticker: str,
    quote_cache: dict,
    candle_cache: dict,
    analyst_cache: dict,
    earnings_cache: dict,
    conn,
) -> dict | None:
    """
    Run the full 5-component scoring on a ticker without any user-specific data
    (no P&L, no stop-loss, no affordable shares). Used by the deep screener.
    Does NOT refresh sentiment — uses cached news_articles data to avoid DB lock conflicts.
    Returns a dict with all scores and signal, or None if no price data.
    """

    price_score, current_price, price_reason = _compute_price_score(ticker, conn, quote_cache)
    if not current_price:
        return None

    tech_score, tech_reason   = _compute_technical_score(ticker, candle_cache)
    sent_score, sent_reason   = _compute_sentiment_score(ticker, conn)
    analyst_score, anal_reason = _compute_analyst_score(ticker, analyst_cache)
    earn_score, earn_reason   = _compute_earnings_score(ticker, earnings_cache)

    combined = (
        PRICE_WEIGHT     * price_score +
        TECHNICAL_WEIGHT * tech_score +
        SENTIMENT_WEIGHT * sent_score +
        ANALYST_WEIGHT   * analyst_score +
        EARNINGS_WEIGHT  * earn_score
    )
    combined = round(combined, 4)
    signal   = _signal_from_score(combined)

    reason = " | ".join(filter(None, [price_reason, tech_reason, sent_reason, anal_reason, earn_reason]))

    return {
        "ticker":          ticker,
        "signal":          signal,
        "combined_score":  combined,
        "price_score":     round(price_score, 4),
        "technical_score": round(tech_score, 4),
        "sentiment_score": round(sent_score, 4),
        "analyst_score":   round(analyst_score, 4),
        "earnings_score":  round(earn_score, 4),
        "current_price":   current_price,
        "reason":          reason,
    }


def generate_recommendations(user_id: int) -> list[dict]:
    with get_db() as conn:
        user = conn.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
        if not user:
            return []

        holdings = conn.execute(
            "SELECT * FROM holdings WHERE user_id = ?", (user_id,)
        ).fetchall()
        watchlist = conn.execute(
            "SELECT DISTINCT ticker FROM watchlist WHERE user_id = ?", (user_id,)
        ).fetchall()

        today = datetime.utcnow().strftime("%Y-%m-%d")
        budget_row = conn.execute(
            "SELECT * FROM daily_budgets WHERE user_id = ? AND date = ?",
            (user_id, today),
        ).fetchone()

        if budget_row:
            remaining_budget = (
                budget_row["base_budget"]
                + budget_row["sell_profits"]
                - budget_row["amount_spent"]
            )
        else:
            remaining_budget = 100.0

        holding_map: dict[str, dict] = {}
        for h in holdings:
            holding_map[h["ticker"]] = {
                "shares": h["shares"],
                "avg_cost_basis": h["avg_cost_basis"],
                "alert_triggered": h["alert_triggered"] if "alert_triggered" in h.keys() else 0,
                "alert_date": h["alert_date"] if "alert_date" in h.keys() else None,
            }

        all_tickers = sorted(
            set(h["ticker"] for h in holdings) | set(r["ticker"] for r in watchlist)
        )

        # Fetch external API data in parallel (no DB access in threads)
        from concurrent.futures import ThreadPoolExecutor, as_completed

        quote_cache: dict[str, dict] = {}
        candle_cache: dict[str, list] = {}
        analyst_cache: dict[str, tuple] = {}
        earnings_cache: dict[str, tuple] = {}

        def _prefetch_external(ticker: str):
            _fetch_quote_cached(ticker, quote_cache)
            _fetch_candles_cached(ticker, candle_cache)
            _compute_analyst_score(ticker, analyst_cache)
            _compute_earnings_score(ticker, earnings_cache)

        if not all_tickers:
            return []

        with ThreadPoolExecutor(max_workers=min(len(all_tickers), 2)) as ex:
            futures = {ex.submit(_prefetch_external, t): t for t in all_tickers}
            for f in as_completed(futures):
                f.result()

        # Sentiment refresh uses DB connection — must stay sequential
        for ticker in all_tickers:
            refresh_sentiment_for_ticker(ticker, conn)

        results = []
        for ticker in all_tickers:
            price_score, current_price, price_reason = _compute_price_score(ticker, conn, quote_cache)
            tech_score, tech_reason = _compute_technical_score(ticker, candle_cache)
            sent_score, sent_reason = _compute_sentiment_score(ticker, conn)
            analyst_score, analyst_reason = _compute_analyst_score(ticker, analyst_cache)
            earnings_score, earnings_reason = _compute_earnings_score(ticker, earnings_cache)

            combined = (
                price_score * PRICE_WEIGHT
                + tech_score * TECHNICAL_WEIGHT
                + sent_score * SENTIMENT_WEIGHT
                + analyst_score * ANALYST_WEIGHT
                + earnings_score * EARNINGS_WEIGHT
            )
            signal = _signal_from_score(combined)

            affordable_shares = None
            unrealized_gl = None
            pl_pct = None
            personal_adjustment = 0.0
            reasons = [price_reason, tech_reason, sent_reason, analyst_reason, earnings_reason]

            alert_triggered = False
            alert_date = None
            if ticker in holding_map:
                h = holding_map[ticker]
                alert_triggered = bool(h.get("alert_triggered"))
                alert_date = h.get("alert_date")
                if current_price > 0 and h["avg_cost_basis"] > 0:
                    cost_basis = h["shares"] * h["avg_cost_basis"]
                    market_val = h["shares"] * current_price
                    unrealized_gl = round(market_val - cost_basis, 2)
                    pl_pct = round((current_price - h["avg_cost_basis"]) / h["avg_cost_basis"] * 100, 2)

                    if pl_pct < -STOP_LOSS_PCT:
                        personal_adjustment = -0.20
                        reasons.append(f"Stop-loss: down {abs(pl_pct):.1f}% from cost basis (${h['avg_cost_basis']:.2f})")
                    elif pl_pct > TAKE_PROFIT_PCT:
                        personal_adjustment = -0.15
                        reasons.append(f"Take-profit: up {pl_pct:.1f}% from cost basis (${h['avg_cost_basis']:.2f})")
                    else:
                        reasons.append(f"Position P&L: {pl_pct:+.1f}% (cost basis ${h['avg_cost_basis']:.2f})")

                if alert_triggered and alert_date:
                    reasons.append(f"Opened via BUY alert on {alert_date}")
            else:
                reasons.append("Not currently held (watchlist)")

            combined = round(combined + personal_adjustment, 4)
            signal = _signal_from_score(combined)
            reason = " | ".join(filter(None, reasons))

            if signal in ("BUY", "STRONG_BUY") and current_price > 0:
                affordable_shares = round(remaining_budget / current_price, 4)
                if affordable_shares > 0:
                    reason += f" | Can afford {affordable_shares:.2f} shares with ${remaining_budget:.2f} budget"
                else:
                    reason += f" | Insufficient budget (${remaining_budget:.2f})"

            rec = {
                "user_id": user_id,
                "ticker": ticker,
                "signal": signal,
                "combined_score": combined,
                "price_score": round(price_score, 4),
                "technical_score": round(tech_score, 4),
                "sentiment_score": round(sent_score, 4),
                "analyst_score": round(analyst_score, 4),
                "earnings_score": round(earnings_score, 4),
                "current_price": current_price if current_price > 0 else None,
                "reason": reason,
                "affordable_shares": affordable_shares,
                "unrealized_gain_loss": unrealized_gl,
                "pl_pct": pl_pct,
                "alert_triggered": alert_triggered,
                "alert_date": alert_date,
            }
            results.append(rec)

        results.sort(key=lambda r: abs(r["combined_score"]), reverse=True)

        for rec in results:
            conn.execute(
                """INSERT INTO recommendations
                   (user_id, ticker, signal, combined_score, price_score, technical_score,
                    sentiment_score, current_price, reason, affordable_shares, unrealized_gain_loss)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    rec["user_id"],
                    rec["ticker"],
                    rec["signal"],
                    rec["combined_score"],
                    rec["price_score"],
                    rec["technical_score"],
                    rec["sentiment_score"],
                    rec["current_price"],
                    rec["reason"],
                    rec["affordable_shares"],
                    rec["unrealized_gain_loss"],
                ),
            )

    return results
