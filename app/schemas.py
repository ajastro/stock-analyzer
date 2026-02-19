from pydantic import BaseModel, Field
from typing import Optional


class UserCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    phone_number: str = Field(..., min_length=10, max_length=20)


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
