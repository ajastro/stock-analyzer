from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

_analyzer = SentimentIntensityAnalyzer()


def analyze_text(text: str) -> dict:
    if not text or not text.strip():
        return {"compound": 0.0, "positive": 0.0, "negative": 0.0, "neutral": 0.0, "label": "neutral"}
    scores = _analyzer.polarity_scores(text)
    compound = scores["compound"]
    if compound >= 0.05:
        label = "bullish"
    elif compound <= -0.05:
        label = "bearish"
    else:
        label = "neutral"
    return {
        "compound": round(compound, 4),
        "positive": round(scores["pos"], 4),
        "negative": round(scores["neg"], 4),
        "neutral": round(scores["neu"], 4),
        "label": label,
    }


def analyze_article(headline: str, summary: str) -> dict:
    h_scores = analyze_text(headline)
    s_scores = analyze_text(summary)
    combined = h_scores["compound"] * 0.6 + s_scores["compound"] * 0.4
    if combined >= 0.05:
        label = "bullish"
    elif combined <= -0.05:
        label = "bearish"
    else:
        label = "neutral"
    return {
        "compound": round(combined, 4),
        "headline_sentiment": h_scores["compound"],
        "summary_sentiment": s_scores["compound"],
        "label": label,
    }
