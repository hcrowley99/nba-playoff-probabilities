"""
SQLite database layer: schema, initialization, and all CRUD helpers.
"""
import sqlite3
import hashlib
import json
from typing import Optional


def get_conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(db_path: str) -> None:
    with get_conn(db_path) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS teams (
                team_id     TEXT PRIMARY KEY,
                abbreviation TEXT NOT NULL,
                full_name   TEXT NOT NULL,
                conference  TEXT NOT NULL,
                division    TEXT NOT NULL,
                logo_url    TEXT
            );

            CREATE TABLE IF NOT EXISTS standings (
                team_id     TEXT PRIMARY KEY REFERENCES teams(team_id),
                wins        INTEGER NOT NULL DEFAULT 0,
                losses      INTEGER NOT NULL DEFAULT 0,
                win_pct     REAL NOT NULL DEFAULT 0.0,
                conf_rank   INTEGER NOT NULL DEFAULT 0,
                streak      TEXT,
                last_10     TEXT,
                updated_at  TEXT
            );

            CREATE TABLE IF NOT EXISTS games (
                game_id      TEXT PRIMARY KEY,
                game_date    TEXT NOT NULL,
                home_team_id TEXT NOT NULL REFERENCES teams(team_id),
                away_team_id TEXT NOT NULL REFERENCES teams(team_id),
                home_score   INTEGER,
                away_score   INTEGER,
                status       TEXT NOT NULL DEFAULT 'scheduled',
                season_year  INTEGER NOT NULL DEFAULT 2026,
                manually_set INTEGER NOT NULL DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_games_date ON games(game_date);
            CREATE INDEX IF NOT EXISTS idx_games_status ON games(status);

            CREATE TABLE IF NOT EXISTS simulation_cache (
                cache_key   TEXT PRIMARY KEY,
                results_json TEXT NOT NULL,
                created_at  TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS impact_cache (
                game_id         TEXT PRIMARY KEY REFERENCES games(game_id),
                standings_hash  TEXT NOT NULL,
                impact_json     TEXT NOT NULL,
                computed_at     TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS config (
                key     TEXT PRIMARY KEY,
                value   TEXT NOT NULL
            );
        """)


# ---------------------------------------------------------------------------
# Standings fingerprint (used for cache invalidation)
# ---------------------------------------------------------------------------

def standings_hash(conn: sqlite3.Connection) -> str:
    rows = conn.execute(
        "SELECT team_id, wins, losses FROM standings ORDER BY team_id"
    ).fetchall()
    payload = json.dumps([(r["team_id"], r["wins"], r["losses"]) for r in rows])
    return hashlib.md5(payload.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Teams
# ---------------------------------------------------------------------------

def upsert_team(conn: sqlite3.Connection, t: dict) -> None:
    conn.execute("""
        INSERT INTO teams (team_id, abbreviation, full_name, conference, division, logo_url)
        VALUES (:team_id, :abbreviation, :full_name, :conference, :division, :logo_url)
        ON CONFLICT(team_id) DO UPDATE SET
            abbreviation = excluded.abbreviation,
            full_name    = excluded.full_name,
            conference   = excluded.conference,
            division     = excluded.division,
            logo_url     = excluded.logo_url
    """, t)


def get_teams(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM teams ORDER BY conference, division, abbreviation").fetchall()]


def get_team(conn: sqlite3.Connection, team_id: str) -> Optional[dict]:
    row = conn.execute("SELECT * FROM teams WHERE team_id = ?", (team_id,)).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Standings
# ---------------------------------------------------------------------------

def upsert_standing(conn: sqlite3.Connection, s: dict) -> None:
    conn.execute("""
        INSERT INTO standings (team_id, wins, losses, win_pct, conf_rank, streak, last_10, updated_at)
        VALUES (:team_id, :wins, :losses, :win_pct, :conf_rank, :streak, :last_10, :updated_at)
        ON CONFLICT(team_id) DO UPDATE SET
            wins       = excluded.wins,
            losses     = excluded.losses,
            win_pct    = excluded.win_pct,
            conf_rank  = excluded.conf_rank,
            streak     = excluded.streak,
            last_10    = excluded.last_10,
            updated_at = excluded.updated_at
    """, s)


def get_standings(conn: sqlite3.Connection, conference: Optional[str] = None) -> list[dict]:
    query = """
        SELECT s.*, t.abbreviation, t.full_name, t.conference, t.division, t.logo_url,
               (SELECT COUNT(*) FROM games g
                WHERE g.status = 'scheduled'
                  AND (g.home_team_id = s.team_id OR g.away_team_id = s.team_id)) AS games_remaining
        FROM standings s
        JOIN teams t ON t.team_id = s.team_id
    """
    params: list = []
    if conference:
        query += " WHERE t.conference = ?"
        params.append(conference)
    query += " ORDER BY t.conference, s.conf_rank"
    return [dict(r) for r in conn.execute(query, params).fetchall()]


# ---------------------------------------------------------------------------
# Games
# ---------------------------------------------------------------------------

def upsert_game(conn: sqlite3.Connection, g: dict) -> None:
    conn.execute("""
        INSERT INTO games (game_id, game_date, home_team_id, away_team_id,
                           home_score, away_score, status, season_year, manually_set)
        VALUES (:game_id, :game_date, :home_team_id, :away_team_id,
                :home_score, :away_score, :status, :season_year, :manually_set)
        ON CONFLICT(game_id) DO UPDATE SET
            game_date    = excluded.game_date,
            home_score   = CASE WHEN games.manually_set = 1 THEN games.home_score ELSE excluded.home_score END,
            away_score   = CASE WHEN games.manually_set = 1 THEN games.away_score ELSE excluded.away_score END,
            status       = CASE WHEN games.manually_set = 1 THEN games.status ELSE excluded.status END
    """, g)


def get_game(conn: sqlite3.Connection, game_id: str) -> Optional[dict]:
    row = conn.execute("SELECT * FROM games WHERE game_id = ?", (game_id,)).fetchone()
    return dict(row) if row else None


def get_remaining_games(conn: sqlite3.Connection, season_year: int = 2026) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM games WHERE status = 'scheduled' AND season_year = ? ORDER BY game_date",
        (season_year,)
    ).fetchall()]


def get_games_by_date_range(
    conn: sqlite3.Connection,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    season_year: int = 2026
) -> list[dict]:
    query = """
        SELECT g.*,
               th.abbreviation AS home_abbr, th.full_name AS home_name,
               th.conference AS home_conf, th.division AS home_div,
               ta.abbreviation AS away_abbr, ta.full_name AS away_name,
               ta.conference AS away_conf,
               sh.wins AS home_wins, sh.losses AS home_losses, sh.win_pct AS home_win_pct,
               sa.wins AS away_wins, sa.losses AS away_losses, sa.win_pct AS away_win_pct
        FROM games g
        JOIN teams th ON th.team_id = g.home_team_id
        JOIN teams ta ON ta.team_id = g.away_team_id
        LEFT JOIN standings sh ON sh.team_id = g.home_team_id
        LEFT JOIN standings sa ON sa.team_id = g.away_team_id
        WHERE g.season_year = ?
    """
    params: list = [season_year]
    if start_date:
        query += " AND g.game_date >= ?"
        params.append(start_date)
    if end_date:
        query += " AND g.game_date <= ?"
        params.append(end_date)
    query += " ORDER BY g.game_date, g.game_id"
    return [dict(r) for r in conn.execute(query, params).fetchall()]


def set_game_result(
    conn: sqlite3.Connection,
    game_id: str,
    home_score: int,
    away_score: int
) -> bool:
    """Manually record a game result. Updates standings accordingly."""
    game = get_game(conn, game_id)
    if not game:
        return False

    was_scheduled = game["status"] == "scheduled"

    conn.execute("""
        UPDATE games SET home_score = ?, away_score = ?, status = 'final', manually_set = 1
        WHERE game_id = ?
    """, (home_score, away_score, game_id))

    if was_scheduled:
        # Update wins/losses for both teams
        if home_score > away_score:
            winner_id, loser_id = game["home_team_id"], game["away_team_id"]
        else:
            winner_id, loser_id = game["away_team_id"], game["home_team_id"]

        conn.execute("UPDATE standings SET wins = wins + 1 WHERE team_id = ?", (winner_id,))
        conn.execute("UPDATE standings SET losses = losses + 1 WHERE team_id = ?", (loser_id,))
        # Recalculate win_pct
        conn.execute("""
            UPDATE standings SET win_pct = CAST(wins AS REAL) / (wins + losses)
            WHERE team_id IN (?, ?)
        """, (winner_id, loser_id))

    return True


# ---------------------------------------------------------------------------
# Simulation cache
# ---------------------------------------------------------------------------

def get_simulation_cache(conn: sqlite3.Connection, cache_key: str) -> Optional[dict]:
    row = conn.execute(
        "SELECT * FROM simulation_cache WHERE cache_key = ?", (cache_key,)
    ).fetchone()
    return dict(row) if row else None


def set_simulation_cache(conn: sqlite3.Connection, cache_key: str, results_json: str) -> None:
    from datetime import datetime, timezone
    conn.execute("""
        INSERT INTO simulation_cache (cache_key, results_json, created_at)
        VALUES (?, ?, ?)
        ON CONFLICT(cache_key) DO UPDATE SET results_json = excluded.results_json, created_at = excluded.created_at
    """, (cache_key, results_json, datetime.now(timezone.utc).isoformat()))


def invalidate_simulation_cache(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM simulation_cache")


# ---------------------------------------------------------------------------
# Impact cache
# ---------------------------------------------------------------------------

def get_impact_cache(conn: sqlite3.Connection, game_id: str, s_hash: str) -> Optional[dict]:
    row = conn.execute(
        "SELECT * FROM impact_cache WHERE game_id = ? AND standings_hash = ?",
        (game_id, s_hash)
    ).fetchone()
    return dict(row) if row else None


def set_impact_cache(
    conn: sqlite3.Connection, game_id: str, s_hash: str, impact_json: str
) -> None:
    from datetime import datetime, timezone
    conn.execute("""
        INSERT INTO impact_cache (game_id, standings_hash, impact_json, computed_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(game_id) DO UPDATE SET
            standings_hash = excluded.standings_hash,
            impact_json    = excluded.impact_json,
            computed_at    = excluded.computed_at
    """, (game_id, s_hash, impact_json, datetime.now(timezone.utc).isoformat()))


def invalidate_impact_cache(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM impact_cache")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def get_config(conn: sqlite3.Connection, key: str, default: Optional[str] = None) -> Optional[str]:
    row = conn.execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_config(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("""
        INSERT INTO config (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
    """, (key, value))
