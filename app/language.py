"""Presentation language validation; proper names and measurement units stay intact."""
import re


def _name_forms(names: list[str]) -> list[str]:
    """Full names plus player surnames ('Wren' for 'Luca Wren'): Chinese copy may use either."""
    forms = set(names)
    for name in names:
        parts = name.split()
        if len(parts) >= 2 and parts[-1].isalpha() and parts[-1] not in {"FC", "AFC", "CF", "Athletic"}:
            forms.add(parts[-1])
    return sorted(forms, key=len, reverse=True)


def chinese_copy_problem(text: str, names: list[str]) -> str | None:
    if not re.search(r"[一-鿿]", text):
        return "Requested Chinese copy was not Chinese"
    remaining = text
    for name in _name_forms(names):
        remaining = re.sub(rf"(?<![A-Za-z]){re.escape(name)}(?![A-Za-z])", "", remaining)
    remaining = re.sub(r"\[evt-\d+\]|\bxG\b|\bkm/h\b|\bkm\b|\bm\b", "", remaining)
    if re.search(r"[A-Za-z]{2,}", remaining):
        return "Chinese copy contains untranslated English wording"
    return None
