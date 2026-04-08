'use strict';

/**
 * Client-side Monte Carlo simulation engine for NBA playoff probabilities.
 * Ports backend/simulation.py to JavaScript.
 *
 * Main export: runSimulation(teams, games, manualOutcomes, nSims)
 */

// ---------------------------------------------------------------------------
// Tiebreaker helpers
// ---------------------------------------------------------------------------

/**
 * Recursively resolve tied team indices into a ranked order (best first).
 * @param {number[]} tiedIndices
 * @param {Float64Array} simWins
 * @param {Int32Array} h2hWins - flat [a*nTeams+b] = wins of a over b
 * @param {Int32Array} divWins
 * @param {Int32Array} confWins
 * @param {Object[]} teams
 * @param {Object} divLeaders - division -> team index of leader this sim
 * @param {number} nTeams
 * @returns {number[]} sorted indices best-to-worst
 */
function resolveTies(tiedIndices, simWins, h2hWins, divWins, confWins, teams, divLeaders, nTeams) {
  if (tiedIndices.length === 1) return tiedIndices;

  if (tiedIndices.length === 2) {
    const [a, b] = tiedIndices;

    // 1. Head-to-head
    const h2hA = h2hWins[a * nTeams + b];
    const h2hB = h2hWins[b * nTeams + a];
    const totalH2H = h2hA + h2hB;
    if (totalH2H > 0 && h2hA !== h2hB) return h2hA > h2hB ? [a, b] : [b, a];

    // 2. Division leader advantage
    const isLeaderA = divLeaders[teams[a].division] === a;
    const isLeaderB = divLeaders[teams[b].division] === b;
    if (isLeaderA && !isLeaderB) return [a, b];
    if (isLeaderB && !isLeaderA) return [b, a];

    // 3. Division win% (same division only)
    if (teams[a].division === teams[b].division && divWins[a] !== divWins[b]) {
      return divWins[a] > divWins[b] ? [a, b] : [b, a];
    }

    // 4. Conference win%
    if (confWins[a] !== confWins[b]) return confWins[a] > confWins[b] ? [a, b] : [b, a];

    // 5. Random
    return Math.random() < 0.5 ? [a, b] : [b, a];
  }

  // --- 3+ team tiebreaker ---

  // 1. Division leader advantage: leaders go first
  const leaders = tiedIndices.filter(i => divLeaders[teams[i].division] === i);
  const nonLeaders = tiedIndices.filter(i => !leaders.includes(i));

  if (leaders.length > 0 && nonLeaders.length > 0) {
    const rankedLeaders = leaders.length > 1
      ? resolveTies(leaders, simWins, h2hWins, divWins, confWins, teams, divLeaders, nTeams)
      : leaders;
    const rankedNon = nonLeaders.length > 1
      ? resolveTies(nonLeaders, simWins, h2hWins, divWins, confWins, teams, divLeaders, nTeams)
      : nonLeaders;
    return [...rankedLeaders, ...rankedNon];
  }

  // 2. H2H win% among all tied
  const h2hPcts = {};
  for (const i of tiedIndices) {
    let winsVsTied = 0, gamesVsTied = 0;
    for (const j of tiedIndices) {
      if (j === i) continue;
      winsVsTied += h2hWins[i * nTeams + j];
      gamesVsTied += h2hWins[i * nTeams + j] + h2hWins[j * nTeams + i];
    }
    h2hPcts[i] = gamesVsTied > 0 ? winsVsTied / gamesVsTied : 0.5;
  }

  const vals = Object.values(h2hPcts);
  if (Math.max(...vals) !== Math.min(...vals)) {
    const sorted = [...tiedIndices].sort((a, b) => h2hPcts[b] - h2hPcts[a]);
    return resolveGrouped(sorted, h2hPcts, simWins, h2hWins, divWins, confWins, teams, divLeaders, nTeams);
  }

  // 3. Division win% (all same division)
  const divSet = new Set(tiedIndices.map(i => teams[i].division));
  if (divSet.size === 1) {
    const divPcts = {};
    for (const i of tiedIndices) divPcts[i] = divWins[i];
    const dvals = Object.values(divPcts);
    if (new Set(dvals).size > 1) {
      const sorted = [...tiedIndices].sort((a, b) => divPcts[b] - divPcts[a]);
      return resolveGrouped(sorted, divPcts, simWins, h2hWins, divWins, confWins, teams, divLeaders, nTeams);
    }
  }

  // 4. Conference win%
  const confPcts = {};
  for (const i of tiedIndices) confPcts[i] = confWins[i];
  const cvals = Object.values(confPcts);
  if (new Set(cvals).size > 1) {
    const sorted = [...tiedIndices].sort((a, b) => confPcts[b] - confPcts[a]);
    return resolveGrouped(sorted, confPcts, simWins, h2hWins, divWins, confWins, teams, divLeaders, nTeams);
  }

  // 5. Random shuffle
  const shuffled = [...tiedIndices];
  for (let i = shuffled.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [shuffled[i], shuffled[j]] = [shuffled[j], shuffled[i]];
  }
  return shuffled;
}

