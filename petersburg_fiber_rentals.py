#!/usr/bin/env python3
"""Find Petersburg, IN rentals and keep only addresses with Frontier fiber.

Listings come from the Craigslist search for Petersburg, IN 47567, over plain HTTP.
Frontier is checked in one Chrome window
on the official availability page (https://frontier.com/buy). Images, video,
and fonts are not downloaded. Addresses already checked are read from a local
cache, and the run stops if Frontier blocks the session instead of retrying
every listing.

    python petersburg_fiber_rentals.py

Uses the Google Chrome already installed on the machine. Playwright itself is
required; a second Chromium download is not.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

# ---------------------------------------------------------------------------
# Run settings. Change these instead of hunting through the rest of the file.
# ---------------------------------------------------------------------------

MAX_MONTHLY_RENT = 1100
CITY = "Petersburg"
STATE = "IN"
ZIP_CODE = "47567"
SEARCH_RADIUS_MILES = 8
OUTPUT_CSV = "fiber_rentals.csv"
CACHE_PATH = ".fiber_check_cache.json"
MIN_DELAY_SECONDS = 3.0
MAX_DELAY_SECONDS = 7.0
ADDRESS_CHECK_TIMEOUT_SECONDS = 35
CACHE_MAX_AGE_HOURS = 24 * 7
MAX_LISTINGS = 25

FRONTIER_BUY_URL = "https://frontier.com/buy"
CRAIGSLIST_SEARCH = (
    "https://www.craigslist.org/search/city/petersburg-in"
    "?cat=apa&postal={zip}&radius={radius}"
)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36"
)

CSV_COLUMNS = (
    "Address",
    "Price",
    "Beds/Baths",
    "Max Confirmed Fiber Speed",
    "Listing URL",
)

# Named symmetrical fiber tiers sold on the Frontier buy flow.
# Longer names are listed first so "Fiber 1 Gig" is not read as "Fiber 1".
FIBER_TIERS: tuple[tuple[re.Pattern[str], int, str], ...] = (
    (re.compile(r"fiber\s*7\s*gig", re.I), 7000, "Fiber 7 Gig"),
    (re.compile(r"fiber\s*5\s*gig", re.I), 5000, "Fiber 5 Gig"),
    (re.compile(r"fiber\s*2\s*gig", re.I), 2000, "Fiber 2 Gig"),
    (re.compile(r"fiber\s*1\s*gig|\bfiber\s*gig\b", re.I), 1000, "Fiber 1 Gig"),
    (re.compile(r"fiber\s*500\b", re.I), 500, "Fiber 500"),
    (re.compile(r"fiber\s*200\b", re.I), 200, "Fiber 200"),
)

TIER_CODES = {
    "fiber7gig": (7000, "Fiber 7 Gig"),
    "fiber5gig": (5000, "Fiber 5 Gig"),
    "fiber2gig": (2000, "Fiber 2 Gig"),
    "fiber1gig": (1000, "Fiber 1 Gig"),
    "fibergig": (1000, "Fiber 1 Gig"),
    "fiber500": (500, "Fiber 500"),
    "fiber200": (200, "Fiber 200"),
}

PRODUCT_NAME_KEYS = {
    "name",
    "productname",
    "displayname",
    "offername",
    "productcode",
    "offercode",
    "sku",
    "title",
    "label",
    "dataspeed",
    "downloadspeed",
    "speed",
    "broadbandtype",
    "technology",
}

SKIP_JSON_KEYS = {
    "legal",
    "disclaimer",
    "footnote",
    "terms",
    "description",
    "tooltip",
    "html",
    "fineprint",
}

COPPER_TECH = {"COPPER", "DSL", "ADSL", "VDSL", "IPDSL", "BONDED"}
FIBER_TECH = {"FIBER", "FTTH", "GPON", "XGS", "XGSPON", "FIBER_OPTIC"}

STREET_SUFFIXES = {
    "street": "st",
    "st": "st",
    "avenue": "ave",
    "ave": "ave",
    "road": "rd",
    "rd": "rd",
    "drive": "dr",
    "dr": "dr",
    "lane": "ln",
    "ln": "ln",
    "court": "ct",
    "ct": "ct",
    "boulevard": "blvd",
    "blvd": "blvd",
    "place": "pl",
    "pl": "pl",
    "circle": "cir",
    "cir": "cir",
    "highway": "hwy",
    "hwy": "hwy",
    "north": "n",
    "south": "s",
    "east": "e",
    "west": "w",
}

log = logging.getLogger("fiber_rentals")


@dataclass
class Listing:
    address: str
    price: int
    beds: str
    baths: str
    url: str

    @property
    def beds_baths(self) -> str:
        beds = self.beds if self.beds else "n/a"
        baths = self.baths if self.baths else "n/a"
        return f"{beds} bd / {baths} ba"


@dataclass
class FiberCheck:
    matched: bool
    max_speed: str
    reason: str


def parse_price(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    match = re.search(r"\d[\d,]*", str(value))
    if not match:
        return None
    return int(match.group(0).replace(",", ""))


def format_price(price: int) -> str:
    return f"${price:,}"


def _street_number(address: str) -> str:
    match = re.match(r"\s*(\d+)", address or "")
    return match.group(1) if match else ""


def normalize_street(address: str) -> str:
    text = (address or "").lower().replace(".", " ")
    text = re.sub(r"[#,]", " ", text)
    words = [STREET_SUFFIXES.get(word, word) for word in text.split()]
    return " ".join(words)


def within_budget(price: int | None, maximum: int) -> bool:
    return price is not None and price <= maximum


def _json_ld_blocks(html: str) -> list[Any]:
    blocks: list[Any] = []
    for raw in re.findall(
        r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
        html,
        flags=re.I | re.S,
    ):
        try:
            blocks.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    return blocks


def _walk_nodes(node: Any) -> Iterable[dict[str, Any]]:
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk_nodes(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk_nodes(value)


def parse_search_results(html: str) -> list[dict[str, str]]:
    """Craigslist search cards. Street addresses are filled in from the listing page."""
    results: list[dict[str, str]] = []
    for match in re.finditer(
        r'<li class="cl-static-search-result"[^>]*>(.*?)</li>',
        html,
        flags=re.I | re.S,
    ):
        block = match.group(1)
        href = re.search(r'href="([^"]+)"', block)
        title = re.search(r'class="title"[^>]*>(.*?)</div>', block, flags=re.S)
        price = re.search(r'class="price"[^>]*>(.*?)</div>', block, flags=re.S)
        if not href:
            continue
        results.append(
            {
                "url": href.group(1).strip(),
                "title": re.sub(r"\s+", " ", title.group(1)).strip() if title else "",
                "price": re.sub(r"<[^>]+>", "", price.group(1)).strip() if price else "",
            }
        )
    return results


def _count_label(value: Any) -> str:
    if value is None or value == "":
        return ""
    text = str(value).strip()
    match = re.search(r"\d+(?:\.\d+)?", text)
    return match.group(0) if match else ""


def parse_listing_page(html: str, url: str) -> Listing | None:
    address = ""
    locality = ""
    postal = ""
    beds = ""
    baths = ""
    price: int | None = None

    for block in _json_ld_blocks(html):
        for node in _walk_nodes(block):
            postal_address = node.get("address")
            if isinstance(postal_address, dict) and postal_address.get("streetAddress"):
                address = str(postal_address.get("streetAddress") or "").strip()
                locality = str(postal_address.get("addressLocality") or "").strip()
                postal = str(postal_address.get("postalCode") or "").strip()
            if node.get("numberOfBedrooms") is not None:
                beds = _count_label(node.get("numberOfBedrooms")) or beds
            if node.get("numberOfBathroomsTotal") is not None:
                baths = _count_label(node.get("numberOfBathroomsTotal")) or baths
            if price is None and node.get("price") is not None:
                price = parse_price(node.get("price"))

    if not address:
        map_address = re.search(
            r'class="mapaddress"[^>]*>\s*([^<]+)',
            html,
            flags=re.I,
        )
        if map_address:
            address = map_address.group(1).strip()

    if price is None:
        price_tag = re.search(r'class="price"[^>]*>\s*\$?([\d,]+)', html, flags=re.I)
        if price_tag:
            price = parse_price(price_tag.group(1))

    if not beds or not baths:
        housing = re.search(
            r"(\d+(?:\.\d+)?)\s*BR\b.*?(\d+(?:\.\d+)?)\s*Ba\b",
            html,
            flags=re.I | re.S,
        )
        if housing:
            beds = beds or housing.group(1)
            baths = baths or housing.group(2)

    if not address or price is None:
        return None

    full = address
    if locality and locality.lower() not in full.lower():
        full = f"{full}, {locality}"
    if STATE.lower() not in full.lower():
        full = f"{full}, {STATE}"
    if postal and postal not in full:
        full = f"{full} {postal}"
    elif ZIP_CODE not in full and locality.lower() == CITY.lower():
        full = f"{full} {ZIP_CODE}"

    return Listing(
        address=re.sub(r"\s+", " ", full).strip(" ,"),
        price=price,
        beds=beds,
        baths=baths,
        url=url,
    )


def listing_is_petersburg(listing: Listing) -> bool:
    text = listing.address.lower()
    return ZIP_CODE in listing.address or CITY.lower() in text


def _http_get(url: str, timeout: float = 25) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", "replace")


def fetch_listings(maximum_rent: int, limit: int) -> list[Listing]:
    search_url = CRAIGSLIST_SEARCH.format(zip=ZIP_CODE, radius=SEARCH_RADIUS_MILES)
    log.info("Searching rentals: %s", search_url)
    try:
        html = _http_get(search_url)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        log.error("Listing search failed: %s", exc)
        return []

    cards = parse_search_results(html)
    if not cards:
        log.warning("No rental cards were found for %s %s.", CITY, ZIP_CODE)
        return []

    listings: list[Listing] = []
    seen_urls: set[str] = set()
    for card in cards:
        if len(listings) >= limit:
            break
        url = card["url"]
        if url in seen_urls:
            continue
        seen_urls.add(url)
        card_price = parse_price(card.get("price"))
        if card_price is not None and not within_budget(card_price, maximum_rent):
            log.info("Skipping over budget (%s): %s", format_price(card_price), card.get("title") or url)
            continue
        try:
            page = _http_get(url)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            log.error("Could not read listing %s: %s", url, exc)
            continue
        listing = parse_listing_page(page, url)
        if listing is None:
            log.error("Listing had no street address or rent, skipped: %s", url)
            continue
        if not within_budget(listing.price, maximum_rent):
            log.info("Skipping over budget (%s): %s", format_price(listing.price), listing.address)
            continue
        if not listing_is_petersburg(listing):
            log.info("Skipping outside %s %s: %s", CITY, ZIP_CODE, listing.address)
            continue
        listings.append(listing)
        time.sleep(random.uniform(0.4, 0.9))
    return listings


def _compact(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def tiers_in_text(text: str) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    seen: set[str] = set()
    compact = _compact(text)
    for code, (mbps, label) in TIER_CODES.items():
        if code in compact and label not in seen:
            found.append((mbps, label))
            seen.add(label)
    for pattern, mbps, label in FIBER_TIERS:
        if label not in seen and pattern.search(text):
            found.append((mbps, label))
            seen.add(label)
    return found


def _product_texts(node: Any, inherited_product: bool = False) -> Iterable[str]:
    if isinstance(node, dict):
        is_product = inherited_product or any(
            key.lower() in {"productid", "productcode", "offercode"} or key.lower() in PRODUCT_NAME_KEYS
            for key in node
        )
        pieces: list[str] = []
        for key, value in node.items():
            lowered = key.lower()
            if lowered in SKIP_JSON_KEYS:
                continue
            if isinstance(value, str) and (is_product or lowered in PRODUCT_NAME_KEYS):
                if lowered in PRODUCT_NAME_KEYS or lowered in {"productid", "productcode", "offercode"}:
                    pieces.append(value)
            else:
                yield from _product_texts(value, is_product)
        if pieces:
            yield " ".join(pieces)
    elif isinstance(node, list):
        for value in node:
            yield from _product_texts(value, inherited_product)


def _tech_available(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    value = payload.get("techAvailable") or payload.get("tech_available") or ""
    if not value and isinstance(payload.get("address"), dict):
        value = payload["address"].get("techAvailable") or ""
    return str(value or "").strip().upper()


def classify_frontier(serviceability: Any, products: Any = None) -> FiberCheck:
    """Match only an explicit Frontier fiber tier for this address.

    Copper, DSL, a future-fiber flag, and a generic 'unavailable' response
    are not matches. Plan names are read from the serviceability and product
    payloads the buy page returns, not from site-wide marketing menus.
    """
    tech = _tech_available(serviceability)
    success = True
    future_only = False
    if isinstance(serviceability, dict):
        if serviceability.get("success") is False:
            success = False
        future_only = bool(serviceability.get("isFutureFiberEligible")) and tech not in FIBER_TECH
        redirect = serviceability.get("redirect") or {}
        redirect_url = redirect.get("url", "") if isinstance(redirect, dict) else ""
        if redirect_url and "unserviceable" in redirect_url and tech not in FIBER_TECH:
            return FiberCheck(False, "", "Frontier has no service at this address")

    if tech in COPPER_TECH or tech.startswith("DSL") or "COPPER" in tech:
        return FiberCheck(False, "", f"Legacy copper/DSL ({tech or 'copper'}), not fiber")
    if not success and tech not in FIBER_TECH:
        message = ""
        if isinstance(serviceability, dict):
            message = str(serviceability.get("userErrorMessage") or "")
        return FiberCheck(False, "", message or "Address is not serviceable")

    texts = list(_product_texts(products)) + list(_product_texts(serviceability))
    found: list[tuple[int, str]] = []
    for text in texts:
        found.extend(tiers_in_text(text))

    unique: dict[str, int] = {}
    for mbps, label in found:
        unique[label] = mbps
    if not unique:
        if tech in FIBER_TECH:
            return FiberCheck(False, "", "Fiber plant reported, but no speed tier was listed")
        if not success:
            message = ""
            if isinstance(serviceability, dict):
                message = str(serviceability.get("userErrorMessage") or "")
            return FiberCheck(False, "", message or "Address is not serviceable")
        if future_only:
            return FiberCheck(False, "", "Only future fiber eligibility was reported")
        return FiberCheck(False, "", "No explicit fiber tier was returned")

    if tech and tech not in FIBER_TECH and "FIBER" not in tech:
        return FiberCheck(False, "", f"Tiers were listed under non-fiber technology {tech}")

    label, mbps = max(unique.items(), key=lambda item: item[1])
    return FiberCheck(True, label, f"{label} ({mbps} Mbps)")


def choose_prediction(candidates: list[dict[str, Any]], query: str) -> dict[str, Any] | None:
    number = _street_number(query)
    if not number:
        return None
    wanted_zip = ZIP_CODE
    best: tuple[int, dict[str, Any]] | None = None
    street = normalize_street(query)
    for candidate in candidates:
        address = candidate.get("address") if isinstance(candidate.get("address"), dict) else {}
        parsed = candidate.get("parsedAddress") if isinstance(candidate.get("parsedAddress"), dict) else {}
        line = str(address.get("addressLine1") or candidate.get("addressLine1") or "")
        city = str(address.get("city") or candidate.get("city") or "")
        postal = str(
            parsed.get("zipCodeBase")
            or address.get("zipCode")
            or candidate.get("zip")
            or ""
        )
        if _street_number(line) != number:
            continue
        score = 100
        if postal.startswith(wanted_zip):
            score += 40
        if city.lower() == CITY.lower():
            score += 30
        if candidate.get("inFootprint") is True:
            score += 20
        if candidate.get("isParent") is True:
            score -= 25
        if normalize_street(line) and normalize_street(line) in street:
            score += 15
        if best is None or score > best[0]:
            best = (score, candidate)
    return best[1] if best else None


def load_cache(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def cache_is_fresh(entry: dict[str, Any], now: datetime | None = None) -> bool:
    stamp = entry.get("checked_at")
    if not stamp:
        return False
    try:
        checked = datetime.fromisoformat(stamp)
    except ValueError:
        return False
    if checked.tzinfo is None:
        checked = checked.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    age = current - checked
    return age.total_seconds() <= CACHE_MAX_AGE_HOURS * 3600


def save_cache(path: Path, cache: dict[str, Any]) -> None:
    path.write_text(json.dumps(cache, indent=2, sort_keys=True))


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(CSV_COLUMNS))
        writer.writeheader()
        writer.writerows(rows)


def format_table(rows: list[dict[str, str]]) -> str:
    if not rows:
        return "No listings confirmed with Frontier fiber."
    headers = list(CSV_COLUMNS)
    widths = [len(header) for header in headers]
    for row in rows:
        for index, header in enumerate(headers):
            widths[index] = max(widths[index], len(row.get(header, "")))
    line = "  ".join(header.ljust(widths[index]) for index, header in enumerate(headers))
    rule = "  ".join("-" * widths[index] for index in range(len(headers)))
    body = [
        "  ".join(row.get(header, "").ljust(widths[index]) for index, header in enumerate(headers))
        for row in rows
    ]
    return "\n".join([line, rule, *body])


class FrontierChecker:
    """Drive https://frontier.com/buy once and read the address-check responses."""

    def __init__(self, timeout_seconds: int = ADDRESS_CHECK_TIMEOUT_SECONDS) -> None:
        self.timeout_ms = int(timeout_seconds * 1000)
        self._playwright: Any = None
        self._browser: Any = None
        self._page: Any = None
        self._captured: dict[str, Any] = {}
        self.blocked = False

    def __enter__(self) -> "FrontierChecker":
        from playwright.sync_api import sync_playwright

        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(
            channel="chrome",
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
            ],
        )
        context = self._browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1366, "height": 768},
            locale="en-US",
            timezone_id="America/Indiana/Vincennes",
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        )
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        context.route("**/*", self._route)
        context.on("response", self._on_response)
        self._page = context.new_page()
        self._page.on("dialog", lambda dialog: dialog.dismiss())
        return self

    def __exit__(self, *_exc: object) -> None:
        try:
            if self._browser is not None:
                self._browser.close()
        finally:
            if self._playwright is not None:
                self._playwright.stop()

    @staticmethod
    def _route(route: Any) -> None:
        request = route.request
        url = request.url.lower()
        resource = request.resource_type
        blocked_host = any(
            host in url
            for host in (
                "google-analytics",
                "googletagmanager",
                "doubleclick",
                "facebook",
                "newrelic",
                "adobedtm",
                "cookielaw",
                "demdex",
                "omtrdc",
            )
        )
        if resource in {"image", "media", "font"} or blocked_host:
            route.abort()
            return
        route.continue_()

    def _on_response(self, response: Any) -> None:
        url = response.url.lower()
        method = response.request.method.upper()
        if "serviceability/predictive" in url and method == "GET":
            self._captured["predictive_status"] = response.status
            if response.status == 200:
                self._store_json(response, "predictive")
            elif response.status in {401, 403, 429}:
                self._captured["blocked_status"] = response.status
        elif "/serviceability" in url and method == "POST":
            self._captured["serviceability_status"] = response.status
            if response.status == 200:
                self._store_json(response, "serviceability")
            elif response.status in {401, 403, 429}:
                self._captured["blocked_status"] = response.status
        elif "/products/get" in url and method == "POST" and response.status == 200:
            self._store_json(response, "products")

    def _store_json(self, response: Any, key: str) -> None:
        try:
            self._captured[key] = response.json()
        except Exception as exc:  # noqa: BLE001 - keep the run moving
            self._captured[f"{key}_error"] = f"{response.status}: {exc}"

    def _open_checker(self) -> None:
        assert self._page is not None
        self._page.goto(FRONTIER_BUY_URL, wait_until="domcontentloaded", timeout=self.timeout_ms)
        title = ""
        body = ""
        try:
            title = self._page.title()
            body = self._page.inner_text("body", timeout=5000)
        except Exception:  # noqa: BLE001
            body = ""
        blocked_page = "403" in title or "forbidden" in title.lower() or "access denied" in body.lower()
        if blocked_page or "verizon information security" in body.lower():
            self.blocked = True
            raise RuntimeError("Frontier blocked this session")
        for label in ("Accept All", "Accept", "I Agree", "Got it"):
            button = self._page.get_by_role("button", name=re.compile(f"^{label}$", re.I))
            try:
                if button.count() and button.first.is_visible():
                    button.first.click(timeout=1500)
                    break
            except Exception:  # noqa: BLE001
                continue
        self._page.locator("#street-address").wait_for(timeout=self.timeout_ms)

    def check(self, address: str) -> FiberCheck:
        if self.blocked:
            return FiberCheck(False, "", "Skipped because Frontier blocked the session")
        assert self._page is not None
        self._captured = {}
        try:
            self._open_checker()
            field = self._page.locator("#street-address")
            field.click(timeout=5000)
            field.fill("")
            field.press_sequentially(address, delay=30)
            self._wait_for_suggestions()
            if self._captured.get("blocked_status") in {401, 403, 429}:
                self.blocked = True
                return FiberCheck(False, "", "Frontier blocked the address lookup")
            predictive = self._captured.get("predictive")
            candidates = predictive if isinstance(predictive, list) else []
            chosen = choose_prediction(candidates, address) if candidates else None
            option = self._matching_option(chosen, address)
            if option is None:
                lines = []
                for candidate in candidates[:5]:
                    address_node = candidate.get("address") if isinstance(candidate, dict) else None
                    if isinstance(address_node, dict):
                        lines.append(str(address_node.get("addressLine1") or ""))
                    elif isinstance(candidate, dict):
                        lines.append(str(candidate.get("addressLine1") or candidate)[:80])
                log.info(
                    "No exact suggestion for %s. Predictive hits: %s. Blocked status: %s.",
                    address,
                    lines or "none",
                    self._captured.get("blocked_status") or self._captured.get("predictive_error") or "none",
                )
                return FiberCheck(False, "", "No exact Frontier address suggestion")
            option.click(timeout=5000)
            submit = self._page.locator("button.btn-check-address")
            if submit.count() == 0:
                submit = self._page.get_by_role("button", name=re.compile("check availability", re.I))
            submit.first.click(timeout=5000)
            self._wait_for_result()
        except Exception as exc:  # noqa: BLE001
            message = str(exc).splitlines()[0][:240]
            if self.blocked or "blocked" in message.lower() or "access denied" in message.lower():
                self.blocked = True
                return FiberCheck(False, "", "Frontier blocked this session")
            return FiberCheck(False, "", f"Check failed: {message}")

        if self._captured.get("blocked_status") in {401, 403, 429} and "serviceability" not in self._captured:
            self.blocked = True
            return FiberCheck(False, "", "Frontier blocked this session")

        try:
            url = self._page.url.lower()
        except Exception:  # noqa: BLE001
            url = ""
        if "unserviceable" in url and "serviceability" not in self._captured:
            return FiberCheck(False, "", "Frontier marked the address unserviceable")
        return classify_frontier(
            self._captured.get("serviceability"),
            self._captured.get("products"),
        )

    def _wait_for_suggestions(self) -> None:
        assert self._page is not None
        deadline = time.time() + 8
        while time.time() < deadline:
            if "predictive_status" in self._captured or isinstance(self._captured.get("predictive"), list):
                return
            options = self._page.locator("[class*='address-form__option'], [id*='option-']")
            if options.count():
                return
            self._page.wait_for_timeout(200)

    def _matching_option(self, chosen: dict[str, Any] | None, query: str) -> Any:
        assert self._page is not None
        options = self._page.locator("[class*='address-form__option'], [id*='option-']")
        try:
            options.first.wait_for(timeout=8000)
        except Exception:  # noqa: BLE001
            return None
        count = min(options.count(), 8)
        seen: list[str] = []
        wanted_line = ""
        if chosen:
            address = chosen.get("address") if isinstance(chosen.get("address"), dict) else {}
            wanted_line = str(address.get("addressLine1") or "")
        number = _street_number(wanted_line or query)
        for index in range(count):
            item = options.nth(index)
            try:
                text = item.inner_text(timeout=1000)
            except Exception:  # noqa: BLE001
                continue
            seen.append(re.sub(r"\s+", " ", text).strip())
            if wanted_line and normalize_street(wanted_line) in normalize_street(text):
                return item
            if number and _street_number(text) == number and CITY.lower() in text.lower():
                return item
        if seen:
            log.info("Address suggestions on the page: %s", seen)
        return None

    def _wait_for_result(self) -> None:
        assert self._page is not None
        deadline = time.time() + (self.timeout_ms / 1000)
        while time.time() < deadline:
            if "serviceability" in self._captured or "serviceability_error" in self._captured:
                break
            if "unserviceable" in self._page.url.lower():
                return
            self._page.wait_for_timeout(250)
        else:
            raise TimeoutError("Frontier address check timed out")
        # The plan list arrives in a second call. Give it a short beat, then move on.
        extra = time.time() + 8
        while time.time() < extra and "products" not in self._captured:
            self._page.wait_for_timeout(250)


