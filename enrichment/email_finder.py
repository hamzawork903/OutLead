"""
The email extraction engine — pure functions, no network. This is the tricky
part of the whole milestone, so it lives on its own and is unit-tested against
saved HTML samples.

It pulls emails out of a page four ways, because sites hide them four ways:
  1. mailto: links               (the honest case)
  2. plain text / regex          (email sitting in the HTML)
  3. Cloudflare "email protection" (data-cfemail hex — decoded here)
  4. obfuscation                 ("name [at] domain [dot] com", HTML entities)

Then it filters junk (image@2x.png, you@example.com, tracking noise) and ranks
what's left so we pick the *useful* email — a real business address over a
generic one.
"""

import html as html_module
import re
from urllib.parse import urljoin, urlparse

# A deliberately strict email pattern (needs a real TLD), so de-obfuscated
# prose like "meet at noon" can't accidentally form a "valid" email.
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,24}")

_MAILTO_RE = re.compile(r'mailto:([^"\'>?\s]+)', re.IGNORECASE)
_CFEMAIL_RE = re.compile(r'data-cfemail=["\']([0-9a-fA-F]+)["\']')
_HREF_RE = re.compile(r'href=["\']([^"\']+)["\']', re.IGNORECASE)

# Role/shared mailboxes — real, but lower value than a named person.
_ROLE_LOCALPARTS = {
    "info", "contact", "hello", "admin", "sales", "support", "office", "mail",
    "enquiries", "enquiry", "reception", "team", "booking", "bookings",
    "appointments", "help", "service", "hi", "care", "frontdesk",
}
_FREEMAIL = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "aol.com",
    "icloud.com", "live.com", "msn.com", "ymail.com",
}
# Obvious non-business noise.
_JUNK_DOMAINS = {
    "example.com", "example.org", "example.net", "domain.com", "yourdomain.com",
    "email.com", "test.com",
}
# Substrings that mark a domain as tooling/tracking, not a business inbox
# (Wix's Sentry error-reporting emails are the classic false positive).
_JUNK_DOMAIN_SUBSTR = ("wixpress.com", "sentry", "example.")
_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".bmp")
_JUNK_SUBSTRINGS = ("@2x", "@3x", "your@email", "name@", "user@", "email@example")
# A long all-hex local part is a machine token, never a real person's address.
_HEXHASH_LOCAL_RE = re.compile(r"^[0-9a-f]{20,}$")


# --------------------------------------------------------------- decode ----

def decode_cfemail(hex_str: str) -> str | None:
    """Decode a Cloudflare-obfuscated email. First byte is the XOR key; the
    rest are the email bytes XORed with it."""
    try:
        data = bytes.fromhex(hex_str)
        if len(data) < 2:
            return None
        key = data[0]
        return "".join(chr(b ^ key) for b in data[1:])
    except (ValueError, TypeError):
        return None


def deobfuscate(text: str) -> str:
    """Turn common "human, not a bot" spellings back into real emails.
    Bracketed and spaced forms of 'at' / 'dot' become @ / . — safe because
    only strings that then match EMAIL_RE survive extraction."""
    t = html_module.unescape(text)              # &#64; / &commat; -> @
    t = re.sub(r"\s*[\[\(\{]\s*(?:at|@)\s*[\]\)\}]\s*", "@", t, flags=re.I)
    t = re.sub(r"\s*[\[\(\{]\s*dot\s*[\]\)\}]\s*", ".", t, flags=re.I)
    t = re.sub(r"\s+at\s+", "@", t, flags=re.I)
    t = re.sub(r"\s+dot\s+", ".", t, flags=re.I)
    return t


# ------------------------------------------------------------- extract ----

def emails_from_html(page_html: str) -> set[str]:
    """Every reasonable email found in one page, cleaned and de-junked."""
    found: set[str] = set()

    for raw in _MAILTO_RE.findall(page_html):
        found.add(html_module.unescape(raw))

    for hex_str in _CFEMAIL_RE.findall(page_html):
        decoded = decode_cfemail(hex_str)
        if decoded:
            found.add(decoded)

    for match in EMAIL_RE.findall(deobfuscate(page_html)):
        found.add(match)

    cleaned = {_normalize(e) for e in found}
    return {e for e in cleaned if e and _is_valid(e) and not is_junk(e)}


