const state = { mode: "fan", language: "en", player: "", team: "", view: "tv" };
const demoParams = new URLSearchParams(window.location.search);
const demoMode = demoParams.get("demo") === "1";
const $ = (selector) => document.querySelector(selector);
let replaySource;
let moments = [];
let allEvents = [];
let tracking = [];
let trackingAlerts = [];
let telemetryEntries = new Map();
let lastTelemetrySecond = 0;
let telemetryPinned = [];
let telemetrySeen = new Set();
let telemetryHideTimer = null;
let lastSnapshot = null;
let recapVersion = 0;
let lastTraces = [];
let lastEvent = null;
let replayLimit = 0;
let timedOverlay = null;
let overlayShownAt = 0;
let refreshVersion = 0;
let frames = [];
let lineup = [];
let playerNodes = new Map();
let playerShadows = new Map();
let focusTrail;
let focusTrailLines = [];
let ballNode;
let ballShadow;
let flightLine;
let replaySecond = 0;
let animationFrame = 0;
let previousFrameTime = 0;
let currentScore = {};
let announcedGoals = new Set();
let goalCeremony = null;
let goalCeremonyTimer = null;
let pitchWidth = 0;
let pitchHeight = 0;
let match = { home_team: "Home", away_team: "Away" };
const prefersReducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
const voiceAvailable = Boolean(window.speechSynthesis && window.SpeechSynthesisUtterance);
let voiceEnabled = false;
let lastSpokenCard = "";

function updateVoiceButton() {
  const button = $("#voice-toggle");
  button.disabled = !voiceAvailable;
  button.setAttribute("aria-pressed", String(voiceEnabled));
  button.textContent = !voiceAvailable
    ? (state.language === "zh" ? "语音不可用" : "Voice unavailable")
    : voiceEnabled
      ? (state.language === "zh" ? "语音：开" : "Voice on")
      : (state.language === "zh" ? "语音：关" : "Voice off");
  button.title = state.language === "zh" ? "正常速度播放时播报洞察；加速回放时暂停朗读" : "Read insights at normal replay speed; skip speech during fast replay";
}

function updateViewButton() {
  const button = $("#view-angle");
  $("#pitch").dataset.view = state.view;
  button.setAttribute("aria-pressed", String(state.view === "tv"));
  button.textContent = state.language === "zh"
    ? (state.view === "tv" ? "电视视角" : "战术视角")
    : (state.view === "tv" ? "TV view" : "Tactical view");
}

function stopVoice() {
  if (voiceAvailable) window.speechSynthesis.cancel();
}

function speakCard(card, force = false) {
  if (!voiceEnabled || !voiceAvailable || !card?.headline) return;
  const key = `${state.language}:${card.kind}:${card.display_at_second}`;
  if (!force && (key === lastSpokenCard || replaySource && Number($("#replay-speed").value) > 2)) return;
  lastSpokenCard = key;
  stopVoice();
  const utterance = new window.SpeechSynthesisUtterance(`${card.headline}. ${card.body || ""}`);
  utterance.lang = state.language === "zh" ? "zh-CN" : "en-GB";
  utterance.rate = 1;
  const voice = window.speechSynthesis.getVoices().find(item => item.lang.toLowerCase().startsWith(state.language === "zh" ? "zh" : "en"));
  if (voice) utterance.voice = voice;
  window.speechSynthesis.speak(utterance);
}

function teamCode(name) {
  return String(name || "").replace(/[^A-Za-z]/g, "").slice(0, 3).toUpperCase();
}

function playerSurname(name) {
  return String(name || "").trim().split(/\s+/).at(-1) || "";
}

function numberedPlayer(player) {
  return player ? `#${player.number} ${player.player_name}` : "";
}

function numberedEventPlayer(event) {
  const player = lineup.find(item => item.player_id === event.player_id);
  return player ? numberedPlayer(player) : event.player_name;
}

function endGoalCeremony() {
  if (goalCeremonyTimer) clearTimeout(goalCeremonyTimer);
  goalCeremonyTimer = null;
  goalCeremony = null;
  const banner = $("#goal-banner");
  banner.hidden = true;
  banner.classList.remove("leaving");
  $("#scoreboard").classList.remove("goal-flash");
  if (timedOverlay) {
    const card = $("#overlay-card");
    card.hidden = false;
    card.classList.remove("arrive");
    requestAnimationFrame(() => card.classList.add("arrive"));
  }
}

function showGoalCeremony(event) {
  if (!event || event.outcome !== "goal") return;
  endGoalCeremony();
  goalCeremony = event.event_id;
  const banner = $("#goal-banner");
  banner.dataset.team = teamSide(event.team_id);
  $("#goal-word").textContent = state.language === "zh" ? "进球" : "GOAL";
  $("#goal-player").textContent = `${numberedEventPlayer(event)} · ${formatClock(event.minute * 60 + event.second)}`;
  banner.hidden = false;
  $("#overlay-card").hidden = true;
  $("#scoreboard").classList.add("goal-flash");
  renderPitchAt(replaySecond);
  goalCeremonyTimer = setTimeout(() => {
    if (prefersReducedMotion) { endGoalCeremony(); return; }
    banner.classList.add("leaving");
    goalCeremonyTimer = setTimeout(endGoalCeremony, 250);
  }, prefersReducedMotion ? 1500 : 1600);
}

function showGoalNear(second) {
  const goal = allEvents.findLast(item => item.outcome === "goal" && item.minute * 60 + item.second <= second);
  if (goal && second - (goal.minute * 60 + goal.second) <= 20) showGoalCeremony(goal);
  else endGoalCeremony();
}

function teamSide(teamId) {
  return teamId === match.home_team ? "home" : teamId === match.away_team ? "away" : "";
}

function fixtureLine() {
  return state.language === "zh"
    ? `${match.home_team} 对 ${match.away_team}，合成数据`
    : `${match.home_team} v ${match.away_team}, synthetic data`;
}

function engineLabel(provider, live) {
  if (!live) return tr("Local agents");
  const name = { deepseek: "DeepSeek", foundry: "Microsoft Foundry" }[provider] || provider;
  return `${name} ${tr("Live")}`;
}

function sentenceCase(text) {
  return /[A-Z]/.test(text) && text === text.toUpperCase() ? text.charAt(0) + text.slice(1).toLowerCase() : text;
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
}

function formatClock(second) {
  const safe = Math.max(0, Math.floor(second));
  return `${String(Math.floor(safe / 60)).padStart(2, "0")}:${String(safe % 60).padStart(2, "0")}`;
}

function profileParams() {
  const params = { mode: state.mode, language: state.language };
  if (state.player) params.player = state.player;
  if (state.team) params.team = state.team;
  return params;
}

