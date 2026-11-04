"""Read-only browser capture with Playwright.

For each (page, viewport) it loads the page once in a fresh context (no
cookies, fixed locale/timezone, reduced motion, animations disabled), resolves
every target on that page, and records screenshots, element crops, DOM
snippets, per-landmark text and fingerprints. Every browser request, including
subrequests, passes the network policy in ``netguard``.

Target resolution follows the spec: search inside the reviewed landmark for
the phase-specific accessible name/text; require exactly one visible match;
never fall back to a same-text element elsewhere. When neither the new nor the
old value is found, the baseline's structural path is used only to report
what the reviewed element now shows.
"""

from __future__ import annotations

import io
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from PIL import Image

from . import netguard
from .settings import VIEWPORTS, app_settings

MOBILE_UA = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0 Mobile Safari/537.36 FeludaCareAgent/0.1"
)
DESKTOP_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36 FeludaCareAgent/0.1"
NO_MOTION_CSS = "*,*::before,*::after{transition:none!important;animation:none!important;caret-color:transparent!important;scroll-behavior:auto!important}"
LANDMARK_SELECTORS = {
    "banner": "header:not(main header):not(article header), [role=banner]",
    "navigation": "nav, [role=navigation]",
    "main": "main, [role=main]",
    "contentinfo": "footer:not(main footer):not(article footer), [role=contentinfo]",
}
CACHE_HEADERS = ("x-cache", "age", "cache-control", "cf-cache-status", "x-proxy-cache", "last-modified", "etag")
CROP_PAD = 24

ELEMENT_INFO_JS = """
(el) => {
  const landmarkSel = 'header,footer,nav,main,[role=banner],[role=contentinfo],[role=navigation],[role=main]';
  const r = el.getBoundingClientRect();
  const cs = getComputedStyle(el);
  const parts = [];
  let node = el, landmark = el.closest(landmarkSel);
  while (node && node !== landmark && node !== document.body) {
    const tag = node.tagName.toLowerCase();
    let i = 1, sib = node;
    while ((sib = sib.previousElementSibling)) if (sib.tagName === node.tagName) i++;
    parts.unshift(`${tag}:nth-of-type(${i})`);
    node = node.parentElement;
  }
  let prefix = 'body';
  if (landmark) {
    const lt = landmark.tagName.toLowerCase();
    const same = Array.from(document.querySelectorAll(lt));
    prefix = `${lt}:nth-match-${same.indexOf(landmark)}`;
  }
  return {
    tag: el.tagName.toLowerCase(),
    text: (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim(),
    aria_label: el.getAttribute('aria-label'),
    href: el.getAttribute('href'),
    resolved_href: el.href || null,
    bbox: {x: r.left + window.scrollX, y: r.top + window.scrollY, w: r.width, h: r.height},
    visible: r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none' && parseFloat(cs.opacity) > 0.05,
    color: cs.color, background: cs.backgroundColor, font_size: cs.fontSize,
    outer_html: el.outerHTML.slice(0, 2000),
    img: el.tagName === 'IMG' ? {current_src: el.currentSrc || el.src, alt: el.alt, complete: el.complete,
      natural_w: el.naturalWidth, natural_h: el.naturalHeight, rendered_w: r.width, rendered_h: r.height,
      viewport_w: window.innerWidth} : null,
    landmark: landmark ? landmark.tagName.toLowerCase() : null,
    path: {prefix, parts},
  };
}
"""

INTERACTABLE_JS = """
(el) => {
  const r = el.getBoundingClientRect();
  if (r.width <= 0 || r.height <= 0) return {ok: false, reason: 'zero size'};
  const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
  if (cx < 0 || cy < 0 || cx > window.innerWidth || cy > window.innerHeight) return {ok: false, reason: 'outside viewport after scrolling'};
  const hit = document.elementFromPoint(cx, cy);
  const cs = getComputedStyle(el);
  if (cs.pointerEvents === 'none') return {ok: false, reason: 'pointer-events: none'};
  if (!hit) return {ok: false, reason: 'nothing at element centre'};
  if (hit === el || el.contains(hit) || hit.contains(el)) return {ok: true, reason: 'clickable'};
  return {ok: false, reason: 'covered by ' + hit.tagName.toLowerCase() + (hit.className ? '.' + String(hit.className).split(' ')[0] : '')};
}
"""