/**
 * Given a sorted array of indices and a value map, group by equal value and
 * recursively resolve each sub-group.
 */
function resolveGrouped(sortedIndices, valMap, simWins, h2hWins, divWins, confWins, teams, divLeaders, nTeams) {
  const groups = [];
  let cur = [sortedIndices[0]];
  for (let k = 1; k < sortedIndices.length; k++) {
    const idx = sortedIndices[k];
    if (Math.abs(valMap[idx] - valMap[cur[0]]) < 1e-9) {
      cur.push(idx);
    } else {
      groups.push(cur);
      cur = [idx];
    }
  }
  groups.push(cur);

  const result = [];
  for (const grp of groups) {
    if (grp.length > 1) {
      result.push(...resolveTies(grp, simWins, h2hWins, divWins, confWins, teams, divLeaders, nTeams));
    } else {
      result.push(...grp);
    }
  }
  return result;
}

/**
 * For one simulation, return conference team indices sorted by seed (best first).
 */
function computeSeedsSingleSim(confIndices, simWins, h2hWins, divWins, confWins, teams, nTeams) {
  // Determine division leaders (most sim wins in division)
  const divGroups = {};
  for (const i of confIndices) {
    const div = teams[i].division;
    if (!divGroups[div]) divGroups[div] = [];
    divGroups[div].push(i);
  }

  const divLeaders = {};
  for (const [div, members] of Object.entries(divGroups)) {
    let best = members[0];
    for (const m of members) {
      if (simWins[m] > simWins[best] || (simWins[m] === simWins[best] && Math.random() < 0.5)) {
        best = m;
      }
    }
    divLeaders[div] = best;
  }

  // Group teams by total wins
  const winGroups = {};
  for (const i of confIndices) {
    const w = Math.floor(simWins[i]);
    if (!winGroups[w]) winGroups[w] = [];
    winGroups[w].push(i);
  }

  // Sort win groups descending, resolve ties within each group
  const sorted = [];
  const winLevels = Object.keys(winGroups).map(Number).sort((a, b) => b - a);
  for (const w of winLevels) {
    const group = winGroups[w];
    if (group.length === 1) {
      sorted.push(group[0]);
    } else {
      sorted.push(...resolveTies(group, simWins, h2hWins, divWins, confWins, teams, divLeaders, nTeams));
    }
  }

  return sorted;
}

// ---------------------------------------------------------------------------
// Main simulation
// ---------------------------------------------------------------------------

/**
 * Run Monte Carlo simulation and return aggregated results.
 *
 * @param {Object[]} teams     - Array of {team_id, abbreviation, conference, division, wins, losses, win_pct}
 * @param {Object[]} games     - Array of scheduled games: {game_id, home_team_id, away_team_id}
 * @param {Object}  manualOutcomes - Map of game_id -> homeWins (true=home wins, false=away wins)
 * @param {number}  nSims      - Number of Monte Carlo simulations
 * @returns {Object} { East: {...}, West: {...} } matching backend format
 */
