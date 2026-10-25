"""House style shared by the templates, the DeepSeek prompts and the Evidence Guardian.

docs/WRITING_STYLE.md explains the rules. This module holds the parts code can enforce:
broadcast minutes, chance-quality wording, short club names, and phrases that are never shown.
"""

from __future__ import annotations

import re
import zlib

# Phrases that make copy read like a generated report. The Guardian rejects them in model output,
# and tests run every deterministic template through the same check.
BANNED_EN = (
    "it is worth noting", "worth noting", "evidence-grounded", "this highlights", "testament",
    "underscores", "showcasing", "pivotal", "in conclusion", "delve", "notably", "tapestry",
    "not a proven tactical cause",
)
BANNED_ZH = ("值得注意的是", "彰显", "凸显", "不能仅据此", "综上所述", "总而言之", "毋庸置疑", "见证了")

_GENERIC_NAME = re.compile(r"\bPlayer\s+\d+$")
_CLUB_SUFFIXES = {"FC", "AFC", "CF", "Athletic"}


def banned_phrase(text: str) -> str | None:
    lower = text.lower()
    for phrase in BANNED_EN:
        if re.search(rf"(?<![a-z]){re.escape(phrase)}", lower):
            return phrase
    return next((phrase for phrase in BANNED_ZH if phrase in text), None)


def minute_mark(minute: int) -> int:
    """Broadcast minute: 57:17 is played in the 58th minute, shown as 58'."""
    return min(minute + 1, 90)


def mark_en(minute: int) -> str:
    return f"{minute_mark(minute)}'"


def mark_zh(minute: int) -> str:
    return f"第{minute_mark(minute)}分钟"


def ordinal(number: int) -> str:
    suffix = "th" if 10 <= number % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th")
    return f"{number}{suffix}"


CHANCE_WORDS = (  # (minimum xG, English, Chinese)
    (0.30, "a big chance", "绝佳机会"),
    (0.12, "a good chance", "不错的机会"),
    (0.05, "a half-chance", "半个机会"),
    (0.0, "a long shot", "机会很小的射门"),
)


def chance_tier(xg: float) -> int:
    """0 big chance, 1 good chance, 2 half-chance, 3 long shot."""
    return next(i for i, (floor, _, _) in enumerate(CHANCE_WORDS) if xg >= floor or floor == 0.0)


def chance_words(xg: float, language: str = "en") -> str:
    _, english, chinese = CHANCE_WORDS[chance_tier(xg)]
    return chinese if language == "zh" else english


def short_club(name: str) -> str:
    """'Northbridge FC' -> 'Northbridge'. Used in English prose; Chinese keeps the full name."""
    words = [word for word in name.split() if word not in _CLUB_SUFFIXES]
    return " ".join(words) or name


def surname(name: str, others: set[str] | None = None) -> str:
    """Last name when it is unambiguous; generic 'N. Player 7' style names stay whole."""
    if _GENERIC_NAME.search(name) or " " not in name.strip():
        return name
    last = name.rsplit(" ", 1)[-1]
    if others and any(other != name and other.rsplit(" ", 1)[-1] == last for other in others):
        return name
    return last


KIND_LABELS = {
    "goal": ("goal", "进球"),
    "chance": ("big chance", "绝佳机会"),
    "transition": ("counter-attack", "快速反击"),
    "high_pressure": ("high press", "高位逼抢"),
    "momentum_shift": ("momentum", "势头转换"),
    "control": ("pressure", "压制"),
    "milestone": ("milestone", "里程碑"),
    "rhythm": ("tempo", "节奏变化"),
    "player_moment": ("player focus", "球员聚焦"),
}


def variant(key: str, count: int) -> int:
    """Stable choice between phrasings, so repeated card types do not read word for word the same.
    crc32 rather than hash(): the choice must not change between processes or replays."""
    return zlib.crc32(key.encode("utf-8")) % count


def kind_label(kind: str, language: str = "en") -> str:
    english, chinese = KIND_LABELS.get(kind, (kind, kind))
    return chinese if language == "zh" else english


_REASONS_ZH = (
    (r"^numbers not backed by evidence: (.*)$", "这些数字在证据里找不到：{0}"),
    (r"^scorelines not in evidence: (.*)$", "比分和证据对不上：{0}"),
    (r"^players not in cited events: (.*)$", "提到了引用事件之外的球员：{0}"),
    (r"^banned phrase: (.*)$", "用了写作规范禁止的说法：{0}"),
    (r"^unknown or future event ids: (.*)$", "引用了不存在或尚未发生的事件：{0}"),
    (r"^narrative mentions unknown event ids: (.*)$", "文中提到了不存在的事件：{0}"),
    (r"^no event ids cited$", "没有引用任何事件"),
    (r"^empty narrative$", "文案为空"),
    (r"^Keep club name exactly as supplied: (.*); do not translate it$", "球队名必须保持原文：{0}"),
    (r"^Requested Chinese copy was not Chinese$", "要求中文，返回的不是中文"),
    (r"^fan copy uses decimals$", "球迷文案里出现了小数"),
    (r"^Chinese copy contains untranslated English wording$", "中文文案里夹杂了未翻译的英文"),
)


def reason_zh(reason: str) -> str:
    """Translate Guardian reasons for the Chinese pipeline view; unknown reasons pass through."""
    parts = []
    for part in reason.split("; "):
        for pattern, template in _REASONS_ZH:
            match = re.match(pattern, part)
            if match:
                parts.append(template.format(*match.groups()))
                break
        else:
            parts.append(part)
    return "；".join(parts)
