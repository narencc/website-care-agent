"""Bright Smile Dental: a local stand-in for an approved WordPress test site.

Public pages use WordPress block-theme markup (header/footer template parts,
site logo, navigation block with a mobile menu toggle, Buttons and Image
blocks, a contact form). /wp-admin/ mimics the labels the demo recipes are
validated against, so a builder can make (or deliberately botch) changes.
An optional page cache keyed by path ignores query strings, so cache-busting
alone does not prove freshness. The contact form delivers into a local test
inbox (/__demo/inbox) unless mail delivery is switched off in its settings.

    uvicorn demo_site.app:app --port 8100
"""

from __future__ import annotations

import io
import json
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from html import escape
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response

HERE = Path(__file__).parent
STATE_FILE = Path(os.environ.get("DEMO_SITE_STATE") or HERE / "state.json")
INBOX_FILE = Path(os.environ.get("DEMO_SITE_INBOX") or HERE / "inbox.json")
MEDIA_DIR = Path(os.environ.get("DEMO_SITE_MEDIA") or HERE / "media")

DEFAULT_STATE = {
    "site_name": "Bright Smile Dental",
    "header_phone_text": "(555) 201-4410",
    "header_phone_href": "tel:+15552014410",
    "footer_phone_text": "(555) 201-4410",
    "footer_phone_href": "tel:+15552014410",
    "footer_email_text": "hello@brightsmile.example",
    "footer_email_href": "mailto:hello@brightsmile.example",
    "home_button_label": "Book an appointment",
    "home_button_href": "/contact/",
    "contact_paragraph": "Call our front desk on (555) 201-4410 to book or reschedule.",
    "hours_text": "Mon–Fri 8am–6pm, Sat 9am–1pm",
    "logo_src": "/media/logo-teal.png",
    "logo_alt": "Bright Smile Dental logo",
    "hero_src": "/media/hero-family.png",
    "hero_alt": "Smiling family in the clinic",
    "menu_items": [{"label": "Home", "href": "/"}, {"label": "Services", "href": "/services/"}, {"label": "Contact", "href": "/contact/"}],
    "form_recipient": "frontdesk@brightsmile.example",
    "form_delivery": "on",
    "form_mode": "post",  # "ajax": submit with fetch and show the reply in place, like Contact Form 7
    "cache_enabled": False,
}

FIELDS = {
    "header": [("header_phone_text", "Phone link text"), ("header_phone_href", "Phone link URL")],
    "footer": [
        ("footer_phone_text", "Phone link text"),
        ("footer_phone_href", "Phone link URL"),
        ("footer_email_text", "Email link text"),
        ("footer_email_href", "Email link URL"),
    ],
    "home": [("home_button_label", "Button text"), ("home_button_href", "Button link")],
    "contact": [("contact_paragraph", "Contact details paragraph")],
}
IMAGE_FIELDS = {"header": ("logo_src", "Site logo"), "home": ("hero_src", "Hero image")}
MENU_ROWS = 6

_lock = threading.Lock()
_cache: dict[str, tuple[float, str]] = {}

app = FastAPI(title="Bright Smile Dental (demo WordPress site)")


# --- Media library ------------------------------------------------------------------


