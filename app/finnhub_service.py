import os
from datetime import datetime, timedelta

import finnhub

def _get_client() -> finnhub.Client:
    api_key = os.environ.get("FINNHUB_API_KEY", "")
    if not api_key:
        raise RuntimeError(
            "FINNHUB_API_KEY environment variable is not set. "
            "Get a free key at https://finnhub.io/register"
        )
    return finnhub.Client(api_key=api_key)


def get_quote(ticker: str) -> dict:
    client = _get_client()
    data = client.quote(ticker.upper())
    if not data or data.get("c", 0) == 0:
        return {}
    return {
        "ticker": ticker.upper(),
        "current_price": data["c"],
        "change": data["d"],
        "percent_change": data["dp"],
        "high": data["h"],
        "low": data["l"],
        "open": data["o"],
        "previous_close": data["pc"],
        "timestamp": data["t"],
    }


def get_company_profile(ticker: str) -> dict:
    client = _get_client()
    data = client.company_profile2(symbol=ticker.upper())
    if not data:
        return {}
    return {
        "ticker": data.get("ticker", ticker.upper()),
        "name": data.get("name", ""),
        "country": data.get("country", ""),
        "currency": data.get("currency", ""),
        "exchange": data.get("exchange", ""),
        "ipo_date": data.get("ipo", ""),
        "market_cap": data.get("marketCapitalization", 0),
        "industry": data.get("finnhubIndustry", ""),
        "logo": data.get("logo", ""),
        "web_url": data.get("weburl", ""),
    }


def search_symbol(query: str) -> list[dict]:
    client = _get_client()
    data = client.symbol_lookup(query)
    results = data.get("result", [])
    return [
        {
            "symbol": r.get("symbol", ""),
            "description": r.get("description", ""),
            "type": r.get("type", ""),
        }
        for r in results[:10]
    ]


def get_candles(ticker: str, resolution: str = "D", days: int = 60) -> list[dict]:
    import yfinance as yf
    hist = yf.Ticker(ticker.upper()).history(period=f"{days}d")
    if hist.empty:
        return []
    return [
        {
            "timestamp": int(ts.timestamp()),
            "open": row["Open"],
            "high": row["High"],
            "low": row["Low"],
            "close": row["Close"],
            "volume": row["Volume"],
        }
        for ts, row in hist.iterrows()
    ]


def get_company_news(ticker: str, days: int = 7) -> list[dict]:
    client = _get_client()
    to_date = datetime.utcnow().strftime("%Y-%m-%d")
    from_date = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
    articles = client.company_news(ticker.upper(), _from=from_date, to=to_date)
    return [
        {
            "headline": a.get("headline", ""),
            "source": a.get("source", ""),
            "url": a.get("url", ""),
            "summary": a.get("summary", ""),
            "datetime": a.get("datetime", 0),
            "related": a.get("related", ""),
        }
        for a in (articles or [])[:20]
    ]