async function getJson(url) {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`${url} → HTTP ${response.status}`);
  return response.json();
}

async function loadHealth() {
  const health = await getJson("/api/health");
  $("#provider").dataset.live = String(Boolean(health.llm_ready || health.foundry_ready));
  $("#provider").textContent = engineLabel(health.provider, Boolean(health.llm_ready || health.foundry_ready));
}

async function loadMatch() {
  match = await getJson("/api/match");
  // Broadcast scorebug uses three-letter codes; full names live in the stats panel and header.
  $("#home-team").textContent = teamCode(match.home_team);
  $("#away-team").textContent = teamCode(match.away_team);
  $("#home-team").title = match.home_team;
  $("#away-team").title = match.away_team;
  $("#stat-home-name").textContent = match.home_team;
  $("#stat-away-name").textContent = match.away_team;
  $("#fixture").textContent = fixtureLine();
  const select = $("#team-focus");
  [match.home_team, match.away_team].forEach((team) => {
    const option = document.createElement("option");
    option.value = team;
    option.textContent = team;
    select.appendChild(option);
  });
  const buttons = $("#team-buttons");
  [{value:"", label:tr("No favourite"), code:"—"}, ...[match.home_team, match.away_team].map(team => ({value:team, label:team, code:teamCode(team)}))].forEach(item => {
    const button = document.createElement("button");
    button.type = "button";
    button.dataset.team = item.value;
    button.setAttribute("aria-label", item.label);
    button.setAttribute("aria-pressed", String(!item.value));
    button.innerHTML = `<span class="team-emblem ${teamSide(item.value)}">${escapeHtml(item.code)}</span><span>${escapeHtml(item.value ? item.code : item.label)}</span>`;
    button.addEventListener("click", () => {
      select.value = item.value;
      select.dispatchEvent(new Event("change"));
    });
    buttons.appendChild(button);
  });
}

function updateChoiceButtons() {
  document.querySelectorAll("#team-buttons button").forEach(button => button.setAttribute("aria-pressed", String(button.dataset.team === state.team)));
  document.querySelectorAll("#metric-buttons button").forEach(button => button.setAttribute("aria-pressed", String(button.dataset.metric === $("#metric-focus").value)));
  document.querySelectorAll("#speed-buttons button").forEach(button => button.setAttribute("aria-pressed", String(button.dataset.speed === $("#sprint-threshold").value)));
}

function updateFeedVisibility() {
  $(".feed-block").hidden = !$("#event-feed").querySelector(".feed-event");
}

function initializePitch() {
  const pitch = $("#pitch");
  const plane = $("#pitch-plane");
  const bounds = pitch.getBoundingClientRect();
  pitchWidth = bounds.width;
  pitchHeight = bounds.height;
  plane.querySelectorAll(".player-marker, .player-shadow, .ball-marker, .ball-shadow, .flight-overlay, .focus-trail").forEach((node) => node.remove());
  playerNodes = new Map();
  playerShadows = new Map();
  lineup.forEach((player) => {
    const shadow = document.createElement("span");
    shadow.className = "player-shadow";
    shadow.setAttribute("aria-hidden", "true");
    plane.appendChild(shadow);
    playerShadows.set(player.player_id, shadow);
    const marker = document.createElement("span");
    marker.className = `player-marker ${player.team_id === match.home_team ? "home" : "away"}${player.role === "GK" ? " goalkeeper" : ""}`;
    marker.dataset.playerId = player.player_id;
    marker.innerHTML = `<b>${escapeHtml(player.number)}</b><small>${escapeHtml(player.role)}</small><em class="player-name">${escapeHtml(playerSurname(player.player_name))}</em>`;
    marker.title = `${numberedPlayer(player)} · ${player.role}`;
    marker.addEventListener("click", () => {
      state.player = player.player_id;
      $("#player-focus").value = state.player;
      renderPitchAt(replaySecond);
      profileChanged();
    });
    plane.appendChild(marker);
    playerNodes.set(player.player_id, marker);
  });
  ballNode = document.createElement("span");
  ballNode.className = "ball-marker";
  ballNode.setAttribute("aria-label", "Ball");
  plane.appendChild(ballNode);
  ballShadow = document.createElement("span");
  ballShadow.className = "ball-shadow";
  plane.appendChild(ballShadow);
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 105 68");
  svg.setAttribute("preserveAspectRatio", "none");
  svg.classList.add("flight-overlay");
  flightLine = document.createElementNS("http://www.w3.org/2000/svg", "line");
  svg.appendChild(flightLine);
  plane.appendChild(svg);
  focusTrail = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  focusTrail.setAttribute("viewBox", "0 0 105 68");
  focusTrail.setAttribute("preserveAspectRatio", "none");
  focusTrail.classList.add("focus-trail");
  focusTrailLines = Array.from({length: 8}, () => {
    const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
    focusTrail.appendChild(line);
    return line;
  });
  plane.appendChild(focusTrail);
  const status = document.createElement("div");
  status.id = "pitch-action";
  pitch.appendChild(status);
  for (const id of ["metric-hud", "pitch-caption"]) {
    const node = document.createElement("div");
    node.id = id; node.hidden = true; pitch.appendChild(node);
  }
  if (!pitch.resizeObserver) {
    pitch.resizeObserver = new ResizeObserver((entries) => {
      pitchWidth = entries[0].contentRect.width;
      pitchHeight = entries[0].contentRect.height;
      renderPitchAt(replaySecond);
    });
    pitch.resizeObserver.observe(pitch);
  }
}

function positionNode(node, x, y, upright = true) {
  const pixelX = Math.max(0, Math.min(pitchWidth, (x / 105) * pitchWidth));
  const pixelY = Math.max(0, Math.min(pitchHeight, (y / 68) * pitchHeight));
  node.style.transform = `translate3d(${pixelX}px,${pixelY}px,0) translate(-50%,-50%)${upright ? " rotateX(calc(0deg - var(--tilt)))" : ""}`;
}

function trailPosition(second, playerIndex) {
  const at = lastAt(frames, Math.max(0, second), frame => frame.second);
  const a = frames[Math.max(0, at)];
  const b = frames[Math.min(frames.length - 1, Math.max(0, at) + 1)];
  const ratio = Math.max(0, Math.min(1, (second - a.second) / Math.max(.001, b.second - a.second)));
  return [0, 1].map(axis => a.positions[playerIndex][axis] + (b.positions[playerIndex][axis] - a.positions[playerIndex][axis]) * ratio);
}