def _make_media() -> None:
    """Generate the demo media library (distinct, recognisable images) on first run."""
    from PIL import Image, ImageDraw

    MEDIA_DIR.mkdir(parents=True, exist_ok=True)

    def logo(name, colour):
        img = Image.new("RGB", (160, 160), "white")
        d = ImageDraw.Draw(img)
        d.ellipse((8, 8, 152, 152), fill=colour)
        d.polygon([(52, 48), (108, 48), (116, 84), (100, 128), (86, 98), (74, 98), (60, 128), (44, 84)], fill="white")
        img.save(MEDIA_DIR / name)

    def hero(name, top, bottom, accent):
        img = Image.new("RGB", (800, 360), top)
        d = ImageDraw.Draw(img)
        for y in range(360):
            t = y / 359
            d.line([(0, y), (800, y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(top, bottom)))
        for i, x in enumerate((220, 400, 580)):
            d.ellipse((x - 60, 90 + 20 * (i % 2), x + 60, 210 + 20 * (i % 2)), fill=accent)
            d.rectangle((x - 80, 210, x + 80, 360), fill=accent)
        img.save(MEDIA_DIR / name)

    if not (MEDIA_DIR / "logo-teal.png").exists():
        logo("logo-teal.png", (15, 110, 120))
        logo("logo-orange.png", (227, 100, 20))
        hero("hero-family.png", (210, 236, 240), (120, 180, 190), (245, 222, 190))
        hero("hero-smile.png", (255, 236, 210), (240, 160, 120), (60, 90, 120))


def media_files() -> list[str]:
    _make_media()
    return sorted(p.name for p in MEDIA_DIR.iterdir() if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"))


@app.get("/media/{name}")
def media(name: str):
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name) or name not in media_files():
        return Response(status_code=404)
    return FileResponse(MEDIA_DIR / name, headers={"Cache-Control": "no-cache"})


# --- State ------------------------------------------------------------------------------


def load_state() -> dict:
    with _lock:
        if STATE_FILE.exists():
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            return {**DEFAULT_STATE, **data}
        return json.loads(json.dumps(DEFAULT_STATE))


def save_state(state: dict) -> None:
    with _lock:
        STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def load_inbox() -> list[dict]:
    with _lock:
        return json.loads(INBOX_FILE.read_text(encoding="utf-8")) if INBOX_FILE.exists() else []


def deliver(message: dict) -> None:
    with _lock:
        inbox = json.loads(INBOX_FILE.read_text(encoding="utf-8")) if INBOX_FILE.exists() else []
        inbox.append(message)
        INBOX_FILE.write_text(json.dumps(inbox, indent=2), encoding="utf-8")


CSS = """
*{box-sizing:border-box} body{margin:0;font-family:Georgia,serif;color:#1d2b36;background:#fbfaf7}
.wp-site-blocks{min-height:100vh;display:flex;flex-direction:column}
header.wp-block-template-part{background:#0f4c5c;color:#fff;padding:14px 32px;display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap}
header a{color:#fff} .site-brand{display:flex;align-items:center;gap:10px;text-decoration:none}
.custom-logo{width:48px;height:48px;border-radius:50%;background:#fff;display:block} .site-title{font-size:24px;font-weight:bold}
nav ul{list-style:none;display:flex;gap:18px;margin:0;padding:0} nav a{text-decoration:none}
.menu-toggle{display:none;background:transparent;color:#fff;border:1px solid #fff;border-radius:4px;padding:6px 12px;font-size:15px}
.header-phone{font-family:Arial,sans-serif;font-weight:bold;background:#e36414;padding:8px 14px;border-radius:6px;text-decoration:none}
main{flex:1;padding:40px 32px;max-width:960px;margin:0 auto;width:100%}
.hero{background:#fff;border:1px solid #e5e1d8;border-radius:10px;padding:32px}
.hero-image{display:block;width:100%;max-width:800px;height:auto;border-radius:8px;margin:16px 0}
.wp-block-button__link{display:inline-block;background:#0f4c5c;color:#fff;padding:12px 22px;border-radius:6px;text-decoration:none;font-family:Arial,sans-serif}
.offer{margin-top:24px;padding:12px;background:#fff3cd;border-radius:6px;font-family:Arial,sans-serif}
form.wpcf7-form label{display:block;margin-top:10px;font-family:Arial,sans-serif} form.wpcf7-form input,form.wpcf7-form textarea{width:100%;max-width:480px;padding:8px;font-size:15px}
form.wpcf7-form button{margin-top:12px;background:#0f4c5c;color:#fff;border:0;padding:10px 18px;border-radius:6px;font-size:15px}
.wpcf7-response-output{margin-top:16px;padding:12px;border:2px solid #46b450;border-radius:6px;font-family:Arial,sans-serif}
footer.wp-block-template-part{background:#1d2b36;color:#d9e2ec;padding:24px 32px;font-family:Arial,sans-serif}
footer a{color:#fff}
@media (max-width:600px){header.wp-block-template-part{flex-direction:column;align-items:flex-start;padding:12px 16px} main{padding:20px 16px}
 .menu-toggle{display:inline-block} nav ul{display:none;flex-direction:column;gap:10px;margin-top:8px} nav.is-open ul{display:flex}}
"""

MENU_JS = """document.addEventListener('click',e=>{const b=e.target.closest('.menu-toggle');if(!b)return;
const n=document.getElementById(b.getAttribute('aria-controls'));const o=b.getAttribute('aria-expanded')!=='true';
b.setAttribute('aria-expanded',String(o));n.classList.toggle('is-open',o);});"""


def layout(state: dict, title: str, body: str) -> str:
    s = {k: escape(str(v)) for k, v in state.items() if not isinstance(v, list)}
    offer = ["Free whitening consult this week", "New patients: 10% off first visit", "Ask about Saturday appointments"][
        int(time.time() // 7) % 3
    ]
    menu = "".join(f'<li><a href="{escape(i["href"])}">{escape(i["label"])}</a></li>' for i in state["menu_items"] if i.get("label"))
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(title)} – {s['site_name']}</title><style>{CSS}</style></head>
<body class="wp-theme-twentytwentyfour"><div class="wp-site-blocks">
<header class="wp-block-template-part" role="banner">
  <a class="site-brand custom-logo-link" href="/"><img class="custom-logo" src="{s['logo_src']}" alt="{s['logo_alt']}" width="48" height="48"><span class="site-title">{s['site_name']}</span></a>
  <nav class="wp-block-navigation" id="primary-menu-nav" aria-label="Main">
    <button class="menu-toggle" type="button" aria-expanded="false" aria-controls="primary-menu-nav">Menu</button>
    <ul id="primary-menu">{menu}</ul></nav>
  <div class="wp-block-group"><a class="header-phone" href="{s['header_phone_href']}">{s['header_phone_text']}</a></div>
</header>
<main class="wp-block-group">{body}
  <p class="offer" data-dynamic="offer">Today: {escape(offer)}</p>
</main>
<footer class="wp-block-template-part" role="contentinfo">
  <p><strong>{s['site_name']}</strong> · 12 Harbour Street, Springfield</p>
  <p>Phone: <a class="footer-phone" href="{s['footer_phone_href']}">{s['footer_phone_text']}</a>
     · Email: <a class="footer-email" href="{s['footer_email_href']}">{s['footer_email_text']}</a></p>
  <p>Opening hours: {s['hours_text']}</p>
</footer></div><script>{MENU_JS}</script></body></html>"""


CONTACT_FORM = """<form class="wpcf7-form" id="contact-form" method="post" action="/contact/send">
<label for="your-name">Your name</label><input id="your-name" name="your-name" type="text" required>
<label for="your-email">Your email</label><input id="your-email" name="your-email" type="email" required>
<label for="your-message">Your message</label><textarea id="your-message" name="your-message" rows="4" required></textarea>
<button type="submit">Send message</button></form>"""

AJAX_FORM_JS = """document.getElementById('contact-form').addEventListener('submit', function (ev) {
  ev.preventDefault();
  var form = ev.target;
  fetch(form.action + '?ajax=1', {method: 'POST', body: new FormData(form)}).then(function (r) { return r.text(); }).then(function (msg) {
    var out = document.createElement('div'); out.className = 'wpcf7-response-output'; out.setAttribute('role', 'status');
    out.textContent = msg; form.replaceWith(out);
  });
});"""


def render_page(path: str, state: dict) -> str | None:
    s = {k: escape(str(v)) for k, v in state.items() if not isinstance(v, list)}
    if path == "/":
        body = f"""<section class="hero"><h1>Gentle dental care for the whole family</h1>
<figure class="wp-block-image"><img class="hero-image" src="{s['hero_src']}" alt="{s['hero_alt']}" width="800" height="360"></figure>
<p>Check-ups, cleaning, whitening and emergency appointments in Springfield.</p>
<div class="wp-block-buttons"><div class="wp-block-button"><a class="wp-block-button__link wp-element-button" href="{s['home_button_href']}">{s['home_button_label']}</a></div></div>
</section><h2>Why patients choose us</h2><ul><li>Same-week appointments</li><li>Evening slots on Thursdays</li><li>All major insurance accepted</li></ul>"""
        return layout(state, "Home", body)
    if path == "/services/":
        body = """<h1>Our services</h1><ul><li>Check-ups and cleaning</li><li>Teeth whitening</li><li>Fillings and crowns</li><li>Emergency dental care</li></ul>"""
        return layout(state, "Services", body)
    if path == "/contact/":
        body = f"""<h1>Contact us</h1><p class="contact-details">{s['contact_paragraph']}</p>
<p>12 Harbour Street, Springfield. Parking is available behind the clinic.</p>{CONTACT_FORM}"""
        if state.get("form_mode") == "ajax":
            body += f"<script>{AJAX_FORM_JS}</script>"
        return layout(state, "Contact", body)
    return None


def _public(request: Request, path: str) -> Response:
    state = load_state()
    if state.get("cache_enabled") and path in _cache:
        cached_at, html = _cache[path]
        return HTMLResponse(html, headers={"x-cache": "HIT", "age": str(int(time.time() - cached_at))})
    html = render_page(path, state)
    if html is None:
        return HTMLResponse("<h1>Not found</h1>", status_code=404)
    if state.get("cache_enabled"):
        _cache[path] = (time.time(), html)
    return HTMLResponse(html, headers={"x-cache": "MISS" if state.get("cache_enabled") else "BYPASS"})


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return _public(request, "/")


@app.get("/services/", response_class=HTMLResponse)
def services(request: Request):
    return _public(request, "/services/")


@app.get("/contact/", response_class=HTMLResponse)
def contact(request: Request):
    return _public(request, "/contact/")


@app.post("/contact/send", response_class=HTMLResponse)
async def contact_send(request: Request):
    form = await request.form()
    state = load_state()
    if state.get("form_delivery") == "on":
        deliver(
            {
                "id": uuid.uuid4().hex,
                "to": state["form_recipient"],
                "from_name": str(form.get("your-name", ""))[:200],
                "from_email": str(form.get("your-email", ""))[:200],
                "subject": f"New message from {state['site_name']} website",
                "body": str(form.get("your-message", ""))[:4000],
                "received_at": datetime.now(timezone.utc).isoformat(),
            }
        )
    # Like many misconfigured sites, the visitor sees success even when mail is not sent.
    if request.query_params.get("ajax"):
        return PlainTextResponse("Thank you for your message. It has been sent.")
    body = '<h1>Contact us</h1><div class="wpcf7-response-output" role="status">Thank you for your message. It has been sent.</div>'
    return HTMLResponse(layout(state, "Contact", body))


@app.get("/services")
def services_noslash():
    return RedirectResponse("/services/", status_code=301)


@app.get("/contact")
def contact_noslash():
    return RedirectResponse("/contact/", status_code=301)


@app.get("/wp-sitemap.xml")
def sitemap(request: Request):
    base = str(request.base_url).rstrip("/")
    urls = "".join(f"<url><loc>{base}{p}</loc></url>" for p in ("/", "/services/", "/contact/"))
    return Response(f'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>', media_type="application/xml")


# --- Simulated wp-admin -------------------------------------------------------

ADMIN_CSS = """body{margin:0;font-family:-apple-system,Segoe UI,Arial,sans-serif;background:#f0f0f1;color:#1d2327;display:flex;min-height:100vh}
#adminmenu{width:200px;background:#1d2327;color:#fff;padding:12px 0} #adminmenu a{display:block;color:#f0f0f1;padding:8px 16px;text-decoration:none} #adminmenu a:hover{background:#2c3338}
#adminmenu .sub{padding-left:28px;font-size:13px;color:#c3c4c7} .wrap{flex:1;padding:24px 32px}
.notice{background:#fff;border-left:4px solid #00a32a;padding:10px 14px;margin-bottom:16px} .card{background:#fff;padding:20px;border:1px solid #c3c4c7;max-width:680px}
label{display:block;font-weight:600;margin-top:14px} input[type=text],select{width:100%;padding:6px 8px;font-size:15px} button{margin-top:18px;background:#2271b1;color:#fff;border:0;padding:8px 16px;border-radius:3px;font-size:14px;cursor:pointer}
ul.parts a{font-size:16px} table.menu td{padding:4px 6px} .thumb{width:96px;height:auto;border:1px solid #ccc;margin:4px}"""


def admin_page(title: str, body: str, notice: str = "") -> HTMLResponse:
    n = f'<div class="notice">{escape(notice)}</div>' if notice else ""
    return HTMLResponse(f"""<!doctype html><html><head><meta charset="utf-8"><title>{escape(title)} ‹ WordPress</title><style>{ADMIN_CSS}</style></head><body>
<div id="adminmenu"><a href="/wp-admin/">Dashboard</a><a href="/wp-admin/site-editor.php">Appearance</a><a class="sub" href="/wp-admin/site-editor.php">Editor</a>
<a class="sub" href="/wp-admin/nav-menus.php">Menus</a><a href="/wp-admin/upload.php">Media</a><a class="sub" href="/wp-admin/media-new.php">Add New Media File</a>
<a href="/wp-admin/edit.php?post_type=page">Pages</a><a href="/wp-admin/admin.php?page=contact-form">Contact</a>
<a href="/wp-admin/options-general.php?page=cache">Settings</a><a class="sub" href="/wp-admin/options-general.php?page=cache">Cache</a></div>
<div class="wrap"><h1>{escape(title)}</h1>{n}{body}</div></body></html>""")


def _text_fields(state: dict, part: str) -> str:
    return "".join(
        f'<label for="{k}">{escape(lbl)}</label><input type="text" id="{k}" name="{k}" value="{escape(str(state[k]))}">' for k, lbl in FIELDS[part]
    )


def _image_field(state: dict, part: str) -> str:
    if part not in IMAGE_FIELDS:
        return ""
    key, label = IMAGE_FIELDS[part]
    options = "".join(
        f'<option value="/media/{escape(f)}" {"selected" if state[key] == "/media/" + f else ""}>{escape(f)}</option>' for f in media_files()
    )
    return f'<label for="{key}">{escape(label)}</label><select id="{key}" name="{key}">{options}</select>'


@app.get("/wp-admin/")
def admin_home():
    return admin_page("Dashboard", '<div class="card"><p>Welcome to WordPress (simulated for Feluda demos). Use Appearance &gt; Editor for header and footer, Appearance &gt; Menus for navigation, Media for images, Pages for page content and Contact for the contact form.</p></div>')


@app.get("/wp-admin/site-editor.php")
def site_editor(part: str | None = None, saved: int = 0):
    state = load_state()
    if part in ("header", "footer"):
        label = part.capitalize()
        return admin_page(
            f"Editor › Patterns › Template parts › {label}",
            f'<form class="card" method="post" action="/wp-admin/site-editor.php?part={part}">{_image_field(state, part)}{_text_fields(state, part)}<button type="submit">Save</button></form>',
            f"{label} template part updated." if saved else "",
        )
    return admin_page(
        "Editor › Patterns › Template parts",
        '<div class="card"><ul class="parts"><li><a href="/wp-admin/site-editor.php?part=header">Header</a></li><li><a href="/wp-admin/site-editor.php?part=footer">Footer</a></li></ul></div>',
    )


def _apply(state: dict, form, part: str) -> None:
    for key, _ in FIELDS.get(part, []):
        if key in form:
            state[key] = str(form[key]).strip()
    if part in IMAGE_FIELDS and IMAGE_FIELDS[part][0] in form:
        value = str(form[IMAGE_FIELDS[part][0]])
        if value.removeprefix("/media/") in media_files():
            state[IMAGE_FIELDS[part][0]] = value


@app.post("/wp-admin/site-editor.php")
async def site_editor_save(request: Request, part: str):
    form = await request.form()
    state = load_state()
    _apply(state, form, part)
    save_state(state)
    return RedirectResponse(f"/wp-admin/site-editor.php?part={part}&saved=1", status_code=303)


@app.get("/wp-admin/nav-menus.php")
def menus(saved: int = 0):
    state = load_state()
    items = state["menu_items"] + [{"label": "", "href": ""}] * (MENU_ROWS - len(state["menu_items"]))
    rows = "".join(
        f'<tr><td>{i + 1}</td><td><input type="text" name="label_{i}" aria-label="Menu item {i + 1} label" value="{escape(it["label"])}"></td>'
        f'<td><input type="text" name="href_{i}" aria-label="Menu item {i + 1} URL" value="{escape(it["href"])}"></td></tr>'
        for i, it in enumerate(items[:MENU_ROWS])
    )
    return admin_page(
        "Menus › Primary menu",
        f'<form class="card" method="post" action="/wp-admin/nav-menus.php"><p>Items appear in row order. Leave a row empty to remove it.</p>'
        f'<table class="menu"><tr><th>Position</th><th>Navigation label</th><th>URL</th></tr>{rows}</table><button type="submit">Save Menu</button></form>',
        "Primary menu has been updated." if saved else "",
    )


@app.post("/wp-admin/nav-menus.php")
async def menus_save(request: Request):
    form = await request.form()
    state = load_state()
    state["menu_items"] = [
        {"label": str(form.get(f"label_{i}", "")).strip(), "href": str(form.get(f"href_{i}", "")).strip()}
        for i in range(MENU_ROWS)
        if str(form.get(f"label_{i}", "")).strip()
    ]
    save_state(state)
    return RedirectResponse("/wp-admin/nav-menus.php?saved=1", status_code=303)


@app.get("/wp-admin/upload.php")
def media_library():
    thumbs = "".join(f'<figure style="display:inline-block"><img class="thumb" src="/media/{escape(f)}" alt=""><figcaption>{escape(f)}</figcaption></figure>' for f in media_files())
    return admin_page("Media Library", f'<div class="card">{thumbs}</div>')


@app.get("/wp-admin/media-new.php")
def media_new(uploaded: str = ""):
    return admin_page(
        "Upload New Media",
        '<form class="card" method="post" action="/wp-admin/media-new.php" enctype="multipart/form-data"><label for="file">Select file (PNG, JPEG or WebP)</label>'
        '<input type="file" id="file" name="file" accept="image/png,image/jpeg,image/webp"><button type="submit">Upload</button></form>',
        f"Uploaded {uploaded}." if uploaded else "",
    )


@app.post("/wp-admin/media-new.php")
async def media_upload(request: Request):
    form = await request.form()
    up = form.get("file")
    if up is None or not getattr(up, "filename", ""):
        return RedirectResponse("/wp-admin/media-new.php", status_code=303)
    data = await up.read()
    from PIL import Image

    try:
        img = Image.open(io.BytesIO(data))
        fmt = img.format.lower()
    except Exception:
        return admin_page("Upload New Media", "<p>That file is not an image.</p>")
    stem = re.sub(r"[^a-z0-9-]+", "-", Path(up.filename).stem.lower()).strip("-") or "upload"
    ext = {"jpeg": "jpg"}.get(fmt, fmt)
    name = f"{stem}.{ext}"
    i = 1
    while (MEDIA_DIR / name).exists():
        i += 1
        name = f"{stem}-{i}.{ext}"
    media_files()
    (MEDIA_DIR / name).write_bytes(data[: 8 * 1024 * 1024])
    return RedirectResponse(f"/wp-admin/media-new.php?uploaded={name}", status_code=303)


@app.get("/wp-admin/edit.php")
def pages_list(post_type: str = "page"):
    return admin_page(
        "Pages",
        '<div class="card"><ul class="parts"><li><a href="/wp-admin/post.php?page=home">Home</a></li><li><a href="/wp-admin/post.php?page=contact">Contact</a></li></ul></div>',
    )


@app.get("/wp-admin/post.php")
def page_editor(page: str, saved: int = 0):
    state = load_state()
    if page not in ("home", "contact"):
        return admin_page("Pages", "<p>Unknown page.</p>")
    return admin_page(
        f"Edit Page › {page.capitalize()}",
        f'<form class="card" method="post" action="/wp-admin/post.php?page={page}">{_image_field(state, page)}{_text_fields(state, page)}<button type="submit">Update</button></form>',
        "Page updated." if saved else "",
    )


@app.post("/wp-admin/post.php")
async def page_save(request: Request, page: str):
    form = await request.form()
    state = load_state()
    _apply(state, form, page)
    save_state(state)
    return RedirectResponse(f"/wp-admin/post.php?page={page}&saved=1", status_code=303)


@app.get("/wp-admin/admin.php")
def contact_settings(page: str = "contact-form", saved: int = 0):
    state = load_state()
    on = "selected" if state["form_delivery"] == "on" else ""
    off = "selected" if state["form_delivery"] != "on" else ""
    return admin_page(
        "Contact › Contact form",
        f'<form class="card" method="post" action="/wp-admin/admin.php?page=contact-form">'
        f'<label for="form_recipient">Send notifications to</label><input type="text" id="form_recipient" name="form_recipient" value="{escape(state["form_recipient"])}">'
        f'<label for="form_delivery">Mail delivery</label><select id="form_delivery" name="form_delivery"><option value="on" {on}>On</option><option value="off" {off}>Off</option></select>'
        f'<button type="submit">Save Changes</button></form>',
        "Contact form settings saved." if saved else "",
    )


@app.post("/wp-admin/admin.php")
async def contact_settings_save(request: Request, page: str = "contact-form"):
    form = await request.form()
    state = load_state()
    state["form_recipient"] = str(form.get("form_recipient", state["form_recipient"])).strip()
    state["form_delivery"] = "on" if form.get("form_delivery") == "on" else "off"
    save_state(state)
    return RedirectResponse("/wp-admin/admin.php?page=contact-form&saved=1", status_code=303)


@app.get("/wp-admin/options-general.php")
def cache_settings(page: str = "cache", purged: int = 0):
    state = load_state()
    on = "enabled" if state.get("cache_enabled") else "disabled"
    return admin_page(
        "Settings › Cache",
        f'<form class="card" method="post" action="/wp-admin/options-general.php?page=cache"><p>Page cache is <strong>{on}</strong>.</p><button type="submit">Purge all caches</button></form>',
        "All caches purged." if purged else "",
    )


@app.post("/wp-admin/options-general.php")
def cache_purge(page: str = "cache"):
    _cache.clear()
    return RedirectResponse("/wp-admin/options-general.php?page=cache&purged=1", status_code=303)


# --- Test control and test inbox (scenario scripts and evals only) --------------------


@app.get("/__demo/inbox")
def demo_inbox(contains: str = ""):
    """Test-inbox connector endpoint: messages whose body contains ``contains``."""
    return JSONResponse([m for m in load_inbox() if contains in m.get("body", "")])


@app.get("/__demo/state")
def demo_state():
    return JSONResponse(load_state())


@app.post("/__demo/set")
async def demo_set(request: Request):
    changes = await request.json()
    state = load_state()
    unknown = [k for k in changes if k not in DEFAULT_STATE]
    if unknown:
        return JSONResponse({"error": f"unknown keys {unknown}"}, status_code=400)
    state.update(changes)
    save_state(state)
    if not state.get("cache_enabled"):
        _cache.clear()
    return JSONResponse(state)


@app.post("/__demo/reset")
def demo_reset():
    save_state(json.loads(json.dumps(DEFAULT_STATE)))
    with _lock:
        INBOX_FILE.write_text("[]", encoding="utf-8")
    _cache.clear()
    return JSONResponse(DEFAULT_STATE)


@app.get("/__demo/redirect")
def demo_redirect(to: str):
    """Redirects anywhere: lets tests prove the capture guard checks every redirect hop."""
    return RedirectResponse(to, status_code=302)


@app.get("/__demo/health")
def health():
    return PlainTextResponse(datetime.now(timezone.utc).isoformat())
