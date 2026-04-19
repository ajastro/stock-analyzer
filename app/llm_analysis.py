"""
LLM-based stock analysis using the Claude API (claude-haiku-4-5).

Takes the formula-computed component scores and raw data context, then asks
Claude to review everything and produce a final signal, score, and narrative reason.

Falls back gracefully to None if the API is unavailable or not configured.
"""
import json
import logging
import os

logger = logging.getLogger(__name__)

_VALID_SIGNALS = {"STRONG_BUY", "BUY", "HOLD", "SELL", "STRONG_SELL"}

_SYSTEM_PROMPT = """\
You are a quantitative stock analyst. You will be given formula-based scoring data for a stock \
and must provide your own independent signal based on the evidence.

Respond ONLY with a single valid JSON object — no markdown, no explanation outside the JSON:
{
  "signal": "<STRONG_BUY|BUY|HOLD|SELL|STRONG_SELL>",
  "score": <float between -1.0 and 1.0>,
  "reason": "<2-3 sentence narrative explaining your signal>"
}

Guidelines:
- STRONG_BUY: score >= 0.3, clear multi-factor bullish alignment
- BUY: score 0.1 to 0.3, moderate bullish lean
- HOLD: score -0.1 to 0.1, mixed or neutral signals
- SELL: score -0.3 to -0.1, moderate bearish lean
- STRONG_SELL: score <= -0.3, clear multi-factor bearish alignment
- Your reason should be specific to the data provided, not generic boilerplate.
- If the position has stop-loss or take-profit context, weigh it appropriately.\
"""


def analyze_with_claude(ticker: str, formula_data: dict) -> dict | None:
    """
    Call Claude to review formula scores and produce a final signal override.

    Parameters
    ----------
    ticker : str
        Stock ticker symbol.
    formula_data : dict
        Keys: price_score, price_reason, technical_score, tech_reason,
              sentiment_score, sent_reason, analyst_score, analyst_reason,
              earnings_score, earn_reason, formula_combined, formula_signal,
              pl_pct (optional), unrealized_gain_loss (optional),
              current_price (optional).

    Returns
    -------
    dict with keys "signal", "score", "reason", or None on failure.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        logger.debug("ANTHROPIC_API_KEY not set — skipping LLM analysis")
        return None

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
    except ImportError:
        logger.warning("anthropic package not installed — skipping LLM analysis")
        return None

    user_prompt = _build_prompt(ticker, formula_data)

    try:
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
        raw = message.content[0].text.strip()
        result = json.loads(raw)

        signal = result.get("signal", "").upper()
        score = float(result.get("score", 0.0))
        reason = str(result.get("reason", "")).strip()

        if signal not in _VALID_SIGNALS:
            logger.warning(f"LLM returned invalid signal '{signal}' for {ticker} — falling back")
            return None

        score = max(-1.0, min(1.0, score))
        if not reason:
            logger.warning(f"LLM returned empty reason for {ticker} — falling back")
            return None

        return {"signal": signal, "score": score, "reason": reason}

    except json.JSONDecodeError as e:
        logger.warning(f"LLM returned non-JSON for {ticker}: {e}")
        return None
    except Exception as e:
        logger.warning(f"LLM analysis failed for {ticker}: {e}")
        return None


def _build_prompt(ticker: str, d: dict) -> str:
    lines = [f"Ticker: {ticker}"]

    if d.get("current_price"):
        lines.append(f"Current price: ${d['current_price']:.2f}")

    lines.append("")
    lines.append("Formula component scores (-1.0 to +1.0 scale):")
    lines.append(f"  Price momentum     {d['price_score']:+.3f}  —  {d['price_reason']}")
    lines.append(f"  Technical (SMA/RSI/Vol) {d['technical_score']:+.3f}  —  {d['tech_reason']}")
    lines.append(f"  News sentiment     {d['sentiment_score']:+.3f}  —  {d['sent_reason']}")
    lines.append(f"  Analyst consensus  {d['analyst_score']:+.3f}  —  {d['analyst_reason']}")
    lines.append(f"  Earnings (EPS)     {d['earnings_score']:+.3f}  —  {d['earn_reason']}")
    lines.append("")
    lines.append(f"Formula result: {d['formula_signal']} (combined score {d['formula_combined']:+.3f})")

    pl_pct = d.get("pl_pct")
    unrealized = d.get("unrealized_gain_loss")
    if pl_pct is not None and unrealized is not None:
        lines.append(f"Position P&L: {pl_pct:+.1f}% (unrealized ${unrealized:+.2f})")
        if pl_pct < -15:
            lines.append("⚠️  Position is down >15% — stop-loss territory.")
        elif pl_pct > 25:
            lines.append("⚠️  Position is up >25% — take-profit territory.")
    else:
        lines.append("Position: watchlist (not currently held)")

    lines.append("")
    lines.append("Provide your signal, score, and reason as JSON.")

    return "\n".join(lines)
