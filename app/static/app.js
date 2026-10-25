const state = { mode: "fan", language: "en", player: "", team: "", view: "tv" };
const demoParams = new URLSearchParams(window.location.search);
const demoMode = demoParams.get("demo") === "1";
const $ = (selector) => document.querySelector(selector);
let replaySource;
let moments = [];
let commentary = [];
let feedIndex = -2;
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
// Cards about a spell of play are timed to its end; a short preroll is enough to see them appear.
const STATE_KINDS = new Set(["control", "momentum_shift", "high_pressure", "rhythm"]);

// ------------------------------------------------------------------ TV camera
// The grass is a CSS 3D plane. Players and the ball are upright, so they are drawn in a flat layer
// and placed with the same perspective maths; a CSS counter-rotation leaves them as flat ovals.
const PERSPECTIVE = 1600;
const TV_TILT = 26;
let tilt = TV_TILT;
let tiltAnimation = 0;
let geometry = { x: 0, y: 0, w: 1, h: 1, theta: TV_TILT * Math.PI / 180 };

function computeGeometry() {
  const W = Math.max(1, pitchWidth), H = Math.max(1, pitchHeight);
  const k = tilt / TV_TILT;                       // 1 = TV camera, 0 = tactical (top-down)
  const tvW = W, tvH = W * 68 / 105;              // full width, true pitch proportions, bottom aligned
  const tacW = Math.min(W, H * 105 / 68), tacH = tacW * 68 / 105;
  const w = tacW + (tvW - tacW) * k, h = tacH + (tvH - tacH) * k;
  const yTv = H - tvH, yTac = (H - tacH) / 2;
  geometry = { x: (W - w) / 2, y: yTac + (yTv - yTac) * k, w, h, theta: tilt * Math.PI / 180 };
  const plane = $("#pitch-plane");
  if (!plane) return;
  plane.style.left = `${geometry.x}px`;
  plane.style.top = `${geometry.y}px`;
  plane.style.width = `${w}px`;
  plane.style.height = `${h}px`;
  plane.style.transform = `perspective(${PERSPECTIVE}px) rotateX(${tilt}deg)`;
}

function project(xm, ym) {
  const g = geometry;
  const X = xm / 105 * g.w - g.w / 2;
  const Y = ym / 68 * g.h - g.h;                  // origin at the near touchline, like the CSS plane
  const depth = 1 - (Y * Math.sin(g.theta)) / PERSPECTIVE;
  return { x: g.x + g.w / 2 + X / depth, y: g.y + g.h + (Y * Math.cos(g.theta)) / depth, scale: 1 / depth };
}

function clampPitch(x, y) {
  return [Math.max(0, Math.min(105, x)), Math.max(0, Math.min(68, y))];
}

function placeOnGround(node, x, y) {
  const [cx, cy] = clampPitch(x, y);
  node.style.transform = `translate3d(${cx / 105 * geometry.w}px,${cy / 68 * geometry.h}px,0) translate(-50%,-50%)`;
}

function placeUpright(node, x, y, lift = 0, grow = 1) {
  const [cx, cy] = clampPitch(x, y);
  const p = project(cx, cy);
  const anchor = 50 + 50 * (tilt / TV_TILT);      // feet on the ground in TV view, centred top-down
  const top = p.y - lift * (geometry.w / 105) * p.scale;
  node.style.transform = `translate3d(${p.x}px,${top}px,0) translate(-50%,-${anchor}%) scale(${p.scale * grow})`;
  node.style.setProperty("--s", (p.scale * grow).toFixed(3));
  return p;
}

function animateTilt(target) {
  if (tiltAnimation) cancelAnimationFrame(tiltAnimation);
  const start = tilt, began = performance.now(), duration = prefersReducedMotion ? 0 : 320;
  const step = (now) => {
    const t = duration ? Math.min(1, (now - began) / duration) : 1;
    const eased = t < .5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2;
    tilt = start + (target - start) * eased;
    computeGeometry();
    renderPitchAt(replaySecond);
    tiltAnimation = t < 1 ? requestAnimationFrame(step) : 0;
  };
  tiltAnimation = requestAnimationFrame(step);
}

// ------------------------------------------------------------------ small helpers