REGIONS_JS = """
([masks, landmarkSelectors]) => {
  for (const sel of masks) { try { document.querySelectorAll(sel).forEach(e => e.style.display = 'none'); } catch (e) {} }
  const norm = t => (t || '').split('\\n').map(l => l.replace(/\\s+/g, ' ').trim()).filter(Boolean).join('\\n');
  const out = {page: norm(document.body.innerText)};
  for (const [role, sel] of Object.entries(landmarkSelectors)) {
    const els = Array.from(document.querySelectorAll(sel));
    if (els.length) out[role] = els.map(e => norm(e.innerText)).join('\\n');
  }
  // Menu order is structural, independent of whether a mobile menu is collapsed.
  const navSel = landmarkSelectors.navigation.split(',').map(x => x.trim() + ' a').join(',');
  const navLinks = Array.from(document.querySelectorAll(navSel));
  out.navigation_links = navLinks.map(a => (a.textContent || '').replace(/\\s+/g, ' ').trim()).filter(Boolean).join('\\n');
  out.navigation_hrefs = navLinks.map(a => a.href).join('\\n');
  return out;
}
"""


@dataclass
class ElementResult:
    matched_phase: str | None  # after | before | structural | None
    match_count: int
    info: dict[str, Any] | None = None
    interactable: dict[str, Any] | None = None
    tried: list[dict[str, Any]] = field(default_factory=list)
    ambiguous: bool = False


@dataclass
class PageCapture:
    target_id: str
    page_url: str
    requested_url: str
    viewport: str
    final_url: str = ""
    status: int | None = None
    http: dict[str, Any] = field(default_factory=dict)
    page_png: bytes | None = None
    crop_png: bytes | None = None
    context_png: bytes | None = None
    context_offset: tuple[int, int] = (0, 0)
    element: ElementResult | None = None
    regions_text: dict[str, str] = field(default_factory=dict)
    error: str | None = None
    captured_at: float = 0.0
    settings: dict[str, Any] = field(default_factory=dict)


def with_cache_bust(url: str, token: str | None) -> str:
    if not token:
        return url
    p = urlsplit(url)
    q = parse_qsl(p.query) + [("feluda_cb", token)]
    return urlunsplit((p.scheme, p.netloc, p.path, urlencode(q), p.fragment))


def _crop(png: bytes, box: tuple[int, int, int, int]) -> bytes:
    img = Image.open(io.BytesIO(png))
    x1, y1, x2, y2 = box
    x1, y1 = max(0, x1), max(0, y1)
    x2, y2 = min(img.width, x2), min(img.height, y2)
    if x2 <= x1 or y2 <= y1:
        return b""
    out = io.BytesIO()
    img.crop((x1, y1, x2, y2)).save(out, "PNG")
    return out.getvalue()


def structural_locator(page, path: dict[str, Any] | None):
    """Rebuild the baseline element's position under its landmark as a locator."""
    if not path or not path.get("parts"):
        return None
    m = re.match(r"(\w+):nth-match-(\d+)", path["prefix"])
    chain = " > ".join(path["parts"])
    if m:
        return page.locator(m.group(1)).nth(int(m.group(2))).locator(":scope > " + chain)
    return page.locator("body > " + chain)