def _normalize(email: str) -> str:
    return email.strip().strip(".,;:").lower()


def _is_valid(email: str) -> bool:
    return bool(EMAIL_RE.fullmatch(email)) and len(email) <= 254


def is_junk(email: str) -> bool:
    el = email.lower()
    if any(sub in el for sub in _JUNK_SUBSTRINGS):
        return True
    if el.endswith(_IMAGE_EXTS):
        return True
    local, _, domain = el.partition("@")
    if domain in _JUNK_DOMAINS:
        return True
    if any(sub in domain for sub in _JUNK_DOMAIN_SUBSTR):
        return True
    if _HEXHASH_LOCAL_RE.match(local):
        return True
    return False


# ---------------------------------------------------------------- rank ----

def score(email: str, site_domain: str | None) -> int:
    """Higher = more useful. Same-domain business address beats a role inbox
    beats a free-mail address."""
    local, _, domain = email.partition("@")
    points = 0
    if site_domain and domain == site_domain:
        points += 3                       # clearly this business's own address
    points += 1 if local in _ROLE_LOCALPARTS else 2   # named person > role
    if domain in _FREEMAIL:
        points -= 1
    return points


def pick_best(emails, site_domain: str | None):
    """Return (best_email, all_ranked_unique). best is None if empty."""
    ranked = sorted(set(emails), key=lambda e: (-score(e, site_domain), e))
    return (ranked[0] if ranked else None), ranked


# --------------------------------------------------------------- links ----

def contact_links(page_html: str, base_url: str, hints) -> list[str]:
    """Same-site pages whose URL hints they might hold contact details."""
    base_host = urlparse(base_url).netloc.lower()
    out, seen = [], set()
    for href in _HREF_RE.findall(page_html):
        low = href.lower()
        if low.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        if not any(hint in low for hint in hints):
            continue
        full = urljoin(base_url, href)
        host = urlparse(full).netloc.lower()
        if host and host != base_host:
            continue                      # stay on this business's own site
        if full not in seen:
            seen.add(full)
            out.append(full)
    return out


def domain_of(url: str) -> str | None:
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else (host or None)


# --------------------------------------------------------------- social ----
# Platforms we recognize, and the URL fragments that mean "this is a share
# button / widget / post, not a profile" (so we skip them).
SOCIAL_PLATFORMS = ("facebook", "instagram", "linkedin", "youtube", "tiktok", "twitter")

_SOCIAL_SKIP = {
    "facebook":  ("/sharer", "/plugins", "/dialog", "/share", "/tr", "/events/"),
    "twitter":   ("/intent", "/share", "/home", "/hashtag", "/search"),
    "instagram": ("/p/", "/explore", "/reel", "/reels", "/stories", "/tv/"),
    "linkedin":  ("/share", "/sharing", "/sharearticle", "/cws/"),
    "youtube":   ("/watch", "/embed", "/results", "/playlist"),
    "tiktok":    ("/video", "/tag", "/discover", "/music"),
}
# LinkedIn / YouTube are only useful at specific path prefixes (a real profile).
_LINKEDIN_OK = ("/company/", "/in/", "/school/", "/showcase/")
_YOUTUBE_OK = ("/channel/", "/c/", "/user/", "/@")

# Website-builder / platform brand accounts that show up in template footers —
# they belong to Wix/Squarespace/etc., NOT the business. Reject them. Also the
# social networks' own handles, and empty facebook profile.php links.
_BRAND_HANDLES = {
    "wix", "wixcom", "wixlounge", "squarespace", "wordpress", "wordpressdotcom",
    "automattic", "shopify", "godaddy", "weebly", "webflow", "duda", "jimdo",
    "site123", "strikingly", "bigcommerce", "clickfunnels", "google",
    "googlemybusiness", "meta", "facebook", "instagram", "twitter", "tiktok",
    "linkedin", "youtube",
}
_JUNK_HANDLES = {"profilephp", "pages", "pg", "people", "sharer", "share",
                 "home", "login", "signup", "help", "about"}


