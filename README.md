# MatchPulse Studio

[Open the live Azure demo](https://matchpulse.blueriver-19b29690.eastasia.azurecontainerapps.io/) · [Deployment and verification](AZURE_DEPLOYMENT.md)

The demo runs on Azure Container Apps in East Asia with DeepSeek behind Microsoft Agent Framework. It can scale to zero while idle, so the first visit may take longer to load.

The 22 demo players now have distinct fictional names; their stable IDs, numbers, roles and the seeded match outcome are unchanged. The optional read-aloud toggle (speaker icon under the pitch) reads the current card in English or Chinese through the visitor's browser speech engine. It is user-initiated, stops on pause or language switch, and skips automatic reading above 2× replay speed. Voice availability and pronunciation depend on the browser and installed voices; no audio is sent to a server by this feature.

The studio opens in a tilted TV camera view; the camera icon under the pitch switches to the top-down tactical view. The grass is a CSS 3D plane, while players and the ball are drawn upright in a flat layer placed with the same perspective projection, so markers stay round and shrink with distance. Goals receive a short broadcast banner, and the lower third stays for 12 real seconds during replay. The sidebar offers club and player choices for everyone, metric and speed alerts in analyst mode, curated key moments and a commentary feed. For repeatable recording, open [`?demo=1`](https://matchpulse.blueriver-19b29690.eastasia.azurecontainerapps.io/?demo=1): it starts at 51:30 and 12× in TV view and hides developer exports. Add `&lang=zh&mode=analyst` for a Chinese analyst take. In demo mode use **Space** to play/pause, **L** to switch language, **M** to switch audience, and **1/2/3** to replay each goal. These are synthetic match events and browser-native graphics, not video recognition.

A [captured live Guardian rejection](evidence/guardian-numeric-rejection.json) documents a real DeepSeek response at match time 69:46. The model introduced an unsupported 1–2 scoreline; Evidence Guardian rejected the numbers, the model revised its explanation, and the accepted card cites `evt-2176` and `evt-2183`. This is a saved API response, not a scripted test or a fabricated demo frame.

Playback sends verified local match state immediately while model narration runs separately. Live AI updates retain their original match timestamp and are discarded when their display window has passed. Seeking uses `/api/insight?quick=true` for an immediate local card, then replaces it with a checked model response. Local cards are labelled honestly; this does not claim sub-second model inference. Chinese model copy is checked for untranslated English terms (names, units and xG are preserved), with a Chinese template fallback.

MatchPulse Studio is a live synthetic football intelligence system for broadcast studios, streaming platforms and fans. It turns a realistic event stream into explainable moments, evidence checked overlays, personalized storytelling and an automatic match recap.

> Data statement: the project uses fictional teams, fictional players and synthetic football events only. It does not contain or redistribute real Premier League match data, footage, crests or player profiles.

## What changed in 0.8: copy that reads like a broadcast

- **House style.** [`docs/WRITING_STYLE.md`](docs/WRITING_STYLE.md) defines the voice for every on-screen line: short sentences, football vocabulary, broadcast minutes (57:17 is shown as 58'), chance quality in words for fans (a big chance, a half-chance) and no disclaimers on the graphic. The DeepSeek prompts carry the same rules, and the Evidence Guardian rejects phrases such as "it is worth noting" or "彰显" alongside unsupported numbers and names.
- **Cards that know the game state.** A goal is told as "opens the scoring", "equalises", "completes the turnaround" or "pulls one back", with the run of play for analysts. Spell cards (pressure, momentum, high press, tempo) wait until eight minutes have been played, and pressure and momentum cards also need final-third play to point at; the old 04:00 card that claimed 100% momentum from two passes no longer appears. Phrasings rotate through a replay, so two counter-attacks never read the same.
- **Commentary feed.** `/api/commentary` turns about 2,800 raw events into roughly 75 lines: shots with the pass that made them, goals, corners, offsides and regains in the final third, plus half-time and full-time. Analyst mode adds a data line (xG, distance from goal, shot speed).
- **Key moments** come from the verified card timeline (`/api/highlights`): goals, big chances, momentum swings and counter-attacks, each with its headline.
- **Match report.** The full-time recap is a story in both languages (result, goals in order, the losing side's response, one line of numbers with a point of view). Citations appear as small numbers; clicking one replays that moment. The Recap Agent still validates every number, name and scoreline.
- **Agent notes in both languages.** Every trace carries `summary_zh`, so the Chinese pipeline view reads naturally instead of repeating one template.

## What works today

- Deterministic synthetic match generation with passes, shots, tackles, pressure and possession changes
- Match metrics for pass distance, pass difficulty, accuracy, xG, ball speed, shot speed, momentum and match phase
- Conditional event routing: routine events update shared state; key moments invoke the narrative workflow
- Four specialist roles: Tactical Analyst, Moment Explainer, Evidence Guardian and Audience Director
- Evidence validation with up to two handbacks to the Explainer before a deterministic safe fallback
- Fan and analyst modes, English and Chinese output, and a player focus view
- Server-sent event replay with a continuous match clock, 22-player 4-3-3 tactical field and interpolated ball/player movement
- Full-time Recap Agent with goals, key chances and match metrics
- Microsoft Agent Framework narrative agents backed by DeepSeek, with an optional Microsoft Foundry backend

## Challenge pipeline

1. **Ingest** — `app/simulator.py` emits timestamped synthetic events.
2. **Interpret** — `app/metrics.py` calculates team metrics, momentum and controlled/contested/chaotic states.
3. **Explain** — `app/agents.py` routes critical events through specialist roles and verifies event evidence.
4. **Render** — the API returns timed, prioritized overlay cards; the studio replays them through SSE.
5. **Personalize** — fan/analyst modes, language and player focus change the presentation.

## Run locally

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
uvicorn app.main:app --reload
```

Open `http://127.0.0.1:8000`.

## Model connection

DeepSeek is the tested live provider. See **DeepSeek connection** below. Foundry remains an optional legacy adapter; it is not needed to run this version. Do not claim an active Microsoft AI integration from its presence alone.

## API

- `GET /api/health` — service and initialized narrative provider
- `GET /api/events?limit=30` — synthetic event records
- `GET /api/insight?minute=64&mode=analyst&language=en` — snapshot, overlay and agent trace
- `GET /api/player-metrics` — estimated player distance and peak speed from event coordinates
- `GET /api/recap?language=en` — full-time match report with event citations
- `GET /api/commentary?language=en&mode=fan` — curated commentary lines for the feed
- `GET /api/highlights?language=en` — key moments taken from the verified card timeline
- `GET /api/replay?speed=12&mode=fan&language=en` — server-sent event replay

## Tests

### 连续比赛模拟

当前演示由固定 0.25 秒步长的比赛循环驱动，输出每 0.5 秒一帧的 22 人轨迹。
战术层计算接应位置和唯一逼抢者，运动层限制速度与加速度，球权层处理传球飞行、
接球、拦截和死球。短传、长传、直塞和吊球拥有不同速度或高度，球员需要实际到达球附近。
越位按出球时接球者、球与倒数第二名防守者的位置判断；直接角球、界外球和球门球不判越位。
比赛目前固定主队向右的战术视角，下半场更换开球队伍，屏幕不交换左右方向。

这是一套启发式二维合成模拟：射门结果仍使用概率模型，防守封堵与门将扑救尚未实现完整碰撞，
暂不包含犯规、红黄牌、换人和头球动作。不能作为真实比赛追踪数据使用。
页面默认 1 倍速自动播放，支持球员点击聚焦、传球线路和高球高度显示。

新运动行为由 `tests/test_physics.py` 验证。叙事与 Guardian 测试使用
`tests/fixture_match.py` 固定旧赛程，避免随机赛程改变掩盖证据校验的回归问题；
该夹具不参与演示服务。

```powershell
python -m pytest
```

## Deployment

The API and static studio UI are served by one FastAPI process, so the container can be deployed as a single Azure Container App. A `Dockerfile` is included. Configure LLM_API_KEY, LLM_MODEL and LLM_BASE_URL as deployment secrets/environment references; never commit credentials or `.env` files.


## DeepSeek connection

The app loads `.env` first, then `matchpulse.env` from the project root. Existing process environment variables take priority. Configure:

```dotenv
LLM_BASE_URL=https://api.deepseek.com
LLM_API_KEY=your-private-key
LLM_MODEL=deepseek-flash
LLM_TIMEOUT_SECONDS=20
```

Run `python check_connection.py`, then `python -m uvicorn app.main:app --reload`. DeepSeek takes priority over optional Foundry settings. Thinking is disabled for live cards. Existing evidence verification, revision and deterministic fallbacks still apply. `/api/health` reports the initialized provider; the connection script verifies an actual response. Never publish either private environment file.

## Audience and broadcast outputs

- Club choice tags each card YOUR TEAM or OPPONENT on the lower third; the copy itself stays neutral.
- Player focus can select a recent shot, successful tackle or progressive completed pass as its own evidence-checked story. Match-critical goals take priority. Ordinary actions do not generate a card.
- Milestones cover first team shot on target, repeat scorer goals and every tenth team shot. Goal evidence also includes that scorer's match goal count.
- Tempo changes compare possession switches in the current 5-minute window with the window ending 60 seconds earlier, using the same phase thresholds as metrics, and need a change of at least five turnovers. These overlapping windows are descriptive, not causal evidence; that caveat lives here rather than on screen. "On top" and "momentum swing" cards share one cooldown per team.
- A fixed metric HUD supports xG, possession, passing accuracy or shots. It displays its snapshot timestamp.
- Player position frames drive continuous distance and current speed; alerts offer 20/22/30 km/h thresholds and 5/10 km distance milestones. The simulation caps outfield speed at 6.5 m/s (23.4 km/h), so a 30 km/h alert does not trigger with current physics. Do not misrepresent synthetic speeds as real tracking.
- Shot speed appears on the pitch for everyone; pass distance, flight speed and difficulty appear in analyst mode. Focused or hovered players show name labels. Big-chance cards use the distance from goal when the shot was struck (the event's `distance_m` is how far the ball travelled).

### Export APIs

`GET /api/dataset?format=json` returns full synthetic events plus match metadata; `format=csv` downloads CSV with the attributes object JSON-encoded. `GET /api/dataset/schema` documents fields, units and limitations. No keys or private environment configuration are exported.

`GET /api/overlays/timeline?mode=analyst&language=zh&player=n-10&team=Northbridge%20FC` returns renderer-ready cards with event evidence, match-second timing, duration and priority. Profile parameters are optional. This reproducible export deliberately uses deterministic verified templates and reports `model_calls: 0`; it is not a recording of live DeepSeek text. Resolve coincident cards by priority in the consuming renderer. No video recognition or extra language support is claimed.

Validation: `python -m pytest`, `node --check app/static/app.js`, `node tests/i18n.cjs`, `node tests/replay_controls.cjs`. `tests/test_house_style.py` reads every card the live match produces in all four audience profiles.

## Broadcast UI and external input

The current UI is based on the supplied `matchpulse-v0.3-ui.zip` broadcast layout, preserving its self-hosted fonts, scoreboard and compact rail. Static controls and telemetry share an English/Chinese dictionary; `/api/recap?language=zh` generates and validates Chinese copy with deterministic Chinese fallback.

External normalized event ingress is documented in [LIVE_INTEGRATION.md](LIVE_INTEGRATION.md). It is authenticated and isolated from the synthetic animation. It is an in-memory pilot, not a production data-provider connection.
