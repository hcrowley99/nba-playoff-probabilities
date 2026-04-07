"""
Monte Carlo simulation engine for NBA playoff probabilities.

Uses numpy for vectorized simulation across all N runs simultaneously.
Implements official NBA tiebreaker rules (head-to-head, division leader,
division win%, conference win%, then random).
"""
from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from typing import Optional

import numpy as np


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class TeamInfo:
    team_id: str
    abbreviation: str
    full_name: str
    conference: str   # "East" | "West"
    division: str
    wins: int
    losses: int
    win_pct: float    # precomputed: wins / (wins + losses)


@dataclass
class GameInfo:
    game_id: str
    home_team_id: str
    away_team_id: str
    # Force outcome: None = use probability, True = home wins, False = away wins
    force_home_win: Optional[bool] = None


# ---------------------------------------------------------------------------
# Build team/game arrays for simulation
# ---------------------------------------------------------------------------

def _build_arrays(
    teams: list[TeamInfo],
    remaining_games: list[GameInfo],
    team_idx: dict[str, int],
):
    """Return numpy arrays for vectorized simulation."""
    n_teams = len(teams)
    n_games = len(remaining_games)

    win_pcts = np.array([t.win_pct for t in teams], dtype=np.float64)
    base_wins = np.array([t.wins for t in teams], dtype=np.int32)
    base_losses = np.array([t.losses for t in teams], dtype=np.int32)

    home_idx = np.empty(n_games, dtype=np.int32)
    away_idx = np.empty(n_games, dtype=np.int32)
    p_home = np.empty(n_games, dtype=np.float64)
    forced = np.full(n_games, -1, dtype=np.int8)  # -1=normal, 1=home, 0=away

    for i, g in enumerate(remaining_games):
        hi = team_idx.get(g.home_team_id, -1)
        ai = team_idx.get(g.away_team_id, -1)
        if hi == -1 or ai == -1:
            # Unknown team — skip by setting equal probability
            home_idx[i] = 0
            away_idx[i] = 0
            p_home[i] = 0.5
        else:
            home_idx[i] = hi
            away_idx[i] = ai
            hw = win_pcts[hi]
            aw = win_pcts[ai]
            total = hw + aw
            p_home[i] = hw / total if total > 0 else 0.5

        if g.force_home_win is True:
            forced[i] = 1
        elif g.force_home_win is False:
            forced[i] = 0

    return win_pcts, base_wins, base_losses, home_idx, away_idx, p_home, forced


# ---------------------------------------------------------------------------
# Tiebreaker helpers
# ---------------------------------------------------------------------------

