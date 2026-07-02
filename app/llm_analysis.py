"""
LLM-based portfolio decision using the Claude API (claude-opus-4-8).

One call per user per day: the formula engine produces a shortlist (all current
holdings + top buy candidates the user does not own), and Claude makes the
portfolio-level decision — what to buy, what to sell, what to hold — with the
whole picture in view (relative attractiveness, concentration, budget).

This replaces the previous per-ticker analyze_with_claude() design, which paid
one API call per ticker for the model to re-derive the formula signal it was
already handed. A single portfolio-level call is both cheaper and lets the
model do cross-sectional reasoning the per-ticker calls could not.

Falls back gracefully to None if the API is unavailable or not configured;
callers then use the formula signals directly.
"""
import json
import logging
import os

logger = logging.getLogger(__name__)

MODEL = "claude-opus-4-8"
MAX_TOKENS = 8000

_SYSTEM_PROMPT = """\
You are a disciplined portfolio analyst making one daily decision for a small retail
portfolio. The user invests ~$100/day of fresh budget plus realized sell profits.

You will receive:
1. HOLDINGS — stocks the user owns, with cost basis, P&L, formula scores and recent headlines.
2. BUY CANDIDATES — stocks the user does NOT own, pre-screened by a quantitative formula.
3. Today's remaining cash budget.

Your job:
- From BUY CANDIDATES only: pick 0-3 to buy today. Prefer fewer, higher-conviction picks
  over spreading the budget thin. Allocations must sum to at most the available budget.
  Avoid doubling up on candidates that represent the same bet (same sector/theme) —
  pick the better one.
- From HOLDINGS only: flag any that should be sold (deteriorating evidence, stop-loss
  breach, or take-profit worth banking). Every holding you don't sell goes in holds.
- It is completely fine — often correct — to buy nothing and sell nothing.

Rules:
- Never put a holding in buys. Never put a non-holding in sells or holds.
- Every holding must appear in exactly one of sells or holds.
- Reasons must cite the specific evidence provided (scores, P&L, headlines), not
  generic boilerplate. 1-2 sentences each.
- The summary is 2-4 sentences: the single most important action today and why.\
"""

_DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": "2-4 sentence portfolio-level assessment of today's decision",
        },
        "buys": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string"},
                    "conviction": {"type": "string", "enum": ["HIGH", "MEDIUM", "LOW"]},
                    "allocation_usd": {
                        "type": "number",
                        "description": "Dollars of today's budget to put into this ticker",
                    },
                    "reason": {"type": "string"},
                },
                "required": ["ticker", "conviction", "allocation_usd", "reason"],
                "additionalProperties": False,
            },
        },
        "sells": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string"},
                    "urgency": {"type": "string", "enum": ["HIGH", "MEDIUM", "LOW"]},
                    "reason": {"type": "string"},
                },
                "required": ["ticker", "urgency", "reason"],
                "additionalProperties": False,
            },
        },
        "holds": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["ticker", "reason"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["summary", "buys", "sells", "holds"],
    "additionalProperties": False,
}


def decide_portfolio(
    holdings: list[dict],
    candidates: list[dict],
    budget_remaining: float,
) -> tuple[dict, dict] | None:
    """
    Make the single daily portfolio decision.

    Parameters
    ----------
    holdings : list of dicts prepared by daily_decision.py — each has
        ticker, shares, avg_cost_basis, current_price, pl_pct,
        unrealized_gain_loss, signal, combined_score, reason, headlines.
    candidates : list of dicts — each has ticker, current_price, signal,
        combined_score, reason, headlines, source ("watchlist"/"screener").
    budget_remaining : today's available cash.

    Returns
    -------
    (decision, usage) where decision matches _DECISION_SCHEMA and usage has
    input_tokens/output_tokens, or None on any failure (callers fall back to
    the formula signals).
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        logger.debug("ANTHROPIC_API_KEY not set — skipping LLM decision")
        return None

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
    except ImportError:
        logger.warning("anthropic package not installed — skipping LLM decision")
        return None

    prompt = _build_prompt(holdings, candidates, budget_remaining)

    try:
        message = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            thinking={"type": "adaptive"},
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
            output_config={"format": {"type": "json_schema", "schema": _DECISION_SCHEMA}},
        )

        if message.stop_reason == "refusal":
            logger.warning("LLM declined the request — falling back to formula signals")
            return None

        raw = next((b.text for b in message.content if b.type == "text"), "")
        decision = json.loads(raw)

        usage = {
            "input_tokens": message.usage.input_tokens,
            "output_tokens": message.usage.output_tokens,
        }
        return decision, usage

    except json.JSONDecodeError as e:
        logger.warning(f"LLM returned non-JSON decision: {e}")
        return None
    except Exception as e:
        logger.warning(f"LLM portfolio decision failed: {e}")
        return None


def _format_headlines(headlines: list[dict]) -> str:
    if not headlines:
        return "    headlines: (none recent)"
    lines = []
    for h in headlines:
        label = f" [{h['sentiment_label']}]" if h.get("sentiment_label") else ""
        lines.append(f'    - "{h["headline"]}"{label}')
    return "\n".join(lines)


def _format_holding(h: dict) -> str:
    pl = ""
    if h.get("pl_pct") is not None:
        pl = f" | P&L {h['pl_pct']:+.1f}% (${h.get('unrealized_gain_loss', 0):+.2f} unrealized)"
    price = f"${h['current_price']:.2f}" if h.get("current_price") else "price n/a"
    return (
        f"- {h['ticker']}: {h['shares']:g} shares @ ${h['avg_cost_basis']:.2f} avg cost, "
        f"now {price}{pl}\n"
        f"    formula: {h['signal']} ({h['combined_score']:+.3f}) — {h['reason']}\n"
        f"{_format_headlines(h.get('headlines', []))}"
    )


def _format_candidate(c: dict) -> str:
    price = f"${c['current_price']:.2f}" if c.get("current_price") else "price n/a"
    src = c.get("source", "screener")
    return (
        f"- {c['ticker']} ({src}): {price}\n"
        f"    formula: {c['signal']} ({c['combined_score']:+.3f}) — {c['reason']}\n"
        f"{_format_headlines(c.get('headlines', []))}"
    )


def _build_prompt(holdings: list[dict], candidates: list[dict], budget: float) -> str:
    parts = []

    parts.append("HOLDINGS (you own these — each must end up in sells or holds):")
    if holdings:
        parts.extend(_format_holding(h) for h in holdings)
    else:
        parts.append("(none — portfolio is empty)")

    parts.append("")
    parts.append("BUY CANDIDATES (you do NOT own these — buys may only come from this list):")
    if candidates:
        parts.extend(_format_candidate(c) for c in candidates)
    else:
        parts.append("(none passed the formula screen today)")

    parts.append("")
    parts.append(f"Cash budget available today: ${budget:.2f}")
    parts.append("")
    parts.append("Make today's decision.")

    return "\n".join(parts)
