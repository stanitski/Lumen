from __future__ import annotations

TELEGRAM_MARKDOWN_V2_SPECIALS = set(r"_*[]()~`>#+-=|{}.!")


def escape_markdown_v2(text: str) -> str:
    escaped: list[str] = []
    for char in text:
        if char == "\\":
            escaped.append("\\\\")
            continue
        if char in TELEGRAM_MARKDOWN_V2_SPECIALS:
            escaped.append(f"\\{char}")
            continue
        escaped.append(char)
    return "".join(escaped)
