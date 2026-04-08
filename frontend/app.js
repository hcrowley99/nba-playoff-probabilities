'use strict';

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

const API = window.location.origin;

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

const State = {
  activeTab: 'seedings',
  simulation: null,
  baselineSimulation: null,   // sim snapshot before any scenario was applied
  prevSimulation: null,        // sim snapshot just before last update (for animation)
  manualOutcomes: {},          // gameId -> homeWins (true/false); per-user, never written to DB
  gameData: null,              // {teams, games} for client-side simulation
  schedule: null,
  standings: {},               // abbr -> {wins, losses}
  impactPollTimer: null,
  refreshPollTimer: null,
  savingOutcome: false,        // guard against double-submission

  // Fan mode
  fanStep: 'team',             // 'team' | 'outcomes' | 'impact'
  fanTeam: null,               // selected team abbreviation
  fanConf: null,               // 'east' | 'west'
  fanGranularity: 'opponent',  // 'playoff_spot' | 'opponent' | 'seed' | 'seed_matchup'
  fanOutcomes: [],             // [{type, opponent?, seed?, label, prob}]
  fanOutcomeMap: {},           // key -> outcome object (set by renderFanOutcomePicker)
  fanImpactResult: null,
  fanLoading: false,
};

// ---------------------------------------------------------------------------
// API helpers
// ---------------------------------------------------------------------------