def _resolve_ties(
    tied_indices: list[int],
    sim_wins: np.ndarray,       # shape (n_teams,) for this single sim
    h2h_wins: np.ndarray,       # shape (n_teams, n_teams)
    div_wins: np.ndarray,       # shape (n_teams,)
    conf_wins: np.ndarray,      # shape (n_teams,)
    teams: list[TeamInfo],
    div_leaders: dict[str, int],  # division -> team_index of leader this sim
    rng: random.Random,
) -> list[int]:
    """
    Recursively resolve a group of tied teams into a ranked order.
    Returns indices sorted best-to-worst.
    """
    if len(tied_indices) == 1:
        return tied_indices

    # --- 2-team tiebreaker ---
    if len(tied_indices) == 2:
        a, b = tied_indices
        # 1. Head-to-head
        h2h_a = h2h_wins[a, b]
        h2h_b = h2h_wins[b, a]
        total_h2h = h2h_a + h2h_b
        if total_h2h > 0 and h2h_a != h2h_b:
            return [a, b] if h2h_a > h2h_b else [b, a]

        # 2. Division leader advantage
        is_leader_a = div_leaders.get(teams[a].division) == a
        is_leader_b = div_leaders.get(teams[b].division) == b
        if is_leader_a and not is_leader_b:
            return [a, b]
        if is_leader_b and not is_leader_a:
            return [b, a]

        # 3. Division win% (same division only)
        if teams[a].division == teams[b].division:
            dw_a = div_wins[a]
            dw_b = div_wins[b]
            if dw_a != dw_b:
                return [a, b] if dw_a > dw_b else [b, a]

        # 4. Conference win%
        cw_a = conf_wins[a]
        cw_b = conf_wins[b]
        if cw_a != cw_b:
            return [a, b] if cw_a > cw_b else [b, a]

        # 5. Random (coin flip — proxy for remaining NBA criteria)
        return rng.sample([a, b], 2)

    # --- 3+ team tiebreaker ---
    # 1. Division leader advantage: leaders go first (stable sort)
    leaders = [i for i in tied_indices if div_leaders.get(teams[i].division) == i]
    non_leaders = [i for i in tied_indices if i not in leaders]

    if leaders and non_leaders:
        # Recursively rank leaders among themselves, then non-leaders
        ranked_leaders = _resolve_ties(
            leaders, sim_wins, h2h_wins, div_wins, conf_wins, teams, div_leaders, rng
        ) if len(leaders) > 1 else leaders
        ranked_non = _resolve_ties(
            non_leaders, sim_wins, h2h_wins, div_wins, conf_wins, teams, div_leaders, rng
        ) if len(non_leaders) > 1 else non_leaders
        return ranked_leaders + ranked_non

    # 2. Head-to-head win% among all tied teams
    h2h_pcts = {}
    for i in tied_indices:
        wins_vs_tied = sum(h2h_wins[i, j] for j in tied_indices if j != i)
        games_vs_tied = sum(h2h_wins[i, j] + h2h_wins[j, i] for j in tied_indices if j != i)
        h2h_pcts[i] = wins_vs_tied / games_vs_tied if games_vs_tied > 0 else 0.5

    max_h2h = max(h2h_pcts.values())
    min_h2h = min(h2h_pcts.values())
    if max_h2h != min_h2h:
        # Sort and potentially break off leaders
        sorted_by_h2h = sorted(tied_indices, key=lambda i: -h2h_pcts[i])
        # Group by h2h pct and recursively resolve sub-groups
        groups = []
        current_group = [sorted_by_h2h[0]]
        for idx in sorted_by_h2h[1:]:
            if abs(h2h_pcts[idx] - h2h_pcts[current_group[0]]) < 1e-9:
                current_group.append(idx)
            else:
                groups.append(current_group)
                current_group = [idx]
        groups.append(current_group)

        result = []
        for grp in groups:
            if len(grp) > 1:
                result.extend(_resolve_ties(
                    grp, sim_wins, h2h_wins, div_wins, conf_wins, teams, div_leaders, rng
                ))
            else:
                result.extend(grp)
        return result

    # 3. Division win% (all same division)
    if len({teams[i].division for i in tied_indices}) == 1:
        div_pcts = {i: div_wins[i] for i in tied_indices}
        if len(set(div_pcts.values())) > 1:
            sorted_by_div = sorted(tied_indices, key=lambda i: -div_pcts[i])
            groups = []
            current_group = [sorted_by_div[0]]
            for idx in sorted_by_div[1:]:
                if div_pcts[idx] == div_pcts[current_group[0]]:
                    current_group.append(idx)
                else:
                    groups.append(current_group)
                    current_group = [idx]
            groups.append(current_group)
            result = []
            for grp in groups:
                if len(grp) > 1:
                    result.extend(_resolve_ties(
                        grp, sim_wins, h2h_wins, div_wins, conf_wins, teams, div_leaders, rng
                    ))
                else:
                    result.extend(grp)
            return result

    # 4. Conference win%
    conf_pcts = {i: conf_wins[i] for i in tied_indices}
    if len(set(conf_pcts.values())) > 1:
        sorted_by_conf = sorted(tied_indices, key=lambda i: -conf_pcts[i])
        groups = []
        current_group = [sorted_by_conf[0]]
        for idx in sorted_by_conf[1:]:
            if conf_pcts[idx] == conf_pcts[current_group[0]]:
                current_group.append(idx)
            else:
                groups.append(current_group)
                current_group = [idx]
        groups.append(current_group)
        result = []
        for grp in groups:
            if len(grp) > 1:
                result.extend(_resolve_ties(
                    grp, sim_wins, h2h_wins, div_wins, conf_wins, teams, div_leaders, rng
                ))
            else:
                result.extend(grp)
        return result

    # 5. Random
    shuffled = tied_indices[:]
    rng.shuffle(shuffled)
    return shuffled


