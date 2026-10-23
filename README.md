# MatchPulse Studio

MatchPulse Studio is a live synthetic football intelligence system for broadcast studios, streaming platforms and fans. It turns a realistic event stream into explainable moments, evidence checked overlays, personalized storytelling and an automatic match recap.

> Data statement: the project uses fictional teams, fictional players and synthetic football events only. It does not contain or redistribute real Premier League match data, footage, crests or player profiles.

## What works today

- Deterministic synthetic match generation with passes, shots, tackles, pressure and possession changes
- Match metrics for pass distance, pass difficulty, accuracy, xG, ball speed, shot speed, momentum and match phase
- Conditional event routing: routine events update shared state; key moments invoke the narrative workflow
- Four specialist roles: Tactical Analyst, Moment Explainer, Evidence Guardian and Audience Director
- Evidence validation with up to two handbacks to the Explainer before a deterministic safe fallback
- Fan and analyst modes, English and Chinese output, and a player focus view
- Server-sent event replay with a continuous match clock, 22-player 4-3-3 tactical field and interpolated ball/player movement
- Full-time Recap Agent with goals, key chances and match metrics
- Optional Microsoft Foundry and Microsoft Agent Framework workflow adapter

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
- `GET /api/recap` — full-time recap
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

- Club choice changes fan copy to your-club or opposition perspective. Analyst copy stays neutral.
- Player focus can select a recent shot, successful tackle or progressive completed pass as its own evidence-checked story. Match-critical goals take priority. Ordinary actions do not generate a card.
- Milestones cover first team shot on target, repeat scorer goals and every tenth team shot. Goal evidence also includes that scorer's match goal count.
- Rhythm changes compare possession switches in the current 5-minute window with the window ending 60 seconds earlier, using the same phase thresholds as metrics. These overlapping windows are descriptive, not causal evidence. Cooldowns reduce repeated state cards.
- A fixed metric HUD supports xG, possession, passing accuracy or shots. It displays its snapshot timestamp.
- Player position frames drive continuous distance and current speed; alerts offer 20/22/30 km/h thresholds and 5/10 km distance milestones. The simulation caps outfield speed at 6.5 m/s (23.4 km/h), so a 30 km/h alert does not trigger with current physics. Do not misrepresent synthetic speeds as real tracking.
- Pass distance/difficulty/flight speed and synthetic shot speed appear on the pitch and event feed. Focused/hovered players show name labels. Timed captions use each card's display time and duration.

### Export APIs

`GET /api/dataset?format=json` returns full synthetic events plus match metadata; `format=csv` downloads CSV with the attributes object JSON-encoded. `GET /api/dataset/schema` documents fields, units and limitations. No keys or private environment configuration are exported.

`GET /api/overlays/timeline?mode=analyst&language=zh&player=n-10&team=Northbridge%20FC` returns renderer-ready cards with event evidence, match-second timing, duration and priority. Profile parameters are optional. This reproducible export deliberately uses deterministic verified templates and reports `model_calls: 0`; it is not a recording of live DeepSeek text. Resolve coincident cards by priority in the consuming renderer. No video recognition or extra language support is claimed.

Validation: `python -m pytest`, `node --check app/static/app.js`, `node tests/replay_controls.cjs`.

## Broadcast UI and external input

The current UI is based on the supplied `matchpulse-v0.3-ui.zip` broadcast layout, preserving its self-hosted fonts, scoreboard and compact rail. Static controls and telemetry share an English/Chinese dictionary; `/api/recap?language=zh` generates and validates Chinese copy with deterministic Chinese fallback.

External normalized event ingress is documented in [LIVE_INTEGRATION.md](LIVE_INTEGRATION.md). It is authenticated and isolated from the synthetic animation. It is an in-memory pilot, not a production data-provider connection.