function updateVoiceButton() {
  const button = $("#voice-toggle");
  button.disabled = !voiceAvailable;
  button.setAttribute("aria-pressed", String(voiceEnabled));
  const label = !voiceAvailable
    ? (state.language === "zh" ? "此浏览器不支持朗读" : "Read aloud is not available in this browser")
    : voiceEnabled
      ? (state.language === "zh" ? "朗读已开启（加速回放时暂停）" : "Reading cards aloud (paused during fast replay)")
      : (state.language === "zh" ? "朗读卡片" : "Read cards aloud");
  button.setAttribute("aria-label", label);
  button.title = label;
}

function updateViewButton() {
  const button = $("#view-angle");
  $("#pitch").dataset.view = state.view;
  button.setAttribute("aria-pressed", String(state.view === "tv"));
  const label = state.language === "zh"
    ? (state.view === "tv" ? "电视视角，点击切换到战术视角" : "战术视角，点击切换到电视视角")
    : (state.view === "tv" ? "TV view: switch to tactical view" : "Tactical view: switch to TV view");
  button.setAttribute("aria-label", label);
  button.title = label;
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

function broadcastMinute(second) {
  return `${Math.min(90, Math.floor(Math.max(0, second) / 60) + 1)}'`;
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

function lastAt(items, second, time) {
  let lo = 0, hi = items.length - 1, result = -1;
  while (lo <= hi) { const mid = (lo + hi) >> 1; if (time(items[mid]) <= second) { result = mid; lo = mid + 1; } else hi = mid - 1; }
  return result;
}

// ------------------------------------------------------------------ goal ceremony

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
  $("#goal-player").textContent = `${numberedEventPlayer(event)}  ${broadcastMinute(event.minute * 60 + event.second)}`;
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

// ------------------------------------------------------------------ loading

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
      option.textContent = `${numberedPlayer(player)}, ${player.role}`;
      group.appendChild(option);
    });
    select.appendChild(group);
  });
}

// ------------------------------------------------------------------ pitch

function initializePitch() {
  const pitch = $("#pitch");
  const plane = $("#pitch-plane");
  const overlay = $("#pitch-overlay");
  const bounds = pitch.getBoundingClientRect();
  pitchWidth = bounds.width;
  pitchHeight = bounds.height;
  computeGeometry();
  plane.querySelectorAll(".player-shadow, .ball-shadow, .flight-overlay, .focus-trail").forEach((node) => node.remove());
  overlay.replaceChildren();
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
    marker.innerHTML = `<b>${escapeHtml(player.number)}</b><em class="player-name">${escapeHtml(playerSurname(player.player_name))}</em>`;
    marker.title = `${numberedPlayer(player)}, ${player.role}`;
    marker.addEventListener("click", () => {
      state.player = player.player_id;
      $("#player-focus").value = state.player;
      renderPitchAt(replaySecond);
      profileChanged();
    });
    overlay.appendChild(marker);
    playerNodes.set(player.player_id, marker);
  });
  ballShadow = document.createElement("span");
  ballShadow.className = "ball-shadow";
  plane.appendChild(ballShadow);
  ballNode = document.createElement("span");
  ballNode.className = "ball-marker";
  ballNode.setAttribute("aria-label", "Ball");
  overlay.appendChild(ballNode);
  // Ground-level lines stay on the plane so the camera tilt applies to them. They start hidden:
  // an SVG line with no coordinates draws a dot at the plane's corner.
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 105 68");
  svg.setAttribute("preserveAspectRatio", "none");
  svg.classList.add("flight-overlay");
  flightLine = document.createElementNS("http://www.w3.org/2000/svg", "line");
  flightLine.style.display = "none";
  svg.appendChild(flightLine);
  plane.appendChild(svg);
  focusTrail = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  focusTrail.setAttribute("viewBox", "0 0 105 68");
  focusTrail.setAttribute("preserveAspectRatio", "none");
  focusTrail.classList.add("focus-trail");
  focusTrail.style.display = "none";  // SVG elements ignore the hidden property
  focusTrailLines = Array.from({length: 8}, () => {
    const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
    focusTrail.appendChild(line);
    return line;
  });
  plane.appendChild(focusTrail);
  if (!$("#metric-hud")) {
    const node = document.createElement("div");
    node.id = "metric-hud"; node.hidden = true; pitch.appendChild(node);
  }
  if (!pitch.resizeObserver) {
    pitch.resizeObserver = new ResizeObserver((entries) => {
      pitchWidth = entries[0].contentRect.width;
      pitchHeight = entries[0].contentRect.height;
      computeGeometry();
      renderPitchAt(replaySecond);
    });
    pitch.resizeObserver.observe(pitch);
  }
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
    if (focusTrail) focusTrail.style.display = "none";
    return;
  }
  const index = lineup.findIndex(player => player.player_id === state.player);
  if (index < 0) { focusTrail.style.display = "none"; return; }
  focusTrail.style.display = "";
  focusTrail.dataset.team = lineup[index].team_id === match.home_team ? "home" : "away";
  const points = Array.from({length: 9}, (_, i) => trailPosition(Math.max(0, second - 8 + i), index));
  focusTrailLines.forEach((line, i) => {
    line.setAttribute("x1", points[i][0]); line.setAttribute("y1", points[i][1]);
    line.setAttribute("x2", points[i + 1][0]); line.setAttribute("y2", points[i + 1][1]);
    line.style.opacity = String(.13 + i * .065);
  });
}

