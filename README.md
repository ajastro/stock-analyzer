# Stock Analyzer

A stock-tracking application that suggests stocks to buy (that you don't own) and sell (that you do own), delivered via email and a web dashboard.

## How recommendations work

Two-stage pipeline, run each weekday morning:

1. **Formula screen (no LLM).** A 6:00 AM job scores the full S&P 500 on five factors
   (price momentum, technicals, news sentiment, analyst consensus, earnings). The same
   scoring runs on your holdings and watchlist. This stage is the cheap filter.
2. **One LLM call per user per day.** The shortlist — every holding plus the top buy
   candidates you don't own, each with scores, P&L, and recent headlines — goes to
   Claude (`claude-opus-4-8`) in a single portfolio-level call with a guaranteed JSON
   schema. It returns the day's decision: what to buy (with budget allocation), what
   to sell, what to hold, and why. Stored in `daily_decisions`, shown at the top of
   the morning email and dashboard.

If `ANTHROPIC_API_KEY` is not set or the call fails, the app falls back to the
formula signals — nothing breaks.

At roughly 5K input / 1K output tokens per day, the LLM layer costs about **$2–3/month**.

### Key environment variables

| Variable | Purpose |
|----------|---------|
| `ANTHROPIC_API_KEY` | Enables the daily AI portfolio decision |
| `FINNHUB_API_KEY` | Market data (quotes, candles, news) |
| `RESEND_API_KEY`, `ALERT_EMAIL` | Morning/weekly report emails |
| `DASHBOARD_USER`, `DASHBOARD_PASSWORD` | Dashboard basic auth |
| `API_SECRET_KEY` | Bearer token for the REST API |

## Phase 1: Onboarding + Portfolio DB

### Features
- User registration with phone number
- Onboarding flow: declare existing stock holdings or start fresh
- Portfolio tracking (CRUD for holdings)
- Daily budget engine ($100/day base + realized sell profits)
- Transaction history (buy/sell records)

### API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/users/register` | Register a new user |
| GET | `/users/{user_id}` | Get user profile |
| POST | `/users/{user_id}/onboard` | Complete onboarding with optional holdings |
| GET | `/users/{user_id}/portfolio` | List all holdings |
| POST | `/users/{user_id}/portfolio` | Add a holding |
| PUT | `/users/{user_id}/portfolio/{holding_id}` | Update a holding |
| DELETE | `/users/{user_id}/portfolio/{holding_id}` | Remove a holding |
| GET | `/users/{user_id}/budget` | Get today's budget |
| POST | `/users/{user_id}/budget/buy` | Record a buy (deducts from budget) |
| POST | `/users/{user_id}/budget/sell` | Record a sell (profit added to budget) |
| GET | `/users/{user_id}/budget/transactions` | View transaction history |

### Tech Stack
- FastAPI (Python)
- SQLite (persistent storage)
- Pydantic for request/response validation

### Running Locally
```bash
poetry install
poetry run uvicorn app.main:app --host 0.0.0.0 --port 8000
```