function renderFocusTrail(second) {
  if (!focusTrail || !state.player || !frames.length) {
    if (focusTrail) focusTrail.hidden = true;
    return;
  }
  const index = lineup.findIndex(player => player.player_id === state.player);
  if (index < 0) { focusTrail.hidden = true; return; }
  focusTrail.hidden = false;
  focusTrail.dataset.team = lineup[index].team_id === match.home_team ? "home" : "away";
  const points = Array.from({length: 9}, (_, i) => trailPosition(Math.max(0, second - 8 + i), index));
  focusTrailLines.forEach((line, i) => {
    line.setAttribute("x1", points[i][0]); line.setAttribute("y1", points[i][1]);
    line.setAttribute("x2", points[i + 1][0]); line.setAttribute("y2", points[i + 1][1]);
    line.style.opacity = String(.13 + i * .065);
  });
}

function renderPitchAt(second) {
  if (!frames.length) return;
  let low = 0;
  let high = frames.length - 1;
  while (low < high) {
    const middle = Math.ceil((low + high) / 2);
    if (frames[middle].second <= second) low = middle;
    else high = middle - 1;
  }
  const left = frames[low];
  const right = frames[Math.min(low + 1, frames.length - 1)];
  const span = Math.max(0.001, right.second - left.second);
  const frameProgress = Math.max(0, Math.min(1, (second - left.second) / span));
  const progress = prefersReducedMotion && frameProgress < 1 ? 0 : frameProgress;
  left.positions.forEach((coordinates, index) => {
    const next = right.positions[index] || coordinates;
    const player = lineup[index];
    const marker = player && playerNodes.get(player.player_id);
    if (!marker) return;
    const x = coordinates[0] + (next[0] - coordinates[0]) * progress;
    const y = coordinates[1] + (next[1] - coordinates[1]) * progress;
    const shadow = playerShadows.get(player.player_id);
    if (shadow) positionNode(shadow, x, y, false);
    positionNode(marker, x, y);
    marker.classList.toggle("focused", Boolean(state.player) && marker.dataset.playerId === state.player);
    marker.classList.toggle("has-ball", left.owner === player.player_id);
  });
  renderFocusTrail(second);
  if (ballNode) {
    const ball = left.ball;
    const nextBall = right.ball || ball;
    positionNode(ballNode, ball.x + (nextBall.x - ball.x) * progress, ball.y + (nextBall.y - ball.y) * progress);
    positionNode(ballShadow, ball.x + (nextBall.x - ball.x) * progress, ball.y + (nextBall.y - ball.y) * progress, false);
    const height = (ball.z || 0) + ((nextBall.z || 0) - (ball.z || 0)) * progress;
    ballNode.style.marginTop = `-${height * 6}px`;
    ballNode.style.scale = String(1 + height * 0.08);
    ballNode.classList.toggle("goal", nextBall.event_type === "goal");
  }
  const labels = state.language === "zh" ? {short_pass:"短传配合",long_pass:"长传转移",through_pass:"直塞前插",lofted_pass:"高球传递",
    carry:"持球推进",shot:"射门",offside:"越位",corner:"角球",throw_in:"界外球",goal_kick:"球门球",kickoff:"中圈开球"}
    : {short_pass:"Short pass",long_pass:"Long pass",through_pass:"Through ball",lofted_pass:"Lofted pass",carry:"Ball carry",shot:"Shot",offside:"Offside",corner:"Corner",throw_in:"Throw-in",goal_kick:"Goal kick",kickoff:"Kickoff"};
  $("#pitch-action").textContent = left.flight ? (labels[left.phase] || left.phase || "") : "";
  flightLine.style.display = left.flight ? "" : "none";
  if (left.flight) {
    flightLine.setAttribute("x1", left.flight.start[0]);
    flightLine.setAttribute("y1", left.flight.start[1]);
    flightLine.setAttribute("x2", left.flight.end[0]);
    flightLine.setAttribute("y2", left.flight.end[1]);
  }
  renderTelemetry(second, low);
  const caption = $("#pitch-caption");
  caption.hidden = !timedOverlay || second < timedOverlay.display_at_second || second >= timedOverlay.display_at_second + timedOverlay.duration_seconds;
  if (!caption.hidden) caption.textContent = `${timedOverlay.headline} · ${timedOverlay.body}`;
  const goals = allEvents.filter(e => e.outcome === "goal" && e.minute * 60 + e.second <= second);
  $("#home-score").textContent = goals.filter(e => e.team_id === match.home_team).length;
  $("#away-score").textContent = goals.filter(e => e.team_id === match.away_team).length;
  if (timedOverlay && !goalCeremony && replaySource && performance.now() - overlayShownAt >= 12000) {
    timedOverlay = null;
    $("#overlay-card").hidden = true;
  }
}

function updateClock(second) {
  replaySecond = Math.max(0, Math.min(90 * 60, second));
  $("#clock").textContent = formatClock(replaySecond);
  $("#minute").value = Math.floor(replaySecond);
  $("#minute-output").textContent = formatClock(replaySecond);
}

function renderTrace(trace) {
  lastTraces = trace;
  $("#agent-trace").innerHTML = trace.map((item, index) => `
    <article class="agent ${item.status === "fallback" ? "fallback" : ""} ${item.status === "rejected" ? "rejected" : ""}">
      <span class="agent-index">${String(index + 1).padStart(2, "0")}</span>
      <div><b>${escapeHtml(tr(item.agent))}</b><p>${escapeHtml(state.language === "zh" ? `${tr(item.agent)}：${tr(item.status)}。${item.status === "rejected" ? "输出未通过证据检查，已请求修订。" : item.status === "fallback" ? "使用经过校验的本地文案。" : "处理结果可通过下方事件引用核对。"}` : item.summary)}</p></div>
      <time>${escapeHtml((item.provider === "local" ? tr("Local") : item.provider.toUpperCase()))} · ${escapeHtml(tr(item.status))} · ${item.duration_ms} ms</time>
    </article>`).join("");
}

function renderEvidenceTable(overlay) {
  const table = $("#evidence-table");
  if (!table) return;
  if (!overlay) { table.innerHTML = ""; return; }
  const rows = overlay.evidence.map((item) => `<tr><th scope="row">${escapeHtml(tr(item.label))}</th><td>${escapeHtml(evidenceValue(item.value))}</td><td>${item.event_ids.length ? escapeHtml(item.event_ids.join(", ")) : "—"}</td></tr>`).join("");
  table.innerHTML = `<table><caption>${escapeHtml(tr("Evidence behind this card"))}</caption><tbody>${rows}</tbody></table>`;
}

function evidenceValue(value) {
  if (state.language !== "zh") return value;
  return tr(value).replace(/ actions\b/g," 次").replace(/ regains\b/g," 次抢回").replace(/ goals\b/g," 球").replace(/ shots\b/g," 次射门").replace(/ touches\b/g," 次触球").replace(/ passes\b/g," 次传球").replace(/ vs /g," 对 ");
}

