import json
import os

import httpx

from models import PageInfo, SiteData

# Free OpenRouter models are heavily rate-limited (429 in practice), so we
# default to a cheap, reliable paid model. Override with OPENROUTER_MODEL.
DEFAULT_MODEL = "openai/gpt-4o-mini"
DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
TIMEOUT_SECONDS = 30.0

_SYSTEM_PROMPT = (
    "You curate a website's pages for an llms.txt file. "
    "Given a list of pages (url, title, description), respond with JSON only, in this shape: "
    '{"summary": "<one-line site summary>", '
    '"pages": [{"url": "<verbatim url>", "section": "<short section name>", '
    '"description": "<concise one-line description>"}]}. '
    "Rules: keep every url exactly as given; write one clear one-line description per page; "
    "group pages into a few meaningful sections; put low-value pages (legal, login, etc.) "
    'in a section named exactly "Optional"; respond with JSON only, no prose or code fences.'
)


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        fence = text.rfind("```")
        if fence != -1:
            text = text[:fence]
    return text.strip()


async def _request_curation(payload: list[dict], site_title: str,
                            client: httpx.AsyncClient) -> dict:
    # Free OpenRouter models vary in support for response_format, so we ask for
    # JSON in the prompt and parse defensively rather than relying on it.
    key = os.getenv("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY not set")
    base = os.getenv("OPENROUTER_BASE_URL", DEFAULT_BASE_URL)
    model = os.getenv("OPENROUTER_MODEL", DEFAULT_MODEL)
    response = await client.post(
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {key}", "X-Title": "llms.txt generator"},
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": f"Site title: {site_title}\nPages:\n{json.dumps(payload)}"},
            ],
        },
        timeout=TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    content = response.json()["choices"][0]["message"]["content"]
    parsed = json.loads(_strip_fences(content))
    if not isinstance(parsed, dict):
        # json.loads happily accepts top-level arrays/strings/numbers; only an
        # object matches the shape curate() expects, so anything else is an
        # LLM response we can't use.
        raise ValueError("LLM response was not a JSON object")
    return parsed


async def curate(site: SiteData, client: httpx.AsyncClient, warnings: list[str]) -> SiteData:
    payload = [
        {"url": page.url, "title": page.title, "description": page.description or ""}
        for pages in site.sections.values()
        for page in pages
    ]
    if not payload:
        return site

    try:
        data = await _request_curation(payload, site.title, client)
    except Exception as error:
        # httpx.HTTPStatusError's str() spans multiple lines; keep the warning
        # to a single line so callers can safely join warnings on newlines.
        reason = (str(error).splitlines() or [type(error).__name__])[0]
        warnings.append(f"AI enhancement skipped: {reason}")
        return site

    overrides = {
        item["url"]: (item.get("section") or "Pages", item.get("description") or "")
        for item in (data.get("pages") or [])
        if isinstance(item, dict) and item.get("url")
    }

    sections: dict[str, list[PageInfo]] = {}
    for name, pages in site.sections.items():
        for page in pages:
            if page.url in overrides:
                section, description = overrides[page.url]
                page = PageInfo(page.url, page.title, description, page.lastmod, page.priority)
            else:
                section = name
            sections.setdefault(section, []).append(page)

    summary = (data.get("summary") or "").strip() or site.summary
    return SiteData(title=site.title, summary=summary, sections=sections, warnings=site.warnings)