class Capturer:
    """Owns one Chromium instance; use as a context manager inside one thread."""

    def __init__(self):
        self._pw = None
        self._browser = None

    def __enter__(self) -> "Capturer":
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(args=["--disable-features=Translate", "--force-color-profile=srgb"])
        return self

    def __exit__(self, *exc):
        try:
            if self._browser:
                self._browser.close()
        finally:
            if self._pw:
                self._pw.stop()

    def _guard(self, route):
        url = route.request.url
        if not url.startswith(("http://", "https://")):
            return route.continue_()
        try:
            netguard.check_url(url)
        except netguard.BlockedURL:
            return route.abort("blockedbyclient")
        # Chromium follows redirects without consulting the route, so fetch each hop
        # ourselves with redirects off: a 3xx goes back to the browser, whose next
        # request (the Location) passes through this guard again.
        try:
            response = route.fetch(max_redirects=0)
        except Exception:
            return route.abort("failed")
        return route.fulfill(response=response)

    def _locate(self, page, target: dict[str, Any], phases: list[str], page_key: str):
        tried = []
        for phase in phases:
            for cand in [c for c in target["locator_candidates"] if c["phase"] == phase]:
                scope = page
                within = cand.get("within_role")
                if within:
                    scope = page.locator(LANDMARK_SELECTORS.get(within, f"[role={within}]"))
                if cand.get("css"):
                    loc = scope.locator(cand["css"])
                elif cand.get("role"):
                    loc = scope.get_by_role(cand["role"], name=cand["name"])
                else:
                    loc = scope.get_by_text(cand["text"])
                handles = [h for h in loc.all() if h.is_visible()]
                tried.append({**cand, "visible_matches": len(handles)})
                if len(handles) == 1:
                    return ElementResult(phase, 1, handles[0].evaluate(ELEMENT_INFO_JS), tried=tried), handles[0]
                if len(handles) > 1:
                    return ElementResult(phase, len(handles), tried=tried, ambiguous=True), None
        anchor = target.get("structural_anchor") or {}
        path = (anchor.get("paths") or {}).get(page_key) or anchor.get("path")
        loc = structural_locator(page, path) if "structural" in phases else None
        if loc is not None:
            handles = [h for h in loc.all() if h.is_visible()]
            tried.append({"phase": "structural", "path": path, "visible_matches": len(handles)})
            if len(handles) == 1:
                return ElementResult("structural", 1, handles[0].evaluate(ELEMENT_INFO_JS), tried=tried), handles[0]
        return ElementResult(None, 0, tried=tried), None

    def _open_mobile_menu(self, page) -> dict[str, Any]:
        """Open a collapsed mobile menu the way a visitor would (a visible toggle button)."""
        toggle = page.locator(f"{LANDMARK_SELECTORS['banner']}, {LANDMARK_SELECTORS['navigation']}").locator(
            "button[aria-expanded='false'], [role=button][aria-expanded='false']"
        )
        for loc in toggle.all():
            if loc.is_visible():
                # Hold the element itself: after the click it no longer matches aria-expanded='false'.
                el = loc.element_handle()
                label = (el.text_content() or el.get_attribute("aria-label") or "").strip()
                el.click()
                page.wait_for_timeout(300)
                return {"toggle": label, "opened": el.get_attribute("aria-expanded") == "true"}
        return {"toggle": None, "opened": False}

    def run_form(
        self,
        page_url: str,
        *,
        fields: dict[str, str],
        submit_selector: str,
        success_text: str,
        on_submitting,
        viewport: str = "desktop",
    ) -> dict[str, Any]:
        """Fill and submit one form exactly once. ``on_submitting`` runs just before the
        click so the caller can durably record that a submission may have happened."""
        from playwright.sync_api import TimeoutError as PlaywrightTimeout

        vp = VIEWPORTS[viewport]
        out: dict[str, Any] = {"submitted": False, "success_seen": False, "error": None, "page_png": None, "final_url": "", "status": None, "feedback": None}
        try:
            netguard.check_url(page_url)
        except netguard.BlockedURL as e:
            out["error"] = f"blocked by network policy: {e}"
            return out
        context = self._browser.new_context(viewport={"width": vp.width, "height": vp.height}, locale="en-US", timezone_id="UTC", service_workers="block")
        clicked = False
        try:
            context.route("**/*", self._guard)
            page = context.new_page()
            page.set_default_timeout(app_settings().capture_timeout_ms)
            page.goto(page_url, wait_until="load")
            for selector, value in fields.items():
                page.locator(selector).fill(value)
            posts: list[int] = []
            page.on("response", lambda r: posts.append(r.status) if r.request.method == "POST" else None)
            on_submitting()
            clicked = True
            page.locator(submit_selector).click()
            out["submitted"] = True
            # Classic forms navigate; AJAX forms (e.g. Contact Form 7) update the page in place.
            try:
                page.wait_for_function(
                    "t => !!document.body && document.body.innerText.toLowerCase().includes(t)",
                    arg=success_text.lower(),
                    timeout=app_settings().capture_timeout_ms,
                )
            except PlaywrightTimeout:
                pass  # no success message: reported as such below
            page.wait_for_load_state("load")
            out["status"] = posts[-1] if posts else None
            out["final_url"] = page.url
            body = page.inner_text("body")
            out["success_seen"] = success_text.lower() in body.lower()
            out["feedback"] = next((line.strip() for line in body.splitlines() if success_text.lower() in line.lower()), None)
            out["page_png"] = page.screenshot(full_page=True, animations="disabled")
        except Exception as e:
            out["error"] = f"{type(e).__name__}: {e}"
            out["submitted"] = out["submitted"] or clicked  # a click may have reached the server
        finally:
            context.close()
        return out

    def capture_page(
        self,
        page_url: str,
        viewport: str,
        targets: list[dict[str, Any]],
        *,
        phases: list[str],
        masks: list[str],
        cache_bust: str | None = None,
    ) -> list[PageCapture]:
        s = app_settings()
        vp = VIEWPORTS[viewport]
        requested = with_cache_bust(page_url, cache_bust)
        settings = {
            "viewport": {"width": vp.width, "height": vp.height},
            "device_scale_factor": 1,
            "locale": "en-US",
            "timezone": "UTC",
            "reduced_motion": "reduce",
            "animations": "disabled",
            "cookies": "fresh context",
            "masks": masks,
            "cache_bust": cache_bust,
        }
        results = [PageCapture(t["id"], page_url, requested, viewport, settings=settings, captured_at=time.time()) for t in targets]
        try:
            netguard.check_url(requested)
        except netguard.BlockedURL as e:
            for r in results:
                r.error = f"blocked by network policy: {e}"
            return results
        context = self._browser.new_context(
            viewport={"width": vp.width, "height": vp.height},
            device_scale_factor=1,
            is_mobile=viewport == "mobile",
            has_touch=viewport == "mobile",
            user_agent=MOBILE_UA if viewport == "mobile" else DESKTOP_UA,
            locale="en-US",
            timezone_id="UTC",
            reduced_motion="reduce",
            service_workers="block",
        )
        try:
            context.route("**/*", self._guard)
            context.add_init_script(
                f"document.addEventListener('DOMContentLoaded',()=>{{const s=document.createElement('style');s.textContent={NO_MOTION_CSS!r};document.head.appendChild(s);}});"
            )
            page = context.new_page()
            page.set_default_timeout(s.capture_timeout_ms)
            response = page.goto(requested, wait_until="load")
            try:
                page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass
            page.evaluate("document.fonts ? document.fonts.ready.then(() => true) : true")
            http = {"status": response.status if response else None}
            if response:
                headers = response.all_headers()
                http["headers"] = {k: headers[k] for k in CACHE_HEADERS if k in headers}
            final_url = page.url
            try:
                netguard.check_url(final_url)
            except netguard.BlockedURL as e:
                raise RuntimeError(f"redirected to a blocked destination: {e}") from e

            menu_opened = None
            if viewport == "mobile" and any(t.get("region") == "navigation" for t in targets):
                menu_opened = self._open_mobile_menu(page)
            located = [self._locate(page, t, phases, f"{page_url}|{viewport}") for t in targets]
            interact = []
            for _el, h in located:
                if h is not None:
                    try:
                        h.scroll_into_view_if_needed(timeout=3000)
                        interact.append(h.evaluate(INTERACTABLE_JS))
                    except Exception as e:  # pragma: no cover - browser edge cases
                        interact.append({"ok": False, "reason": f"scroll failed: {e}"})
                else:
                    interact.append(None)
            page.evaluate("window.scrollTo(0, 0)")
            page_png = page.screenshot(full_page=True, animations="disabled")
            regions = page.evaluate(REGIONS_JS, [masks, LANDMARK_SELECTORS])
            for r, (el, _h), inter in zip(results, located, interact):
                r.final_url, r.status, r.http = final_url, http.get("status"), http
                if menu_opened is not None:
                    r.settings = {**r.settings, "mobile_menu": menu_opened}
                r.page_png, r.regions_text = page_png, regions
                el.interactable = inter
                r.element = el
                if el.info and el.info.get("bbox"):
                    b = el.info["bbox"]
                    r.crop_png = _crop(page_png, (int(b["x"] - CROP_PAD), int(b["y"] - CROP_PAD), int(b["x"] + b["w"] + CROP_PAD), int(b["y"] + b["h"] + CROP_PAD)))
                    top = max(0, int(b["y"] + b["h"] / 2 - vp.height / 2))
                    r.context_offset = (0, top)
                    r.context_png = _crop(page_png, (0, top, vp.width, top + vp.height))
        except Exception as e:
            for r in results:
                r.error = f"{type(e).__name__}: {e}"
        finally:
            context.close()
        return results
