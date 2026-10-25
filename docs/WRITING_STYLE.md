# MatchPulse 写作规范（屏幕文案、本地模板、DeepSeek 提示词共用）

目标：读起来像转播解说和赛后报道，而不是 AI 生成的报告。
`app/house_style.py` 里的禁用词会被 Evidence Guardian 直接拒绝，所以这份规范是强制的，不只是建议。

## 1. 基本口吻

- 短句，主动语态，一句只说一件事。
- 用转播常用词：levelled、went close、against the run of play、pull one back、makes it 2-1；中文用"扳平""错失良机""逆转""反超""扳回一球"。
- 写出比赛局面：比分状态（领先、扳平、落后）、时间（"58'"、"第58分钟"）。
- 球员第一次出现写全名，之后只写姓（Nouri）。英文正文里球队用简称（Riverside、Northbridge）；中文正文保留球队全名原文（Northbridge FC），不翻译。比分牌用三字母缩写。
- 句子开头要变化，不要连续用同一个句式，例如三句都以"X scored for Y at…"开头。

## 2. 数字

- **分析师模式**：数字照抄证据，不四舍五入、不换算。
- **球迷模式**：不出现小数。xG 用档位词表达：

| xG | 英文 | 中文 |
|---|---|---|
| ≥ 0.30 | a big chance | 绝佳机会 |
| 0.12–0.30 | a good chance | 不错的机会 |
| 0.05–0.12 | a half-chance | 半个机会 |
| < 0.05 | a long shot | 机会很小的射门 |

档位只决定用词；具体句子由各模板自己组织，比如低档进球写成"几乎凭空破门"，而不是把档位词硬塞进句子。

- DeepSeek 写的球迷文案如果出现小数，Guardian 会退回并改用本地文案。

- 比赛时间写成转播格式：57:17 写作 58'（中文"第58分钟"），不写 57:17。分析师卡片里需要精确到秒时才写 57:17。

## 3. 屏幕上不写的东西

- 免责声明和方法论说明，例如"这只说明……不代表……""not a proven tactical cause"。这些写进文档，不放在转播画面上。
- 反复强调可信："evidence-grounded""verified""已校验"。字幕条上一个 ✓ 就够了。
- 原始字段名：pressure success、possession change、difficulty 0.14/1。
- 用"·"把一串字段连起来，例如"Local · completed · 1 ms"。

## 4. 禁用词（Guardian 会打回）

英文：it is worth noting / worth noting / evidence-grounded / this highlights / testament / underscores / showcasing / pivotal / in conclusion / delve / notably / tapestry / not a proven tactical cause

中文：值得注意的是 / 彰显 / 凸显 / 不能仅据此 / 综上所述 / 总而言之 / 毋庸置疑 / 见证了

## 5. 例子

| 不要 | 要 |
|---|---|
| Elias Nouri scored for Riverside Athletic at 57:17 from a 0.07 xG chance. | Nouri wins it on 58'. Not much of a chance, but Riverside lead 2-1. |
| #10 Luca Wren / short pass · 13.7 m / success | 19:56 Marsh lays it off to Wren, and the keeper saves. |
| Riverside Athletic: 10 shots. Shot volume alone does not establish chance quality. | Riverside reach 10 shots. |
| Match rhythm: contested to chaotic. These adjacent rolling windows suggest… | The game has opened up: 14 changes of possession in five minutes, up from 9. |
| 战术分析：已完成。处理结果可通过下方事件引用核对。 | 从 3 个候选中选出：进球（Riverside Athletic）。 |

## 6. 上屏节奏

- 开场 8 分钟内不出"压制 / 势头 / 逼抢 / 节奏"类卡片；"占据场面优势"和"场面转向"共用一个冷却时间。
- 同一类卡片在一场回放里轮换说法，相邻两张卡片标题不重复。
- 回放时每张卡片至少停留 4.5 秒，后来的卡片排队；进球直接上屏，晚了 90 秒（比赛时间）的卡片不再播出。

## 7. 赛后回顾结构

1. 一句话交代结果和走势（逆转、小胜、大胜、握手言和）。
2. 进球过程用连接词串起来：首开纪录 → 扳平 → 反超或锁定胜局。
3. 输球一方最好的机会，或者比赛后段的压力。
4. 最多一句数据，并且要有观点（谁的机会更好），不要罗列数字。
