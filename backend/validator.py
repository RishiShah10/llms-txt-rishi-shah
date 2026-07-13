import re
from dataclasses import dataclass

_LINK_RE = re.compile(r"^- \[[^\]]+\]\((https?://[^)]+)\)(: .+)?$")
_DEEP_HEADING_RE = re.compile(r"^#{3,} ")


@dataclass
class ValidationResult:
    ok: bool
    errors: list[str]


def validate(text: str) -> ValidationResult:
    lines = text.splitlines()
    non_blank = [line for line in lines if line.strip()]
    errors: list[str] = []

    if not non_blank or not non_blank[0].startswith("# "):
        errors.append("first content line must be an H1 (`# Title`)")
    if sum(1 for line in lines if line.startswith("# ")) != 1:
        errors.append("document must contain exactly one H1")

    for line in lines:
        if line.startswith("- ") and not _LINK_RE.match(line):
            errors.append(f"malformed link line: {line!r}")
        if _DEEP_HEADING_RE.match(line):
            errors.append(f"heading deeper than H2 not allowed: {line!r}")

    return ValidationResult(ok=not errors, errors=errors)