function renderStats(snapshot) {
  const home = snapshot.metrics[snapshot.home_team];
  const away = snapshot.metrics[snapshot.away_team];
  const rows = [
    ["Possession", `${home.possession_pct.toFixed(0)}%`, `${away.possession_pct.toFixed(0)}%`, home.possession_pct, away.possession_pct],
    ["xG", home.xg.toFixed(2), away.xg.toFixed(2), home.xg, away.xg],
    ["Shots", `${home.shots} (${home.shots_on_target})`, `${away.shots} (${away.shots_on_target})`, home.shots, away.shots],
    ["Pass accuracy", `${home.pass_accuracy.toFixed(0)}%`, `${away.pass_accuracy.toFixed(0)}%`, home.pass_accuracy, away.pass_accuracy],
    ["Progressive passes", home.progressive_passes, away.progressive_passes, home.progressive_passes, away.progressive_passes],
  ];
  $("#stats-rows").innerHTML = rows.map(([label, h, a, hv, av]) => {
    const total = hv + av || 1;
    return `<div class="stat-row"><span>${escapeHtml(h)}</span><div><small>${escapeHtml(tr(label))}</small><div class="stat-bar"><i style="width:${(hv / total) * 100}%"></i><i style="width:${(av / total) * 100}%"></i></div></div><span>${escapeHtml(a)}</span></div>`;
  }).join("");
}

function renderPlayer(snapshot) {
  const stats = state.player && snapshot.players ? snapshot.players[state.player] : null;
  const chosen = lineup.find(player => player.player_id === state.player);
  $("#player-line").hidden = !chosen;
  $(".player-metrics").hidden = !chosen;
  if (chosen) $("#player-line").textContent = `${numberedPlayer(chosen)} · ${chosen.role}`;
  $("#player-distance").textContent = stats ? `${(stats.distance_m / 1000).toFixed(2)} km` : "";
  // Current tracking speed is rendered alongside the pitch frame.
  if (!state.player) $("#player-speed").textContent = "";
  $("#player-events").textContent = stats ? `${stats.touches} ${tr("touches")}` : "";
}

function renderSnapshot(snapshot, overlay, trace) {
  if (!snapshot) return;
  lastSnapshot = snapshot;
  renderMetricHud();
  const home = snapshot.home_team;
  $("#home-score").textContent = snapshot.score_home;
  $("#away-score").textContent = snapshot.score_away;
  currentScore = { [home]: snapshot.score_home, [snapshot.away_team]: snapshot.score_away };
  if (!replaySource) updateClock(replaySecond);
  $("#phase").textContent = state.language === "zh" ? tr(snapshot.phase) : sentenceCase(snapshot.phase.toUpperCase());
  $("#phase").hidden = false;
  $("#phase").dataset.phase = snapshot.phase;
  $("#phase").title = state.language === "zh"
    ? {controlled:"一方持续控球并稳定推进", contested:"双方争夺球权", chaotic:"球权转换频繁"}[snapshot.phase]
    : {controlled:"One side keeps stable possession", contested:"Both sides contest possession", chaotic:"Frequent changes of possession"}[snapshot.phase];
  const homeShare = snapshot.metrics[home].momentum_score;
  const awayShare = snapshot.metrics[snapshot.away_team].momentum_score;
  $("#momentum-home").style.width = `${homeShare}%`;
  $("#momentum-away").style.width = `${awayShare}%`;
  $("#momentum-home-pct").textContent = `${homeShare.toFixed(0)}%`;
  $("#momentum-away-pct").textContent = `${awayShare.toFixed(0)}%`;
  $("#momentum-label").textContent = state.language === "zh" ? "近 5 分钟势头" : "Momentum, last 5 minutes";
  renderStats(snapshot);
  renderPlayer(snapshot);
  // During replay keep the last interesting workflow visible instead of routine "no story" traces.
  if (trace && trace.length && (overlay || !replaySource)) renderTrace(trace);
  applyLocale();
  if (!overlay) {
    if (!timedOverlay) $("#overlay-card").hidden = true;
    return;
  }
  if (!timedOverlay || timedOverlay.insight_id !== overlay.insight_id) overlayShownAt = performance.now();
  timedOverlay = overlay;
  $("#overlay-card").hidden = Boolean(goalCeremony);
  $("#overlay-card").dataset.team = teamSide(overlay.team_id);
  $("#lt-badge").textContent = teamCode(overlay.team_id || "");
  $("#overlay-card").dataset.verified = String(overlay.verified);
  $("#verified").textContent = overlay.verified ? tr("Evidence checked") : tr("Safe fallback");
  $("#provider").textContent = engineLabel(overlay.provider, overlay.provider !== "local");
  $("#eyebrow").innerHTML = overlay.eyebrow.split(" · ").map((tag) => `<span>${escapeHtml(sentenceCase(tag))}</span>`).join("");
  $("#headline").textContent = overlay.headline;
  $("#body").textContent = overlay.body;
  $("#overlay-time").textContent = formatClock(overlay.display_at_second);
  // The on-pitch card stays short: two facts for fans, three for analysts. Event ids live in the
  // "How this card was made" panel below, not on the broadcast graphic.
  $("#evidence").innerHTML = overlay.evidence.filter((item) => item.label !== "Player focus" && !overlay.headline.includes(item.value.replace(/^\d\d:\d\d · /, ""))).slice(0, state.mode === "analyst" ? 3 : 2).map((item) => `
    <div><small>${escapeHtml(tr(item.label))}</small><strong>${escapeHtml(state.language === "zh" ? item.value.replace(/ actions\b/g," 次").replace(/ regains\b/g," 次抢回").replace(/ goals\b/g," 球").replace(/ shots\b/g," 次射门").replace(/ touches\b/g," 次触球").replace(/ passes\b/g," 次传球").replace(/ vs /g," 对 ") : item.value)}</strong></div>`).join("");
  renderEvidenceTable(overlay);
  const card = $("#overlay-card");
  card.dataset.kind = overlay.kind || "";
  card.classList.remove("arrive");
  requestAnimationFrame(() => card.classList.add("arrive"));
  speakCard(overlay);
}

