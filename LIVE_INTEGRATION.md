# 外部比赛事件接入（试点接口）

当前浏览器的 22 人动画是合成演示。外部事件输入使用独立命名空间 `/api/live`，不会替换默认演示，也不会凭事件数据捏造真实球员轨迹。仅有传球/射门事件无法还原所有球员的跑动；真实画面需要另接有授权的视频或追踪坐标。

## 配置与调用

在本机 `.env` 或 `matchpulse.env` 配置独立的 `INGEST_API_KEY`，然后重启服务。它与 DeepSeek 密钥不同。所有外部接口都要求请求头 `X-Ingest-Key`，不要将该密钥放在网页前端、URL 或公开仓库中。未配置返回 503，错误密钥返回 401。

1. `POST /api/live/matches`，JSON 示例：

```json
{"match_id":"feed-001","home_team":"Home","away_team":"Away","source":"synthetic"}
```

2. `POST /api/live/matches/feed-001/events`，每批 1–200 条事件，字段契约见 `/api/dataset/schema`：

```json
{"events":[{"event_id":"evt-0001","match_id":"feed-001","sequence":1,"minute":0,"second":5,"event_type":"shot","team_id":"Home","player_id":"h-9","player_name":"Demo Forward","x":94,"y":34,"outcome":"saved","possession_team_id":"Away","attributes":{"xg":0.25,"distance_m":11,"shot_speed_kmh":90,"on_target":true}}]}
```

3. `GET /api/live/matches/feed-001/snapshot` 返回该场比赛的聚合指标。
4. `GET /api/live/matches/feed-001/insight?language=zh&mode=fan&player=h-9&team=Home` 返回该场比赛洞察与渲染卡片；如果配置 DeepSeek，会调用模型，失败则使用本地文案。

相同事件重复投递幂等；同 ID 不同内容、倒序事件返回 409；错误球队或比赛 ID 返回 422。整批验证成功后才写入，避免半批写入。

实际供应商需要编写适配器，将自己的球员/球队 ID、时间戳、坐标方向、单位、事件分类映射到该契约。`source` 可以标注为 `licensed_live`，它只是来源声明，不会自动连接供应商或获得数据授权。比赛提交仍使用合成数据。

## 正式上线前还缺什么

这是一套单进程内存试点：最多 16 场、每场 20,000 个事件，重启会丢失。正式接入前需持久化数据库/事件队列、跨实例状态、迟到事件与修正/撤销协议、请求限流、观测与告警、租户鉴权及密钥轮换、模型费用控制、真实供应商回放验收。使用 HTTPS。此版本不能宣称生产就绪或已有真实比赛数据源。
