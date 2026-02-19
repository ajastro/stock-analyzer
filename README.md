# Stock Analyzer

A stock-tracking application that provides buy/sell recommendations based on stock prices and news sentiment, delivered via WhatsApp.

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
