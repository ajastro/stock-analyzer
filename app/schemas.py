from pydantic import BaseModel, Field
from typing import Optional


class UserCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    phone_number: Optional[str] = Field(None, max_length=20)


class UserResponse(BaseModel):
    id: int
    name: str
    phone_number: str
    is_onboarded: bool
    has_prior_stocks: bool
    created_at: str


class OnboardRequest(BaseModel):
    has_prior_stocks: bool
    holdings: list["HoldingCreate"] = Field(default_factory=list)


class HoldingCreate(BaseModel):
    ticker: str = Field(..., min_length=1, max_length=10)
    shares: float = Field(..., gt=0)
    avg_cost_basis: float = Field(..., ge=0)
    acquired_date: str


class HoldingUpdate(BaseModel):
    ticker: Optional[str] = Field(None, min_length=1, max_length=10)
    shares: Optional[float] = Field(None, gt=0)
    avg_cost_basis: Optional[float] = Field(None, ge=0)
    acquired_date: Optional[str] = None


class HoldingResponse(BaseModel):
    id: int
    user_id: int
    ticker: str
    shares: float
    avg_cost_basis: float
    acquired_date: str
    created_at: str


class DailyBudgetResponse(BaseModel):
    user_id: int
    date: str
    base_budget: float
    sell_profits: float
    amount_spent: float
    remaining_budget: float


class SellRecord(BaseModel):
    ticker: str = Field(..., min_length=1, max_length=10)
    shares: float = Field(..., gt=0)
    price_per_share: float = Field(..., gt=0)


class BuyRecord(BaseModel):
    ticker: str = Field(..., min_length=1, max_length=10)
    shares: float = Field(..., gt=0)
    price_per_share: float = Field(..., gt=0)


class TransactionResponse(BaseModel):
    id: int
    user_id: int
    ticker: str
    action: str
    shares: float
    price_per_share: float
    total_amount: float
    profit: Optional[float]
    created_at: str


class StockQuoteResponse(BaseModel):
    ticker: str
    current_price: float
    change: Optional[float]
    percent_change: Optional[float]
    high: Optional[float]
    low: Optional[float]
    open: Optional[float]
    previous_close: Optional[float]
    timestamp: Optional[int]


class CompanyProfileResponse(BaseModel):
    ticker: str
    name: str
    country: str
    currency: str
    exchange: str
    ipo_date: str
    market_cap: float
    industry: str
    logo: str
    web_url: str


class SymbolSearchResult(BaseModel):
    symbol: str
    description: str
    type: str


class CandleResponse(BaseModel):
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float


class NewsArticleResponse(BaseModel):
    headline: str
    source: str
    url: str
    summary: str
    datetime: int
    related: str


class PriceSnapshotResponse(BaseModel):
    id: int
    ticker: str
    current_price: float
    change: Optional[float]
    percent_change: Optional[float]
    high: Optional[float]
    low: Optional[float]
    open: Optional[float]
    previous_close: Optional[float]
    fetched_at: str


class WatchlistItemResponse(BaseModel):
    id: int
    user_id: int
    ticker: str
    added_at: str


class PortfolioSummaryItem(BaseModel):
    ticker: str
    shares: float
    avg_cost_basis: float
    current_price: Optional[float]
    market_value: Optional[float]
    gain_loss: Optional[float]
    gain_loss_percent: Optional[float]


class PortfolioSummaryResponse(BaseModel):
    user_id: int
    holdings: list[PortfolioSummaryItem]
    total_cost_basis: float
    total_market_value: Optional[float]
    total_gain_loss: Optional[float]


class SentimentArticleResponse(BaseModel):
    id: int
    ticker: str
    headline: str
    source: Optional[str]
    url: str
    summary: Optional[str]
    article_datetime: Optional[int]
    sentiment_compound: Optional[float]
    sentiment_label: Optional[str]
    headline_sentiment: Optional[float]
    summary_sentiment: Optional[float]
    analyzed_at: str


class SentimentSummaryResponse(BaseModel):
    ticker: str
    total_articles: int
    avg_sentiment: float
    bullish_count: int
    bearish_count: int
    neutral_count: int
    signal: str
    latest_articles: list[SentimentArticleResponse]


class PortfolioSentimentItem(BaseModel):
    ticker: str
    total_articles: int
    avg_sentiment: float
    signal: str


class PortfolioSentimentResponse(BaseModel):
    user_id: int
    tickers: list[PortfolioSentimentItem]
    overall_signal: str


class RecommendationResponse(BaseModel):
    id: Optional[int] = None
    user_id: int
    ticker: str
    signal: str
    combined_score: float
    price_score: Optional[float]
    technical_score: Optional[float]
    sentiment_score: Optional[float]
    current_price: Optional[float]
    reason: Optional[str]
    affordable_shares: Optional[float]
    unrealized_gain_loss: Optional[float]
    generated_at: Optional[str] = None


class GenerateRecommendationsResponse(BaseModel):
    user_id: int
    generated_count: int
    remaining_budget: float
    recommendations: list[RecommendationResponse]


class SendMessageResponse(BaseModel):
    user_id: int
    message_sid: Optional[str]
    status: str
    message_type: str


class MessageLogResponse(BaseModel):
    id: int
    user_id: int
    message_sid: Optional[str]
    message_type: str
    status: str
    body: Optional[str]
    error: Optional[str]
    sent_at: str


class BulkSendResponse(BaseModel):
    sent: int
    failed: int
    results: list[SendMessageResponse]
