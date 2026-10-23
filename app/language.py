"""Presentation language validation; proper names and measurement units stay intact."""
import re


def chinese_copy_problem(text: str, names: list[str]) -> str | None:
    if not re.search(r"[\u4e00-\u9fff]", text):
        return "Requested Chinese copy was not Chinese"
    remaining = text
    for name in sorted(set(names), key=len, reverse=True):
        remaining = remaining.replace(name, "")
    remaining = re.sub(r"\[evt-\d+\]|\bxG\b|\bkm/h\b|\bkm\b|\bm\b", "", remaining)
    if re.search(r"[A-Za-z]{2,}", remaining):
        return "Chinese copy contains untranslated English wording"
    return None
