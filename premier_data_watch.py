#!/usr/bin/env python3
"""Check two Premier Data lists and send a webhook only when one changes.

This file does not log in. It uses a session cookie from a browser that is
already signed in to mypremierdata.com. Fill the open settings in
premier_data_config.json. Anything still blank is skipped, not guessed.

    python3 premier_data_watch.py

Exit codes:
    0  finished (no change, or a change was sent)
    2  config is missing something required
    3  the site sent the browser back to the sign-in page
    1  some other error
"""

from __future__ import annotations

import hashlib
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

CONFIG_PATH = Path("premier_data_config.json")
SIGN_IN_MARKERS = ("signin.aspx", "you are not signed-in", "you are not signed in")
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)


class ConfigError(Exception):
    pass


class SessionExpired(Exception):
    pass


class TableParser(HTMLParser):
    """Pull text tables out of an old HTML page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self._table_stack: list[list[list[str]]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in {"script", "style"}:
            self._skip += 1
            return
        if self._skip:
            return
        if tag == "table":
            self._table_stack.append([])
        elif tag == "tr" and self._table_stack:
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style"} and self._skip:
            self._skip -= 1
            return
        if self._skip:
            return
        if tag in {"td", "th"} and self._cell is not None and self._row is not None:
            text = " ".join("".join(self._cell).split())
            self._row.append(text)
            self._cell = None
        elif tag == "tr" and self._row is not None and self._table_stack:
            if any(cell.strip() for cell in self._row):
                self._table_stack[-1].append(self._row)
            self._row = None
        elif tag == "table" and self._table_stack:
            rows = self._table_stack.pop()
            if rows:
                self.tables.append(rows)

    def handle_data(self, data: str) -> None:
        if self._skip or self._cell is None:
            return
        self._cell.append(data)


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"Missing {path.name}. Copy premier_data_config.example.json and fill the open fields.")
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path.name} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path.name} must be a JSON object.")
    return data


def required_text(config: dict[str, Any], key: str) -> str:
    value = str(config.get(key) or "").strip()
    if not value:
        raise ConfigError(f"Open field is still blank: {key}")
    return value


def rows_from_html(html: str, minimum_columns: int) -> list[dict[str, str]]:
    parser = TableParser()
    parser.feed(html)
    best: list[list[str]] = []
    for table in parser.tables:
        width = max((len(row) for row in table), default=0)
        if width >= minimum_columns and len(table) > len(best):
            best = table
    if not best:
        return []
    header = [_header_name(cell, index) for index, cell in enumerate(best[0])]
    records: list[dict[str, str]] = []
    for raw in best[1:]:
        if len(raw) < len(header):
            raw = raw + [""] * (len(header) - len(raw))
        record = {header[index]: raw[index].strip() for index in range(len(header))}
        if any(record.values()):
            records.append(record)
    return records


def _header_name(cell: str, index: int) -> str:
    name = " ".join(cell.split())
    return name or f"column_{index + 1}"


def row_key(row: dict[str, str]) -> str:
    payload = json.dumps(row, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def diff_rows(previous: list[dict[str, str]], current: list[dict[str, str]]) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    old = {row_key(row): row for row in previous}
    new = {row_key(row): row for row in current}
    added = [new[key] for key in new if key not in old]
    removed = [old[key] for key in old if key not in new]
    return added, removed


def snapshot_hash(rows: list[dict[str, str]]) -> str:
    payload = json.dumps(rows, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def cookie_header(config: dict[str, Any]) -> str:
    direct = str(config.get("cookie_header") or "").strip()
    if direct:
        return direct
    cookie_file = str(config.get("cookie_file") or "").strip()
    if not cookie_file:
        raise ConfigError("Open field is still blank: cookie_header or cookie_file")
    path = Path(cookie_file)
    if not path.exists():
        raise ConfigError(f"Cookie file was not found: {cookie_file}")
    pairs: list[str] = []
    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_") :]
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 7:
            pairs.append(f"{parts[5]}={parts[6]}")
    if not pairs:
        raise ConfigError(f"No cookies were read from {cookie_file}")
    return "; ".join(pairs)


def fetch_html(url: str, cookie: str, timeout: int) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Cookie": cookie,
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            final_url = response.geturl()
            body = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code} for {url}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not open {url}: {exc.reason}") from exc
    lowered = (final_url + "\n" + body[:4000]).lower()
    if any(marker in lowered for marker in SIGN_IN_MARKERS):
        raise SessionExpired(url)
    return body


def follow_next_links(html: str, page_url: str, cookie: str, timeout: int, needle: str, max_pages: int) -> str:
    """Append later pages when a next-link snippet was configured."""
    if not needle or max_pages <= 1:
        return html
    combined = [html]
    seen = {page_url}
    current = html
    current_url = page_url
    for _ in range(max_pages - 1):
        href = _next_href(current, needle)
        if not href:
            break
        next_url = urllib.parse.urljoin(current_url, href)
        if next_url in seen:
            break
        seen.add(next_url)
        current = fetch_html(next_url, cookie, timeout)
        current_url = next_url
        combined.append(current)
    return "\n".join(combined)


def _next_href(html: str, needle: str) -> str:
    parser = _LinkParser(needle)
    parser.feed(html)
    return parser.href


class _LinkParser(HTMLParser):
    def __init__(self, needle: str) -> None:
        super().__init__(convert_charrefs=True)
        self.needle = needle.lower()
        self.href = ""
        self._href = ""
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        self._href = dict(attrs).get("href") or ""
        self._text = []

    def handle_data(self, data: str) -> None:
        if self._href:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or not self._href or self.href:
            return
        blob = (self._href + " " + "".join(self._text)).lower()
        if self.needle in blob:
            self.href = self._href
        self._href = ""


def post_webhook(url: str, payload: dict[str, Any], timeout: int) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Webhook HTTP {exc.code} for {url}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Webhook failed for {url}: {exc.reason}") from exc


def watch_list(
    name: str,
    page_url: str,
    webhook_url: str,
    cookie: str,
    config: dict[str, Any],
    state: dict[str, Any],
) -> str:
    timeout = int(config.get("request_timeout_seconds") or 30)
    minimum_columns = int(config.get("minimum_columns") or 2)
    html = fetch_html(page_url, cookie, timeout)
    html = follow_next_links(
        html,
        page_url,
        cookie,
        timeout,
        str(config.get("next_link_contains") or "").strip(),
        int(config.get("max_pages") or 1),
    )
    rows = rows_from_html(html, minimum_columns)
    digest = snapshot_hash(rows)
    previous = state.get(name) if isinstance(state.get(name), dict) else {}
    previous_rows = previous.get("rows") if isinstance(previous.get("rows"), list) else []
    first_run = "hash" not in previous
    changed = (not first_run) and previous.get("hash") != digest
    snapshot = {"hash": digest, "count": len(rows), "rows": rows}
    if first_run and not config.get("fire_webhook_on_first_run"):
        state[name] = snapshot
        return f"{name}: first snapshot saved ({len(rows)} rows), no webhook"
    if not changed and not (first_run and config.get("fire_webhook_on_first_run")):
        state[name] = snapshot
        return f"{name}: no change ({len(rows)} rows)"
    added, removed = diff_rows(previous_rows, rows)
    payload = {
        "source": "mypremierdata.com",
        "list": name,
        "page_url": page_url,
        "count": len(rows),
        "added_count": len(added),
        "removed_count": len(removed),
        "added": added,
        "removed": removed,
        "rows": rows,
    }
    post_webhook(webhook_url, payload, timeout)
    state[name] = snapshot
    return f"{name}: change sent ({len(added)} added, {len(removed)} removed, {len(rows)} rows)"


LISTS = (
    ("callbacks", "callback_list_url", "callback_webhook_url"),
    ("showed_appointments", "showed_appointments_url", "showed_webhook_url"),
)


def run(config_path: Path = CONFIG_PATH) -> int:
    config = load_config(config_path)
    cookie = cookie_header(config)
    for _, page_key, hook_key in LISTS:
        required_text(config, page_key)
        required_text(config, hook_key)
    state_path = Path(str(config.get("state_file") or "premier_watch_state.json"))
    state = load_state(state_path)
    notes: list[str] = []
    try:
        for name, page_key, hook_key in LISTS:
            notes.append(
                watch_list(
                    name,
                    required_text(config, page_key),
                    required_text(config, hook_key),
                    cookie,
                    config,
                    state,
                )
            )
    except SessionExpired as exc:
        print(f"SESSION_EXPIRED {exc}", file=sys.stderr)
        return 3
    finally:
        save_state(state_path, state)
    for note in notes:
        print(note)
    return 0


def main() -> int:
    try:
        return run()
    except ConfigError as exc:
        print(f"CONFIG {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