async function refresh() {
  const version = ++refreshVersion;
  const minute = Math.floor(replaySecond / 60);
  const params = new URLSearchParams({ ...profileParams(), minute, second: Math.floor(replaySecond % 60) });
  timedOverlay = null;
  $("#overlay-card").hidden = true;
  const quick = await getJson(`/api/insight?${params}&quick=true`);
  if (version !== refreshVersion || replaySource) return;
  renderSnapshot(quick.snapshot, quick.overlay, quick.trace);
  renderRecentEvents();
  const result = await getJson(`/api/insight?${params}`);
  if (version !== refreshVersion || replaySource) return;
  if (!result.overlay) {
    // Like a real broadcast: no graphic when there is nothing worth saying.
    timedOverlay = null;
    $("#overlay-card").hidden = true;
  }
  renderSnapshot(result.snapshot, result.overlay, result.trace);
}

async function loadMatchFrames() {
  const [lineupData, frameData, dataset] = await Promise.all([getJson("/api/lineup"), getJson("/api/frames"), getJson("/api/dataset")]);
  lineup = lineupData;
  frames = frameData;
  allEvents = dataset.events;
  buildTracking();
  initializePitch();
  renderPitchAt(replaySecond);
  $("#loading-match").hidden = true;
  const select = $("#player-focus");
  [match.home_team, match.away_team].forEach((team) => {
    const group = document.createElement("optgroup");
    group.label = team;
    lineup.filter((player) => player.team_id === team).forEach((player) => {
      const option = document.createElement("option");
      option.value = player.player_id;
      option.textContent = `${numberedPlayer(player)} · ${player.role}`;
      group.appendChild(option);
    });
    select.appendChild(group);
  });
}

async function loadRecap() {
  const version = ++recapVersion;
  const language = state.language;
  const recap = await getJson(`/api/recap?language=${language}`);
  if (version !== recapVersion || language !== state.language) return;
  $("#ft-home").textContent = recap.home_team;
  $("#ft-away").textContent = recap.away_team;
  // Show event-id citations as small chips so the recap stays readable but auditable.
  $("#recap-summary").innerHTML = escapeHtml(recap.summary).replace(/\s*\[(evt-\d+)\]/g, ' <span class="cite">$1</span>');
  $("#recap-provider").textContent = `${tr("Recap")} · ${recap.summary_provider === "local" ? tr("Local") : recap.summary_provider.toUpperCase()} · ${tr("Verified")}`;
  $("#recap-score").textContent = `${recap.score[recap.home_team]}–${recap.score[recap.away_team]}`;
  $("#recap-goals").innerHTML = recap.goals.map((goal) => `<li><time>${escapeHtml(goal.minute)}</time><b>${escapeHtml(goal.player)}</b><span>${escapeHtml(goal.team)} · ${Number(goal.xg).toFixed(2)} xG</span></li>`).join("") || `<li>${tr("No goals in this synthetic match.")}</li>`;
  $("#recap-goal-heading").textContent = `${tr("Goals")} · ${recap.goals.length}`;
  $("#recap-chances").innerHTML = recap.key_chances.map((chance) => `<li><time>${escapeHtml(chance.minute)}</time><b>${escapeHtml(chance.player)}</b><span>${escapeHtml(chance.team)} · ${Number(chance.xg).toFixed(2)} xG</span></li>`).join("");
  renderGoalMarkers(recap.goals);
}

// Goal markers on the timeline, in team colours; clicking one jumps just after the goal.
function renderGoalMarkers(goals) {
  const box = $("#markers");
  if (!box) return;
  box.innerHTML = `<span class="marker half" style="left:50%"></span>` + goals.map((goal) => {
    const [m, sec] = goal.minute.split(":").map(Number);
    const at = m * 60 + sec;
    const label = `${goal.minute} ${goal.player} (${goal.team})`;
    return `<button class="marker ${teamSide(goal.team)}" style="left:${(at / 5400) * 100}%" data-second="${at}" title="${escapeHtml(label)}" aria-label="${escapeHtml(label)}"></button>`;
  }).join("");
  box.querySelectorAll("button.marker").forEach((button) => button.addEventListener("click", () => {
    if (replaySource) pauseReplay();
    updateClock(Number(button.dataset.second));
    renderPitchAt(replaySecond);
    showGoalNear(replaySecond);
    refresh();
  }));
}

function animateReplay(timestamp) {
  if (!replaySource) return;
  if (!previousFrameTime) previousFrameTime = timestamp;
  const elapsed = (timestamp - previousFrameTime) / 1000;
  previousFrameTime = timestamp;
  updateClock(Math.min(replayLimit, replaySecond + elapsed * Number($("#replay-speed").value)));
  renderPitchAt(replaySecond);
  if (replaySecond < 90 * 60) animationFrame = requestAnimationFrame(animateReplay);
}

function pauseReplay(message = "REPLAY PAUSED") {
  if (replaySource) replaySource.close();
  replaySource = null;
  if (animationFrame) cancelAnimationFrame(animationFrame);
  animationFrame = 0;
  previousFrameTime = 0;
  stopVoice();
  $("#replay-status").textContent = tr(message);
  $("#replay-button").textContent = tr("Resume replay");
}