function renderPitchAt(second) {
  renderFeed();
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
    if (shadow) placeOnGround(shadow, x, y);
    const focused = Boolean(state.player) && marker.dataset.playerId === state.player;
    const p = placeUpright(marker, x, y);
    marker.style.zIndex = String((focused ? 2000 : 10) + Math.round(p.y));  // nearer players in front
    marker.classList.toggle("focused", focused);
    marker.classList.toggle("has-ball", left.owner === player.player_id);
  });
  renderFocusTrail(second);
  if (ballNode) {
    const ball = left.ball;
    const nextBall = right.ball || ball;
    const bx = ball.x + (nextBall.x - ball.x) * progress;
    const by = ball.y + (nextBall.y - ball.y) * progress;
    const height = (ball.z || 0) + ((nextBall.z || 0) - (ball.z || 0)) * progress;
    placeUpright(ballNode, bx, by, height, 1 + height * 0.06);
    ballNode.style.zIndex = "4000";
    placeOnGround(ballShadow, bx, by);
    ballNode.classList.toggle("goal", nextBall.event_type === "goal");
  }
  flightLine.style.display = left.flight ? "" : "none";
  if (left.flight) {
    flightLine.setAttribute("x1", left.flight.start[0]);
    flightLine.setAttribute("y1", left.flight.start[1]);
    flightLine.setAttribute("x2", left.flight.end[0]);
    flightLine.setAttribute("y2", left.flight.end[1]);
  }
  renderTelemetry(second, low);
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

// ------------------------------------------------------------------ commentary feed

async function loadCommentary() {
  const language = state.language, mode = state.mode;
  const lines = await getJson(`/api/commentary?language=${language}&mode=${mode}`);
  if (language !== state.language || mode !== state.mode) return;
  commentary = lines;
  $("#commentary-export").href = `/api/commentary?language=${language}&mode=${mode}`;
  renderFeed(true);
}

function renderFeed(force = false) {
  const feed = $("#event-feed");
  if (!feed) return;
  const upto = lastAt(commentary, replaySecond, line => line.second);
  if (!force && upto === feedIndex) return;
  feedIndex = upto;
  const recent = upto < 0 ? [] : commentary.slice(Math.max(0, upto - 5), upto + 1).reverse();
  feed.innerHTML = recent.map(line => `
    <li class="feed-line ${teamSide(line.team)} ${line.kind}">
      <time>${escapeHtml(line.clock)}</time>
      <div><p>${escapeHtml(line.text)}</p>${line.detail ? `<small>${escapeHtml(line.detail)}</small>` : ""}</div>
    </li>`).join("") || `<li class="feed-empty">${escapeHtml(tr("Commentary starts at kick-off."))}</li>`;
}

// ------------------------------------------------------------------ how the card was made

