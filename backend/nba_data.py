"""
NBA data fetching.
- Standings: ESPN unofficial API (site.api.espn.com) — no auth needed, reliable
- Schedule:  NBA CDN (cdn.nba.com) — public static JSON
"""
import re
import logging
from datetime import datetime, timezone
from typing import Optional

import requests

from database import (
    get_conn, upsert_team, upsert_standing, upsert_game,
    set_config, invalidate_simulation_cache, invalidate_impact_cache,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SEASON_YEAR = 2026  # 2025-26 season

ESPN_STANDINGS_URL = "https://site.api.espn.com/apis/v2/sports/basketball/nba/standings"
SCHEDULE_URL = "https://cdn.nba.com/static/json/staticData/scheduleLeagueV2_1.json"

# ESPN logo CDN (reliable, no auth)
ESPN_LOGO_URL = "https://a.espncdn.com/i/teamlogos/nba/500/{slug}.png"

# Division membership keyed by 3-letter abbreviation
TEAM_DIVISIONS: dict[str, str] = {
    # Atlantic
    "BOS": "Atlantic", "BKN": "Atlantic", "NYK": "Atlantic",
    "PHI": "Atlantic", "TOR": "Atlantic",
    # Central
    "CHI": "Central", "CLE": "Central", "DET": "Central",
    "IND": "Central", "MIL": "Central",
    # Southeast
    "ATL": "Southeast", "CHA": "Southeast", "MIA": "Southeast",
    "ORL": "Southeast", "WAS": "Southeast",
    # Northwest
    "DEN": "Northwest", "MIN": "Northwest", "OKC": "Northwest",
    "POR": "Northwest", "UTA": "Northwest",
    # Pacific
    "GSW": "Pacific", "LAC": "Pacific", "LAL": "Pacific",
    "PHX": "Pacific", "SAC": "Pacific",
    # Southwest
    "DAL": "Southwest", "HOU": "Southwest", "MEM": "Southwest",
    "NOP": "Southwest", "SAS": "Southwest",
}

# NBA team ID → abbreviation (for mapping schedule data)
NBA_ID_TO_ABBR: dict[str, str] = {
    "1610612738": "BOS", "1610612751": "BKN", "1610612752": "NYK",
    "1610612755": "PHI", "1610612761": "TOR",
    "1610612741": "CHI", "1610612739": "CLE", "1610612765": "DET",
    "1610612754": "IND", "1610612749": "MIL",
    "1610612737": "ATL", "1610612766": "CHA", "1610612748": "MIA",
    "1610612753": "ORL", "1610612764": "WAS",
    "1610612743": "DEN", "1610612750": "MIN", "1610612760": "OKC",
    "1610612757": "POR", "1610612762": "UTA",
    "1610612744": "GSW", "1610612746": "LAC", "1610612747": "LAL",
    "1610612756": "PHX", "1610612758": "SAC",
    "1610612742": "DAL", "1610612745": "HOU", "1610612763": "MEM",
    "1610612740": "NOP", "1610612759": "SAS",
}

# ESPN slug → abbreviation corrections (ESPN sometimes uses different slugs)
ESPN_ABBR_MAP: dict[str, str] = {
    "GS": "GSW",   # Golden State
    "NY": "NYK",   # New York
    "NO": "NOP",   # New Orleans
    "SA": "SAS",   # San Antonio
    "OKC": "OKC",
}


# ---------------------------------------------------------------------------
# Fetch standings via ESPN
# ---------------------------------------------------------------------------

def fetch_standings(session: requests.Session) -> list[dict]:
    """
    Fetch current NBA standings from ESPN's unofficial API.
    Returns a list of team dicts with all fields needed for the DB.
    """
    resp = session.get(ESPN_STANDINGS_URL, timeout=20)
    resp.raise_for_status()
    data = resp.json()

    teams = []
    for conf_block in data.get("children", []):
        conf_name_raw = conf_block.get("abbreviation", "")
        # ESPN returns "East" or "West"
        conference = "East" if "East" in conf_block.get("name", "") else "West"

        entries = conf_block.get("standings", {}).get("entries", [])
        for entry in entries:
            team_info = entry.get("team", {})
            abbr_raw = team_info.get("abbreviation", "")
            # Normalize ESPN abbreviation to standard 3-letter
            abbr = ESPN_ABBR_MAP.get(abbr_raw, abbr_raw)

            stats_list = entry.get("stats", [])
            stats = {s["name"]: s for s in stats_list if "name" in s}

            def stat_val(name: str, default=0.0):
                s = stats.get(name, {})
                return float(s.get("value", default)) if s else default

            wins = int(stat_val("wins"))
            losses = int(stat_val("losses"))
            total = wins + losses
            win_pct = wins / total if total > 0 else 0.0
            seed = int(stat_val("playoffSeed", 0))
            streak_raw = stats.get("streak", {})
            streak = streak_raw.get("displayValue", "") if streak_raw else ""

            logo_url = ""
            logos = team_info.get("logos", [])
            if logos:
                logo_url = logos[0].get("href", "")

            teams.append({
                "team_id": abbr,            # Use abbreviation as primary key
                "abbreviation": abbr,
                "full_name": team_info.get("displayName", abbr),
                "conference": conference,
                "division": TEAM_DIVISIONS.get(abbr, "Unknown"),
                "logo_url": logo_url,
                "wins": wins,
                "losses": losses,
                "win_pct": win_pct,
                "conf_rank": seed,
                "streak": streak,
                "last_10": None,            # ESPN doesn't provide L10 easily
            })

    return teams


# ---------------------------------------------------------------------------
# Fetch schedule via NBA CDN
# ---------------------------------------------------------------------------

def fetch_schedule(session: requests.Session) -> list[dict]:
    """
    Fetch full season schedule from NBA CDN.
    Maps NBA team IDs → abbreviations using NBA_ID_TO_ABBR.
    """
    resp = session.get(SCHEDULE_URL, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    league_schedule = data.get("leagueSchedule", {})
    game_dates = league_schedule.get("gameDates", [])

    games = []
    for date_entry in game_dates:
        for game in date_entry.get("games", []):
            game_id = str(game.get("gameId", ""))
            if not game_id:
                continue

            game_date_raw = (
                game.get("gameDateEst") or
                game.get("gameDateTimeEst") or
                date_entry.get("gameDate", "")
            )
            game_date = _parse_game_date(game_date_raw)
            if not game_date or not _is_current_season(game_date):
                continue

            home = game.get("homeTeam", {})
            away = game.get("awayTeam", {})
            home_nba_id = str(home.get("teamId", ""))
            away_nba_id = str(away.get("teamId", ""))

            # Map NBA IDs to abbreviations (our primary key)
            # Fall back to teamTricode if not in map
            home_id = NBA_ID_TO_ABBR.get(home_nba_id) or home.get("teamTricode", "")
            away_id = NBA_ID_TO_ABBR.get(away_nba_id) or away.get("teamTricode", "")

            if not home_id or not away_id:
                continue

            game_status = game.get("gameStatus", 1)
            if game_status == 3:
                status = "final"
                home_score = home.get("score")
                away_score = away.get("score")
                if home_score == 0 and away_score == 0:
                    home_score = None
                    away_score = None
                    status = "scheduled"
            else:
                status = "scheduled"
                home_score = None
                away_score = None

            games.append({
                "game_id": game_id,
                "game_date": game_date,
                "home_team_id": home_id,
                "away_team_id": away_id,
                "home_score": home_score,
                "away_score": away_score,
                "status": status,
                "season_year": SEASON_YEAR,
                "manually_set": 0,
            })

    return games


def _parse_game_date(raw: str) -> Optional[str]:
    if not raw:
        return None
    # Handles "2025-10-22T00:00:00Z", "10/22/2025 00:00:00", "2025-10-22"
    match = re.match(r"(\d{4}-\d{2}-\d{2})", raw)
    if match:
        return match.group(1)
    # MM/DD/YYYY format
    match = re.match(r"(\d{2})/(\d{2})/(\d{4})", raw)
    if match:
        m, d, y = match.groups()
        return f"{y}-{m}-{d}"
    return None


def _is_current_season(date_str: str) -> bool:
    if not date_str:
        return False
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d")
        return (
            (d.year == 2025 and d.month >= 10) or
            (d.year == 2026 and d.month <= 6)
        )
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Refresh all
# ---------------------------------------------------------------------------

def refresh_all(db_path: str) -> dict:
    from database import init_db
    init_db(db_path)

    session = requests.Session()
    errors = []
    counts = {"teams": 0, "standings": 0, "games": 0}

    # --- Standings (ESPN) ---
    try:
        standings_data = fetch_standings(session)
        with get_conn(db_path) as conn:
            for item in standings_data:
                upsert_team(conn, {k: item[k] for k in
                    ["team_id", "abbreviation", "full_name", "conference", "division", "logo_url"]})
                upsert_standing(conn, {
                    "team_id": item["team_id"],
                    "wins": item["wins"],
                    "losses": item["losses"],
                    "win_pct": item["win_pct"],
                    "conf_rank": item["conf_rank"],
                    "streak": item["streak"],
                    "last_10": item["last_10"],
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                })
            counts["teams"] = len(standings_data)
            counts["standings"] = len(standings_data)
        logger.info(f"Upserted {len(standings_data)} standings from ESPN")
    except Exception as e:
        logger.error(f"Failed to fetch standings: {e}")
        errors.append(f"standings: {e}")

    # --- Schedule (NBA CDN) ---
    try:
        games_data = fetch_schedule(session)
        with get_conn(db_path) as conn:
            known_teams = {r["team_id"] for r in conn.execute("SELECT team_id FROM teams").fetchall()}
            inserted = 0
            for g in games_data:
                if g["home_team_id"] in known_teams and g["away_team_id"] in known_teams:
                    upsert_game(conn, g)
                    inserted += 1
            counts["games"] = inserted
        logger.info(f"Upserted {inserted} games from NBA CDN")
    except Exception as e:
        logger.error(f"Failed to fetch schedule: {e}")
        errors.append(f"schedule: {e}")

    # --- Invalidate caches ---
    with get_conn(db_path) as conn:
        invalidate_simulation_cache(conn)
        invalidate_impact_cache(conn)
        set_config(conn, "last_refresh_at", datetime.now(timezone.utc).isoformat())

    return {
        "success": len(errors) == 0,
        "counts": counts,
        "errors": errors,
        "refreshed_at": datetime.now(timezone.utc).isoformat(),
    }


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    db = sys.argv[1] if len(sys.argv) > 1 else "nba.db"
    result = refresh_all(db)
    print(result)