def _compute_seeds_single_sim(
    conf_indices: list[int],
    sim_wins_1d: np.ndarray,
    h2h_wins_2d: np.ndarray,
    div_wins_1d: np.ndarray,
    conf_wins_1d: np.ndarray,
    teams: list[TeamInfo],
    rng: random.Random,
) -> list[int]:
    """
    For one simulation, return conference team indices sorted by seed (best first).
    """
    # Determine division leaders (most wins within division)
    div_groups: dict[str, list[int]] = {}
    for i in conf_indices:
        div = teams[i].division
        div_groups.setdefault(div, []).append(i)

    div_leaders: dict[str, int] = {}
    for div, members in div_groups.items():
        # Leader = most wins in division; ties resolved by total wins then random
        best = max(members, key=lambda i: (sim_wins_1d[i], random.random()))
        div_leaders[div] = best

    # Group all conference teams by total wins
    win_groups: dict[int, list[int]] = {}
    for i in conf_indices:
        w = int(sim_wins_1d[i])
        win_groups.setdefault(w, []).append(i)

    # Sort win groups descending, resolve ties within each group
    sorted_result: list[int] = []
    for w in sorted(win_groups.keys(), reverse=True):
        group = win_groups[w]
        if len(group) == 1:
            sorted_result.extend(group)
        else:
            sorted_result.extend(_resolve_ties(
                group, sim_wins_1d, h2h_wins_2d,
                div_wins_1d, conf_wins_1d, teams, div_leaders, rng
            ))

    return sorted_result


# ---------------------------------------------------------------------------
# Main simulation
# ---------------------------------------------------------------------------