function renderTrace(trace) {
  lastTraces = trace;
  const zh = state.language === "zh";
  $("#agent-trace").innerHTML = trace.map((item) => {
    const note = zh ? (item.summary_zh || item.summary) : item.summary;
    const model = item.provider !== "local";
    const via = model ? `<span class="meta"><span class="via">${item.provider === "deepseek" ? "DeepSeek" : "Foundry"}</span> ${(item.duration_ms / 1000).toFixed(1)} s</span>` : "";
    return `<li class="agent ${escapeHtml(item.status)}"><b>${escapeHtml(tr(item.agent))}</b><p>${escapeHtml(note)}</p>${via}</li>`;
  }).join("");
}

function renderEvidenceTable(overlay) {
  const table = $("#evidence-table");
  if (!table) return;
  if (!overlay) { table.innerHTML = ""; return; }
  const rows = overlay.evidence.map((item) => `<tr><th scope="row">${escapeHtml(tr(item.label))}</th><td>${escapeHtml(evidenceValue(item.value))}</td><td>${item.event_ids.length ? escapeHtml(item.event_ids.join(", ")) : "—"}</td></tr>`).join("");
  table.innerHTML = `<table><caption>${escapeHtml(tr("Evidence behind this card"))}</caption><tbody>${rows}</tbody></table>`;
}

function evidenceValue(value) {
  if (state.language !== "zh") return /^[a-z]+$/.test(value) ? value.charAt(0).toUpperCase() + value.slice(1) : value;
  return tr(value).replace(/ actions\b/g," 次").replace(/ regains\b/g," 次抢回").replace(/ goals?\b/g," 球").replace(/ shots\b/g," 次射门").replace(/ touches\b/g," 次触球").replace(/ passes\b/g," 次传球").replace(/ vs /g," 对 ");
}

function addsToCard(item, overlay) {
  if (item.label === "Player focus" || item.label === "Outcome") return false;
  const value = item.value.replace(/^\d\d:\d\d · /, "");
  if (overlay.headline.includes(value)) return false;
  const numbers = value.match(/\d+(?:\.\d+)?%?/g) || [];
  return !numbers.some(number => new RegExp(`(?<![\\d.])${number.replace(".", "\\.")}(?![\\d.])`).test(overlay.body));
}

// ------------------------------------------------------------------ stats and the lower third

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
  if (chosen) $("#player-line").textContent = `${numberedPlayer(chosen)}, ${chosen.role}`;
  $("#player-distance").textContent = stats ? `${(stats.distance_m / 1000).toFixed(2)} km` : "";
  if (!state.player) $("#player-speed").textContent = "";
  $("#player-events").textContent = stats ? `${stats.touches} ${tr("touches")}` : "";
}

const PHASE_LABELS = {
  en: { controlled: "Settled", contested: "Contested", chaotic: "Open" },
  zh: { controlled: "平稳", contested: "拉锯", chaotic: "开放" },
};
const PHASE_HINTS = {
  en: { controlled: "Few turnovers: one side is keeping the ball", contested: "Both sides are fighting for the ball", chaotic: "The ball is changing hands all the time" },
  zh: { controlled: "球权转换少，一方能稳住球", contested: "双方争夺球权", chaotic: "球权频繁易手" },
};

function renderSnapshot(snapshot, overlay, trace) {
  if (!snapshot) return;
  lastSnapshot = snapshot;
  renderMetricHud();
  const home = snapshot.home_team;
  $("#home-score").textContent = snapshot.score_home;
  $("#away-score").textContent = snapshot.score_away;
  if (!replaySource) updateClock(replaySecond);
  const language = state.language === "zh" ? "zh" : "en";
  $("#phase").textContent = PHASE_LABELS[language][snapshot.phase];
  $("#phase").hidden = false;
  $("#phase").dataset.phase = snapshot.phase;
  $("#phase").title = PHASE_HINTS[language][snapshot.phase];
  const homeShare = snapshot.metrics[home].momentum_score;
  const awayShare = snapshot.metrics[snapshot.away_team].momentum_score;
  $("#momentum-home").style.width = `${homeShare}%`;
  $("#momentum-away").style.width = `${awayShare}%`;
  $("#momentum-home-pct").textContent = `${homeShare.toFixed(0)}%`;
  $("#momentum-away-pct").textContent = `${awayShare.toFixed(0)}%`;
  $("#momentum-label").textContent = state.language === "zh" ? "近 5 分钟势头" : "Momentum, last 5 minutes";
  renderStats(snapshot);
  renderPlayer(snapshot);
  // A trace without a card (a quiet moment) is only worth showing while the viewer is scrubbing.
  if (trace && trace.length && !overlay && !replaySource) renderTrace(trace);
  applyLocale();
  if (!overlay) {
    if (!timedOverlay) $("#overlay-card").hidden = true;
    return;
  }
  // During replay each card stays up long enough to read. Newer cards wait in order; a goal goes
  // straight on air and clears the queue.
  const fresh = !timedOverlay || overlay.insight_id !== timedOverlay.insight_id;
  const busy = replaySource && timedOverlay && fresh && performance.now() - overlayShownAt < MIN_CARD_MS;
  if (busy && overlay.priority !== "critical") {
    if (!pendingOverlays.some(item => item.overlay.insight_id === overlay.insight_id)) {
      pendingOverlays.push({ overlay, trace });
      if (pendingOverlays.length > 3) pendingOverlays.shift();
    }
    if (!pendingTimer) pendingTimer = setTimeout(flushPendingOverlay, MIN_CARD_MS - (performance.now() - overlayShownAt));
    return;
  }
  if (fresh) clearPendingOverlay();
  showOverlay(overlay, trace);
}

