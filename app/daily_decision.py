"""
Orchestrates the once-daily LLM portfolio decision.

Gathers everything the formula engine already produced (per-ticker
recommendations, screener candidates, recent headlines, remaining budget),
hands it to llm_analysis.decide_portfolio() in a single call, validates the
result against what the user actually owns, and persists it to the
daily_decisions table for the dashboard/email and later evaluation.
"""
import json
import logging
from datetime import datetime

from app.database import get_db
from app.llm_analysis import MODEL, decide_portfolio
from app.screener import get_cached_results

logger = logging.getLogger(__name__)

MAX_CANDIDATES = 15
HEADLINES_PER_TICKER = 3


def _recent_headlines(conn, ticker: str, limit: int = HEADLINES_PER_TICKER) -> list[dict]:
    rows = conn.execute(
        """SELECT headline, sentiment_label FROM news_articles
           WHERE ticker = ? ORDER BY analyzed_at DESC LIMIT ?""",
        (ticker, limit),
    ).fetchall()
    return [{"headline": r["headline"], "sentiment_label": r["sentiment_label"]} for r in rows]


def _remaining_budget(conn, user_id: int) -> float:
    today = datetime.utcnow().strftime("%Y-%m-%d")
    row = conn.execute(
        "SELECT * FROM daily_budgets WHERE user_id = ? AND date = ?",
        (user_id, today),
    ).fetchone()
    if row:
        return row["base_budget"] + row["sell_profits"] - row["amount_spent"]
    return 100.0


def run_daily_decision(user_id: int, recs: list[dict]) -> dict | None:
    """
    Make and persist today's portfolio decision for a user.

    recs: the formula recommendations just produced by generate_recommendations().
    Returns the decision dict (with generated_at/model/token metadata) or None
    when the LLM is unavailable — callers fall back to formula signals.
    """
    with get_db() as conn:
        holding_rows = conn.execute(
            "SELECT ticker, shares, avg_cost_basis FROM holdings WHERE user_id = ?",
            (user_id,),
        ).fetchall()
        holding_map = {r["ticker"]: r for r in holding_rows}

        watchlist = {
            r["ticker"] for r in conn.execute(
                "SELECT DISTINCT ticker FROM watchlist WHERE user_id = ?", (user_id,)
            ).fetchall()
        }

        budget = _remaining_budget(conn, user_id)

        holdings_data = []
        candidates_data = []
        for rec in recs:
            ticker = rec["ticker"]
            base = {
                "ticker": ticker,
                "current_price": rec.get("current_price"),
                "signal": rec["signal"],
                "combined_score": rec["combined_score"],
                "reason": rec.get("reason") or "",
                "headlines": _recent_headlines(conn, ticker),
            }
            if ticker in holding_map:
                h = holding_map[ticker]
                base.update(
                    shares=h["shares"],
                    avg_cost_basis=h["avg_cost_basis"],
                    pl_pct=rec.get("pl_pct"),
                    unrealized_gain_loss=rec.get("unrealized_gain_loss"),
                )
                holdings_data.append(base)
            else:
                base["source"] = "watchlist"
                candidates_data.append(base)

        # Screener discoveries the user doesn't own or watch
        exclude = set(holding_map) | watchlist
        screener_rows = get_cached_results(exclude_tickers=exclude, top_n=MAX_CANDIDATES)
        for s in screener_rows:
            candidates_data.append({
                "ticker": s["ticker"],
                "current_price": s.get("current_price"),
                "signal": s["signal"],
                "combined_score": s["combined_score"],
                "reason": s.get("reason") or "",
                "headlines": _recent_headlines(conn, s["ticker"]),
                "source": "screener",
            })

        candidates_data = candidates_data[:MAX_CANDIDATES]

    if not holdings_data and not candidates_data:
        logger.info(f"User {user_id}: nothing to decide on — skipping LLM decision")
        return None

    result = decide_portfolio(holdings_data, candidates_data, budget)
    if result is None:
        return None
    decision, usage = result

    decision = _validate_decision(
        decision,
        holding_tickers=set(holding_map),
        candidate_tickers={c["ticker"] for c in candidates_data},
        budget=budget,
    )

    with get_db() as conn:
        cursor = conn.execute(
            """INSERT INTO daily_decisions
               (user_id, summary, decision_json, model, input_tokens, output_tokens)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                user_id,
                decision.get("summary", ""),
                json.dumps(decision),
                MODEL,
                usage.get("input_tokens"),
                usage.get("output_tokens"),
            ),
        )
        row = conn.execute(
            "SELECT generated_at FROM daily_decisions WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()

    decision["model"] = MODEL
    decision["generated_at"] = row["generated_at"] if row else None
    decision["usage"] = usage
    logger.info(
        f"User {user_id}: daily decision — {len(decision['buys'])} buy, "
        f"{len(decision['sells'])} sell, {len(decision['holds'])} hold "
        f"({usage.get('input_tokens')}in/{usage.get('output_tokens')}out tokens)"
    )
    return decision


def _validate_decision(
    decision: dict,
    holding_tickers: set[str],
    candidate_tickers: set[str],
    budget: float,
) -> dict:
    """Enforce the hard rules even if the model slips: buys only from
    candidates, sells/holds only from holdings, allocations within budget."""
    buys = [b for b in decision.get("buys", []) if b.get("ticker") in candidate_tickers]
    sells = [s for s in decision.get("sells", []) if s.get("ticker") in holding_tickers]
    holds = [h for h in decision.get("holds", []) if h.get("ticker") in holding_tickers]

    # Any holding the model forgot lands in holds
    covered = {s["ticker"] for s in sells} | {h["ticker"] for h in holds}
    for ticker in sorted(holding_tickers - covered):
        holds.append({"ticker": ticker, "reason": "No action recommended."})

    # Scale allocations down if they exceed the budget
    total = sum(max(0.0, float(b.get("allocation_usd", 0))) for b in buys)
    if total > budget > 0:
        scale = budget / total
        for b in buys:
            b["allocation_usd"] = round(max(0.0, float(b.get("allocation_usd", 0))) * scale, 2)

    return {
        "summary": decision.get("summary", ""),
        "buys": buys,
        "sells": sells,
        "holds": holds,
    }


def get_latest_decision(conn, user_id: int) -> dict | None:
    """Read the most recent stored decision for the dashboard."""
    row = conn.execute(
        """SELECT summary, decision_json, model, generated_at
           FROM daily_decisions WHERE user_id = ?
           ORDER BY generated_at DESC, id DESC LIMIT 1""",
        (user_id,),
    ).fetchone()
    if not row:
        return None
    try:
        decision = json.loads(row["decision_json"])
    except (json.JSONDecodeError, TypeError):
        return None
    decision["model"] = row["model"]
    decision["generated_at"] = row["generated_at"]
    return decision