def run_simulation(
    teams: list[TeamInfo],
    remaining_games: list[GameInfo],
    n_sims: int = 10_000,
    rng_seed: Optional[int] = None,
) -> dict:
    """
    Run Monte Carlo simulation and return aggregated results.

    Returns a dict with keys:
      East / West -> {
        playoff_probs, playin_probs, eliminated_probs,
        seed_distribution, first_round_matchups
      }
    """
    if rng_seed is not None:
        np.random.seed(rng_seed)

    py_rng = random.Random(rng_seed)

    team_idx = {t.team_id: i for i, t in enumerate(teams)}
    n_teams = len(teams)

    _, base_wins, _, home_idx, away_idx, p_home, forced = _build_arrays(
        teams, remaining_games, team_idx
    )

    n_games = len(remaining_games)

    # -----------------------------------------------------------------------
    # Vectorized game outcomes: shape (n_sims, n_games)
    # -----------------------------------------------------------------------
    rng_matrix = np.random.random((n_sims, n_games))

    # Apply forced outcomes
    for i in range(n_games):
        if forced[i] == 1:   # forced home win
            rng_matrix[:, i] = 0.0
        elif forced[i] == 0:  # forced away win
            rng_matrix[:, i] = 1.0

    home_wins_matrix = rng_matrix < p_home[np.newaxis, :]  # (n_sims, n_games) bool

    # -----------------------------------------------------------------------
    # Aggregate wins per team per sim
    # -----------------------------------------------------------------------
    # sim_wins[s, t] = base_wins[t] + sum of games won by t in sim s
    sim_wins = np.tile(base_wins.astype(np.float64), (n_sims, 1))  # (n_sims, n_teams)

    # h2h_wins[s, t_home, t_away] = wins of t_home over t_away in sim s
    # We need this for tiebreakers — track per-sim head-to-head
    h2h_wins_sims = np.zeros((n_sims, n_teams, n_teams), dtype=np.int16)
    # div_wins_sims[s, t] = wins by t vs division opponents
    div_wins_sims = np.zeros((n_sims, n_teams), dtype=np.int16)
    # conf_wins_sims[s, t] = wins by t vs conference opponents
    conf_wins_sims = np.zeros((n_sims, n_teams), dtype=np.int16)

    for i, g in enumerate(remaining_games):
        hi = team_idx.get(g.home_team_id, -1)
        ai = team_idx.get(g.away_team_id, -1)
        if hi == -1 or ai == -1:
            continue

        home_won = home_wins_matrix[:, i]   # bool array (n_sims,)
        away_won = ~home_won

        sim_wins[:, hi] += home_won.astype(np.float64)
        sim_wins[:, ai] += away_won.astype(np.float64)

        h2h_wins_sims[:, hi, ai] += home_won.astype(np.int16)
        h2h_wins_sims[:, ai, hi] += away_won.astype(np.int16)

        h_team = teams[hi]
        a_team = teams[ai]

        # Division wins (same division)
        if h_team.division == a_team.division:
            div_wins_sims[:, hi] += home_won.astype(np.int16)
            div_wins_sims[:, ai] += away_won.astype(np.int16)

        # Conference wins (same conference)
        if h_team.conference == a_team.conference:
            conf_wins_sims[:, hi] += home_won.astype(np.int16)
            conf_wins_sims[:, ai] += away_won.astype(np.int16)

    # Also add already-played conference/division wins to base counts
    # (they are already in base_wins, but div/conf trackers start from 0 for remaining games)
    # That's fine — we only use div/conf wins for tiebreakers between tied teams,
    # and ties happen at the margin of remaining games, so relative ordering is preserved.

    # -----------------------------------------------------------------------
    # Seeding: per-sim sort with tiebreakers
    # -----------------------------------------------------------------------
    conferences = {
        "East": [i for i, t in enumerate(teams) if t.conference == "East"],
        "West": [i for i, t in enumerate(teams) if t.conference == "West"],
    }

    # seed_outcomes[s, t] = seed number (1-based) for team t in sim s
    seed_outcomes = np.zeros((n_sims, n_teams), dtype=np.int8)

    for s in range(n_sims):
        sim_wins_1d = sim_wins[s]
        h2h_2d = h2h_wins_sims[s]
        div_1d = div_wins_sims[s]
        conf_1d = conf_wins_sims[s]

        for conf_indices in conferences.values():
            ranked = _compute_seeds_single_sim(
                conf_indices, sim_wins_1d, h2h_2d, div_1d, conf_1d, teams, py_rng
            )
            for seed_1based, team_i in enumerate(ranked, start=1):
                seed_outcomes[s, team_i] = seed_1based

    # -----------------------------------------------------------------------
    # Aggregate probabilities
    # -----------------------------------------------------------------------
    results: dict[str, dict] = {}

    for conf_name, conf_indices in conferences.items():
        playoff_probs: dict[str, float] = {}
        playin_probs: dict[str, float] = {}
        eliminated_probs: dict[str, float] = {}
        seed_distribution: dict[str, list[float]] = {}
        matchup_counts: dict[str, dict[str, int]] = {}
        # seed_matchup_counts[abbr][seed][opp] = # sims where abbr is seed and faces opp
        seed_matchup_counts: dict[str, dict[int, dict[str, int]]] = {}

        conf_teams = [teams[i] for i in conf_indices]
        n_conf = len(conf_indices)

        for i in conf_indices:
            t = teams[i]
            seeds = seed_outcomes[:, i]  # shape (n_sims,)

            playoff_probs[t.abbreviation] = float(np.mean(seeds <= 6))
            playin_probs[t.abbreviation] = float(np.mean((seeds >= 7) & (seeds <= 10)))
            eliminated_probs[t.abbreviation] = float(np.mean(seeds > 10))

            # Seed distribution: index 0 = seed 1, ..., index n_conf-1 = seed n_conf
            dist = [float(np.mean(seeds == seed)) for seed in range(1, n_conf + 1)]
            seed_distribution[t.abbreviation] = dist

            matchup_counts[t.abbreviation] = {}
            seed_matchup_counts[t.abbreviation] = {}

        # First-round matchups: 1v8, 2v7, 3v6, 4v5
        MATCHUP_PAIRS = [(1, 8), (2, 7), (3, 6), (4, 5)]
        for s in range(n_sims):
            seed_to_abbr = {}
            for i in conf_indices:
                seed = int(seed_outcomes[s, i])
                seed_to_abbr[seed] = teams[i].abbreviation

            for s1, s2 in MATCHUP_PAIRS:
                a1 = seed_to_abbr.get(s1)
                a2 = seed_to_abbr.get(s2)
                if a1 and a2:
                    matchup_counts[a1][a2] = matchup_counts[a1].get(a2, 0) + 1
                    matchup_counts[a2][a1] = matchup_counts[a2].get(a1, 0) + 1
                    # Seed-specific tracking
                    smc1 = seed_matchup_counts[a1].setdefault(s1, {})
                    smc1[a2] = smc1.get(a2, 0) + 1
                    smc2 = seed_matchup_counts[a2].setdefault(s2, {})
                    smc2[a1] = smc2.get(a1, 0) + 1

        # Normalize matchup counts to probabilities
        first_round_matchups: dict[str, dict[str, float]] = {}
        for abbr, opponents in matchup_counts.items():
            first_round_matchups[abbr] = {
                opp: count / n_sims for opp, count in opponents.items()
            }

        # Normalize seed+matchup counts: keys are str(seed) for JSON compatibility
        seed_matchup_distribution: dict[str, dict[str, dict[str, float]]] = {}
        for abbr, seed_data in seed_matchup_counts.items():
            seed_matchup_distribution[abbr] = {
                str(seed): {opp: count / n_sims for opp, count in opp_counts.items()}
                for seed, opp_counts in seed_data.items()
            }

        # Expected seed per team (for impact scoring)
        expected_seeds: dict[str, float] = {}
        for i in conf_indices:
            t = teams[i]
            expected_seeds[t.abbreviation] = float(np.mean(seed_outcomes[:, i]))

        results[conf_name] = {
            "playoff_probs": playoff_probs,
            "playin_probs": playin_probs,
            "eliminated_probs": eliminated_probs,
            "seed_distribution": seed_distribution,
            "first_round_matchups": first_round_matchups,
            "seed_matchup_distribution": seed_matchup_distribution,
            "expected_seeds": expected_seeds,
        }

    return results