const MIN_CARD_MS = 4500;
const STALE_CARD_SECONDS = 90;  // match seconds after which a queued card is no longer news
let pendingOverlays = [];
let pendingTimer = 0;

function clearPendingOverlay() {
  if (pendingTimer) clearTimeout(pendingTimer);
  pendingTimer = 0;
  pendingOverlays = [];
}

function flushPendingOverlay() {
  pendingTimer = 0;
  if (!replaySource) { pendingOverlays = []; return; }
  while (pendingOverlays.length) {
    const next = pendingOverlays.shift();
    if (replaySecond - next.overlay.display_at_second > STALE_CARD_SECONDS) continue;
    showOverlay(next.overlay, next.trace);
    break;
  }
  if (pendingOverlays.length) pendingTimer = setTimeout(flushPendingOverlay, MIN_CARD_MS);
}

function showOverlay(overlay, trace) {
  if (trace && trace.length) renderTrace(trace);
  if (!timedOverlay || timedOverlay.insight_id !== overlay.insight_id) overlayShownAt = performance.now();
  timedOverlay = overlay;
  const card = $("#overlay-card");
  card.hidden = Boolean(goalCeremony);
  card.dataset.team = teamSide(overlay.team_id);
  card.dataset.mode = overlay.mode;
  card.dataset.verified = String(overlay.verified);
  $("#lt-badge").textContent = teamCode(overlay.team_id || "");
  $("#verified").hidden = !overlay.verified;
  $("#provider").textContent = engineLabel(overlay.provider, overlay.provider !== "local");
  $("#eyebrow").innerHTML = overlay.eyebrow.split(" · ").map((tag) => `<span>${escapeHtml(tag)}</span>`).join("");
  $("#headline").textContent = overlay.headline;
  $("#body").textContent = overlay.body;
  $("#overlay-time").textContent = broadcastMinute(overlay.display_at_second);
  // Fans get the sentence; analysts also get up to three numbers the sentence does not already say.
  // Event ids and the full evidence list stay in the panel below.
  $("#evidence").innerHTML = overlay.mode !== "analyst" ? "" : overlay.evidence
    .filter((item) => addsToCard(item, overlay))
    .slice(0, 3)
    .map((item) => `<div><small>${escapeHtml(tr(item.label))}</small><strong>${escapeHtml(evidenceValue(item.value))}</strong></div>`).join("");
  renderEvidenceTable(overlay);
  card.dataset.kind = overlay.kind || "";
  card.classList.remove("arrive");
  requestAnimationFrame(() => card.classList.add("arrive"));
  speakCard(overlay);
}

async function refresh() {
  clearPendingOverlay();
  const version = ++refreshVersion;
  const minute = Math.floor(replaySecond / 60);
  const params = new URLSearchParams({ ...profileParams(), minute, second: Math.floor(replaySecond % 60) });
  timedOverlay = null;
  $("#overlay-card").hidden = true;
  renderFeed(true);
  const quick = await getJson(`/api/insight?${params}&quick=true`);
  if (version !== refreshVersion || replaySource) return;
  renderSnapshot(quick.snapshot, quick.overlay, quick.trace);
  const result = await getJson(`/api/insight?${params}`);
  if (version !== refreshVersion || replaySource) return;
  if (!result.overlay) {
    // Like a real broadcast: no graphic when there is nothing worth saying.
    timedOverlay = null;
    $("#overlay-card").hidden = true;
  }
  renderSnapshot(result.snapshot, result.overlay, result.trace);
}