function startReplay() {
  endGoalCeremony();
  stopVoice();
  lastSpokenCard = "";
  ++refreshVersion;
  if (animationFrame) cancelAnimationFrame(animationFrame);
  if (replaySecond >= 90 * 60) replaySecond = 0;
  if (replaySource) replaySource.close();
  announcedGoals = new Set();
  replayLimit = replaySecond;
  timedOverlay = null;
  $("#overlay-card").hidden = true;
  $("#event-feed").replaceChildren();
  updateFeedVisibility();
  const speed = Number($("#replay-speed").value);
  const params = new URLSearchParams({ ...profileParams(), from_second: Math.floor(replaySecond), speed });
  replaySource = new EventSource(`/api/replay?${params}`);
  const source = replaySource;
  $("#replay-status").textContent = tr("REPLAYING LIVE EVENT FEED");
  $("#replay-button").textContent = tr("Pause replay");
  previousFrameTime = 0;
  animationFrame = requestAnimationFrame(animateReplay);
  replaySource.addEventListener("match_event", (event) => {
    if (source !== replaySource) return;
    const item = JSON.parse(event.data);
    lastEvent = item;
    replayLimit = Math.max(replayLimit, item.minute * 60 + item.second);
    const clock = formatClock(item.minute * 60 + item.second);
    if (item.outcome === "goal" && !announcedGoals.has(item.event_id)) {
      announcedGoals.add(item.event_id);
      showGoalCeremony(item);
    }
    const label = tr((item.attributes.pass_type ? item.attributes.pass_type + " pass" : item.event_type).replaceAll("_", " "));
    const measured = eventDetails(item);
    $("#last-event").textContent = `${clock} · ${numberedEventPlayer(item)} · ${label}`;
    const line = document.createElement("div");
    line.className = `feed-event ${item.outcome === "goal" ? "goal" : ""}`;
    line.innerHTML = `<time>${clock}</time><b>${escapeHtml(numberedEventPlayer(item))}</b><span>${escapeHtml(label)}${measured ? `<small> · ${escapeHtml(measured)}</small>` : ""}</span><i>${escapeHtml(tr(item.outcome))}</i>`;
    const feed = $("#event-feed");
    feed.prepend(line);
    while (feed.children.length > 8) feed.lastElementChild.remove();
    updateFeedVisibility();
  });
  replaySource.addEventListener("match_update", (event) => {
    if (source !== replaySource) return;
    const packet = JSON.parse(event.data);
    // A remote model may finish after the pitch has moved on. Never rewind the display.
    const packetSecond = packet.snapshot.minute * 60 + (packet.snapshot.second || 0);
    const nextEvent = allEvents.find(e => e.minute*60+e.second > packetSecond);
    replayLimit = nextEvent ? nextEvent.minute*60+nextEvent.second : 5400;
    updateClock(packetSecond);
    renderSnapshot(packet.snapshot, packet.overlay, packet.trace);
    renderPitchAt(replaySecond);
  });
  replaySource.addEventListener("narrative_update", event => {
    if (source !== replaySource) return;
    const packet = JSON.parse(event.data), card = packet.overlay;
    if (!card || card.language !== state.language || card.mode !== state.mode || !lastSnapshot) return;
    if (replaySecond < card.display_at_second || replaySecond >= card.display_at_second + card.duration_seconds) return;
    if (timedOverlay && timedOverlay.display_at_second > card.display_at_second) return;
    renderSnapshot(lastSnapshot, card, packet.trace);
  });
  replaySource.addEventListener("replay_complete", () => {
    if (source !== replaySource) return;
    updateClock(5400);
    renderPitchAt(5400);
    pauseReplay("FULL TIME · RECAP READY");
    $("#replay-button").textContent = tr("Replay match");
  });
  replaySource.onerror = () => {
    if (source !== replaySource) return;
    $("#replay-status").textContent = tr("RECONNECTING EVENT FEED");
  };
}

function profileChanged() {
  $("#timeline-export").href = `/api/overlays/timeline?${new URLSearchParams(profileParams())}`;
  if (replaySource) startReplay();
  else refresh();
}

document.querySelectorAll("[data-mode]").forEach((button) => button.addEventListener("click", () => {
  document.querySelectorAll("[data-mode]").forEach((item) => item.classList.remove("active"));
  button.classList.add("active");
  document.querySelectorAll("[data-mode]").forEach(item => item.setAttribute("aria-pressed", String(item === button)));
  state.mode = button.dataset.mode;
  profileChanged();
}));

$("#language").addEventListener("click", (event) => {
  stopVoice();
  lastSpokenCard = "";
  state.language = state.language === "en" ? "zh" : "en";
  event.currentTarget.textContent = state.language === "en" ? "中文" : "English";
  applyLocale();
  updateViewButton();
  updateVoiceButton();
  loadHealth();
  $("#fixture").textContent = fixtureLine();
  $("#event-feed").replaceChildren();
  updateFeedVisibility();
  renderRecentEvents();
  $("#recap-provider").textContent = "";
  $("#recap-goal-heading").textContent = `${tr("Goals")} · ${$("#recap-goals").children.length}`;
  $("#overlay-card").hidden = true;
  timedOverlay = null;
  if(lastTraces.length) renderTrace(lastTraces);
  buildTracking();
  renderPitchAt(replaySecond);
  loadMoments();
  $("#recap-summary").textContent = tr("Preparing recap…");
  loadRecap().catch(error => { $("#recap-summary").textContent = tr("Recap unavailable. Please retry."); });
  if (lastSnapshot) renderSnapshot(lastSnapshot, null, []);
  profileChanged();
});

$("#view-angle").addEventListener("click", () => {
  state.view = state.view === "tv" ? "tactical" : "tv";
  updateViewButton();
  renderPitchAt(replaySecond);
});

$("#voice-toggle").addEventListener("click", () => {
  if (!voiceAvailable) return;
  voiceEnabled = !voiceEnabled;
  updateVoiceButton();
  if (voiceEnabled && timedOverlay) speakCard(timedOverlay, true);
  if (!voiceEnabled) stopVoice();
});

$("#player-focus").addEventListener("change", (event) => {
  state.player = event.target.value;
  renderPitchAt(replaySecond);
  profileChanged();
});

$("#team-focus").addEventListener("change", (event) => {
  state.team = event.target.value;
  updateChoiceButtons();
  profileChanged();
});
document.querySelectorAll("#metric-buttons button").forEach(button => button.addEventListener("click", () => {
  $("#metric-focus").value = button.dataset.metric;
  updateChoiceButtons();
  renderMetricHud();
}));
document.querySelectorAll("#speed-buttons button").forEach(button => button.addEventListener("click", () => {
  $("#sprint-threshold").value = button.dataset.speed;
  updateChoiceButtons();
  buildTracking();
}));

$("#minute").addEventListener("input", (event) => {
  if (replaySource) pauseReplay();
  ++refreshVersion;
  replaySecond = Number(event.target.value);
  $("#minute-output").textContent = formatClock(replaySecond);
  $("#clock").textContent = formatClock(replaySecond);
  renderPitchAt(replaySecond);
});
$("#minute").addEventListener("change", () => { showGoalNear(replaySecond); highlightMomentFor(replaySecond); refresh(); });
$("#replay-speed").addEventListener("change", () => {
  if (replaySource) startReplay();
});
$("#replay-button").addEventListener("click", () => {
  if (replaySource) pauseReplay();
  else startReplay();
});
$("#recap-button").addEventListener("click", () => $("#recap").scrollIntoView({ behavior: "smooth" }));

function eventDetails(event) {
  const a = event.attributes;
  if (event.event_type === "pass") {
    const difficulty = Number(a.difficulty || 0);
    return `${Number(a.distance_m || 0).toFixed(1)} m · ${Number(a.ball_speed_kmh || 0).toFixed(1)} km/h · ${tr("difficulty")} ${difficulty.toFixed(2)}/1 (${tr(difficulty < .33 ? "easy" : difficulty < .66 ? "medium" : "hard")})`;
  }
  if (event.event_type === "shot") return `${Number(a.shot_speed_kmh || 0).toFixed(1)} km/h · ${Number(a.xg || 0).toFixed(2)} xG · ${tr(event.outcome)}`;
  return "";
}