function runSimulation(teams, games, manualOutcomes = {}, nSims = 10000) {
  const nTeams = teams.length;
  const nGames = games.length;

  // Build team index
  const teamIdx = {};
  for (let i = 0; i < nTeams; i++) teamIdx[teams[i].team_id] = i;

  // Conference groupings
  const conferences = { East: [], West: [] };
  for (let i = 0; i < nTeams; i++) {
    const conf = teams[i].conference; // "East" or "West"
    if (conferences[conf]) conferences[conf].push(i);
  }

  // Pre-compute per-game data
  const homeIdx = new Int32Array(nGames);
  const awayIdx = new Int32Array(nGames);
  const pHome = new Float64Array(nGames);
  const forced = new Int8Array(nGames).fill(-1); // -1=random, 1=home wins, 0=away wins

  for (let i = 0; i < nGames; i++) {
    const g = games[i];
    const hi = teamIdx[g.home_team_id] ?? -1;
    const ai = teamIdx[g.away_team_id] ?? -1;
    homeIdx[i] = hi;
    awayIdx[i] = ai;

    if (hi === -1 || ai === -1) {
      pHome[i] = 0.5;
    } else {
      const hw = teams[hi].win_pct;
      const aw = teams[ai].win_pct;
      const total = hw + aw;
      pHome[i] = total > 0 ? hw / total : 0.5;
    }

    if (Object.prototype.hasOwnProperty.call(manualOutcomes, g.game_id)) {
      forced[i] = manualOutcomes[g.game_id] ? 1 : 0;
    }
  }

  // Base wins from current standings
  const baseWins = new Int32Array(nTeams);
  for (let i = 0; i < nTeams; i++) baseWins[i] = teams[i].wins;

  // Result accumulators
  const playoffCount = new Int32Array(nTeams);
  const playinCount = new Int32Array(nTeams);
  const elimCount = new Int32Array(nTeams);
  const expectedSeedSum = new Float64Array(nTeams);

  // seedCounts[i] = Int32Array of length nConf for team i's conference
  const confSizes = {};
  for (const [conf, indices] of Object.entries(conferences)) confSizes[conf] = indices.length;
  const seedCounts = teams.map(t => new Int32Array(confSizes[t.conference] || 15));

  // matchupCounts[abbr][oppAbbr] = count
  // seedMatchupCounts[abbr][seed][oppAbbr] = count
  const matchupCounts = {};
  const seedMatchupCounts = {};
  for (const t of teams) {
    matchupCounts[t.abbreviation] = {};
    seedMatchupCounts[t.abbreviation] = {};
  }

  // Per-sim working arrays (reused each iteration)
  const simWins = new Float64Array(nTeams);
  const h2hWins = new Int32Array(nTeams * nTeams);
  const divWins = new Int32Array(nTeams);
  const confWins = new Int32Array(nTeams);

  // Precompute same-division and same-conference flags for each game pair
  const sameDivision = new Uint8Array(nGames);
  const sameConference = new Uint8Array(nGames);
  for (let i = 0; i < nGames; i++) {
    const hi = homeIdx[i], ai = awayIdx[i];
    if (hi === -1 || ai === -1) continue;
    sameDivision[i] = teams[hi].division === teams[ai].division ? 1 : 0;
    sameConference[i] = teams[hi].conference === teams[ai].conference ? 1 : 0;
  }

  const MATCHUP_PAIRS = [[1, 8], [2, 7], [3, 6], [4, 5]];
  const confNames = Object.keys(conferences);

  // ---------------------------------------------------------------------------
  // Main simulation loop
  // ---------------------------------------------------------------------------
  for (let s = 0; s < nSims; s++) {
    // Reset working arrays
    for (let i = 0; i < nTeams; i++) simWins[i] = baseWins[i];
    h2hWins.fill(0);
    divWins.fill(0);
    confWins.fill(0);

    // Simulate games
    for (let i = 0; i < nGames; i++) {
      const hi = homeIdx[i], ai = awayIdx[i];
      if (hi === -1 || ai === -1) continue;

      let homeWon;
      const f = forced[i];
      if (f === 1) homeWon = true;
      else if (f === 0) homeWon = false;
      else homeWon = Math.random() < pHome[i];

      if (homeWon) {
        simWins[hi]++;
        h2hWins[hi * nTeams + ai]++;
        if (sameDivision[i]) divWins[hi]++;
        if (sameConference[i]) confWins[hi]++;
      } else {
        simWins[ai]++;
        h2hWins[ai * nTeams + hi]++;
        if (sameDivision[i]) divWins[ai]++;
        if (sameConference[i]) confWins[ai]++;
      }
    }

    // Compute seeds per conference
    const seedToTeam = {}; // "East:1" -> teamIdx

    for (const confName of confNames) {
      const confIndices = conferences[confName];
      const ranked = computeSeedsSingleSim(confIndices, simWins, h2hWins, divWins, confWins, teams, nTeams);

      for (let rank = 0; rank < ranked.length; rank++) {
        const teamI = ranked[rank];
        const seed = rank + 1;

        seedCounts[teamI][rank]++;
        expectedSeedSum[teamI] += seed;

        if (seed <= 6) playoffCount[teamI]++;
        else if (seed <= 10) playinCount[teamI]++;
        else elimCount[teamI]++;

        seedToTeam[`${confName}:${seed}`] = teamI;
      }
    }

    // First-round matchups: 1v8, 2v7, 3v6, 4v5
    for (const confName of confNames) {
      for (const [s1, s2] of MATCHUP_PAIRS) {
        const t1 = seedToTeam[`${confName}:${s1}`];
        const t2 = seedToTeam[`${confName}:${s2}`];
        if (t1 === undefined || t2 === undefined) continue;

        const a1 = teams[t1].abbreviation;
        const a2 = teams[t2].abbreviation;

        matchupCounts[a1][a2] = (matchupCounts[a1][a2] || 0) + 1;
        matchupCounts[a2][a1] = (matchupCounts[a2][a1] || 0) + 1;

        const smc1 = seedMatchupCounts[a1];
        if (!smc1[s1]) smc1[s1] = {};
        smc1[s1][a2] = (smc1[s1][a2] || 0) + 1;

        const smc2 = seedMatchupCounts[a2];
        if (!smc2[s2]) smc2[s2] = {};
        smc2[s2][a1] = (smc2[s2][a1] || 0) + 1;
      }
    }
  }

  // ---------------------------------------------------------------------------
  // Aggregate results
  // ---------------------------------------------------------------------------
  const results = {};

  for (const [confName, confIndices] of Object.entries(conferences)) {
    const playoffProbs = {}, playinProbs = {}, eliminatedProbs = {};
    const seedDistribution = {};
    const firstRoundMatchups = {};
    const seedMatchupDistribution = {};
    const expectedSeeds = {};

    for (const i of confIndices) {
      const abbr = teams[i].abbreviation;
      playoffProbs[abbr] = playoffCount[i] / nSims;
      playinProbs[abbr] = playinCount[i] / nSims;
      eliminatedProbs[abbr] = elimCount[i] / nSims;
      expectedSeeds[abbr] = expectedSeedSum[i] / nSims;
      seedDistribution[abbr] = Array.from(seedCounts[i]).map(c => c / nSims);

      const oppMap = matchupCounts[abbr];
      firstRoundMatchups[abbr] = {};
      for (const [opp, count] of Object.entries(oppMap)) {
        firstRoundMatchups[abbr][opp] = count / nSims;
      }

      const smc = seedMatchupCounts[abbr];
      seedMatchupDistribution[abbr] = {};
      for (const [seed, oppCounts] of Object.entries(smc)) {
        seedMatchupDistribution[abbr][String(seed)] = {};
        for (const [opp, count] of Object.entries(oppCounts)) {
          seedMatchupDistribution[abbr][String(seed)][opp] = count / nSims;
        }
      }
    }

    results[confName] = {
      playoff_probs: playoffProbs,
      playin_probs: playinProbs,
      eliminated_probs: eliminatedProbs,
      seed_distribution: seedDistribution,
      first_round_matchups: firstRoundMatchups,
      seed_matchup_distribution: seedMatchupDistribution,
      expected_seeds: expectedSeeds,
    };
  }

  return results;
}
