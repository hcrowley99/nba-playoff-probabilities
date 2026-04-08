# NBA Playoff Probabilities

**Live at: [henry-playoff-probs.up.railway.app](https://henry-playoff-probs.up.railway.app/)**

Monte Carlo simulation of the remaining 2025–26 NBA season to predict playoff probabilities, seedings, and first-round matchups.

## Features

- **Playoff probabilities** — likelihood each team makes the playoffs, play-in, or misses entirely
- **Seeding distribution** — probability of finishing at each seed (1–10)
- **First-round matchup odds** — who each team is likely to face
- **Fan Guide** — pick your team and see which upcoming games matter most for your desired outcome
- **Game impact scores** — ranks upcoming games by how much they affect the playoff picture

## Stack

- **Backend:** Python, FastAPI, SQLite, NumPy
- **Frontend:** Vanilla HTML/CSS/JS (no build step)
- **Data:** NBA stats API (live standings + schedule)
- **Hosting:** Railway

## Running Locally

```bash
cd backend
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

Open [http://localhost:8000](http://localhost:8000), then click **Refresh Data** to fetch current NBA standings and schedule.

## Environment Variables

| Variable | Description |
|---|---|
| `DATABASE_PATH` | Path to SQLite DB (default: `backend/nba.db`) |
| `ADMIN_TOKEN` | Token required for `/api/refresh` and score entry |
| `ALLOWED_ORIGINS` | Comma-separated CORS origins |
| `PORT` | Port to listen on (set automatically by Railway) |