// ------------------------------------------------------------------ full time

function seekToEvent(eventId) {
  const event = allEvents.find(item => item.event_id === eventId);
  if (!event) return;
  if (replaySource) pauseReplay();
  updateClock(Math.max(0, event.minute * 60 + event.second - 6));
  renderPitchAt(replaySecond);
  showGoalNear(replaySecond);
  refresh();
  $(".stage").scrollIntoView({ behavior: prefersReducedMotion ? "auto" : "smooth", block: "start" });
}

function citedSummary(text) {
  // "[evt-0581]" becomes a small numbered link that replays that moment.
  let count = 0;
  return escapeHtml(text).replace(/(?:\s*\[evt-\d+\])+/g, (group) => {
    const links = [...group.matchAll(/evt-\d+/g)].map(([id]) => `<a class="cite" href="#pitch" data-event="${id}" title="${id}">${++count}</a>`);
    return `<sup>${links.join(",")}</sup>`;
  });
}

const OUTCOME_LABELS = { saved: "Saved", blocked: "Blocked", failed: "Off target" };

async function loadRecap() {
  const version = ++recapVersion;
  const language = state.language;
  const recap = await getJson(`/api/recap?language=${language}`);
  if (version !== recapVersion || language !== state.language) return;
  $("#ft-home").textContent = recap.home_team;
  $("#ft-away").textContent = recap.away_team;
  $("#recap-summary").innerHTML = citedSummary(recap.summary);
  $("#recap-summary").querySelectorAll("a.cite").forEach(link => link.addEventListener("click", (event) => {
    event.preventDefault();
    seekToEvent(link.dataset.event);
  }));
  $("#recap-provider").textContent = recap.summary_provider === "local"
    ? tr("Written from the event log")
    : tr("Written by DeepSeek, checked against the event log");
  $("#recap-score").textContent = `${recap.score[recap.home_team]}–${recap.score[recap.away_team]}`;
  for (const [team, list] of [[recap.home_team, "#ft-home-scorers"], [recap.away_team, "#ft-away-scorers"]]) {
    const scorers = new Map();
    recap.goals.filter(goal => goal.team === team).forEach(goal => {
      const marks = scorers.get(goal.player) || [];
      marks.push(`${goal.broadcast_minute}'`);
      scorers.set(goal.player, marks);
    });
    $(list).innerHTML = [...scorers].map(([player, marks]) => `<li>${escapeHtml(playerSurname(player))} ${escapeHtml(marks.join(", "))}</li>`).join("");
  }
  $("#recap-chances").innerHTML = recap.key_chances.map((chance) => `<li class="${teamSide(chance.team)}"><time>${chance.broadcast_minute}'</time><b>${escapeHtml(chance.player)}</b><span>${escapeHtml(tr(OUTCOME_LABELS[chance.outcome] || chance.outcome))}${state.language === "zh" ? "，" : ", "}${Number(chance.xg).toFixed(2)} xG</span></li>`).join("");
  renderGoalMarkers(recap.goals);
}

