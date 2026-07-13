from models import PageInfo, SiteData

OPTIONAL_SECTION = "Optional"


def _clean(text: str) -> str:
    # Brackets and embedded newlines would otherwise break the markdown link
    # syntax (or the H1/blockquote lines) that our own validator enforces.
    return " ".join(text.split()).replace("[", "(").replace("]", ")")


def _link_line(page: PageInfo) -> str:
    title = _clean(page.title)
    description = _clean(page.description) if page.description else page.description
    link = f"- [{title}]({page.md_url or page.url})"
    return f"{link}: {description}" if description else link


def _ordered_section_names(sections: dict[str, list[PageInfo]]) -> list[str]:
    names = [name for name in sections if name != OPTIONAL_SECTION]
    if OPTIONAL_SECTION in sections:
        names.append(OPTIONAL_SECTION)
    return names


def format_llms_txt(site: SiteData) -> str:
    blocks = [f"# {_clean(site.title)}"]
    if site.summary:
        blocks.append(f"> {_clean(site.summary)}")

    for name in _ordered_section_names(site.sections):
        pages = site.sections[name]
        if not pages:
            continue
        lines = "\n".join(_link_line(page) for page in pages)
        blocks.append(f"## {name}\n{lines}")

    return "\n\n".join(blocks) + "\n"