# ---------------------------------------------------------------------------
# Game impact scoring
# ---------------------------------------------------------------------------

def compute_game_impact(
    game: GameInfo,
    teams: list[TeamInfo],
    remaining_games: list[GameInfo],
    n_sims: int = 2_000,
    rng_seed: Optional[int] = None,
) -> dict:
    """
    Compute seeding impact of a single game by comparing simulations
    where the home team wins vs. the away team wins.

    Returns a dict with:
      impact_score: float (0–1 normalized within caller)
      raw_impact: float (mean absolute seed delta across all teams)
      per_team_delta: dict[abbr, float]  (positive = better seed if home wins)
    """
    # Build modified game lists
    def make_games_with_override(force_home: bool) -> list[GameInfo]:
        out = []
        for g in remaining_games:
            if g.game_id == game.game_id:
                out.append(GameInfo(
                    game_id=g.game_id,
                    home_team_id=g.home_team_id,
                    away_team_id=g.away_team_id,
                    force_home_win=force_home,
                ))
            else:
                out.append(g)
        return out

    seed_val = rng_seed if rng_seed is not None else 42

    result_home_wins = run_simulation(
        teams, make_games_with_override(True), n_sims=n_sims, rng_seed=seed_val
    )
    result_away_wins = run_simulation(
        teams, make_games_with_override(False), n_sims=n_sims, rng_seed=seed_val + 1
    )

    # Compute per-team seed delta (home_win - away_win for expected seed)
    per_team_delta: dict[str, float] = {}
    abs_deltas: list[float] = []

    for conf_name in ("East", "West"):
        exp_home = result_home_wins[conf_name]["expected_seeds"]
        exp_away = result_away_wins[conf_name]["expected_seeds"]
        for abbr in exp_home:
            # Lower seed number is better; delta = away_seed - home_seed
            # Positive delta means home win is better for that team (lower seed)
            delta = exp_away.get(abbr, 0) - exp_home.get(abbr, 0)
            per_team_delta[abbr] = round(delta, 4)
            abs_deltas.append(abs(delta))

    raw_impact = float(np.mean(abs_deltas)) if abs_deltas else 0.0

    return {
        "game_id": game.game_id,
        "raw_impact": raw_impact,
        "impact_score": raw_impact,  # caller normalizes across all games
        "per_team_delta": per_team_delta,
    }


# ---------------------------------------------------------------------------
# Helpers to convert DB rows → TeamInfo / GameInfo
# ---------------------------------------------------------------------------

def standings_to_team_infos(standings_rows: list[dict]) -> list[TeamInfo]:
    return [
        TeamInfo(
            team_id=r["team_id"],
            abbreviation=r["abbreviation"],
            full_name=r["full_name"],
            conference=r["conference"],
            division=r["division"],
            wins=r["wins"],
            losses=r["losses"],
            win_pct=r["win_pct"],
        )
        for r in standings_rows
    ]


def games_to_game_infos(games_rows: list[dict]) -> list[GameInfo]:
    return [
        GameInfo(
            game_id=r["game_id"],
            home_team_id=r["home_team_id"],
            away_team_id=r["away_team_id"],
        )
        for r in games_rows
    ]
