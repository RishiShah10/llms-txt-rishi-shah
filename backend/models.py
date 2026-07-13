from dataclasses import dataclass, field


@dataclass
class PageInfo:
    url: str
    title: str
    description: str | None = None
    lastmod: str | None = None
    priority: float | None = None
    # Verified raw-markdown mirror of the page (e.g. /docs/intro.md) --
    # preferred link target in the llms.txt, while `url` stays canonical.
    md_url: str | None = None


@dataclass
class SiteData:
    title: str
    summary: str | None
    sections: dict[str, list[PageInfo]]
    warnings: list[str] = field(default_factory=list)
