import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from models import PageInfo

# llms.txt descriptions are meant to be one-liners. Cap them so a page without a
# real meta description (or a Markdown page, see below) can't dump its whole body.
DESC_LIMIT = 200

_WS = re.compile(r"\s+")
_MD_HEADING = re.compile(r"^#{1,6}\s+(.+)", re.MULTILINE)


def _meta(soup: BeautifulSoup, **attrs: str) -> str | None:
    tag = soup.find("meta", attrs=attrs)
    content = tag.get("content", "").strip() if tag else ""
    return content or None


def _document_title(soup: BeautifulSoup) -> str | None:
    if soup.title and soup.title.string:
        return soup.title.string.strip()
    return None


def _url_slug(url: str) -> str:
    parsed = urlparse(url)
    segments = [segment for segment in parsed.path.split("/") if segment]
    return segments[-1] if segments else parsed.netloc


def _summarize(text: str | None, limit: int = DESC_LIMIT) -> str | None:
    if not text:
        return None
    text = _WS.sub(" ", text).strip()
    if not text:
        return None
    if len(text) <= limit:
        return text
    # Truncate on a word boundary within the limit and mark it as clipped.
    return text[:limit].rsplit(" ", 1)[0].rstrip(" .,;:") + "…"


def _markdown_meta(text: str) -> tuple[str | None, str | None]:
    # Title = first ATX heading; description = first line of real prose.
    heading = _MD_HEADING.search(text)
    title = heading.group(1).strip() if heading else None
    description = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "```", "---", "|", "<")):
            continue
        description = line.lstrip("> ").strip()  # drop a leading blockquote marker
        break
    return title, description


def extract(html: str, url: str) -> PageInfo:
    # `.md` twin pages are Markdown, not HTML — parsing them as HTML collapses the
    # whole document into one text blob, so the old <p> fallback grabbed the entire
    # page as the "description". Handle them as text instead.
    if url.endswith(".md"):
        title, description = _markdown_meta(html)
        return PageInfo(
            url=url,
            title=title or _url_slug(url),
            description=_summarize(description),
        )

    soup = BeautifulSoup(html, "lxml")

    title = (
        _meta(soup, property="og:title")
        or _document_title(soup)
        or (soup.h1.get_text(strip=True) if soup.h1 else None)
        or _url_slug(url)
    )
    description = _summarize(
        _meta(soup, name="description")
        or _meta(soup, property="og:description")
        or (soup.p.get_text(strip=True) if soup.p else None)
    )
    return PageInfo(url=url, title=title, description=description)
