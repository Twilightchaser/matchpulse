const state = { mode: "fan", language: "en", player: "", team: "" };
const $ = (selector) => document.querySelector(selector);
let replaySource;
let moments = [];
let allEvents = [];
let tracking = [];
let trackingAlerts = [];
let lastSnapshot = null;
let recapVersion = 0;
let lastTraces = [];
let timedOverlay = null;
let refreshVersion = 0;
let frames = [];
let lineup = [];
let playerNodes = new Map();
let ballNode;
let ballShadow;
let flightLine;
let replaySecond = 0;
let animationFrame = 0;
let previousFrameTime = 0;
let currentScore = {};
let announcedGoals = new Set();
let pitchWidth = 0;
let pitchHeight = 0;
let match = { home_team: "Home", away_team: "Away" };
const prefersReducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

function teamCode(name) {
  return String(name || "").replace(/[^A-Za-z]/g, "").slice(0, 3).toUpperCase();
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
}

function initializePitch() {
  const pitch = $("#pitch");
  const bounds = pitch.getBoundingClientRect();
  pitchWidth = bounds.width;
  pitchHeight = bounds.height;
  pitch.querySelectorAll(".player-marker, .ball-marker").forEach((node) => node.remove());
  playerNodes = new Map();
  lineup.forEach((player) => {
    const marker = document.createElement("span");
    marker.className = `player-marker ${player.team_id === match.home_team ? "home" : "away"}`;
    marker.dataset.playerId = player.player_id;
    marker.innerHTML = `<b>${escapeHtml(player.number)}</b><small>${escapeHtml(player.role)}</small><em class="player-name">${escapeHtml(player.player_name)}</em>`;
    marker.title = `${player.player_name} · ${player.role}`;
    marker.addEventListener("click", () => {
      state.player = player.player_id;
      $("#player-focus").value = state.player;
      renderPitchAt(replaySecond);
      profileChanged();
    });
    pitch.appendChild(marker);
    playerNodes.set(player.player_id, marker);
  });
  ballNode = document.createElement("span");
  ballNode.className = "ball-marker";
  ballNode.setAttribute("aria-label", "Ball");
  pitch.appendChild(ballNode);
  ballShadow = document.createElement("span");
  ballShadow.className = "ball-shadow";
  pitch.appendChild(ballShadow);
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 105 68");
  svg.setAttribute("preserveAspectRatio", "none");
  svg.classList.add("flight-overlay");
  flightLine = document.createElementNS("http://www.w3.org/2000/svg", "line");
  svg.appendChild(flightLine);
  pitch.appendChild(svg);
  const status = document.createElement("div");
  status.id = "pitch-action";
  pitch.appendChild(status);
  for (const id of ["metric-hud", "tracking-alert", "action-metrics", "pitch-caption"]) {
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

function positionNode(node, x, y) {
  const pixelX = Math.max(0, Math.min(pitchWidth, (x / 105) * pitchWidth));
  const pixelY = Math.max(0, Math.min(pitchHeight, (y / 68) * pitchHeight));
  node.style.transform = `translate3d(${pixelX}px,${pixelY}px,0) translate(-50%,-50%)`;
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
    positionNode(marker, coordinates[0] + (next[0] - coordinates[0]) * progress, coordinates[1] + (next[1] - coordinates[1]) * progress);
    marker.classList.toggle("focused", Boolean(state.player) && marker.dataset.playerId === state.player);
    marker.classList.toggle("has-ball", left.owner === player.player_id);
  });
  if (ballNode) {
    const ball = left.ball;
    const nextBall = right.ball || ball;
    positionNode(ballNode, ball.x + (nextBall.x - ball.x) * progress, ball.y + (nextBall.y - ball.y) * progress);
    positionNode(ballShadow, ball.x + (nextBall.x - ball.x) * progress, ball.y + (nextBall.y - ball.y) * progress);
    const height = (ball.z || 0) + ((nextBall.z || 0) - (ball.z || 0)) * progress;
    ballNode.style.marginTop = `-${height * 6}px`;
    ballNode.style.scale = String(1 + height * 0.08);
    ballNode.classList.toggle("goal", nextBall.event_type === "goal");
  }
  const labels = state.language === "zh" ? {short_pass:"短传配合",long_pass:"长传转移",through_pass:"直塞前插",lofted_pass:"高球传递",
    carry:"持球推进",shot:"射门",offside:"越位",corner:"角球",throw_in:"界外球",goal_kick:"球门球",kickoff:"中圈开球"}
    : {short_pass:"Short pass",long_pass:"Long pass",through_pass:"Through ball",lofted_pass:"Lofted pass",carry:"Ball carry",shot:"Shot",offside:"Offside",corner:"Corner",throw_in:"Throw-in",goal_kick:"Goal kick",kickoff:"Kickoff"};
  $("#pitch-action").textContent = labels[left.phase] || left.phase || "";
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
  if (left.score) {
    $("#home-score").textContent = left.score[0];
    $("#away-score").textContent = left.score[1];
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
  const rows = overlay.evidence.map((item) => `<tr><th scope="row">${escapeHtml(tr(item.label))}</th><td>${escapeHtml(item.value)}</td><td>${item.event_ids.length ? escapeHtml(item.event_ids.join(", ")) : "—"}</td></tr>`).join("");
  table.innerHTML = `<table><caption>${escapeHtml(tr("Evidence behind this card"))}</caption><tbody>${rows}</tbody></table>`;
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
  $("#phase").dataset.phase = snapshot.phase;
  const homeShare = snapshot.metrics[home].momentum_score;
  const awayShare = snapshot.metrics[snapshot.away_team].momentum_score;
  $("#momentum-home").style.width = `${homeShare}%`;
  $("#momentum-away").style.width = `${awayShare}%`;
  const leader = homeShare >= awayShare ? home : snapshot.away_team;
  $("#momentum-label").textContent = `${state.language === "zh" ? "近5分钟势头" : "Momentum, last 5 min"} · ${leader} ${Math.max(homeShare, awayShare).toFixed(0)}%`;
  renderStats(snapshot);
  renderPlayer(snapshot);
  // During replay keep the last interesting workflow visible instead of routine "no story" traces.
  if (trace && trace.length && (overlay || !replaySource)) renderTrace(trace);
  applyLocale();
  if (!overlay) {
    if (!timedOverlay) $("#overlay-card").hidden = true;
    return;
  }
  timedOverlay = overlay;
  $("#overlay-card").hidden = false;
  $("#overlay-card").dataset.team = teamSide(overlay.team_id);
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
}

async function refresh() {
  const version = ++refreshVersion;
  const minute = Math.floor(replaySecond / 60);
  const params = new URLSearchParams({ ...profileParams(), minute, second: Math.floor(replaySecond % 60) });
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
  const select = $("#player-focus");
  [match.home_team, match.away_team].forEach((team) => {
    const group = document.createElement("optgroup");
    group.label = team;
    lineup.filter((player) => player.team_id === team).forEach((player) => {
      const option = document.createElement("option");
      option.value = player.player_id;
      option.textContent = `${player.player_name} · ${player.role}`;
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
    updateClock(Number(button.dataset.second) + 15);
    renderPitchAt(replaySecond);
    refresh();
  }));
}

function animateReplay(timestamp) {
  if (!replaySource) return;
  if (!previousFrameTime) previousFrameTime = timestamp;
  const elapsed = (timestamp - previousFrameTime) / 1000;
  previousFrameTime = timestamp;
  updateClock(replaySecond + elapsed * Number($("#replay-speed").value));
  renderPitchAt(replaySecond);
  if (replaySecond < 90 * 60) animationFrame = requestAnimationFrame(animateReplay);
}

function pauseReplay(message = "REPLAY PAUSED") {
  if (replaySource) replaySource.close();
  replaySource = null;
  if (animationFrame) cancelAnimationFrame(animationFrame);
  animationFrame = 0;
  previousFrameTime = 0;
  $("#replay-status").textContent = tr(message);
  $("#replay-button").textContent = tr("Resume replay");
}

function startReplay() {
  ++refreshVersion;
  if (animationFrame) cancelAnimationFrame(animationFrame);
  if (replaySecond >= 90 * 60) replaySecond = 0;
  if (replaySource) replaySource.close();
  announcedGoals = new Set();
  $("#event-feed").replaceChildren();
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
    const clock = formatClock(item.minute * 60 + item.second);
    if (item.outcome === "goal" && !announcedGoals.has(item.event_id)) {
      announcedGoals.add(item.event_id);
      currentScore[item.team_id] = (currentScore[item.team_id] || 0) + 1;
      $("#home-score").textContent = currentScore[match.home_team] || 0;
      $("#away-score").textContent = currentScore[match.away_team] || 0;
      $("#scoreboard").classList.add("goal-flash");
      window.setTimeout(() => $("#scoreboard").classList.remove("goal-flash"), 900);
    }
    const label = tr((item.attributes.pass_type ? item.attributes.pass_type + " pass" : item.event_type).replaceAll("_", " "));
    const measured = eventDetails(item);
    $("#last-event").textContent = `${clock} · ${item.player_name} · ${label}`;
    const line = document.createElement("div");
    line.className = `feed-event ${item.outcome === "goal" ? "goal" : ""}`;
    line.innerHTML = `<time>${clock}</time><b>${escapeHtml(item.player_name)}</b><span>${escapeHtml(label)}${measured ? `<small> · ${escapeHtml(measured)}</small>` : ""}</span><i>${escapeHtml(tr(item.outcome))}</i>`;
    const feed = $("#event-feed");
    feed.prepend(line);
    while (feed.children.length > 8) feed.lastElementChild.remove();
  });
  replaySource.addEventListener("match_update", (event) => {
    if (source !== replaySource) return;
    const packet = JSON.parse(event.data);
    // A remote model may finish after the pitch has moved on. Never rewind the display.
    const packetSecond = packet.snapshot.minute * 60 + (packet.snapshot.second || 0);
    if (packetSecond > replaySecond + 2 || replaySecond - packetSecond > 12) return;
    renderSnapshot(packet.snapshot, packet.overlay, packet.trace);
    renderPitchAt(replaySecond);
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
  state.language = state.language === "en" ? "zh" : "en";
  event.currentTarget.textContent = state.language === "en" ? "中文" : "English";
  applyLocale();
  loadHealth();
  $("#fixture").textContent = fixtureLine();
  $("#event-feed").replaceChildren();
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

$("#player-focus").addEventListener("change", (event) => {
  state.player = event.target.value;
  renderPitchAt(replaySecond);
  profileChanged();
});

$("#team-focus").addEventListener("change", (event) => {
  state.team = event.target.value;
  profileChanged();
});

$("#minute").addEventListener("input", (event) => {
  if (replaySource) pauseReplay();
  ++refreshVersion;
  replaySecond = Number(event.target.value);
  $("#minute-output").textContent = formatClock(replaySecond);
  $("#clock").textContent = formatClock(replaySecond);
  renderPitchAt(replaySecond);
});
$("#minute").addEventListener("change", refresh);
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
      trackingAlerts.push({second, player:lineup[i].player_id, text:`${lineup[i].player_name} · ${p.speed.toFixed(1)} km/h`});
      lastSpeed[i] = second;
    }
    for (const mark of [5000, 10000]) {
      if (p.distance >= mark && (!k || tracking[k-1][i].distance < mark))
        trackingAlerts.push({second, player:lineup[i].player_id, text:`${lineup[i].player_name} · ${mark/1000} km ${tr("covered")}`});
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
  const idx = lastAt(allEvents, second, e => e.minute*60+e.second);
  let event = null;
  for (let i=idx; i>=0 && second-(allEvents[i].minute*60+allEvents[i].second)<=5; i--) {
    if (eventDetails(allEvents[i])) {event=allEvents[i];break;}
  }
  const detail = $("#action-metrics");
  const text = event ? eventDetails(event) : "";
  detail.hidden = !text || second-(event.minute*60+event.second)>5;
  detail.textContent = event ? `${event.player_name} · ${text}` : "";
  const ai = lastAt(trackingAlerts,second,e=>e.second);
  let alert=null;
  for(let i=ai;i>=0 && second-trackingAlerts[i].second<=5;i--) {
    if(!state.player || trackingAlerts[i].player===state.player) {alert=trackingAlerts[i];break;}
  }
  $("#tracking-alert").hidden = !alert;
  $("#tracking-alert").textContent = alert ? alert.text : "";
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
  hud.textContent=`${label} · ${fmt(home[metric])} — ${fmt(away[metric])} · ${formatClock(snap.minute*60+snap.second)}`;
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
  $("#previous-moment").disabled = $("#next-moment").disabled = moments.length === 0;
}

function seekMoment(index) {
  const moment = moments[index];
  if (!moment) return;
  pauseReplay("KEY MOMENT · PRESS RESUME TO WATCH");
  ++refreshVersion;
  timedOverlay = null;
  updateClock(Math.max(0, moment.second - (moment.kind === "corner" ? 2 : 6)));
  renderPitchAt(replaySecond);
  $("#moment-select").value = String(index);
  $("#event-feed").replaceChildren();
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

applyLocale();
loadMatch()
  .then(() => Promise.all([loadHealth(), loadMatchFrames(), loadMoments()]))
  .then(() => { updateClock(0); startReplay(); loadRecap().catch(error => { $("#recap-summary").textContent = error.message; }); })
  .catch((error) => {
    $("#headline").textContent = "Studio connection interrupted";
    $("#body").textContent = error.message;
  });
