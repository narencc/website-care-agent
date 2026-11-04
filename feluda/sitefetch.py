"""Read-only crawl of a customer site for the planner.

Produces a crawl manifest (pages fetched, origin boundary, page limit and
excluded routes) and a compact per-page outline grouped by landmark region,
which grounds the planner's targets in text that actually exists on the site.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urldefrag, urljoin, urlsplit

import httpx

from .db import utcnow
from .netguard import BlockedURL, guarded_get, origin_of, same_origin

LANDMARK_TAGS = {"header": "banner", "nav": "navigation", "main": "main", "footer": "contentinfo", "aside": "complementary"}
LANDMARK_ROLES = {"banner", "navigation", "main", "contentinfo", "complementary"}
SKIP_TAGS = {"script", "style", "noscript", "template", "svg"}
EXCLUDED_ROUTE_PATTERNS = (r"/wp-admin", r"/wp-login", r"/feed", r"/cart", r"/checkout", r"/my-account", r"\?", r"\.(pdf|jpg|png|zip)$")


def norm_space(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


@dataclass
class OutlineItem:
    region: str
    kind: str  # heading | link | button | text | image
    text: str
    href: str | None = None


@dataclass
class PageOutline:
    url: str
    final_url: str
    status: int
    title: str = ""
    items: list[OutlineItem] = field(default_factory=list)
    links: list[str] = field(default_factory=list)

    def text_blob(self) -> str:
        return " ".join(i.text for i in self.items)

    def region_texts(self) -> dict[str, str]:
        out: dict[str, list[str]] = {}
        for i in self.items:
            out.setdefault(i.region, []).append(f"{i.text} src={i.href}" if i.kind == "image" else i.text)
        return {k: " | ".join(v) for k, v in out.items()}

    def render(self, max_items: int = 160) -> str:
        lines = [f"PAGE {self.final_url} (title: {self.title!r})"]
        for item in self.items[:max_items]:
            extra = f" -> {item.href}" if item.href else ""
            lines.append(f"  [{item.region}] {item.kind}: {item.text}{extra}")
        if len(self.items) > max_items:
            lines.append(f"  ... {len(self.items) - max_items} more items truncated")
        return "\n".join(lines)


class _OutlineParser(HTMLParser):
    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self.base = base_url
        self.stack: list[tuple[str, str | None]] = []  # (tag, landmark role)
        self.skip = 0
        self.title = ""
        self._in_title = False
        self.items: list[OutlineItem] = []
        self.links: list[str] = []
        self._capture: dict | None = None  # open link/button/heading
        self._text: list[str] = []

    def region(self) -> str:
        for _tag, role in reversed(self.stack):
            if role:
                return role
        return "page"

    def _flush_text(self):
        text = norm_space(" ".join(self._text))
        self._text = []
        if text and len(text) > 1:
            self.items.append(OutlineItem(self.region(), "text", text[:300]))

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in SKIP_TAGS:
            self.skip += 1
            return
        if tag == "title":
            self._in_title = True
        role = a.get("role") if a.get("role") in LANDMARK_ROLES else LANDMARK_TAGS.get(tag)
        if tag not in ("br", "img", "input", "meta", "link", "hr", "source"):
            self.stack.append((tag, role))
        if self._capture is None:
            if tag == "a" or tag == "button" or re.fullmatch(r"h[1-6]", tag):
                self._flush_text()
                kind = "link" if tag == "a" else ("button" if tag == "button" else "heading")
                href = urljoin(self.base, a["href"]) if tag == "a" and a.get("href") else None
                if a.get("class") and re.search(r"\b(wp-block-button__link|button|btn)\b", a.get("class") or ""):
                    kind = "button" if tag == "a" else kind
                self._capture = {"tag": tag, "kind": kind, "href": href, "text": [], "aria": a.get("aria-label")}
                if href:
                    self.links.append(href)
            elif tag in ("p", "li", "div", "section", "td", "address", "span"):
                self._flush_text()
        if tag == "img" and a.get("src"):
            self.items.append(OutlineItem(self.region(), "image", f"alt={norm_space(a.get('alt') or '')[:120]}", urljoin(self.base, a["src"])))
        if tag == "form":
            self.items.append(OutlineItem(self.region(), "form", f"form id={a.get('id') or '-'} action={a.get('action') or '-'}"))

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS:
            self.skip = max(0, self.skip - 1)
            return
        if tag == "title":
            self._in_title = False
        if self._capture is not None and tag == self._capture["tag"]:
            text = norm_space(" ".join(self._capture["text"])) or norm_space(self._capture["aria"] or "")
            if text:
                self.items.append(OutlineItem(self.region(), self._capture["kind"], text[:200], self._capture["href"]))
            self._capture = None
        elif tag in ("p", "li", "div", "section", "td", "address"):
            self._flush_text()
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        if self.skip:
            return
        if self._in_title:
            self.title += data
            return
        if self._capture is not None:
            self._capture["text"].append(data)
        else:
            self._text.append(data)

    def close(self):
        super().close()
        self._flush_text()


def parse_outline(html: str, url: str, final_url: str | None = None, status: int = 200) -> PageOutline:
    p = _OutlineParser(final_url or url)
    p.feed(html)
    p.close()
    return PageOutline(url=url, final_url=final_url or url, status=status, title=norm_space(p.title), items=p.items, links=p.links)


def _sitemap_urls(site_url: str, limit: int) -> list[str]:
    origin = origin_of(site_url)
    found: list[str] = []
    for path in ("/wp-sitemap.xml", "/sitemap.xml", "/sitemap_index.xml"):
        try:
            resp = guarded_get(origin + path, timeout=8)
        except (BlockedURL, httpx.HTTPError):
            continue
        if resp.status_code != 200 or "xml" not in resp.headers.get("content-type", "xml"):
            continue
        try:
            root = ET.fromstring(resp.content)
        except ET.ParseError:
            continue
        locs = [el.text.strip() for el in root.iter() if el.tag.endswith("loc") and el.text]
        for loc in locs:
            if loc.endswith(".xml"):  # nested sitemap
                try:
                    sub = ET.fromstring(guarded_get(loc, timeout=8).content)
                    found += [el.text.strip() for el in sub.iter() if el.tag.endswith("loc") and el.text]
                except Exception:
                    continue
            else:
                found.append(loc)
            if len(found) >= limit * 3:
                break
        if found:
            break
    return found


def _excluded(url: str) -> str | None:
    for pat in EXCLUDED_ROUTE_PATTERNS:
        if re.search(pat, url):
            return pat
    return None


def crawl(site_url: str, *, page_limit: int = 8) -> tuple[list[PageOutline], dict]:
    """Fetch the start page, sitemap pages and same-origin nav links up to ``page_limit``."""
    origin = origin_of(site_url)
    queue: list[str] = [site_url]
    seen: set[str] = set()
    pages: list[PageOutline] = []
    excluded: list[dict] = []
    errors: list[dict] = []
    sitemap = _sitemap_urls(site_url, page_limit)
    queue += sitemap
    while queue and len(pages) < page_limit:
        url = urldefrag(queue.pop(0))[0]
        if url in seen:
            continue
        seen.add(url)
        if not same_origin(url, site_url):
            continue
        reason = _excluded(url) if url != site_url else None
        if reason:
            excluded.append({"url": url, "rule": reason})
            continue
        try:
            resp = guarded_get(url)
        except BlockedURL as e:
            errors.append({"url": url, "error": str(e)})
            continue
        except httpx.HTTPError as e:
            errors.append({"url": url, "error": f"{type(e).__name__}: {e}"})
            continue
        if "html" not in resp.headers.get("content-type", "text/html"):
            continue
        outline = parse_outline(resp.text, url, str(resp.url), resp.status_code)
        pages.append(outline)
        for link in outline.links:
            link = urldefrag(link)[0]
            if same_origin(link, site_url) and link not in seen:
                queue.append(link)
    manifest = {
        "origin_boundary": origin,
        "page_limit": page_limit,
        "pages": [{"url": p.url, "final_url": p.final_url, "status": p.status, "title": p.title} for p in pages],
        "sitemap_found": bool(sitemap),
        "excluded_routes": list(EXCLUDED_ROUTE_PATTERNS),
        "excluded": excluded,
        "errors": errors,
        "complete": not queue and not errors,
        "crawled_at": utcnow().isoformat(),
    }
    return pages, manifest


def page_path(url: str) -> str:
    return urlsplit(url).path or "/"
