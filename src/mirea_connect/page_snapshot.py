"""Inert readable course-page snapshots; not an interactive website clone."""
from __future__ import annotations

import html as html_lib
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .materials import _main_region, _soup, redact_url, scrub_text

_ALLOWED = set("a p div span section article main header footer h1 h2 h3 h4 h5 h6 ul ol li dl dt dd table thead tbody tfoot tr th td caption blockquote pre code strong b em i u s sub sup br hr details summary figure figcaption".split())


def content_region(html: str, intro_only: bool = False):
    soup = _soup(html)
    region = _main_region(soup)
    if intro_only:
        region = region.find(id="intro") or region.find(class_="activity-description")
    if region is None:
        return BeautifulSoup("<p>Описание не найдено.</p>", "html.parser")
    for tag in list(region.find_all(["form", "input", "button", "select", "textarea", "link", "meta", "base"])):
        if tag.parent is not None:
            tag.decompose()
    for tag in list(region.select(".submissionstatustable, .submissionsummarytable, .feedback, .logininfo, .usermenu")):
        if tag.parent is not None:
            tag.decompose()
    return region


def _decorative(img) -> bool:
    return img.get("role") == "presentation" or img.get("aria-hidden") == "true" or bool(set(img.get("class") or []) & {"icon", "activityicon", "smallicon"})


def image_sources(html: str, url: str, intro_only: bool = False) -> list[str]:
    region = content_region(html, intro_only)
    return list(dict.fromkeys(urljoin(url, img.get("src") or img.get("data-src") or "") for img in region.find_all("img") if not _decorative(img) and (img.get("src") or img.get("data-src"))))


def static_page(html: str, title: str, url: str, *, intro_only: bool = False, images: dict | None = None, links: dict | None = None) -> bytes:
    region = content_region(html, intro_only)
    images, links = images or {}, links or {}
    for img in list(region.find_all("img")):
        if _decorative(img):
            img.decompose()
            continue
        source = urljoin(url, img.get("src") or img.get("data-src") or "")
        alt = scrub_text(img.get("alt") or "Иллюстрация")
        if source in images:
            img.attrs = {"src": images[source], "alt": alt}
        else:
            replacement = BeautifulSoup("<p></p>", "html.parser").p
            replacement.string = f"[Изображение не сохранено: {alt}; {redact_url(source)}]"
            img.replace_with(replacement)
    for tag in list(region.find_all(True)):
        if tag.name == "img":
            continue
        if tag.name not in _ALLOWED:
            tag.unwrap()
            continue
        attrs = {}
        if tag.name == "a":
            original = urljoin(url, str(tag.get("href") or ""))
            target = links.get(original) or links.get(redact_url(original)) or redact_url(original)
            if target:
                attrs.update(href=target, rel="noreferrer noopener")
        for key in ("colspan", "rowspan", "start", "value"):
            value = str(tag.get(key) or "")
            if value.isdigit():
                attrs[key] = value
        if tag.name == "details":
            attrs["open"] = ""
        tag.attrs = attrs
    region.attrs = {}  # do not retain root event handlers or data attributes either
    for text in list(region.find_all(string=True)):
        if text.parent is not None:
            text.replace_with(scrub_text(str(text)))
    safe_title, safe_url = html_lib.escape(scrub_text(title)), html_lib.escape(redact_url(url), quote=True)
    page = f'''<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data:; style-src 'unsafe-inline'; form-action 'none'; base-uri 'none'">
<meta name="referrer" content="no-referrer"><title>{safe_title}</title>
<style>body{{font:16px/1.6 system-ui;max-width:1000px;margin:32px auto;padding:0 20px;color:#202020}}img{{max-width:100%;height:auto}}table{{border-collapse:collapse}}td,th{{border:1px solid #aaa;padding:8px}}pre{{white-space:pre-wrap}}a{{overflow-wrap:anywhere}}section{{margin:24px 0}}</style></head>
<body><h1>{safe_title}</h1><p>Статический снимок содержимого. <a href="{safe_url}" rel="noreferrer">Оригинал в СДО</a>. Скрипты и формы отключены.</p>{region}</body></html>'''
    return page.encode("utf-8")
