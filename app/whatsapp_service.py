import os

from twilio.rest import Client


def _get_client() -> Client:
    account_sid = os.environ.get("TWILIO_ACCOUNT_SID", "")
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN", "")
    if not account_sid or not auth_token:
        raise RuntimeError(
            "TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN environment variables are required"
        )
    return Client(account_sid, auth_token)


def _get_from_number() -> str:
    from_number = os.environ.get("TWILIO_WHATSAPP_FROM", "")
    if not from_number:
        raise RuntimeError(
            "TWILIO_WHATSAPP_FROM environment variable is required (e.g. whatsapp:+14155238886)"
        )
    return from_number


def send_whatsapp_message(to_phone: str, body: str) -> dict:
    client = _get_client()
    from_number = _get_from_number()

    if not to_phone.startswith("whatsapp:"):
        to_phone = f"whatsapp:{to_phone}"

    message = client.messages.create(
        body=body,
        from_=from_number,
        to=to_phone,
    )

    return {
        "sid": message.sid,
        "status": message.status,
        "to": message.to,
        "body": body,
    }


def format_recommendations_message(
    user_name: str,
    recommendations: list[dict],
    remaining_budget: float,
) -> str:
    lines = [f"Hi {user_name}! Here are your stock recommendations:\n"]

    for rec in recommendations:
        signal = rec["signal"]
        ticker = rec["ticker"]
        score = rec["combined_score"]
        price = rec.get("current_price")
        price_str = f"${price:.2f}" if price else "N/A"

        emoji = _signal_emoji(signal)
        line = f"{emoji} *{ticker}*: {signal} (score: {score:+.2f}) @ {price_str}"

        if signal in ("BUY", "STRONG_BUY") and rec.get("affordable_shares"):
            line += f"\n   Can buy ~{rec['affordable_shares']:.2f} shares"

        if signal in ("SELL", "STRONG_SELL") and rec.get("unrealized_gain_loss") is not None:
            gl = rec["unrealized_gain_loss"]
            line += f"\n   Unrealized P&L: ${gl:+.2f}"

        lines.append(line)

    lines.append(f"\nRemaining budget: ${remaining_budget:.2f}")
    return "\n".join(lines)


def _signal_emoji(signal: str) -> str:
    mapping = {
        "STRONG_BUY": "\u2b06\ufe0f\u2b06\ufe0f",
        "BUY": "\u2b06\ufe0f",
        "HOLD": "\u27a1\ufe0f",
        "SELL": "\u2b07\ufe0f",
        "STRONG_SELL": "\u2b07\ufe0f\u2b07\ufe0f",
    }
    return mapping.get(signal, "\u2753")