def _social_handle(platform: str, path: str) -> str:
    """The identifying slug of a profile URL, normalized (letters+digits only)."""
    segs = [s for s in path.split("/") if s]
    if not segs:
        return ""
    if platform == "tiktok":
        h = segs[0].lstrip("@")
    elif platform == "youtube":
        if segs[0].startswith("@"):
            h = segs[0][1:]
        elif segs[0] in ("user", "c", "channel") and len(segs) > 1:
            h = segs[1]
        else:
            h = segs[0]
    elif platform == "linkedin":
        h = segs[1] if len(segs) > 1 else segs[0]
    else:  # facebook / instagram / twitter -> first path segment
        h = segs[0]
    return re.sub(r"[^a-z0-9]", "", h.lower())


def _classify_social(url: str):
    """Return (platform, cleaned_url) if `url` is a real *business* social
    profile, else None. Rejects share widgets, posts, and builder-brand
    accounts (e.g. facebook.com/wix on a Wix-built site)."""
    clean = url.split("?")[0].split("#")[0].rstrip("/")
    m = re.match(r"https?://(?:[\w-]+\.)?([\w.-]+\.\w+)(/.*)?$", clean, re.I)
    if not m:
        return None
    host, path = m.group(1).lower(), (m.group(2) or "").lower()

    def blocked(platform):
        return any(frag in path for frag in _SOCIAL_SKIP.get(platform, ()))

    platform = None
    if "facebook.com" in host or "fb.com" in host:
        if len(path) > 1 and not blocked("facebook"):
            platform = "facebook"
    elif host in ("twitter.com", "x.com") or host.endswith((".twitter.com", ".x.com")):
        if len(path) > 1 and not blocked("twitter"):
            platform = "twitter"
    elif "instagram.com" in host:
        if len(path) > 1 and not blocked("instagram"):
            platform = "instagram"
    elif "linkedin.com" in host:
        if any(k in path for k in _LINKEDIN_OK):
            platform = "linkedin"
    elif "youtu.be" in host:
        if len(path) > 1:
            platform = "youtube"
    elif "youtube.com" in host:
        if any(k in path for k in _YOUTUBE_OK):
            platform = "youtube"
    elif "tiktok.com" in host:
        if "/@" in path:
            platform = "tiktok"
    if not platform:
        return None

    handle = _social_handle(platform, path)
    if handle in _BRAND_HANDLES or handle in _JUNK_HANDLES:
        return None                    # builder/platform brand, not the business
    return (platform, clean)


_TEL_RE = re.compile(r'href=["\']tel:([+0-9().\-\s]{7,20})["\']', re.IGNORECASE)
# Text phone: optional +1, area code (maybe parens), then a SEPARATOR before each
# group — requiring separators keeps plain digit runs (IDs, prices) out.
_PHONE_TEXT_RE = re.compile(
    r"(?<!\d)(?:\+?1[\s.\-]?)?\(?\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4}(?!\d)")


def phones_from_html(page_html: str, limit: int = 5) -> list:
    """Phone numbers from a page: tel: links first (reliable), then formatted
    numbers in the text. Deduped by their digits; returns display forms."""
    found: dict[str, str] = {}   # digits -> display

    for raw in _TEL_RE.findall(page_html):
        disp = " ".join(raw.split())
        digits = re.sub(r"\D", "", disp)
        if 7 <= len(digits) <= 15:
            found.setdefault(digits.lstrip("1") if len(digits) == 11 else digits, disp)

    for raw in _PHONE_TEXT_RE.findall(page_html):
        disp = " ".join(raw.split())
        digits = re.sub(r"\D", "", disp)
        if 10 <= len(digits) <= 11:
            found.setdefault(digits.lstrip("1") if len(digits) == 11 else digits, disp)

    return list(found.values())[:limit]


def social_links_from_html(page_html: str, base_url: str | None = None) -> dict:
    """First real profile URL per platform found in the page (dict keyed by
    platform). Share buttons, posts, and widgets are ignored."""
    found: dict[str, str] = {}
    for href in _HREF_RE.findall(page_html):
        full = urljoin(base_url, href) if base_url else href
        if not full.lower().startswith("http"):
            continue
        hit = _classify_social(full)
        if hit:
            platform, cleaned = hit
            found.setdefault(platform, cleaned)   # first occurrence wins
    return found
