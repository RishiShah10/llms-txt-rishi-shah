import re
from functools import lru_cache
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup

_ASSET_EXTENSIONS = (
    ".pdf", ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".ico",
    ".zip", ".gz", ".tar", ".mp4", ".mp3", ".css", ".js", ".woff", ".woff2",
)


def origin_of(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def scope_of(url: str) -> tuple[str, str | None]:
    """Resolve a requested URL to (base_url, scope_prefix).

    llms.txt may describe a whole site or, per the spec, a subpath. A bare
    domain scopes to the whole site (prefix None). A deeper URL scopes to that
    path and everything beneath it -- the section it belongs to.
    """
    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    if parsed.path in ("", "/"):
        return origin, None
    return f"{origin}{parsed.path}", parsed.path


def in_scope(url: str, scope_prefix: str | None) -> bool:
    if scope_prefix is None:
        return True
    boundary = scope_prefix.rstrip("/")
    path = urlparse(url).path
    return path == boundary or path.startswith(boundary + "/")


_INDEX_FILENAMES = ("/index.html", "/index.htm")

# Analytics decoration, not page identity: sites stamp their own internal links
# with these, so the same page would otherwise be listed several times. Kept
# conservative -- a generic param like `source`/`id` can be a real page key.
_TRACKING_PARAMS = frozenset({
    "ref", "referrer", "fbclid", "gclid", "msclkid", "yclid", "igshid",
    "mc_cid", "mc_eid", "_hsenc", "_hsmi",
})


def _is_tracking(key: str) -> bool:
    lowered = key.lower()
    return lowered in _TRACKING_PARAMS or lowered.startswith("utm_")


def _clean_query(query: str) -> str:
    # Sorted so ?b=2&a=1 and ?a=1&b=2 are one page, not two.
    kept = [(k, v) for k, v in parse_qsl(query, keep_blank_values=True)
            if not _is_tracking(k)]
    return urlencode(sorted(kept))


def canonical(url: str) -> str:
    """The URL with tracking decoration removed -- what we actually want to emit."""
    parsed = urlparse(url)
    if not parsed.query:
        return url
    return urlunparse(parsed._replace(query=_clean_query(parsed.query)))


def normalize(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path
    # By web convention a trailing /index.html IS the directory: /docs/index.html
    # and /docs/ are one page, served identically. Without this they key
    # differently and the same page is listed twice in the document.
    for filename in _INDEX_FILENAMES:
        if path.endswith(filename):
            path = path[: -len(filename)]
            break
    path = path.rstrip("/") or "/"
    base = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{path}"
    query = _clean_query(parsed.query) if parsed.query else ""
    return f"{base}?{query}" if query else base


def md_twin_of(url: str) -> str | None:
    """Candidate markdown mirror of a page, per the docs-host convention
    (Mintlify, Fumadocs, ...): /page -> /page.md, /docs/ -> /docs/index.html.md.

    A page that IS markdown has no markdown mirror -- probing /page.md.md is a
    request that can only 404 -- so it has no twin (None)."""
    parsed = urlparse(url)
    path = parsed.path
    if path.endswith(".md"):
        return None
    if path.endswith("/") or not path:
        md_path = f"{path or '/'}index.html.md"
    else:
        md_path = f"{path}.md"
    return f"{parsed.scheme}://{parsed.netloc}{md_path}"


def is_asset(url: str) -> bool:
    return urlparse(url).path.lower().endswith(_ASSET_EXTENSIONS)


# A site's own llms.txt is served as an ordinary page; without this filter we would
# list it inside the llms.txt we generate. The spec's filenames + the .html variant.
_LLMS_FILES = (
    "llms.txt", "llms-full.txt", "llms-ctx.txt", "llms-ctx-full.txt", "llms.html",
)


def is_llms_file(url: str) -> bool:
    return urlparse(url).path.rsplit("/", 1)[-1].lower() in _LLMS_FILES


# A JS template that interpolated an unset variable (e.g. a rendered SPA linking
# /cities/undefined/events). Always dead ends, and a real path segment is never the
# literal "undefined". Filter them so we don't fetch and list junk.
_JS_PLACEHOLDER_SEGMENTS = frozenset({"undefined", "null", "nan"})


def has_js_placeholder(url: str) -> bool:
    return any(seg in _JS_PLACEHOLDER_SEGMENTS
               for seg in urlparse(url).path.lower().split("/"))


def is_metadata_file(url: str) -> bool:
    """robots.txt and sitemaps: text and XML, not pages.

    A browser can't help and actively hurts -- Chromium wraps XML in an HTML viewer,
    so a rendered sitemap stops parsing as XML. Never escalate these.
    """
    path = urlparse(url).path.lower()
    return path.endswith("robots.txt") or path.endswith(".xml")


@lru_cache(maxsize=256)
def _rule_pattern(rule: str) -> re.Pattern:
    # RFC 9309 rule syntax: a rule is a path prefix where `*` matches any
    # characters and a trailing `$` anchors the match to the end of the path.
    anchored = rule.endswith("$")
    body = rule[:-1] if anchored else rule
    pattern = ".*".join(re.escape(part) for part in body.split("*"))
    return re.compile("^" + pattern + (r"\Z" if anchored else ""))


def is_disallowed(url: str, disallow: list[str], allow: list[str] | tuple = ()) -> bool:
    parsed = urlparse(url)
    # Rules like `/search?q=` match against the query too, not just the path.
    target = parsed.path or "/"
    if parsed.query:
        target += "?" + parsed.query
    # RFC 9309 §2.2.2: the most specific (longest) matching rule wins, and
    # Allow wins ties -- so `Allow: /private/ok` carves an exception out of
    # `Disallow: /private`.
    longest_disallow = max(
        (len(rule) for rule in disallow if _rule_pattern(rule).match(target)), default=-1
    )
    longest_allow = max(
        (len(rule) for rule in allow if _rule_pattern(rule).match(target)), default=-1
    )
    return longest_disallow > longest_allow


def same_origin_links(html: str, base_url: str, site_origin: str) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    links: list[str] = []
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        # Resolve against the current page, not the site root -- document-relative
        # hrefs (e.g. "intro" on /docs/guide/) are relative to where they appear.
        absolute = canonical(urljoin(base_url, href).split("#")[0])
        if (origin_of(absolute) == site_origin and not is_asset(absolute)
                and not is_llms_file(absolute)
                and not has_js_placeholder(absolute)):
            links.append(absolute)
    return links
