"""
FastAPI application: all routes, background tasks, and static file serving.
"""
from __future__ import annotations

import json
import logging
import os
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import List

from database import (
    get_conn, init_db, standings_hash,
    get_standings, get_teams, get_game,
    get_remaining_games, get_games_by_date_range,
    get_simulation_cache, set_simulation_cache,
    get_impact_cache, set_impact_cache,
    invalidate_simulation_cache, invalidate_impact_cache,
    set_game_result, get_config, set_config,
)
from simulation import (
    run_simulation, compute_game_impact,
    standings_to_team_infos, games_to_game_infos,
    GameInfo,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DB_PATH = os.getenv("DATABASE_PATH", str(Path(__file__).parent / "nba.db"))
FRONTEND_DIR = str(Path(__file__).parent.parent / "frontend")
ALLOWED_ORIGINS = os.getenv(
    "ALLOWED_ORIGINS", "http://localhost:3000,http://localhost:8000,http://127.0.0.1:8000"
).split(",")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")  # Empty = no auth required (local mode)
MIN_REFRESH_SECONDS = 600  # 10 minutes between refreshes

# ---------------------------------------------------------------------------
# In-memory refresh state
# ---------------------------------------------------------------------------

refresh_status: dict = {"status": "idle", "message": "", "last_run": None}
impact_status: dict = {"status": "idle", "total": 0, "completed": 0}

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = FastAPI(title="NBA Playoff Probabilities", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    init_db(DB_PATH)
    # Invalidate simulation cache so seed_matchup_distribution is computed fresh
    with get_conn(DB_PATH) as conn:
        invalidate_simulation_cache(conn)
    logger.info(f"Database initialized at {DB_PATH}")


# Serve frontend static files
if Path(FRONTEND_DIR).exists():
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


@app.get("/")
async def root():
    index = Path(FRONTEND_DIR) / "index.html"
    if index.exists():
        return FileResponse(str(index))
    return JSONResponse({"message": "NBA Playoff Probabilities API"})


@app.get("/style.css")
async def style():
    f = Path(FRONTEND_DIR) / "style.css"
    if f.exists():
        return FileResponse(str(f), media_type="text/css")
    raise HTTPException(404)


@app.get("/app.js")
async def appjs():
    f = Path(FRONTEND_DIR) / "app.js"
    if f.exists():
        return FileResponse(str(f), media_type="application/javascript")
    raise HTTPException(404)


@app.get("/simulation.js")
async def simulationjs():
    f = Path(FRONTEND_DIR) / "simulation.js"
    if f.exists():
        return FileResponse(str(f), media_type="application/javascript")
    raise HTTPException(404)


@app.get("/favicon.svg")
async def favicon():
    f = Path(FRONTEND_DIR) / "favicon.svg"
    if f.exists():
        return FileResponse(str(f), media_type="image/svg+xml")
    raise HTTPException(404)


# ---------------------------------------------------------------------------
# Helper: require admin token on sensitive endpoints
# ---------------------------------------------------------------------------

def _check_admin(token: Optional[str]) -> None:
    if ADMIN_TOKEN and token != ADMIN_TOKEN:
        raise HTTPException(403, "Admin token required")


# ---------------------------------------------------------------------------
# GET /api/teams
# ---------------------------------------------------------------------------

@app.get("/api/teams")
async def api_teams():
    with get_conn(DB_PATH) as conn:
        teams = get_teams(conn)
    return teams


# ---------------------------------------------------------------------------
# GET /api/standings
# ---------------------------------------------------------------------------

@app.get("/api/standings")
async def api_standings(conference: Optional[str] = None):
    with get_conn(DB_PATH) as conn:
        rows = get_standings(conn, conference)
        last_updated = get_config(conn, "last_refresh_at")

    east = [r for r in rows if r["conference"] == "East"]
    west = [r for r in rows if r["conference"] == "West"]

    return {
        "east": east,
        "west": west,
        "updated_at": last_updated,
    }


# ---------------------------------------------------------------------------
# GET /api/game-data  — lightweight data for client-side simulation
# ---------------------------------------------------------------------------

@app.get("/api/game-data")
async def api_game_data():
    """Return teams (with standings) + remaining games for client-side simulation."""
    with get_conn(DB_PATH) as conn:
        standings_rows = get_standings(conn)
        remaining_rows = get_remaining_games(conn)
        last_updated = get_config(conn, "last_refresh_at")

    teams = [
        {
            "team_id": r["team_id"],
            "abbreviation": r["abbreviation"],
            "full_name": r["full_name"],
            "conference": r["conference"],
            "division": r["division"],
            "wins": r["wins"],
            "losses": r["losses"],
            "win_pct": r["win_pct"],
        }
        for r in standings_rows
    ]

    games = [
        {
            "game_id": r["game_id"],
            "game_date": r["game_date"],
            "home_team_id": r["home_team_id"],
            "away_team_id": r["away_team_id"],
        }
        for r in remaining_rows
    ]

    return {
        "teams": teams,
        "games": games,
        "updated_at": last_updated,
    }


# ---------------------------------------------------------------------------
# POST /api/refresh
# ---------------------------------------------------------------------------

def _do_refresh():
    global refresh_status
    from nba_data import refresh_all
    refresh_status = {"status": "running", "message": "Fetching data from NBA API...", "last_run": None}
    try:
        result = refresh_all(DB_PATH)
        msg = f"Fetched {result['counts']['standings']} standings, {result['counts']['games']} games"
        if result["errors"]:
            msg += f" | Errors: {'; '.join(result['errors'])}"
        refresh_status = {
            "status": "complete" if result["success"] else "partial",
            "message": msg,
            "last_run": result["refreshed_at"],
        }
        logger.info(f"Refresh complete: {msg}")
    except Exception as e:
        logger.error(f"Refresh failed: {e}")
        refresh_status = {"status": "error", "message": str(e), "last_run": None}


@app.post("/api/refresh")
async def api_refresh(
    background_tasks: BackgroundTasks,
    token: Optional[str] = None,
):
    _check_admin(token)

    # Rate limit
    with get_conn(DB_PATH) as conn:
        last = get_config(conn, "last_refresh_at")
    if last:
        try:
            last_dt = datetime.fromisoformat(last)
            elapsed = (datetime.now(timezone.utc) - last_dt).total_seconds()
            if elapsed < MIN_REFRESH_SECONDS:
                wait = int(MIN_REFRESH_SECONDS - elapsed)
                return {"status": "rate_limited", "message": f"Please wait {wait}s before refreshing again"}
        except Exception:
            pass

    global refresh_status
    if refresh_status["status"] == "running":
        return {"status": "already_running", "message": "Refresh already in progress"}

    refresh_status = {"status": "queued", "message": "Refresh queued", "last_run": None}
    background_tasks.add_task(_do_refresh)
    return {"status": "queued", "message": "Refresh started in background"}


@app.get("/api/refresh/status")
async def api_refresh_status():
    return refresh_status


# ---------------------------------------------------------------------------
# GET /api/simulate
# ---------------------------------------------------------------------------

@app.get("/api/simulate")
async def api_simulate(force: bool = False, n_sims: int = 10_000):
    with get_conn(DB_PATH) as conn:
        s_hash = standings_hash(conn)
        cached = get_simulation_cache(conn, s_hash)

    if cached and not force:
        result = json.loads(cached["results_json"])
        result["cached"] = True
        result["computed_at"] = cached["created_at"]
        return result

    with get_conn(DB_PATH) as conn:
        standings_rows = get_standings(conn)
        remaining_rows = get_remaining_games(conn)

    if not standings_rows:
        raise HTTPException(503, "No standings data. Please refresh first.")

    teams = standings_to_team_infos(standings_rows)
    games = games_to_game_infos(remaining_rows)

    sim_results = run_simulation(teams, games, n_sims=min(n_sims, 50_000))

    payload = {
        "cached": False,
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "n_simulations": n_sims,
        "east": sim_results["East"],
        "west": sim_results["West"],
    }

    with get_conn(DB_PATH) as conn:
        set_simulation_cache(conn, s_hash, json.dumps(payload))

    return payload


# ---------------------------------------------------------------------------
# GET /api/schedule
# ---------------------------------------------------------------------------

@app.get("/api/schedule")
async def api_schedule(
    filter: str = "all",  # "7days" | "14days" | "all"
    background_tasks: BackgroundTasks = None,
    impacts_only: bool = False,
):
    from datetime import date, timedelta

    today = date.today().isoformat()
    end_date: Optional[str] = None

    if filter == "7days":
        end_date = (date.today() + timedelta(days=7)).isoformat()
    elif filter == "14days":
        end_date = (date.today() + timedelta(days=14)).isoformat()

    with get_conn(DB_PATH) as conn:
        rows = get_games_by_date_range(conn, start_date=today, end_date=end_date)
        s_hash = standings_hash(conn)

    if impacts_only:
        # Return only impact data for games that have it
        impacts = {}
        with get_conn(DB_PATH) as conn:
            for r in rows:
                cached = get_impact_cache(conn, r["game_id"], s_hash)
                if cached:
                    impacts[r["game_id"]] = json.loads(cached["impact_json"])
        return {"impacts": impacts}

    # Build game list with whatever impact data is available
    game_impacts: dict[str, Optional[dict]] = {}
    with get_conn(DB_PATH) as conn:
        for r in rows:
            cached = get_impact_cache(conn, r["game_id"], s_hash)
            game_impacts[r["game_id"]] = json.loads(cached["impact_json"]) if cached else None

    games_out = []
    by_date: dict[str, list[str]] = {}

    for r in rows:
        impact_data = game_impacts.get(r["game_id"])
        game_date = r["game_date"]

        # Win probability for display
        hw = r.get("home_win_pct") or 0.5
        aw = r.get("away_win_pct") or 0.5
        total = hw + aw
        p_home = hw / total if total > 0 else 0.5

        game_entry = {
            "game_id": r["game_id"],
            "game_date": game_date,
            "home_team": {
                "team_id": r["home_team_id"],
                "abbreviation": r["home_abbr"],
                "full_name": r["home_name"],
                "wins": r.get("home_wins", 0),
                "losses": r.get("home_losses", 0),
                "win_pct": r.get("home_win_pct", 0.0),
            },
            "away_team": {
                "team_id": r["away_team_id"],
                "abbreviation": r["away_abbr"],
                "full_name": r["away_name"],
                "wins": r.get("away_wins", 0),
                "losses": r.get("away_losses", 0),
                "win_pct": r.get("away_win_pct", 0.0),
            },
            "status": r["status"],
            "home_score": r["home_score"],
            "away_score": r["away_score"],
            "manually_set": bool(r["manually_set"]),
            "p_home_win": round(p_home, 3),
            "impact": _format_impact(impact_data),
        }

        games_out.append(game_entry)
        by_date.setdefault(game_date, []).append(r["game_id"])

    # Trigger background impact computation if not already running
    if background_tasks and impact_status["status"] != "running":
        background_tasks.add_task(_compute_impacts_background, s_hash)

    return {"games": games_out, "by_date": by_date}


def _format_impact(impact_data: Optional[dict]) -> Optional[dict]:
    if impact_data is None:
        return None
    score = impact_data.get("impact_score", 0.0)
    if score > 0.6:
        label = "High"
    elif score > 0.3:
        label = "Medium"
    else:
        label = "Low"
    return {
        "score": round(score, 3),
        "label": label,
        "per_team_delta": impact_data.get("per_team_delta", {}),
    }


# ---------------------------------------------------------------------------
# Background impact computation
# ---------------------------------------------------------------------------

def _compute_one_impact(args: tuple) -> dict:
    """Worker function (runs in separate process)."""
    game_dict, standings_rows, remaining_rows, seed = args
    teams = standings_to_team_infos(standings_rows)
    games = games_to_game_infos(remaining_rows)
    game = GameInfo(
        game_id=game_dict["game_id"],
        home_team_id=game_dict["home_team_id"],
        away_team_id=game_dict["away_team_id"],
    )
    return compute_game_impact(game, teams, games, n_sims=2_000, rng_seed=seed)


def _compute_impacts_background(s_hash: str) -> None:
    global impact_status

    if impact_status["status"] == "running":
        return

    with get_conn(DB_PATH) as conn:
        remaining_rows = get_remaining_games(conn)
        standings_rows = get_standings(conn)
        # Filter to games without valid cache
        games_needing_impact = [
            r for r in remaining_rows
            if get_impact_cache(conn, r["game_id"], s_hash) is None
        ]

    if not games_needing_impact:
        impact_status = {"status": "complete", "total": 0, "completed": 0}
        return

    # Prioritize games near seed boundaries: get current standings to judge
    team_seed = {}
    with get_conn(DB_PATH) as conn:
        for r in get_standings(conn):
            team_seed[r["team_id"]] = r.get("conf_rank", 15)

    def priority(g: dict) -> int:
        h_seed = team_seed.get(g["home_team_id"], 15)
        a_seed = team_seed.get(g["away_team_id"], 15)
        # Low seed number near boundary (6/7 or 9/10) = high priority
        boundary_score = min(
            abs(h_seed - 6), abs(h_seed - 7), abs(h_seed - 10),
            abs(a_seed - 6), abs(a_seed - 7), abs(a_seed - 10),
        )
        return boundary_score

    games_needing_impact.sort(key=priority)

    impact_status = {
        "status": "running",
        "total": len(games_needing_impact),
        "completed": 0,
    }

    try:
        max_workers = min(4, os.cpu_count() or 2)
        args_list = [
            (g, standings_rows, remaining_rows, i * 100)
            for i, g in enumerate(games_needing_impact)
        ]

        with ProcessPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(_compute_one_impact, args): args[0]["game_id"]
                       for args in args_list}
            completed = 0
            for future in as_completed(futures):
                game_id = futures[future]
                try:
                    result = future.result()
                    with get_conn(DB_PATH) as conn:
                        # Re-check cache isn't stale
                        current_hash = standings_hash(conn)
                        if current_hash == s_hash:
                            set_impact_cache(conn, game_id, s_hash, json.dumps(result))
                    completed += 1
                    impact_status["completed"] = completed
                except Exception as e:
                    logger.warning(f"Impact computation failed for {game_id}: {e}")

        # Normalize impact scores within this batch
        _normalize_impact_scores(s_hash)

        impact_status = {
            "status": "complete",
            "total": len(games_needing_impact),
            "completed": completed,
        }

    except Exception as e:
        logger.error(f"Impact background task failed: {e}")
        impact_status = {"status": "error", "total": 0, "completed": 0}


def _normalize_impact_scores(s_hash: str) -> None:
    """Normalize raw impact scores to 0–1 range across all games."""
    with get_conn(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT game_id, impact_json FROM impact_cache WHERE standings_hash = ?",
            (s_hash,)
        ).fetchall()

    if not rows:
        return

    raw_scores = []
    for r in rows:
        data = json.loads(r["impact_json"])
        raw_scores.append((r["game_id"], data))

    max_score = max((d.get("raw_impact", 0) for _, d in raw_scores), default=1.0)
    if max_score == 0:
        return

    with get_conn(DB_PATH) as conn:
        for game_id, data in raw_scores:
            data["impact_score"] = data.get("raw_impact", 0) / max_score
            conn.execute(
                "UPDATE impact_cache SET impact_json = ? WHERE game_id = ?",
                (json.dumps(data), game_id)
            )


@app.get("/api/impact/status")
async def api_impact_status():
    return impact_status


# ---------------------------------------------------------------------------
# POST /api/game/{game_id}/result
# ---------------------------------------------------------------------------

class GameResultRequest(BaseModel):
    home_score: int
    away_score: int
    token: Optional[str] = None


@app.post("/api/game/{game_id}/result")
async def api_game_result(game_id: str, body: GameResultRequest):
    _check_admin(body.token)

    if body.home_score < 0 or body.away_score < 0:
        raise HTTPException(400, "Scores must be non-negative")
    if body.home_score == body.away_score:
        raise HTTPException(400, "NBA games cannot end in a tie")

    with get_conn(DB_PATH) as conn:
        game = get_game(conn, game_id)
        if not game:
            raise HTTPException(404, f"Game {game_id} not found")

        success = set_game_result(conn, game_id, body.home_score, body.away_score)
        if not success:
            raise HTTPException(500, "Failed to update game result")

        # Invalidate caches since standings changed
        invalidate_simulation_cache(conn)
        invalidate_impact_cache(conn)

    winner = "home" if body.home_score > body.away_score else "away"

    return {
        "success": True,
        "game_id": game_id,
        "winner": winner,
        "home_score": body.home_score,
        "away_score": body.away_score,
        "message": "Result recorded. Simulation cache cleared.",
    }


# ---------------------------------------------------------------------------
# DELETE /api/game/{game_id}/result  (undo manual entry)
# ---------------------------------------------------------------------------

@app.delete("/api/game/{game_id}/result")
async def api_delete_game_result(game_id: str, token: Optional[str] = None):
    _check_admin(token)

    with get_conn(DB_PATH) as conn:
        game = get_game(conn, game_id)
        if not game:
            raise HTTPException(404, f"Game {game_id} not found")
        if not game["manually_set"]:
            raise HTTPException(400, "Game result was not manually set")

        conn.execute(
            "UPDATE games SET home_score=NULL, away_score=NULL, status='scheduled', manually_set=0 WHERE game_id=?",
            (game_id,)
        )
        # Reverse standings adjustment
        if game["home_score"] is not None and game["away_score"] is not None:
            if game["home_score"] > game["away_score"]:
                winner_id, loser_id = game["home_team_id"], game["away_team_id"]
            else:
                winner_id, loser_id = game["away_team_id"], game["home_team_id"]
            conn.execute("UPDATE standings SET wins = wins - 1 WHERE team_id = ?", (winner_id,))
            conn.execute("UPDATE standings SET losses = losses - 1 WHERE team_id = ?", (loser_id,))
            conn.execute("""
                UPDATE standings SET win_pct = CAST(wins AS REAL) / MAX(wins + losses, 1)
                WHERE team_id IN (?, ?)
            """, (winner_id, loser_id))

        invalidate_simulation_cache(conn)
        invalidate_impact_cache(conn)

    return {"success": True, "game_id": game_id, "message": "Manual result removed, game reset to scheduled"}


# ---------------------------------------------------------------------------
# POST /api/fan-impact  — "Root For" feature
# ---------------------------------------------------------------------------

class OutcomeSpec(BaseModel):
    type: str          # "matchup" | "seed_matchup" | "seed" | "playoffs" | "playin"
    opponent: Optional[str] = None
    seed: Optional[int] = None


class FanImpactRequest(BaseModel):
    team_abbr: str
    good_outcomes: List[OutcomeSpec]


def _compute_fan_outcome_prob(sim: dict, team_abbr: str, outcomes: list) -> float:
    """Sum probabilities of selected outcomes for a given team across simulation results.

    Works with both uppercase ('East'/'West') and lowercase ('east'/'west') keys
    since the cached simulation uses lowercase but run_simulation returns uppercase.
    Matchup outcomes are mutually exclusive so summing is correct.
    """
    for conf_key in ("east", "East", "west", "West"):
        conf_data = sim.get(conf_key, {})
        if team_abbr in conf_data.get("playoff_probs", {}):
            total = 0.0
            for o in outcomes:
                otype = o.get("type") if isinstance(o, dict) else getattr(o, "type", None)
                opponent = o.get("opponent") if isinstance(o, dict) else getattr(o, "opponent", None)
                seed = o.get("seed") if isinstance(o, dict) else getattr(o, "seed", None)
                if otype == "matchup" and opponent:
                    total += conf_data.get("first_round_matchups", {}).get(team_abbr, {}).get(opponent, 0.0)
                elif otype == "seed_matchup" and opponent and seed is not None:
                    smd = conf_data.get("seed_matchup_distribution", {}).get(team_abbr, {})
                    total += smd.get(str(seed), {}).get(opponent, 0.0)
                elif otype == "seed" and seed is not None:
                    seed_dist = conf_data.get("seed_distribution", {}).get(team_abbr, [])
                    if 1 <= seed <= len(seed_dist):
                        total += seed_dist[seed - 1]
                elif otype == "playoffs":
                    total += conf_data.get("playoff_probs", {}).get(team_abbr, 0.0)
                elif otype == "playin":
                    total += conf_data.get("playin_probs", {}).get(team_abbr, 0.0)
            return min(total, 1.0)
    return 0.0


def _fan_impact_sync(team_abbr: str, outcomes: list, db_path: str) -> dict:
    """Blocking: compute per-game impact on fan's desired outcomes."""
    from datetime import date, timedelta

    with get_conn(db_path) as conn:
        standings_rows = get_standings(conn)
        remaining_rows = get_remaining_games(conn)
        s_hash = standings_hash(conn)
        cached_sim = get_simulation_cache(conn, s_hash)

    if not standings_rows:
        return {"error": "No standings data. Please refresh first."}

    # Use cached baseline if available, else run a fresh sim
    if cached_sim:
        baseline_sim = json.loads(cached_sim["results_json"])
    else:
        teams_bl = standings_to_team_infos(standings_rows)
        games_bl = games_to_game_infos(remaining_rows)
        baseline_sim = run_simulation(teams_bl, games_bl, n_sims=2000, rng_seed=42)

    baseline_prob = _compute_fan_outcome_prob(baseline_sim, team_abbr, outcomes)

    # Upcoming scheduled games with team metadata (next 14 days, max 12)
    today = date.today().isoformat()
    end_date = (date.today() + timedelta(days=14)).isoformat()
    with get_conn(db_path) as conn:
        upcoming_rows = get_games_by_date_range(conn, start_date=today, end_date=end_date)

    upcoming_rows = [r for r in upcoming_rows if r.get("status") == "scheduled"][:12]

    teams = standings_to_team_infos(standings_rows)
    all_games = games_to_game_infos(remaining_rows)

    game_impacts = []

    for row in upcoming_rows:
        game_id = row["game_id"]

        home_games, away_games = [], []
        for g in all_games:
            if g.game_id == game_id:
                home_games.append(GameInfo(game_id=g.game_id, home_team_id=g.home_team_id,
                                           away_team_id=g.away_team_id, force_home_win=True))
                away_games.append(GameInfo(game_id=g.game_id, home_team_id=g.home_team_id,
                                           away_team_id=g.away_team_id, force_home_win=False))
            else:
                home_games.append(g)
                away_games.append(g)

        sim_h = run_simulation(teams, home_games, n_sims=500, rng_seed=42)
        sim_a = run_simulation(teams, away_games, n_sims=500, rng_seed=43)

        p_h = _compute_fan_outcome_prob(sim_h, team_abbr, outcomes)
        p_a = _compute_fan_outcome_prob(sim_a, team_abbr, outcomes)

        d_h = p_h - baseline_prob
        d_a = p_a - baseline_prob
        max_impact = max(abs(d_h), abs(d_a))

        root_for = "home" if d_h >= d_a else "away"
        root_abbr = row["home_abbr"] if root_for == "home" else row["away_abbr"]
        root_delta = d_h if root_for == "home" else d_a
        other_delta = d_a if root_for == "home" else d_h

        game_impacts.append({
            "game_id": game_id,
            "game_date": row["game_date"],
            "home_team": {"abbreviation": row["home_abbr"], "full_name": row.get("home_name", row["home_abbr"])},
            "away_team": {"abbreviation": row["away_abbr"], "full_name": row.get("away_name", row["away_abbr"])},
            "root_for": root_for,
            "root_for_abbr": root_abbr,
            "p_if_home_wins": round(p_h, 4),
            "p_if_away_wins": round(p_a, 4),
            "delta_home_wins": round(d_h, 4),
            "delta_away_wins": round(d_a, 4),
            "root_delta": round(root_delta, 4),
            "other_delta": round(other_delta, 4),
            "max_impact": round(max_impact, 4),
        })

    game_impacts.sort(key=lambda x: -x["max_impact"])

    return {
        "team_abbr": team_abbr,
        "baseline_prob": round(baseline_prob, 4),
        "games": game_impacts,
    }


@app.post("/api/fan-impact")
async def api_fan_impact(body: FanImpactRequest):
    if not body.good_outcomes:
        raise HTTPException(400, "No outcomes specified")

    import asyncio
    outcomes_list = [{"type": o.type, "opponent": o.opponent, "seed": o.seed} for o in body.good_outcomes]
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        None, _fan_impact_sync, body.team_abbr, outcomes_list, DB_PATH
    )

    if "error" in result:
        raise HTTPException(503, result["error"])

    return result


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)
