from urllib.parse import urlparse

from models import PageInfo, SiteData

DEFAULT_SECTION = "Pages"
OPTIONAL_SECTION = "Optional"

# Sections are inferred from URL path segments -- the one structure every site has.
# Vocabulary-heavy on purpose, so a real docs site fans out into a few buckets. A
# flat site (/billing, /cli) has no structure to find and lands in "Pages".
_SECTION_BY_SEGMENT = {
    # Reference documentation
    "docs": "Docs", "doc": "Docs", "documentation": "Docs", "manual": "Docs",
    "handbook": "Docs",
    # Learning material -- task-oriented, as opposed to reference
    "guides": "Guides", "guide": "Guides", "tutorials": "Guides",
    "tutorial": "Guides", "learn": "Guides", "quickstart": "Guides",
    "quickstarts": "Guides", "get-started": "Guides", "getting-started": "Guides",
    "how-to": "Guides", "howto": "Guides", "cookbook": "Guides", "recipes": "Guides",
    # Machine-facing surface
    "api": "API", "apis": "API", "reference": "API", "sdk": "API", "sdks": "API",
    "cli": "API", "schema": "API", "endpoints": "API",
    # Time-ordered writing
    "blog": "Blog", "posts": "Blog", "post": "Blog", "news": "Blog",
    "articles": "Blog", "changelog": "Blog", "releases": "Blog",
    "release-notes": "Blog", "updates": "Blog",
    # What the thing is and what it costs
    "pricing": "Product", "plans": "Product", "features": "Product",
    "product": "Product", "products": "Product", "solutions": "Product",
    "platform": "Product", "integrations": "Product", "enterprise": "Product",
    # Runnable material -- worth its own bucket, an LLM reaches for these first
    "examples": "Examples", "example": "Examples", "showcase": "Examples",
    "templates": "Examples", "template": "Examples", "demos": "Examples",
    "samples": "Examples", "starters": "Examples",
    # Where a human goes when stuck
    "community": "Community", "support": "Community", "help": "Community",
    "faq": "Community", "faqs": "Community", "forum": "Community",
    "discussions": "Community",
}
# The spec's skippable "Optional" section: legal, auth and company pages that say
# nothing about what the site does.
_OPTIONAL_SEGMENTS = {
    "privacy", "terms", "legal", "login", "signin", "sign-in", "signup",
    "sign-up", "register", "cart", "checkout", "careers", "jobs", "cookie",
    "cookies", "contact", "about", "press", "media", "brand", "imprint",
    "security", "compliance", "status",
}


def _section_for(url: str) -> str:
    segments = [segment.lower() for segment in urlparse(url).path.split("/") if segment]
    if not segments:
        return DEFAULT_SECTION
    for segment in segments:
        if segment in _OPTIONAL_SEGMENTS:
            return OPTIONAL_SECTION
        if segment in _SECTION_BY_SEGMENT:
            return _SECTION_BY_SEGMENT[segment]
    return DEFAULT_SECTION


def rank(pages: list[PageInfo], title: str, summary: str | None) -> SiteData:
    by_priority = sorted(pages, key=lambda page: -(page.priority or 0))
    sections: dict[str, list[PageInfo]] = {}
    for page in by_priority:
        sections.setdefault(_section_for(page.url), []).append(page)
    return SiteData(title=title, summary=summary, sections=sections, warnings=[])