def pause_between_checks() -> None:
    time.sleep(random.uniform(MIN_DELAY_SECONDS, MAX_DELAY_SECONDS))


def run(maximum_rent: int, output: Path, cache_path: Path, refresh: bool, limit: int) -> int:
    listings = fetch_listings(maximum_rent, limit)
    log.info("%s listings at or under %s", len(listings), format_price(maximum_rent))
    cache = {} if refresh else load_cache(cache_path)
    matches: list[dict[str, str]] = []
    errors = 0
    blocked = False

    if not listings:
        write_csv(output, matches)
        print(format_table(matches))
        print(f"\nWrote {output}")
        return 0

    try:
        with FrontierChecker() as checker:
            for index, listing in enumerate(listings):
                key = normalize_street(listing.address)
                cached = cache.get(key)
                if isinstance(cached, dict) and cache_is_fresh(cached):
                    result = FiberCheck(
                        bool(cached.get("matched")),
                        str(cached.get("max_speed") or ""),
                        str(cached.get("reason") or "cached"),
                    )
                    log.info("Cache %s — %s", listing.address, result.reason)
                else:
                    if index > 0:
                        pause_between_checks()
                    log.info("Checking Frontier for %s", listing.address)
                    try:
                        result = checker.check(listing.address)
                    except Exception as exc:  # noqa: BLE001
                        log.error("Unexpected error for %s: %s", listing.address, exc)
                        result = FiberCheck(False, "", f"Check failed: {exc}")
                    definitive = (
                        "blocked" not in result.reason.lower()
                        and not result.reason.startswith("Check failed")
                    )
                    if definitive:
                        cache[key] = {
                            "address": listing.address,
                            "matched": result.matched,
                            "max_speed": result.max_speed,
                            "reason": result.reason,
                            "checked_at": datetime.now(timezone.utc).isoformat(),
                        }
                        try:
                            save_cache(cache_path, cache)
                        except OSError as exc:
                            log.error("Could not write cache: %s", exc)
                    log.info("%s — %s", listing.address, result.reason)
                    if checker.blocked:
                        blocked = True
                        log.error("Stopping early so this run does not keep hitting Frontier.")
                        break
                if result.reason.startswith("Check failed"):
                    errors += 1
                if result.matched:
                    matches.append(
                        {
                            "Address": listing.address,
                            "Price": format_price(listing.price),
                            "Beds/Baths": listing.beds_baths,
                            "Max Confirmed Fiber Speed": result.max_speed,
                            "Listing URL": listing.url,
                        }
                    )
    except Exception as exc:  # noqa: BLE001
        log.error("Could not start Chrome: %s", exc)
        write_csv(output, matches)
        print(format_table(matches))
        print(f"\nWrote {output}")
        return 1

    write_csv(output, matches)
    print()
    print(format_table(matches))
    print(
        f"\nChecked {len(listings)} listing(s) at or under {format_price(maximum_rent)}. "
        f"{len(matches)} matched, {errors} check error(s)."
    )
    if blocked:
        print("Frontier blocked the address lookup from this network, so nothing was marked as fiber.")
    print(f"Wrote {output}")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Petersburg rentals with confirmed Frontier fiber.")
    parser.add_argument("--max-rent", type=int, default=MAX_MONTHLY_RENT, help="Skip rent above this amount.")
    parser.add_argument("--output", type=Path, default=Path(OUTPUT_CSV))
    parser.add_argument("--cache", type=Path, default=Path(CACHE_PATH))
    parser.add_argument("--refresh", action="store_true", help="Ignore the local address cache.")
    parser.add_argument("--limit", type=int, default=MAX_LISTINGS, help="Maximum listings to check.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = parse_args(argv)
    try:
        return run(args.max_rent, args.output, args.cache, args.refresh, args.limit)
    except KeyboardInterrupt:
        log.error("Stopped.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