const api = {
  async get(path) {
    const res = await fetch(`${API}${path}`);
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    return res.json();
  },

  async post(path, body = {}) {
    const res = await fetch(`${API}${path}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `${res.status} ${res.statusText}`);
    }
    return res.json();
  },

  async del(path) {
    const res = await fetch(`${API}${path}`, { method: 'DELETE' });
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    return res.json();
  },
};

// ---------------------------------------------------------------------------
// Toast
// ---------------------------------------------------------------------------

function toast(msg, type = 'info', duration = 3500) {
  const el = document.createElement('div');
  el.className = `toast ${type}`;
  el.textContent = msg;
  document.getElementById('toast-container').appendChild(el);
  setTimeout(() => el.remove(), duration);
}

// ---------------------------------------------------------------------------
// Tabs
// ---------------------------------------------------------------------------

function initTabs() {
  document.querySelectorAll('.tab').forEach(btn => {
    btn.addEventListener('click', () => switchTab(btn.dataset.tab));
  });
}

function switchTab(name) {
  State.activeTab = name;

  document.querySelectorAll('.tab').forEach(btn => {
    const active = btn.dataset.tab === name;
    btn.classList.toggle('active', active);
    btn.setAttribute('aria-selected', active);
  });

  document.querySelectorAll('.tab-panel').forEach(panel => {
    panel.classList.toggle('active', panel.id === `tab-${name}`);
  });

  // Fan mode hides the schedule strip and scenario banner
  document.body.classList.toggle('fan-mode-active', name === 'rootfor');

  if (name === 'seedings' && State.simulation) renderSeedings(State.simulation);
  if (name === 'matchups' && State.simulation) renderMatchups(State.simulation);
  // Note: rootfor tab handled by renderFanMode() above
  if (name === 'rootfor') renderFanMode();
}

// ---------------------------------------------------------------------------
// Refresh
// ---------------------------------------------------------------------------

function initRefreshButton() {
  document.getElementById('btn-refresh').addEventListener('click', async () => {
    const btn = document.getElementById('btn-refresh');
    btn.disabled = true;
    btn.textContent = 'Refreshing…';

    try {
      const res = await api.post('/api/refresh');
      if (res.status === 'rate_limited') {
        toast(res.message, 'info');
        btn.disabled = false;
        btn.textContent = 'Refresh Data';
        return;
      }
      if (res.status === 'already_running') {
        toast('Refresh already running…', 'info');
        btn.disabled = false;
        btn.textContent = 'Refresh Data';
        return;
      }
      showRefreshBanner('Fetching latest NBA data…');
      pollRefreshStatus();
    } catch (e) {
      toast(`Refresh failed: ${e.message}`, 'error');
      btn.disabled = false;
      btn.textContent = 'Refresh Data';
    }
  });
}

function showRefreshBanner(msg) {
  document.getElementById('refresh-banner-msg').textContent = msg;
  document.getElementById('refresh-banner').classList.add('visible');
}

function hideRefreshBanner() {
  document.getElementById('refresh-banner').classList.remove('visible');
}

function pollRefreshStatus() {
  if (State.refreshPollTimer) clearInterval(State.refreshPollTimer);

  State.refreshPollTimer = setInterval(async () => {
    try {
      const status = await api.get('/api/refresh/status');

      if (status.status === 'running' || status.status === 'queued') {
        showRefreshBanner(status.message || 'Fetching…');
        return;
      }

      clearInterval(State.refreshPollTimer);
      hideRefreshBanner();

      const btn = document.getElementById('btn-refresh');
      btn.disabled = false;
      btn.textContent = 'Refresh Data';

      if (status.status === 'complete' || status.status === 'partial') {
        toast('Data refreshed', 'success');
        State.simulation = null;
        State.schedule = null;
        State.gameData = null;
        State.baselineSimulation = null;
        State.prevSimulation = null;
        State.manualOutcomes = {};
        updateScenarioBanner();
        await loadGameData();
        computeSimulation();
        await loadSchedule();
        updateLastUpdated(status.last_run);
      } else if (status.status === 'error') {
        toast(`Refresh error: ${status.message}`, 'error');
      }
    } catch (e) {
      clearInterval(State.refreshPollTimer);
      hideRefreshBanner();
    }
  }, 2000);
}

// ---------------------------------------------------------------------------
// Game data + standings loading
// ---------------------------------------------------------------------------

async function loadGameData() {
  try {
    const data = await api.get('/api/game-data');
    State.gameData = data;
    // Populate standings for UI display from game-data teams
    State.standings = {};
    for (const t of data.teams || []) {
      State.standings[t.abbreviation] = { wins: t.wins, losses: t.losses };
    }
    if (data.updated_at) updateLastUpdated(data.updated_at);
    return data;
  } catch (e) {
    console.error('Failed to load game data:', e);
  }
}

// ---------------------------------------------------------------------------
// Client-side simulation
// ---------------------------------------------------------------------------

function computeSimulation() {
  if (!State.gameData) return;
  if (State.simulation) return;

  const simResults = runSimulation(
    State.gameData.teams,
    State.gameData.games,
    State.manualOutcomes,
    10000,
  );

  const data = {
    east: simResults['East'],
    west: simResults['West'],
    computed_at: new Date().toISOString(),
  };

  State.simulation = data;

  renderSeedings(data);
  if (State.activeTab === 'matchups') renderMatchups(data);
  if (State.activeTab === 'rootfor') renderFanMode();

  // prevSimulation was captured before this call; clear after renders have used it
  State.prevSimulation = null;
}

// ---------------------------------------------------------------------------
// Scenario banner
// ---------------------------------------------------------------------------

function initScenarioBanner() {
  document.getElementById('btn-reset-scenario').addEventListener('click', resetScenario);
}

function updateScenarioBanner() {
  const banner = document.getElementById('scenario-banner');
  const msg = document.getElementById('scenario-banner-msg');

  const count = Object.keys(State.manualOutcomes).length;
  if (count === 0) {
    banner.classList.remove('visible');
    return;
  }

  const label = count === 1 ? '1 outcome set' : `${count} outcomes set`;
  const deltaNote = State.baselineSimulation
    ? ' · probabilities show change vs. baseline'
    : '';
  msg.textContent = `Scenario: ${label}${deltaNote}`;
  banner.classList.add('visible');
}

function resetScenario() {
  State.prevSimulation = State.simulation;
  State.manualOutcomes = {};
  State.baselineSimulation = null;
  State.simulation = null;
  updateScenarioBanner();
  if (State.schedule) renderScheduleStrip(State.schedule);
  computeSimulation();
  toast('Scenario reset.', 'success');
}

// ---------------------------------------------------------------------------
// Animation helpers
// ---------------------------------------------------------------------------

function animateCount(el, fromVal, toVal, formatter, duration = 520) {
  if (Math.abs(toVal - fromVal) < 0.0001) return;
  if (el._animFrame) cancelAnimationFrame(el._animFrame);
  const start = performance.now();
  const diff = toVal - fromVal;

  function step(now) {
    const t = Math.min((now - start) / duration, 1);
    const ease = 1 - Math.pow(1 - t, 3); // ease-out cubic
    el.textContent = formatter(fromVal + diff * ease);
    if (t < 1) {
      el._animFrame = requestAnimationFrame(step);
    } else {
      el.textContent = formatter(toVal);
    }
  }
  el._animFrame = requestAnimationFrame(step);
}

function animateSeedValues(prevSim, newSim) {
  if (!prevSim || !newSim) return;
  const container = document.getElementById('seedings-content');
  if (!container) return;

  for (const conf of ['east', 'west']) {
    const prevConf = prevSim[conf];
    const newConf = newSim[conf];
    if (!prevConf || !newConf) continue;

    container.querySelectorAll('.seed-po-pct[data-team]').forEach(el => {
      const team = el.dataset.team;
      const newPO = (newConf.playoff_probs?.[team] ?? 0) + (newConf.playin_probs?.[team] ?? 0);
      const oldPO = (prevConf.playoff_probs?.[team] ?? 0) + (prevConf.playin_probs?.[team] ?? 0);
      animateCount(el, oldPO, newPO, fmtPct);
    });

    container.querySelectorAll('.seed-val[data-team][data-seed]').forEach(el => {
      const team = el.dataset.team;
      const seed = parseInt(el.dataset.seed);
      const newProb = newConf.seed_distribution?.[team]?.[seed - 1] ?? 0;
      const oldProb = prevConf.seed_distribution?.[team]?.[seed - 1] ?? 0;
      animateCount(el, oldProb, newProb, v => v >= 0.005 ? fmtPct1(v) : '');
    });

    container.querySelectorAll('[data-from-val][data-to-val]').forEach(el => {
      const fromVal = parseFloat(el.dataset.fromVal);
      const toVal = parseFloat(el.dataset.toVal);
      if (Math.abs(toVal - fromVal) > 0.0001) {
        animateCount(el, fromVal, toVal, v => {
          const dp = v * 100;
          const sign = dp >= 0 ? '+' : '';
          return `${sign}${dp.toFixed(1)}%`;
        });
      }
    });
  }
}

function animateMatchupValues(prevSim, newSim) {
  if (!prevSim || !newSim) return;
  const container = document.getElementById('matchups-content');
  if (!container) return;

  for (const conf of ['east', 'west']) {
    const prevConf = prevSim[conf];
    const newConf = newSim[conf];
    if (!prevConf || !newConf) continue;

    container.querySelectorAll('.tm-top-pct[data-team][data-opp]').forEach(el => {
      const team = el.dataset.team;
      const opp = el.dataset.opp;
      if (!opp) return;
      const newProb = newConf.first_round_matchups?.[team]?.[opp] ?? 0;
      const oldProb = prevConf.first_round_matchups?.[team]?.[opp] ?? 0;
      animateCount(el, oldProb, newProb, fmtPct);
    });

    container.querySelectorAll('.tm-opp-pct-val[data-team][data-opp]').forEach(el => {
      const team = el.dataset.team;
      const opp = el.dataset.opp;
      if (!opp) return;
      const newProb = newConf.first_round_matchups?.[team]?.[opp] ?? 0;
      const oldProb = prevConf.first_round_matchups?.[team]?.[opp] ?? 0;
      animateCount(el, oldProb, newProb, fmtPct);
    });

    container.querySelectorAll('[data-from-val][data-to-val]').forEach(el => {
      const fromVal = parseFloat(el.dataset.fromVal);
      const toVal = parseFloat(el.dataset.toVal);
      if (Math.abs(toVal - fromVal) > 0.0001) {
        animateCount(el, fromVal, toVal, v => {
          const dp = v * 100;
          const sign = dp >= 0 ? '+' : '';
          return `${sign}${dp.toFixed(1)}%`;
        });
      }
    });
  }
}

// ---------------------------------------------------------------------------
// Seedings rendering
// ---------------------------------------------------------------------------

function renderSeedings(data) {
  const container = document.getElementById('seedings-content');
  container.innerHTML = '';

  const prevSim = State.prevSimulation;

  for (const conf of ['east', 'west']) {
    const confData = data[conf];
    if (!confData) continue;

    const confBaseline = State.baselineSimulation?.[conf];
    const prevConf = prevSim?.[conf];

    const seedDist = confData.seed_distribution;
    const expectedSeeds = confData.expected_seeds || {};

    const teams = Object.keys(seedDist)
      .map(abbr => ({ abbr, dist: seedDist[abbr], expected: expectedSeeds[abbr] ?? 8 }))
      .sort((a, b) => a.expected - b.expected);

    const nSeeds = teams[0]?.dist.length || 15;
    const seedNums = Array.from({ length: Math.min(nSeeds, 15) }, (_, i) => i + 1);
    const title = conf === 'east' ? 'Eastern Conference' : 'Western Conference';

    const section = document.createElement('div');
    section.style.marginBottom = '32px';

    const thCells = seedNums.map(s => {
      const boundary = s === 6 ? ' seed-boundary' : '';
      return `<th class="${boundary}">${s}</th>`;
    }).join('');

    const rows = teams.map(t => {
      const record = State.standings[t.abbr];
      const recordStr = record ? `${record.wins}–${record.losses}` : '';

      const currentPlayoff =
        (confData.playoff_probs?.[t.abbr] ?? 0) + (confData.playin_probs?.[t.abbr] ?? 0);

      const baselinePlayoff = confBaseline
        ? ((confBaseline.playoff_probs?.[t.abbr] ?? 0) + (confBaseline.playin_probs?.[t.abbr] ?? 0))
        : null;
      const playoffDelta = baselinePlayoff !== null ? currentPlayoff - baselinePlayoff : null;

      const prevPlayoff = prevConf
        ? ((prevConf.playoff_probs?.[t.abbr] ?? 0) + (prevConf.playin_probs?.[t.abbr] ?? 0))
        : null;
      const prevDelta = (prevPlayoff !== null && baselinePlayoff !== null)
        ? prevPlayoff - baselinePlayoff
        : null;

      let deltaHtml = '';
      if (playoffDelta !== null && Math.abs(playoffDelta) >= 0.00005) {
        const displayPct = parseFloat((playoffDelta * 100).toFixed(1));
        const fromVal = prevDelta ?? 0;
        if (displayPct === 0) {
          deltaHtml = `<span class="scenario-delta-zero" data-from-val="${fromVal}" data-to-val="${playoffDelta}">+0.0%</span>`;
        } else {
          const sign = displayPct > 0 ? '+' : '';
          const cls = displayPct > 0 ? 'scenario-delta-pos' : 'scenario-delta-neg';
          deltaHtml = `<span class="${cls}" data-from-val="${fromVal}" data-to-val="${playoffDelta}">${sign}${displayPct.toFixed(1)}%</span>`;
        }
      }

      const baselineDist = confBaseline?.seed_distribution?.[t.abbr] ?? null;
      const prevDist = prevConf?.seed_distribution?.[t.abbr] ?? null;

      const tds = seedNums.map(s => {
        const prob = t.dist[s - 1] ?? 0;
        const baselineProb = baselineDist ? (baselineDist[s - 1] ?? 0) : null;
        const cellDelta = baselineProb !== null ? prob - baselineProb : null;

        const alpha = Math.min(prob * 2.5, 0.85);
        const bg = prob > 0.01 ? `rgba(245, 78, 0, ${alpha.toFixed(3)})` : 'transparent';
        const textColor = alpha > 0.5 ? 'white' : 'var(--text-primary)';
        const boundary = s === 6 ? ' seed-boundary' : '';
        const display = prob >= 0.005 ? fmtPct1(prob) : '';
        const isDark = alpha > 0.5;

        let cellDeltaHtml = '';
        const showDelta = cellDelta !== null
          && Math.abs(cellDelta) >= 0.00005
          && (prob >= 0.005 || (baselineProb ?? 0) >= 0.005);

        if (showDelta) {
          const dp = parseFloat((cellDelta * 100).toFixed(1));
          const prevCellProb = prevDist ? (prevDist[s - 1] ?? 0) : null;
          const prevCellDelta = (prevCellProb !== null && baselineProb !== null)
            ? prevCellProb - baselineProb
            : null;
          const cellFromVal = prevCellDelta ?? 0;

          if (dp === 0) {
            const c = isDark ? 'rgba(255,255,255,0.45)' : 'var(--text-tertiary)';
            cellDeltaHtml = `<span style="font-size:9px;font-weight:600;line-height:1.2;color:${c}" data-from-val="${cellFromVal}" data-to-val="${cellDelta}">+0.0%</span>`;
          } else {
            const sign = dp > 0 ? '+' : '';
            const c = dp > 0
              ? (isDark ? 'rgba(140,240,190,0.95)' : 'var(--success)')
              : (isDark ? 'rgba(255,175,175,0.95)' : 'var(--error)');
            cellDeltaHtml = `<span style="font-size:9px;font-weight:600;line-height:1.2;color:${c}" data-from-val="${cellFromVal}" data-to-val="${cellDelta}">${sign}${dp.toFixed(1)}%</span>`;
          }
        }

        return `<td class="${boundary}">
          <span class="seed-cell${cellDeltaHtml ? ' seed-cell-stacked' : ''}" style="background:${bg};color:${textColor}">
            <span class="seed-val" data-team="${esc(t.abbr)}" data-seed="${s}">${display}</span>
            ${cellDeltaHtml}
          </span>
        </td>`;
      }).join('');

      return `<tr>
        <td>
          <strong>${esc(t.abbr)}</strong>
          ${recordStr ? `<span class="seed-record">${esc(recordStr)}</span>` : ''}
        </td>
        <td class="seed-po-col">
          <span class="seed-po-pct" data-team="${esc(t.abbr)}">${fmtPct(currentPlayoff)}</span>
          ${deltaHtml}
        </td>
        ${tds}
      </tr>`;
    }).join('');

    section.innerHTML = `
      <div style="font-family:var(--font-gothic);font-size:15px;font-weight:600;letter-spacing:-0.1px;margin-bottom:12px;color:var(--text-primary)">${title}</div>
      <div class="card seed-table-wrap">
        <table class="seed-table">
          <thead>
            <tr>
              <th>Team</th>
              <th class="seed-po-col">PO%</th>
              ${thCells}
            </tr>
          </thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
    `;
    container.appendChild(section);
  }

  if (prevSim) {
    requestAnimationFrame(() => animateSeedValues(prevSim, data));
  }
}

// ---------------------------------------------------------------------------
// Matchups rendering — team-centric view
// ---------------------------------------------------------------------------

function renderMatchups(data) {
  const container = document.getElementById('matchups-content');
  container.innerHTML = '';

  const prevSim = State.prevSimulation;

  for (const conf of ['east', 'west']) {
    const confData = data[conf];
    if (!confData) continue;

    const confBaseline = State.baselineSimulation?.[conf];
    const prevConf = prevSim?.[conf];

    const matchups = confData.first_round_matchups;
    const expectedSeeds = confData.expected_seeds || {};

    const teams = Object.keys(expectedSeeds)
      .sort((a, b) => (expectedSeeds[a] ?? 15) - (expectedSeeds[b] ?? 15));

    const section = document.createElement('div');
    section.className = 'matchup-section';
    section.innerHTML = `<h2 class="matchup-title">${conf === 'east' ? 'Eastern' : 'Western'} Conference</h2>`;

    const list = document.createElement('div');
    list.className = 'team-matchup-list';

    for (const abbr of teams) {
      const oppMap = matchups[abbr] || {};
      const totalMatchupProb = Object.values(oppMap).reduce((s, p) => s + p, 0);
      const missProb = Math.max(0, 1 - totalMatchupProb);

      const entries = Object.entries(oppMap)
        .map(([opp, prob]) => ({ opp, prob }))
        .sort((a, b) => b.prob - a.prob);

      if (missProb > 0.005) entries.push({ opp: null, prob: missProb });
      if (entries.length === 0) continue;

      const maxProb = entries[0].prob;
      const seed = Math.round(expectedSeeds[abbr] ?? 0);

      const record = State.standings[abbr];
      const recordStr = record ? `${record.wins}–${record.losses}` : '';

      const card = document.createElement('div');
      card.className = 'team-matchup-card';
      card.dataset.team = abbr;

      const baselineMatchups = confBaseline?.first_round_matchups?.[abbr] || {};
      const prevMatchups = prevConf?.first_round_matchups?.[abbr] || {};

      const topEntry = entries[0];
      const topLabel = topEntry.opp ? `vs ${esc(topEntry.opp)}` : 'Miss Playoffs';

      const topDelta = confBaseline && topEntry.opp
        ? topEntry.prob - (baselineMatchups[topEntry.opp] ?? 0)
        : null;
      const topPrevDelta = prevConf && topEntry.opp && confBaseline
        ? (prevMatchups[topEntry.opp] ?? 0) - (baselineMatchups[topEntry.opp] ?? 0)
        : null;

      let topDeltaHtml = '';
      if (topDelta !== null && Math.abs(topDelta) >= 0.001) {
        const sign = topDelta > 0 ? '+' : '';
        const cls = topDelta > 0 ? 'scenario-delta-pos' : 'scenario-delta-neg';
        const fromVal = topPrevDelta ?? 0;
        topDeltaHtml = `<span class="${cls}" style="margin-left:4px" data-from-val="${fromVal}" data-to-val="${topDelta}">${sign}${(topDelta * 100).toFixed(1)}%</span>`;
      }

      const rows = entries.map(e => {
        const label = e.opp
          ? `vs <strong>${esc(e.opp)}</strong>`
          : '<span style="color:var(--text-secondary)">Miss Playoffs</span>';
        const pct = fmtPct(e.prob);
        const fillPct = maxProb > 0 ? (e.prob / maxProb * 100).toFixed(1) : 0;
        const fillColor = e.opp ? 'var(--gold)' : 'var(--surface-500)';
        const pctColor = e.opp ? 'var(--text-primary)' : 'var(--text-tertiary)';

        const rowDelta = confBaseline && e.opp
          ? e.prob - (baselineMatchups[e.opp] ?? 0)
          : null;
        const rowPrevDelta = prevConf && e.opp && confBaseline
          ? (prevMatchups[e.opp] ?? 0) - (baselineMatchups[e.opp] ?? 0)
          : null;

        let rowDeltaHtml = '';
        if (rowDelta !== null && Math.abs(rowDelta) >= 0.001) {
          const sign = rowDelta > 0 ? '+' : '';
          const cls = rowDelta > 0 ? 'scenario-delta-pos' : 'scenario-delta-neg';
          const fromVal = rowPrevDelta ?? 0;
          rowDeltaHtml = `<span class="${cls}" data-from-val="${fromVal}" data-to-val="${rowDelta}">${sign}${(rowDelta * 100).toFixed(1)}%</span>`;
        }

        return `
          <div class="tm-opp-row">
            <div class="tm-opp-label">${label}</div>
            <div class="tm-opp-bar-track">
              <div class="tm-opp-bar-fill" style="width:${fillPct}%;background:${fillColor}"></div>
            </div>
            <span class="tm-opp-pct" style="color:${pctColor}"><span class="tm-opp-pct-val" data-team="${esc(abbr)}" data-opp="${esc(e.opp || '')}">${pct}</span>${rowDeltaHtml}</span>
          </div>`;
      }).join('');

      card.innerHTML = `
        <div class="tm-header" data-action="toggle-team-matchup" data-team="${esc(abbr)}">
          <div class="tm-identity">
            <span class="tm-seed">${seed}</span>
            <span class="tm-abbr">${esc(abbr)}</span>
            ${recordStr ? `<span class="tm-record">${esc(recordStr)}</span>` : ''}
          </div>
          <div class="tm-top-opp">${topLabel} <span class="tm-top-pct" data-team="${esc(abbr)}" data-opp="${esc(topEntry.opp || '')}">${fmtPct(topEntry.prob)}</span>${topDeltaHtml}</div>
          <span class="tm-chevron">›</span>
        </div>
        <div class="tm-detail" id="tm-detail-${esc(abbr)}">
          ${rows}
        </div>
      `;
      list.appendChild(card);
    }

    section.appendChild(list);
    container.appendChild(section);
  }

  if (!container.dataset.listenerAttached) {
    container.dataset.listenerAttached = '1';
    container.addEventListener('click', e => {
      const btn = e.target.closest('[data-action="toggle-team-matchup"]');
      if (!btn) return;
      const abbr = btn.dataset.team;
      const detail = document.getElementById(`tm-detail-${abbr}`);
      const card = btn.closest('.team-matchup-card');
      if (!detail || !card) return;
      const open = detail.classList.toggle('open');
      card.classList.toggle('expanded', open);
    });
  }

  if (prevSim) {
    requestAnimationFrame(() => animateMatchupValues(prevSim, data));
  }
}

// ---------------------------------------------------------------------------
// Schedule strip loading and rendering
// ---------------------------------------------------------------------------

async function loadSchedule() {
  const container = document.getElementById('schedule-content');

  if (!State.schedule) {
    try {
      const data = await api.get('/api/schedule?filter=all');
      State.schedule = data;
    } catch (e) {
      container.innerHTML = `<span class="strip-loading" style="color:var(--error)">Failed to load schedule</span>`;
      return;
    }
  }

  renderScheduleStrip(State.schedule);
  startImpactPolling();
}

function renderScheduleStrip(data) {
  const container = document.getElementById('schedule-content');
  const games = data.games || [];
  const byDate = data.by_date || {};

  // Show user-scenario games first, then upcoming scheduled games
  const relevantGames = games.filter(g => State.manualOutcomes[g.game_id] !== undefined || g.status === 'scheduled');

  if (relevantGames.length === 0) {
    container.innerHTML = `<span style="font-size:12px;color:var(--text-tertiary);padding:0 4px">No upcoming games</span>`;
    return;
  }

  const gameMap = {};
  for (const g of games) gameMap[g.game_id] = g;

  const dates = Object.keys(byDate).sort();
  let html = '';

  for (const date of dates) {
    const dayGames = byDate[date]
      .map(id => gameMap[id])
      .filter(g => g && (State.manualOutcomes[g.game_id] !== undefined || g.status === 'scheduled'));
    if (dayGames.length === 0) continue;

    html += `<div class="strip-date-chip"><span class="strip-date-label">${fmtDateStrip(date)}</span></div>`;
    html += dayGames.map(g => gamePill(g)).join('');
  }

  container.innerHTML = html;
  attachStripEvents(container);
}

function gamePill(g) {
  const homeAbbr = esc(g.home_team.abbreviation);
  const awayAbbr = esc(g.away_team.abbreviation);
  const homeRec = `${g.home_team.wins ?? 0}-${g.home_team.losses ?? 0}`;
  const awayRec = `${g.away_team.wins ?? 0}-${g.away_team.losses ?? 0}`;

  const homeWins = State.manualOutcomes[g.game_id]; // true/false/undefined
  const isManual = homeWins !== undefined;

  let bottomHtml = '';
  if (isManual) {
    // User-set scenario outcome (client-side only)
    const winner = homeWins ? homeAbbr : awayAbbr;
    bottomHtml = `
      <div class="pill-bottom">
        <span class="pill-outcome">${winner} wins</span>
        <button class="pill-undo-btn" data-action="undo-pill" data-game="${g.game_id}">Undo</button>
      </div>`;
  } else {
    // Upcoming — show "Set" button and inline picker
    bottomHtml = `
      <div class="pill-bottom">
        <span class="pill-records">${awayRec} · ${homeRec}</span>
        <button class="pill-set-btn" data-action="edit-pill" data-game="${g.game_id}">Set ›</button>
      </div>
      <div class="pill-picker" id="pill-pick-${g.game_id}">
        <button class="pill-team-btn" data-action="win-away" data-game="${g.game_id}">${awayAbbr}</button>
        <button class="pill-team-btn" data-action="win-home" data-game="${g.game_id}">${homeAbbr}</button>
        <button class="pill-cancel-btn" data-action="cancel-pill" data-game="${g.game_id}">✕</button>
      </div>`;
  }

  const scenarioCls = isManual ? ' scenario' : '';
  return `
    <div class="game-pill${scenarioCls}" id="game-${g.game_id}">
      <div class="pill-teams">${awayAbbr} @ ${homeAbbr}</div>
      ${bottomHtml}
    </div>`;
}

// ---------------------------------------------------------------------------
// Strip event delegation
// ---------------------------------------------------------------------------

function attachStripEvents(container) {
  if (container.dataset.listenerAttached) return;
  container.dataset.listenerAttached = '1';
  container.addEventListener('click', e => {
    const btn = e.target.closest('[data-action]');
    if (!btn) return;

    const action = btn.dataset.action;
    const gameId = btn.dataset.game;

    if (action === 'edit-pill') {
      const pill = document.getElementById(`game-${gameId}`);
      if (pill) pill.classList.add('picking');
    }

    if (action === 'cancel-pill') {
      const pill = document.getElementById(`game-${gameId}`);
      if (pill) pill.classList.remove('picking');
    }

    if (action === 'win-home') handleSaveWinner(gameId, true);
    if (action === 'win-away') handleSaveWinner(gameId, false);
    if (action === 'undo-pill') handleUndoScore(gameId);
  });
}

function handleSaveWinner(gameId, homeWins) {
  if (State.savingOutcome) return;
  State.savingOutcome = true;

  if (!State.baselineSimulation && State.simulation) {
    State.baselineSimulation = State.simulation;
  }
  State.prevSimulation = State.simulation;

  State.manualOutcomes[gameId] = homeWins;
  State.simulation = null;

  renderScheduleStrip(State.schedule);
  updateScenarioBanner();
  computeSimulation();
  toast('Outcome set.', 'success');

  State.savingOutcome = false;
}

function handleUndoScore(gameId) {
  State.prevSimulation = State.simulation;

  delete State.manualOutcomes[gameId];
  if (Object.keys(State.manualOutcomes).length === 0) State.baselineSimulation = null;
  State.simulation = null;

  renderScheduleStrip(State.schedule);
  updateScenarioBanner();
  computeSimulation();
  toast('Outcome removed.', 'success');
}

// ---------------------------------------------------------------------------
// Impact polling (no-op for strip — no impact display)
// ---------------------------------------------------------------------------

function startImpactPolling() {
  // Impact details are not shown in the strip UI
}

// ---------------------------------------------------------------------------
// Utilities
// ---------------------------------------------------------------------------

function updateLastUpdated(isoStr) {
  if (!isoStr) return;
  const el = document.getElementById('last-updated');
  if (!el) return;
  try {
    const d = new Date(isoStr);
    el.textContent = `Updated ${d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' })} ${d.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' })}`;
  } catch (_) {
    el.textContent = isoStr;
  }
}

function fmtPct(v) {
  const p = (v * 100).toFixed(1);
  if (p === '100.0' && v < 1) return '99.9%';
  if (p === '0.0' && v > 0) return '<0.1%';
  return `${p}%`;
}

function fmtPct1(v) {
  return `${(v * 100).toFixed(1)}%`;
}

function fmtDateStrip(iso) {
  try {
    const [year, month, day] = iso.split('-').map(Number);
    return new Date(year, month - 1, day).toLocaleDateString('en-US', {
      weekday: 'short', month: 'numeric', day: 'numeric',
    });
  } catch (_) {
    return iso;
  }
}

function esc(str) {
  if (!str) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function loadingHtml(msg = 'Loading…') {
  return `<div class="loading-wrap"><div class="spinner"></div><span>${esc(msg)}</span></div>`;
}

function emptyState(title, msg = '') {
  return `
    <div class="empty-state">
      <div class="empty-state-title">${esc(title)}</div>
      ${msg ? `<p>${esc(msg)}</p>` : ''}
    </div>
  `;
}

// ---------------------------------------------------------------------------
// Fan Mode — Root For
// ---------------------------------------------------------------------------

function fanOutcomeKey(o) {
  if (o.type === 'matchup') return `matchup:${o.opponent}`;
  if (o.type === 'seed_matchup') return `seed_matchup:${o.seed}:${o.opponent}`;
  if (o.type === 'seed') return `seed:${o.seed}`;
  return o.type; // 'playoffs' | 'playin'
}

function ordinal(n) {
  const s = ['th', 'st', 'nd', 'rd'];
  const v = n % 100;
  return n + (s[(v - 20) % 10] || s[v] || s[0]);
}

// Single persistent event handler — avoids the double-listener bug that occurs
// when container.innerHTML is replaced but the element itself keeps old listeners.
function initFanMode() {
  const container = document.getElementById('rootfor-content');
  if (!container) return;

  container.addEventListener('click', e => {
    const el = e.target.closest('[data-action]');
    if (!el) return;
    const action = el.dataset.action;

    if (action === 'fan-pick-team') {
      const abbr = el.dataset.team;
      const conf = (State.simulation?.east?.expected_seeds || {})[abbr] != null ? 'east' : 'west';
      State.fanTeam = abbr;
      State.fanConf = conf;
      State.fanOutcomes = [];
      State.fanStep = 'outcomes';
      renderFanMode();
      return;
    }

    if (action === 'fan-back-team') {
      State.fanStep = 'team';
      State.fanTeam = null;  // clear so no team is pre-highlighted
      State.fanConf = null;
      State.fanOutcomes = [];
      State.fanGranularity = 'opponent';
      renderFanMode();
      return;
    }

    if (action === 'fan-set-granularity') {
      const g = el.dataset.granularity;
      if (g === State.fanGranularity) return;
      State.fanGranularity = g;
      State.fanOutcomes = [];
      renderFanOutcomePicker(document.getElementById('rootfor-content'));
      return;
    }

    if (action === 'fan-toggle-outcome') {
      const key = el.dataset.key;
      const outcome = State.fanOutcomeMap[key];
      if (!outcome) return;
      const idx = State.fanOutcomes.findIndex(x => fanOutcomeKey(x) === key);
      if (idx >= 0) {
        State.fanOutcomes.splice(idx, 1);
        el.classList.remove('selected');
      } else {
        State.fanOutcomes.push(outcome);
        el.classList.add('selected');
      }
      updateFanOutcomePickerUI();
      return;
    }

    if (action === 'fan-analyze') {
      if (State.fanOutcomes.length === 0) return;
      State.fanStep = 'impact';
      State.fanLoading = true;
      State.fanImpactResult = null;
      renderFanMode();
      computeFanImpact();
      return;
    }

    if (action === 'fan-back-outcomes') {
      State.fanStep = 'outcomes';
      State.fanLoading = false;
      State.fanImpactResult = null;
      renderFanMode();
      return;
    }
  });
}

function renderFanMode() {
  const container = document.getElementById('rootfor-content');
  if (!container) return;

  if (!State.simulation) {
    container.innerHTML = `<div class="fan-loading"><div class="spinner"></div><span>Loading simulation…</span></div>`;
    return;
  }

  if (State.fanStep === 'team') renderFanTeamPicker(container);
  else if (State.fanStep === 'outcomes') renderFanOutcomePicker(container);
  else if (State.fanStep === 'impact') renderFanImpactView(container);
}

function renderFanTeamPicker(container) {
  const east = State.simulation.east;
  const west = State.simulation.west;
  if (!east || !west) {
    container.innerHTML = emptyState('No simulation data', 'Click "Refresh Data" to fetch the latest standings.');
    return;
  }

  const eastTeams = Object.keys(east.expected_seeds || {})
    .sort((a, b) => (east.expected_seeds[a] ?? 15) - (east.expected_seeds[b] ?? 15));
  const westTeams = Object.keys(west.expected_seeds || {})
    .sort((a, b) => (west.expected_seeds[a] ?? 15) - (west.expected_seeds[b] ?? 15));

  // No pre-highlight on team picker when navigating back
  function teamButtons(teams) {
    return teams.map(abbr =>
      `<button class="fan-team-btn" data-action="fan-pick-team" data-team="${esc(abbr)}">${esc(abbr)}</button>`
    ).join('');
  }

  container.innerHTML = `
    <div class="fan-step-header">
      <h1 class="fan-step-title">Who are you rooting for?</h1>
      <p class="fan-step-subtitle">Select your team to find out which upcoming games matter most to you.</p>
    </div>
    <div class="fan-conf-section">
      <div class="fan-conf-label">Eastern Conference</div>
      <div class="fan-team-grid">${teamButtons(eastTeams)}</div>
    </div>
    <div class="fan-conf-section">
      <div class="fan-conf-label">Western Conference</div>
      <div class="fan-team-grid">${teamButtons(westTeams)}</div>
    </div>
  `;
}

function renderFanOutcomePicker(container) {
  const team = State.fanTeam;
  const conf = State.fanConf;
  if (!team || !conf) { State.fanStep = 'team'; renderFanMode(); return; }

  const confData = State.simulation[conf];
  if (!confData) { State.fanStep = 'team'; renderFanMode(); return; }

  // ---- Build all outcome objects ----

  const newOutcomeMap = {};

  // Playoff Spot (only relevant when there is meaningful uncertainty about making it)
  const directProb = confData.playoff_probs?.[team] ?? 0;
  const playinProb = confData.playin_probs?.[team] ?? 0;
  const playoffSpotOutcomes = [];
  if (directProb >= 0.01 && directProb < 0.999) {
    playoffSpotOutcomes.push({ type: 'playoffs', label: 'Make Playoffs', prob: directProb });
  }
  if (playinProb >= 0.01) {
    playoffSpotOutcomes.push({ type: 'playin', label: 'Make Play-In', prob: playinProb });
  }

  // Opponent
  const matchups = confData.first_round_matchups?.[team] || {};
  const opponentOutcomes = Object.entries(matchups)
    .filter(([, p]) => p >= 0.02)
    .sort((a, b) => b[1] - a[1])
    .map(([opp, p]) => ({ type: 'matchup', opponent: opp, label: `vs. ${opp}`, prob: p }));

  // Seed
  const seedDist = confData.seed_distribution?.[team] || [];
  const seedOutcomes = seedDist
    .map((prob, i) => ({ type: 'seed', seed: i + 1, label: `${ordinal(i + 1)} seed`, prob }))
    .filter(o => o.prob >= 0.02);

  // Seed & Opponent
  const smd = confData.seed_matchup_distribution?.[team];
  const seedMatchupOutcomes = [];
  if (smd) {
    for (const [seedStr, oppMap] of Object.entries(smd)) {
      const seed = parseInt(seedStr);
      for (const [opp, prob] of Object.entries(oppMap)) {
        if (prob >= 0.02) {
          seedMatchupOutcomes.push({ type: 'seed_matchup', seed, opponent: opp, label: `${ordinal(seed)} vs. ${opp}`, prob });
        }
      }
    }
    seedMatchupOutcomes.sort((a, b) => b.prob - a.prob);
  }

  // Populate outcome map
  for (const o of [...playoffSpotOutcomes, ...opponentOutcomes, ...seedOutcomes, ...seedMatchupOutcomes]) {
    newOutcomeMap[fanOutcomeKey(o)] = o;
  }
  State.fanOutcomeMap = newOutcomeMap;

  // ---- Determine available granularities and validate current selection ----

  const granularityOptions = [];
  if (playoffSpotOutcomes.length > 0) granularityOptions.push({ key: 'playoff_spot', label: 'Playoff Spot' });
  if (opponentOutcomes.length > 0) granularityOptions.push({ key: 'opponent', label: 'Opponent' });
  if (seedOutcomes.length > 0) granularityOptions.push({ key: 'seed', label: 'Seed' });
  if (seedMatchupOutcomes.length > 0) granularityOptions.push({ key: 'seed_matchup', label: 'Seed & Opponent' });

  // If no granularity options at all, team is eliminated — show empty state
  if (granularityOptions.length === 0) {
    container.innerHTML = `
      <div class="fan-crumb">
        <span class="fan-crumb-team">${esc(team)}</span>
        <span class="fan-crumb-sep">·</span>
        <span class="fan-crumb-label">Choose your happy outcomes</span>
        <button class="fan-crumb-change" data-action="fan-back-team">Change team</button>
      </div>
      <div class="fan-step-header" style="margin-bottom:var(--space-4)">
        <h1 class="fan-step-title" style="font-size:18px">Which outcomes would make you happy?</h1>
      </div>
      ${emptyState('Season over', `${esc(team)} has been eliminated from playoff contention.`)}
    `;
    return;
  }

  // Fall back to first available granularity if current one isn't available
  if (!granularityOptions.some(g => g.key === State.fanGranularity)) {
    State.fanGranularity = granularityOptions[0].key;
    State.fanOutcomes = [];
  }

  // ---- Pick which outcomes to show based on granularity ----

  const activeOutcomes = {
    playoff_spot: playoffSpotOutcomes,
    opponent: opponentOutcomes,
    seed: seedOutcomes,
    seed_matchup: seedMatchupOutcomes,
  }[State.fanGranularity] || [];

  // ---- Render ----

  function card(o) {
    const key = esc(fanOutcomeKey(o));
    const sel = State.fanOutcomes.some(x => fanOutcomeKey(x) === fanOutcomeKey(o)) ? ' selected' : '';
    return `<div class="fan-outcome-card${sel}" data-action="fan-toggle-outcome" data-key="${key}">
      <div class="fan-outcome-label">${esc(o.label)}</div>
      <div class="fan-outcome-prob">${fmtPct(o.prob)}</div>
    </div>`;
  }

  const granBar = `
    <div class="fan-gran-bar">
      ${granularityOptions.map(g => `
        <button class="fan-gran-btn${State.fanGranularity === g.key ? ' active' : ''}"
          data-action="fan-set-granularity" data-granularity="${g.key}">${esc(g.label)}</button>
      `).join('')}
    </div>`;

  const selCount = State.fanOutcomes.length;

  container.innerHTML = `
    <div class="fan-crumb">
      <span class="fan-crumb-team">${esc(team)}</span>
      <span class="fan-crumb-sep">·</span>
      <span class="fan-crumb-label">Choose your happy outcomes</span>
      <button class="fan-crumb-change" data-action="fan-back-team">Change team</button>
    </div>
    <div class="fan-step-header" style="margin-bottom:var(--space-4)">
      <h1 class="fan-step-title" style="font-size:18px">Which outcomes would make you happy?</h1>
      <p class="fan-step-subtitle">Select one or more outcomes. Switch between granularity levels — selections clear when you switch.</p>
    </div>
    ${granBar}
    <div class="fan-outcome-grid">
      ${activeOutcomes.map(o => card(o)).join('')}
    </div>
    <div class="fan-analyze-row">
      <button class="fan-analyze-btn" data-action="fan-analyze" ${selCount === 0 ? 'disabled' : ''}>
        Analyze →
      </button>
      <span class="fan-selection-count" id="fan-sel-count">
        ${selCount === 0 ? 'Select at least one outcome' : `${selCount} outcome${selCount > 1 ? 's' : ''} selected`}
      </span>
    </div>
  `;
}

function updateFanOutcomePickerUI() {
  const cnt = State.fanOutcomes.length;
  const btn = document.querySelector('[data-action="fan-analyze"]');
  const countEl = document.getElementById('fan-sel-count');
  if (btn) btn.disabled = cnt === 0;
  if (countEl) countEl.textContent = cnt === 0
    ? 'Select at least one outcome'
    : `${cnt} outcome${cnt > 1 ? 's' : ''} selected`;
}


function renderFanImpactView(container) {
  const team = State.fanTeam;
  const outcomes = State.fanOutcomes;
  const outcomeChips = outcomes.map(o => `<span class="fan-chip">${esc(o.label)}</span>`).join('');
  const changeBtn = `<button class="fan-crumb-change" data-action="fan-back-outcomes">Change outcomes</button>`;

  if (State.fanLoading) {
    container.innerHTML = `
      <div class="fan-crumb">
        <span class="fan-crumb-team">${esc(team)}</span>
        <span class="fan-crumb-sep">·</span>
        <div style="display:inline-flex;flex-wrap:wrap;gap:4px">${outcomeChips}</div>
        ${changeBtn}
      </div>
      <div class="fan-loading">
        <div class="spinner"></div>
        <span>Analyzing upcoming games…</span>
        <span style="font-size:12px;color:var(--text-tertiary)">Running simulations, this may take a few seconds.</span>
      </div>
    `;
    return;
  }

  const result = State.fanImpactResult;
  if (!result) {
    container.innerHTML = `
      <div class="fan-crumb">
        <span class="fan-crumb-team">${esc(team)}</span>
        ${changeBtn}
      </div>
      <div class="fan-empty"><div class="fan-empty-title">Something went wrong</div>Could not compute game impacts. Try again.</div>
    `;
    return;
  }

  const visibleGames = result.games.filter(g => g.max_impact >= 0.001).slice(0, 8);

  function impactBadge(maxImpact) {
    if (maxImpact >= 0.05) return `<span class="fan-impact-badge high">High</span>`;
    if (maxImpact >= 0.02) return `<span class="fan-impact-badge medium">Med</span>`;
    return `<span class="fan-impact-badge low">Low</span>`;
  }

  function gameCard(g) {
    const rootTeam = g.root_for === 'home' ? g.home_team.abbreviation : g.away_team.abbreviation;
    const otherTeam = g.root_for === 'home' ? g.away_team.abbreviation : g.home_team.abbreviation;
    const rd = g.root_delta, od = g.other_delta;
    const winCls = rd > 0.005 ? 'pos' : (rd < -0.005 ? 'neg' : 'neutral');
    return `
      <div class="fan-game-card">
        <div class="fan-game-top">
          <div>
            <div class="fan-game-matchup">${esc(g.away_team.abbreviation)} @ ${esc(g.home_team.abbreviation)}</div>
            <div class="fan-game-date">${fmtDateFan(g.game_date)}</div>
          </div>
          ${impactBadge(g.max_impact)}
        </div>
        <div class="fan-game-body">
          <div class="fan-root-for">
            <span class="fan-root-label">Root for</span>
            <span class="fan-root-pill">${esc(rootTeam)}</span>
          </div>
          <div class="fan-impact-nums">
            <span class="fan-impact-win ${winCls}">${rd >= 0 ? '+' : ''}${(rd * 100).toFixed(1)}% if ${esc(rootTeam)} wins</span>
            <span class="fan-impact-lose">${od >= 0 ? '+' : ''}${(od * 100).toFixed(1)}% if ${esc(otherTeam)} wins</span>
          </div>
        </div>
      </div>`;
  }

  const gamesHtml = visibleGames.length > 0
    ? `<div class="fan-games-list">${visibleGames.map(gameCard).join('')}</div>`
    : `<div class="fan-empty"><div class="fan-empty-title">No impactful games found</div>No upcoming games in the next 14 days significantly affect your selected outcomes.</div>`;

  container.innerHTML = `
    <div class="fan-crumb">
      <span class="fan-crumb-team">${esc(team)}</span>
      <span class="fan-crumb-sep">·</span>
      <div style="display:inline-flex;flex-wrap:wrap;gap:4px">${outcomeChips}</div>
      ${changeBtn}
    </div>
    <div class="fan-baseline-bar">
      <span class="fan-baseline-prob">${fmtPct(result.baseline_prob)}</span>
      <span class="fan-baseline-label">current probability of a good outcome</span>
    </div>
    ${visibleGames.length > 0 ? `<div style="font-family:var(--font-mono);font-size:10px;font-weight:600;letter-spacing:1px;text-transform:uppercase;color:var(--text-tertiary);margin-bottom:var(--space-3)">Games ranked by impact</div>` : ''}
    ${gamesHtml}
  `;
}

async function computeFanImpact() {
  const team = State.fanTeam;
  const outcomes = State.fanOutcomes.map(o => ({
    type: o.type,
    opponent: o.opponent || null,
    seed: o.seed != null ? o.seed : null,
  }));

  // Yield to browser so the loading spinner paints before we block the thread
  await new Promise(resolve => setTimeout(resolve, 10));

  try {
    if (!State.simulation || !State.gameData) throw new Error('No simulation data');

    // Baseline probability from the already-computed simulation (free — no extra sim needed)
    const baselineProb = computeFanOutcomeProb(State.simulation, team, outcomes);

    // Upcoming scheduled games in next 14 days (max 12) — same logic as backend
    const today = new Date().toISOString().split('T')[0];
    const endMs = Date.now() + 14 * 24 * 60 * 60 * 1000;
    const endDate = new Date(endMs).toISOString().split('T')[0];

    const upcomingRows = (State.schedule?.games || [])
      .filter(g => g.status === 'scheduled' && g.game_date >= today && g.game_date <= endDate)
      .slice(0, 12);

    const gameImpacts = [];

    for (const row of upcomingRows) {
      const gameId = row.game_id;

      // Two counterfactual sims: force home win, force away win
      const simH = runSimulation(
        State.gameData.teams, State.gameData.games,
        { ...State.manualOutcomes, [gameId]: true }, 500,
      );
      const simA = runSimulation(
        State.gameData.teams, State.gameData.games,
        { ...State.manualOutcomes, [gameId]: false }, 500,
      );

      const pH = computeFanOutcomeProb(simH, team, outcomes);
      const pA = computeFanOutcomeProb(simA, team, outcomes);

      const dH = pH - baselineProb;
      const dA = pA - baselineProb;
      const maxImpact = Math.max(Math.abs(dH), Math.abs(dA));

      const rootFor = dH >= dA ? 'home' : 'away';
      const rootAbbr = rootFor === 'home' ? row.home_team.abbreviation : row.away_team.abbreviation;
      const rootDelta = rootFor === 'home' ? dH : dA;
      const otherDelta = rootFor === 'home' ? dA : dH;

      const r4 = v => Math.round(v * 10000) / 10000;

      gameImpacts.push({
        game_id: gameId,
        game_date: row.game_date,
        home_team: { abbreviation: row.home_team.abbreviation, full_name: row.home_team.full_name },
        away_team: { abbreviation: row.away_team.abbreviation, full_name: row.away_team.full_name },
        root_for: rootFor,
        root_for_abbr: rootAbbr,
        p_if_home_wins: r4(pH),
        p_if_away_wins: r4(pA),
        delta_home_wins: r4(dH),
        delta_away_wins: r4(dA),
        root_delta: r4(rootDelta),
        other_delta: r4(otherDelta),
        max_impact: r4(maxImpact),
      });
    }

    gameImpacts.sort((a, b) => b.max_impact - a.max_impact);

    State.fanImpactResult = {
      team_abbr: team,
      baseline_prob: Math.round(baselineProb * 10000) / 10000,
      games: gameImpacts,
    };
  } catch (e) {
    toast(`Could not compute fan impact: ${e.message}`, 'error');
    State.fanImpactResult = null;
  } finally {
    State.fanLoading = false;
    if (State.activeTab === 'rootfor' && State.fanStep === 'impact') renderFanMode();
  }
}

function fmtDateFan(iso) {
  try {
    const [year, month, day] = iso.split('-').map(Number);
    return new Date(year, month - 1, day).toLocaleDateString('en-US', {
      weekday: 'short', month: 'short', day: 'numeric',
    });
  } catch (_) { return iso; }
}

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

async function init() {
  initTabs();
  initRefreshButton();
  initScenarioBanner();
  initFanMode();
  await loadGameData();         // fetch teams + games, populate State.standings
  computeSimulation();          // run MC locally (synchronous, fast)
  await loadSchedule();         // fetch schedule strip
}

document.addEventListener('DOMContentLoaded', init);