// Goal markers on the timeline, in team colours; clicking one jumps just before the goal.
function renderGoalMarkers(goals) {
  const box = $("#markers");
  if (!box) return;
  box.innerHTML = `<span class="marker half" style="left:50%"></span>` + goals.map((goal) => {
    const [m, sec] = goal.minute.split(":").map(Number);
    const at = m * 60 + sec;
    const label = `${goal.broadcast_minute}' ${goal.player} (${goal.team})`;
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

// ------------------------------------------------------------------ replay

function animateReplay(timestamp) {
  if (!replaySource) return;
  if (!previousFrameTime) previousFrameTime = timestamp;
  const elapsed = (timestamp - previousFrameTime) / 1000;
  previousFrameTime = timestamp;
  updateClock(Math.min(replayLimit, replaySecond + elapsed * Number($("#replay-speed").value)));
  renderPitchAt(replaySecond);
  if (replaySecond < 90 * 60) animationFrame = requestAnimationFrame(animateReplay);
}

function pauseReplay(message = "Paused") {
  clearPendingOverlay();
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
  clearPendingOverlay();
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
  renderFeed(true);
  const speed = Number($("#replay-speed").value);
  const params = new URLSearchParams({ ...profileParams(), from_second: Math.floor(replaySecond), speed });
  replaySource = new EventSource(`/api/replay?${params}`);
  const source = replaySource;
  $("#replay-status").textContent = tr("Replaying the match");
  $("#replay-button").textContent = tr("Pause replay");
  previousFrameTime = 0;
  animationFrame = requestAnimationFrame(animateReplay);
  replaySource.addEventListener("match_event", (event) => {
    if (source !== replaySource) return;
    const item = JSON.parse(event.data);
    replayLimit = Math.max(replayLimit, item.minute * 60 + item.second);
    if (item.outcome === "goal" && !announcedGoals.has(item.event_id)) {
      announcedGoals.add(item.event_id);
      showGoalCeremony(item);
    }
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
    pauseReplay("Full time. The match report is below.");
    $("#replay-button").textContent = tr("Replay match");
  });
  replaySource.onerror = () => {
    if (source !== replaySource) return;
    $("#replay-status").textContent = tr("Reconnecting…");
  };
}

function profileChanged() {
  $("#timeline-export").href = `/api/overlays/timeline?${new URLSearchParams(profileParams())}`;
  if (replaySource) startReplay();
  else refresh();
}

function applyMode() {
  if (document.body) document.body.dataset.mode = state.mode;  // fan view hides analyst-only controls
  renderMetricHud();
}

document.querySelectorAll("[data-mode]").forEach((button) => button.addEventListener("click", () => {
  document.querySelectorAll("[data-mode]").forEach((item) => item.classList.remove("active"));
  button.classList.add("active");
  document.querySelectorAll("[data-mode]").forEach(item => item.setAttribute("aria-pressed", String(item === button)));
  state.mode = button.dataset.mode;
  applyMode();
  loadCommentary().catch(() => {});
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
  $("#recap-provider").textContent = "";
  $("#overlay-card").hidden = true;
  timedOverlay = null;
  if (lastTraces.length) renderTrace(lastTraces);
  buildTracking();
  renderPitchAt(replaySecond);
  loadMoments();
  loadCommentary().catch(() => {});
  $("#recap-summary").textContent = tr("Preparing recap…");
  loadRecap().catch(() => { $("#recap-summary").textContent = tr("Recap unavailable. Please retry."); });
  if (lastSnapshot) renderSnapshot(lastSnapshot, null, []);
  profileChanged();
});

$("#view-angle").addEventListener("click", () => {
  state.view = state.view === "tv" ? "tactical" : "tv";
  updateViewButton();
  animateTilt(state.view === "tv" ? TV_TILT : 0);
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
$("#minute").addEventListener("change", () => { showGoalNear(replaySecond); refresh(); });
$("#replay-speed").addEventListener("change", () => {
  if (replaySource) startReplay();
});
$("#replay-button").addEventListener("click", () => {
  if (replaySource) pauseReplay();
  else startReplay();
});
$("#recap-button").addEventListener("click", () => $("#recap").scrollIntoView({ behavior: "smooth" }));

// ------------------------------------------------------------------ telemetry on the pitch

function eventDetails(event) {
  const a = event.attributes;
  if (event.event_type === "pass") {
    const difficulty = Number(a.difficulty || 0);
    const word = difficulty < .33 ? "easy" : difficulty < .66 ? "medium" : "hard";
    const label = state.language === "zh" ? { easy: "易", medium: "中", hard: "难" }[word] : word;
    return `${Number(a.distance_m || 0).toFixed(1)} m · ${Number(a.ball_speed_kmh || 0).toFixed(0)} km/h · ${label}`;
  }
  if (event.event_type === "shot") {
    const speed = `${Number(a.shot_speed_kmh || 0).toFixed(0)} km/h`;
    return state.mode === "analyst" ? `${speed} · ${Number(a.xg || 0).toFixed(2)} xG` : speed;
  }
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

function renderTelemetry(second, frameIndex) {
  if (second < lastTelemetrySecond - 1.5) { telemetryEntries.clear(); telemetryPinned = []; telemetrySeen.clear(); }
  lastTelemetrySecond = second;
  const now = performance.now();
  for (const [key, entry] of telemetryEntries) if (entry.until <= now) telemetryEntries.delete(key);
  const idx = lastAt(allEvents, second, e => e.minute*60+e.second);
  for (let i = idx; i >= 0 && second - (allEvents[i].minute*60 + allEvents[i].second) <= 3; i--) {
    const event = allEvents[i];
    if (!event.player_id || !["pass", "shot"].includes(event.event_type)) continue;
    if (event.event_type === "pass" && state.mode !== "analyst") continue;  // pass data is analyst detail
    const text = eventDetails(event);
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
  hud.hidden=!metric || !lastSnapshot || state.mode !== "analyst";
  if(hud.hidden) return;
  const label={xg:"xG",possession_pct:"Possession",pass_accuracy:"Pass accuracy",shots:"Shots"}[metric];
  const snap=lastSnapshot, home=snap.metrics[snap.home_team], away=snap.metrics[snap.away_team];
  const fmt=value=>metric.includes("pct") || metric==="pass_accuracy" ? `${value.toFixed(0)}%` : metric==="xg" ? value.toFixed(2) : String(value);
  hud.innerHTML=`<small>${escapeHtml(tr(label))}</small> <b>${escapeHtml(fmt(home[metric]))}</b> <span>–</span> <b>${escapeHtml(fmt(away[metric]))}</b>`;
}
$("#metric-focus").addEventListener("change",renderMetricHud);
$("#sprint-threshold").addEventListener("change",buildTracking);

// ------------------------------------------------------------------ key moments

async function loadMoments() {
  const language = state.language;
  const items = await getJson(`/api/highlights?language=${language}&mode=${state.mode}`);
  if (language !== state.language) return;
  moments = items;
  const select = $("#moment-select");
  const selected = select.value;
  select.replaceChildren();
  const placeholder = document.createElement("option"); placeholder.value=""; placeholder.textContent=tr("Jump to a key moment…"); select.appendChild(placeholder);
  moments.forEach((moment, index) => {
    const option = document.createElement("option");
    option.value = String(index);
    option.textContent = `${moment.minute}' ${moment.headline}`;
    select.appendChild(option);
  });
  select.value = selected;
  const list = $("#moment-list");
  list.replaceChildren();
  moments.forEach((moment, index) => {
    const button = document.createElement("button");
    button.type = "button";
    button.dataset.index = String(index);
    button.className = `moment-item ${moment.kind}`;
    button.innerHTML = `<time>${moment.minute}'</time><i class="team-dot ${teamSide(moment.team)}"></i><span>${escapeHtml(moment.headline)}</span>`;
    button.title = `${moment.label}: ${moment.headline}`;
    button.setAttribute("aria-current", String(selected === String(index)));
    button.addEventListener("click", () => seekMoment(index));
    list.appendChild(button);
  });
  $("#previous-moment").disabled = $("#next-moment").disabled = moments.length === 0;
}

function seekMoment(index) {
  const moment = moments[index];
  if (!moment) return;
  pauseReplay("Key moment. Press Resume to watch it play out.");
  ++refreshVersion;
  timedOverlay = null;
  const preroll = STATE_KINDS.has(moment.kind) || moment.kind === "corner" ? 2 : 6;
  updateClock(Math.max(0, moment.second - preroll));
  renderPitchAt(replaySecond);
  $("#moment-select").value = String(index);
  $("#moment-list").querySelectorAll("button").forEach(button => button.setAttribute("aria-current", String(button.dataset.index === String(index))));
  renderFeed(true);
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

// ------------------------------------------------------------------ start

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
applyMode();
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
  .then(() => Promise.all([loadHealth(), loadMatchFrames(), loadMoments(), loadCommentary()]))
  .then(() => { updateClock(demoMode ? 51 * 60 + 30 : 0); startReplay(); loadRecap().catch(error => { $("#recap-summary").textContent = error.message; }); })
  .catch((error) => {
    $("#loading-match").textContent = tr("Studio connection interrupted");
    $("#headline").textContent = "Studio connection interrupted";
    $("#body").textContent = error.message;
  });