function buildTracking() {
  const totals = lineup.map(() => 0);
  tracking = frames.map((frame, k) => {
    const previous = frames[Math.max(0, k - 1)];
    const dt = frame.second - previous.second;
    return frame.positions.map((xy, i) => {
      const before = previous.positions[i];
      const step = Math.hypot(xy[0] - before[0], xy[1] - before[1]);
      totals[i] += step;
      return {distance: totals[i], speed: dt > 0 ? step / dt * 3.6 : 0};
    });
  });
  trackingAlerts = [];
  const lastSpeed = lineup.map(() => -60);
  const threshold = Number($("#sprint-threshold").value);
  tracking.forEach((players, k) => players.forEach((p, i) => {
    const second = frames[k].second;
    if (p.speed >= threshold && second - lastSpeed[i] > 30) {
      trackingAlerts.push({second, player:lineup[i].player_id, kind:"sprint", value:p.speed});
      lastSpeed[i] = second;
    }
    for (const mark of [5000, 10000]) {
      if (p.distance >= mark && (!k || tracking[k-1][i].distance < mark))
        trackingAlerts.push({second, player:lineup[i].player_id, kind:"distance", value:mark/1000});
    }
  }));
  trackingAlerts.sort((a,b) => a.second-b.second);
}

function lastAt(items, second, time) {
  let lo=0, hi=items.length-1, result=-1;
  while(lo<=hi) { const mid=(lo+hi)>>1; if(time(items[mid])<=second){result=mid;lo=mid+1;}else hi=mid-1; }
  return result;
}

function renderTelemetry(second, frameIndex) {
  if (second < lastTelemetrySecond - 1.5) { telemetryEntries.clear(); telemetryPinned = []; telemetrySeen.clear(); }
  lastTelemetrySecond = second;
  const now = performance.now();
  for (const [key, entry] of telemetryEntries) if (entry.until <= now) telemetryEntries.delete(key);
  const idx = lastAt(allEvents, second, e => e.minute*60+e.second);
  for (let i = idx; i >= 0 && second - (allEvents[i].minute*60 + allEvents[i].second) <= 3; i--) {
    const event = allEvents[i];
    if (!event.player_id || !["pass", "shot"].includes(event.event_type)) continue;
    const a = event.attributes;
    const text = event.event_type === "pass"
      ? `${Number(a.distance_m || 0).toFixed(1)} m · ${Number(a.ball_speed_kmh || 0).toFixed(1)} km/h · ${state.language === "zh" ? (a.difficulty < .33 ? "易" : a.difficulty < .66 ? "中" : "难") : (a.difficulty < .33 ? "easy" : a.difficulty < .66 ? "medium" : "hard")}`
      : `${Number(a.shot_speed_kmh || 0).toFixed(1)} km/h · ${Number(a.xg || 0).toFixed(2)} xG`;
    if (!telemetrySeen.has(event.event_id)) {
      telemetryEntries.set(event.event_id, {player:event.player_id, text, until:now+3000, started:now, priority:2});
      telemetrySeen.add(event.event_id);
    }
    else if (telemetryEntries.has(event.event_id)) telemetryEntries.get(event.event_id).text = text;
  }
  const ai = lastAt(trackingAlerts, second, alert => alert.second);
  for (let i = ai; i >= 0 && second - trackingAlerts[i].second <= 3; i--) {
    const alert = trackingAlerts[i];
    const key = `${alert.player}:${alert.kind}:${alert.second}`;
    const text = alert.kind === "sprint"
      ? `${state.language === "zh" ? "冲刺" : "Sprint"} ${alert.value.toFixed(1)} km/h`
      : `${state.language === "zh" ? "跑动" : "Distance"} ${alert.value} km`;
    if (!telemetrySeen.has(key)) {
      telemetryEntries.set(key, {player:alert.player, text, until:now+3000, started:now, priority:1});
      telemetrySeen.add(key);
    }
    else if (telemetryEntries.has(key)) telemetryEntries.get(key).text = text;
  }
  telemetryPinned = telemetryPinned.filter(key => telemetryEntries.has(key));
  const newest = [...telemetryEntries.entries()].filter(([key]) => !telemetryPinned.includes(key))
    .sort((a,b) => b[1].priority-a[1].priority || b[1].started-a[1].started);
  for (const [key, entry] of newest) {
    if (telemetryPinned.length >= 2) break;
    if (![...telemetryPinned].some(pinned => telemetryEntries.get(pinned)?.player === entry.player)) telemetryPinned.push(key);
  }
  const selected = goalCeremony ? [] : telemetryPinned.map(key => telemetryEntries.get(key)).filter(Boolean);
  for (const [playerId, marker] of playerNodes) {
    let tag = marker.querySelector(".telemetry-tag");
    const item = selected.find(entry => entry.player === playerId);
    if (!item) { if (tag) tag.remove(); continue; }
    if (!tag) { tag = document.createElement("span"); tag.className = "telemetry-tag"; marker.appendChild(tag); }
    tag.textContent = item.text;
    tag.classList.add("visible");
    tag.setAttribute("aria-hidden", "false");
    const box = tag.getBoundingClientRect();
    const blockers = [$("#scoreboard"), $("#overlay-card")].filter(node => node && !node.hidden);
    const blocked = box.top < 0 || box.left < 0 || box.right > window.innerWidth || blockers.some(node => {
      const b = node.getBoundingClientRect();
      return box.left < b.right && box.right > b.left && box.top < b.bottom && box.bottom > b.top;
    });
    if (blocked) { tag.classList.remove("visible"); tag.setAttribute("aria-hidden", "true"); }
  }
  if (telemetryHideTimer) clearTimeout(telemetryHideTimer);
  telemetryHideTimer = null;
  if (!replaySource && selected.length) {
    telemetryHideTimer = setTimeout(() => renderPitchAt(replaySecond), Math.max(1, Math.min(...selected.map(item => item.until)) - now + 5));
  }
  if(state.player) {
    const i=lineup.findIndex(p=>p.player_id===state.player);
    const data=tracking[frameIndex]?.[i];
    if(data) { $("#player-distance").textContent=`${(data.distance/1000).toFixed(2)} km`; $("#player-speed").textContent=`${data.speed.toFixed(1)} km/h`; }
  }
}

function renderMetricHud() {
  const metric=$("#metric-focus").value, hud=$("#metric-hud");
  if(!hud) return;
  hud.hidden=!metric || !lastSnapshot;
  if(hud.hidden) return;
  const label={xg:"xG",possession_pct:"Possession",pass_accuracy:"Pass accuracy",shots:"Shots"}[metric];
  const snap=lastSnapshot, home=snap.metrics[snap.home_team], away=snap.metrics[snap.away_team];
  const fmt=value=>metric.includes("pct") || metric==="pass_accuracy" ? `${value.toFixed(0)}%` : metric==="xg" ? value.toFixed(2) : String(value);
  hud.textContent=`${tr(label)} · ${fmt(home[metric])} — ${fmt(away[metric])} · ${formatClock(snap.minute*60+snap.second)}`;
}

function renderRecentEvents() {
  const recent = allEvents.filter(e => e.minute*60+e.second <= replaySecond).slice(-8).reverse();
  const label = e => tr((e.attributes.pass_type ? e.attributes.pass_type + " pass" : e.event_type).replaceAll("_", " "));
  lastEvent = recent[0] || null;
  $("#last-event").textContent = lastEvent ? `${formatClock(lastEvent.minute*60+lastEvent.second)} · ${numberedEventPlayer(lastEvent)} · ${label(lastEvent)}` : "";
  $("#event-feed").innerHTML = recent.map(e => `<div class="feed-event ${e.outcome === "goal" ? "goal" : ""}"><time>${formatClock(e.minute*60+e.second)}</time><b>${escapeHtml(numberedEventPlayer(e))}</b><span>${escapeHtml(label(e))}<small>${escapeHtml(eventDetails(e))}</small></span><i>${escapeHtml(tr(e.outcome))}</i></div>`).join("");
  updateFeedVisibility();
}
$("#metric-focus").addEventListener("change",renderMetricHud);
$("#sprint-threshold").addEventListener("change",buildTracking);

async function loadMoments() {
  moments = await getJson("/api/moments");
  const select = $("#moment-select");
  const selected = select.value;
  select.replaceChildren();
  const placeholder = document.createElement("option"); placeholder.value=""; placeholder.textContent=tr("Jump to a key moment…"); select.appendChild(placeholder);
  moments.forEach((moment, index) => {
    const option = document.createElement("option");
    option.value = String(index);
    option.textContent = `${formatClock(moment.second)} · ${tr(moment.kind)} · ${moment.team}`;
    select.appendChild(option);
  });
  select.value = selected;
  const list = $("#moment-list");
  list.replaceChildren();
  moments.forEach((moment, index) => {
    const button = document.createElement("button");
    button.type = "button";
    button.dataset.index = String(index);
    button.className = "moment-item";
    button.innerHTML = `<time>${formatClock(moment.second)}</time><i class="team-dot ${teamSide(moment.team)}"></i><span>${escapeHtml(tr(moment.kind))}</span>`;
    button.setAttribute("aria-label", `${formatClock(moment.second)} · ${tr(moment.kind)} · ${moment.team}`);
    button.setAttribute("aria-current", String(selected === String(index)));
    button.addEventListener("click", () => seekMoment(index));
    list.appendChild(button);
  });
  $("#previous-moment").disabled = $("#next-moment").disabled = moments.length === 0;
  highlightMomentFor(replaySecond);
}

function highlightMomentFor(second) {
  const index = moments.findLastIndex(moment => moment.second <= second);
  if (index < 0) return;
  $("#moment-select").value = String(index);
  const list = $("#moment-list");
  const button = list.querySelector(`[data-index="${index}"]`);
  list.querySelectorAll("button").forEach(item => item.setAttribute("aria-current", String(item === button)));
  if (button) list.scrollTop = Math.max(0, button.offsetTop - list.offsetTop - (list.clientHeight - button.offsetHeight)/2);
}

function seekMoment(index) {
  const moment = moments[index];
  if (!moment) return;
  pauseReplay("KEY MOMENT · PRESS RESUME TO WATCH");
  ++refreshVersion;
  timedOverlay = null;
  updateClock(Math.max(0, moment.second - (moment.kind === "corner" ? 2 : 6)));
  renderPitchAt(replaySecond);
  highlightMomentFor(moment.second);
  $("#event-feed").replaceChildren();
  updateFeedVisibility();
  $("#last-event").textContent = `${formatClock(moment.second)} · ${tr(moment.kind)} · ${moment.player}`;
  $("#headline").textContent = state.language === "zh" ? "正在解读这个时刻…" : "Reading this moment…";
  $("#body").textContent = "";
  $("#evidence").replaceChildren();
  refresh().catch(error => { $("#body").textContent = error.message; });
}
$("#moment-select").addEventListener("change", event => {
  if (event.target.value !== "") seekMoment(Number(event.target.value));
});
$("#next-moment").addEventListener("click", () => {
  const selected = $("#moment-select").value;
  const index = selected !== "" ? Number(selected) + 1 : moments.findIndex(m => m.second > replaySecond + 6);
  if (index >= 0 && index < moments.length) seekMoment(index);
});
$("#previous-moment").addEventListener("click", () => {
  const selected = $("#moment-select").value;
  const index = selected !== "" ? Number(selected) - 1 : moments.findLastIndex(m => m.second < replaySecond);
  if (index >= 0) seekMoment(index);
});

if (demoMode) {
  document.body.classList.add("demo-mode");
  state.language = demoParams.get("lang") === "zh" ? "zh" : "en";
  state.mode = demoParams.get("mode") === "analyst" ? "analyst" : "fan";
  document.querySelectorAll("[data-mode]").forEach(button => {
    button.classList.toggle("active", button.dataset.mode === state.mode);
    button.setAttribute("aria-pressed", String(button.dataset.mode === state.mode));
  });
  $("#language").textContent = state.language === "zh" ? "English" : "中文";
  $("#replay-speed").value = "12";
  replaySecond = 51 * 60 + 30;
}
applyLocale();
updateViewButton();
updateVoiceButton();
if (demoMode) document.addEventListener("keydown", event => {
  if (event.altKey || event.ctrlKey || event.metaKey || /INPUT|SELECT|TEXTAREA/.test(document.activeElement?.tagName || "")) return;
  const key = event.key.toLowerCase();
  if ([" ", "l", "m", "1", "2", "3"].includes(key)) event.preventDefault();
  if (key === " ") $("#replay-button").click();
  if (key === "l") $("#language").click();
  if (key === "m") document.querySelector(`[data-mode="${state.mode === "fan" ? "analyst" : "fan"}"]`).click();
  if (["1", "2", "3"].includes(key)) {
    const goal = allEvents.filter(item => item.outcome === "goal")[Number(key) - 1];
    if (!goal) return;
    if (replaySource) pauseReplay();
    updateClock(Math.max(0, goal.minute * 60 + goal.second - 6));
    renderPitchAt(replaySecond);
    startReplay();
  }
});
loadMatch()
  .then(() => Promise.all([loadHealth(), loadMatchFrames(), loadMoments()]))
  .then(() => { updateClock(demoMode ? 51 * 60 + 30 : 0); startReplay(); loadRecap().catch(error => { $("#recap-summary").textContent = error.message; }); })
  .catch((error) => {
    $("#loading-match").textContent = tr("Studio connection interrupted");
    $("#headline").textContent = "Studio connection interrupted";
    $("#body").textContent = error.message;
  });
