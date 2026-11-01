#!/usr/bin/env python3
"""Poll the Crossref REST API for newly registered works and write only DOIs you have not seen before to CSV.

Standard library only. See README.md for usage.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import email.utils
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from pathlib import Path

API = "https://api.crossref.org"
WORKS_URL = API + "/works"
REPO_URL = "https://github.com/fecklesspleb/crossref-new-works-feed"
VERSION = "1.0"

# Crossref limits list/filter requests to 1 per second in the public pool and 3 per second
# in the polite pool (July 2026). We stay at 1 per second, one request at a time, in both.
MIN_INTERVAL = 1.0
MAX_TRIES = 5
MAX_ROWS = 1000  # Crossref's maximum page size

SELECT = ["DOI", "type", "title", "container-title", "ISSN", "publisher", "member",
          "created", "published"]
COLUMNS = ["doi", "type", "title", "journal", "issn", "publisher", "member", "published",
           "created", "url", "first_seen"]
DATE_FIELDS = {"created": "created-date", "index": "index-date", "update": "update-date"}
SORT_FIELDS = {"created": "created", "index": "indexed", "update": "updated"}

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ISSN_RE = re.compile(r"^(\d{4})-?(\d{3}[\dX])$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class Blocked(Exception):
    """Crossref answered 403, or something that is not JSON. Stop; never try to get around it."""


class ApiError(Exception):
    """Crossref kept failing after retries, or rejected the request (400)."""


# ---------------------------------------------------------------- inputs

def normalize_issn(value: str) -> str:
    v = value.strip().upper().replace(" ", "")
    m = ISSN_RE.match(v)
    if not m:
        raise ValueError(f"not an ISSN: {value!r} (expected e.g. 0028-0836)")
    return f"{m.group(1)}-{m.group(2)}"


def parse_date(value: str) -> dt.date:
    if not DATE_RE.match(value or ""):
        raise ValueError(f"not a date: {value!r} (expected YYYY-MM-DD)")
    return dt.date.fromisoformat(value)


def contact_email(env=os.environ) -> str | None:
    """CROSSREF_MAILTO, if it looks like an email address. Crossref may block invalid addresses."""
    value = (env.get("CROSSREF_MAILTO") or "").strip()
    return value if EMAIL_RE.match(value) else None


def user_agent(env=os.environ) -> str:
    """CROSSREF_USER_AGENT wins. Otherwise name the tool and add the mailto contact when there is one."""
    custom = (env.get("CROSSREF_USER_AGENT") or "").strip()
    if custom:
        return custom
    mail = contact_email(env)
    contact = f"{REPO_URL}; mailto:{mail}" if mail else REPO_URL
    return f"crossref-new-works-feed/{VERSION} (+{contact})"


def build_filter(date_field: str, start: dt.date, end: dt.date, types=(), issns=(), members=(),
                 prefixes=(), funders=()) -> str:
    """Crossref filter string. Both ends of the date range are always set (inclusive, UTC);
    Crossref asks for until-index-date whenever you page with from-index-date."""
    if date_field not in DATE_FIELDS:
        raise ValueError(f"date field must be one of {sorted(DATE_FIELDS)}")
    if end < start:
        raise ValueError("the end date is before the start date")
    name = DATE_FIELDS[date_field]
    parts = [f"from-{name}:{start.isoformat()}", f"until-{name}:{end.isoformat()}"]
    parts += [f"type:{t}" for t in types]
    parts += [f"issn:{normalize_issn(i)}" for i in issns]
    parts += [f"member:{m}" for m in members]
    parts += [f"prefix:{p}" for p in prefixes]
    parts += [f"funder:{f}" for f in funders]
    return ",".join(parts)


def works_url(filt: str, cursor: str, rows: int, query: str = "", mailto: str | None = None,
              date_field: str = "created") -> str:
    params = {"filter": filt, "rows": str(rows), "cursor": cursor, "select": ",".join(SELECT),
              "sort": SORT_FIELDS[date_field], "order": "asc"}
    if query:
        params["query.bibliographic"] = query
    if mailto:
        params["mailto"] = mailto
    return WORKS_URL + "?" + urllib.parse.urlencode(params, safe=",:*")


# ---------------------------------------------------------------- HTTP

def urllib_http(url: str, headers: dict, timeout: float = 60):
    """Default transport: returns (status, headers, body) and never raises on HTTP errors."""
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, e.read() or b""


def retry_after_seconds(value: str | None, now: float | None = None) -> float | None:
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    now = time.time() if now is None else now
    return max(0.0, when.timestamp() - now)


class Client:
    def __init__(self, http=urllib_http, sleep=time.sleep, clock=time.monotonic, env=os.environ, log=None):
        self.http, self.sleep, self.clock = http, sleep, clock
        self.headers = {"User-Agent": user_agent(env), "Accept": "application/json"}
        self.interval = MIN_INTERVAL
        self.last = None
        self.requests = 0
        self.log = log or (lambda msg: print(msg, file=sys.stderr))

    def _wait_turn(self):
        if self.last is not None:
            gap = self.interval - (self.clock() - self.last)
            if gap > 0:
                self.sleep(gap)
        self.last = self.clock()

    def _adapt(self, headers: dict):
        """Never go faster than the server's x-rate-limit headers allow, and never faster than 1/s."""
        try:
            limit = int(headers.get("x-rate-limit-limit", ""))
            interval = float(headers.get("x-rate-limit-interval", "").rstrip("s"))
        except ValueError:
            return
        if limit > 0:
            self.interval = max(MIN_INTERVAL, interval / limit)

    def get(self, url: str):
        """GET JSON. Retries 429 (honouring Retry-After), 5xx and network errors; stops on 403."""
        delay = 2.0
        status = None
        for attempt in range(1, MAX_TRIES + 1):
            self._wait_turn()
            self.requests += 1
            try:
                status, headers, body = self.http(url, self.headers)
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
                status, headers, body = None, {}, str(e).encode()
            if status is not None:
                self._adapt(headers)
            if status == 200:
                try:
                    return json.loads(body)
                except ValueError:
                    raise Blocked(f"non-JSON answer from {url[:80]}")
            if status == 403:
                raise Blocked("HTTP 403 from Crossref: stopping (never retried)")
            if status is not None and status != 429 and status < 500:
                raise ApiError(f"HTTP {status}: {body[:300].decode('utf-8', 'replace')}")
            wait = retry_after_seconds(headers.get("retry-after")) if status == 429 else None
            wait = delay if wait is None else min(wait, 300.0)
            if attempt < MAX_TRIES:
                self.log(f"HTTP {status}; waiting {wait:.0f}s (try {attempt} of {MAX_TRIES})")
                self.sleep(wait)
                delay *= 2
        raise ApiError(f"gave up after {MAX_TRIES} tries (last status {status})")

    def robots_allows(self, path: str = "/works") -> bool:
        """Read api.crossref.org/robots.txt with our own User-Agent. Missing file (404) = no rules."""
        self._wait_turn()
        self.requests += 1
        try:
            status, _, body = self.http(API + "/robots.txt", self.headers)
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
            return True
        if status != 200:
            return True
        rp = urllib.robotparser.RobotFileParser()
        rp.parse(body.decode("utf-8", "replace").splitlines())
        return rp.can_fetch(self.headers["User-Agent"], API + path)


# ---------------------------------------------------------------- records

def date_parts(obj) -> str:
    """Crossref date-parts can be [[2026]], [[2026, 11]] or [[2026, 11, 1]]."""
    try:
        parts = (obj or {}).get("date-parts", [[]])[0]
    except (AttributeError, IndexError, TypeError):
        return ""
    if not parts or parts[0] is None:
        return ""
    return "-".join([f"{parts[0]:04d}"] + [f"{p:02d}" for p in parts[1:3] if p is not None])


def first(value) -> str:
    if isinstance(value, list):
        return str(value[0]).strip() if value else ""
    return str(value or "").strip()


def title_filter(words):
    """Crossref's query is a relevance search that matches any of the words, so 'perovskite solar' also
    returns plain 'solar' papers. This keeps only works whose title contains every word (case-insensitive)."""
    words = [w.lower() for w in words if w.strip()]
    if not words:
        return None

    def keep(item):
        title = " ".join(item.get("title") or []).lower()
        return all(w in title for w in words)
    return keep


def to_row(item: dict, seen_at: str) -> dict:
    doi = (item.get("DOI") or "").strip()
    return {
        "doi": doi,
        "type": item.get("type", ""),
        "title": re.sub(r"\s+", " ", first(item.get("title"))),
        "journal": first(item.get("container-title")),
        "issn": "|".join(item.get("ISSN") or []),
        "publisher": item.get("publisher", ""),
        "member": str(item.get("member", "")),
        "published": date_parts(item.get("published")),
        "created": (item.get("created") or {}).get("date-time", ""),
        "url": f"https://doi.org/{doi}" if doi else "",
        "first_seen": seen_at,
    }


# ---------------------------------------------------------------- fetch and state

def fetch(client: Client, filt: str, query: str = "", date_field: str = "created",
          max_works: int = 5000, rows: int = MAX_ROWS, mailto: str | None = None, seen=frozenset(), keep=None):
    """Page through the matches, oldest first, with a cursor.

    Stops after finding max_works DOIs that are not in `seen`, or at the end of the results.
    Works already seen, or rejected by `keep`, do not count, so a run that hit the limit carries on
    where it stopped next time.
    Returns (items, total_results, reached_end)."""
    page_rows = max(1, min(rows, max_works, MAX_ROWS))
    cursor, items, total, fresh, used = "*", [], None, set(), set()
    while True:
        data = client.get(works_url(filt, cursor, page_rows, query, mailto, date_field))
        msg = data.get("message", {}) if isinstance(data, dict) else {}
        if total is None:
            total = int(msg.get("total-results", 0))
        page = msg.get("items") or []
        items.extend(page)
        for item in page:
            key = (item.get("DOI") or "").strip().lower()
            if key and key not in seen and (keep is None or keep(item)):
                fresh.add(key)
        cursor = msg.get("next-cursor")
        reached_end = not page or not cursor or cursor in used or len(items) >= total
        if reached_end or len(fresh) >= max_works:
            break
        used.add(cursor)
    return items, total or 0, reached_end


def load_seen(path: Path) -> set:
    if not path.exists():
        return set()
    return {line.strip().lower() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()}


def save_seen(path: Path, seen: set):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("".join(f"{d}\n" for d in sorted(seen)), encoding="utf-8")
    tmp.replace(path)


def write_csv(path: Path, rows: list, append: bool):
    exists = path.exists() and path.stat().st_size > 0
    with path.open("a" if append else "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        if not (append and exists):
            w.writeheader()
        w.writerows(rows)


def new_rows(items: list, seen: set, seen_at: str, limit: int | None = None):
    """Rows for DOIs not in `seen` (adding them to it), up to `limit`. Returns (rows, left_over).
    Left-over DOIs are not marked as seen, so the next run picks them up."""
    out, left_over = [], 0
    for item in items:
        key = (item.get("DOI") or "").strip().lower()
        if not key or key in seen:
            continue
        if limit is not None and len(out) >= limit:
            left_over += 1
            continue
        seen.add(key)  # also drops duplicates inside one run
        out.append(to_row(item, seen_at))
    return out, left_over


# ---------------------------------------------------------------- CLI

def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Write newly registered Crossref works (new DOIs only) to CSV.")
    p.add_argument("--days", type=int, default=1, help="look back N days before today, UTC (default 1 = yesterday and today)")
    p.add_argument("--from-date", help="start date YYYY-MM-DD, inclusive (overrides --days)")
    p.add_argument("--until-date", help="end date YYYY-MM-DD, inclusive (default today, UTC)")
    p.add_argument("--date-field", choices=sorted(DATE_FIELDS), default="created",
                   help="created = first registered (default); index = any change, much larger; update = changed by the publisher")
    p.add_argument("--type", action="append", dest="types", help="Crossref work type, repeatable (default journal-article; 'all' for every type)")
    p.add_argument("--issn", action="append", default=[], help="journal ISSN, repeatable")
    p.add_argument("--member", action="append", default=[], help="Crossref member ID (publisher), repeatable")
    p.add_argument("--prefix", action="append", default=[], help="DOI prefix such as 10.1038, repeatable")
    p.add_argument("--funder", action="append", default=[], help="funder ID such as 10.13039/100000001, repeatable")
    p.add_argument("--query", default="", help="bibliographic keywords (title, journal, ...)")
    p.add_argument("--title-word", action="append", default=[], help="keep only titles containing this word, repeatable (all must match)")
    p.add_argument("--max-works", type=int, default=5000, help="stop after this many new works (default 5000); the next run carries on")
    p.add_argument("--out-dir", default="data", help="where the CSVs and seen_dois.txt go (default data)")
    p.add_argument("--dry-run", action="store_true", help="print the first request URL and exit, no network")
    a = p.parse_args(argv)
    if a.max_works < 1:
        p.error("--max-works must be at least 1")
    return a


def date_window(args, today: dt.date):
    end = parse_date(args.until_date) if args.until_date else today
    start = parse_date(args.from_date) if args.from_date else end - dt.timedelta(days=max(0, args.days))
    return start, end


def main(argv=None, http=urllib_http, sleep=time.sleep, env=os.environ, now=None) -> int:
    args = parse_args(argv)
    now = now or dt.datetime.now(dt.timezone.utc)
    types = [] if args.types == ["all"] else (args.types or ["journal-article"])
    try:
        start, end = date_window(args, now.date())
        filt = build_filter(args.date_field, start, end, types, args.issn, args.member, args.prefix, args.funder)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if (env.get("CROSSREF_MAILTO") or "").strip() and not contact_email(env):
        print("warning: CROSSREF_MAILTO is not a valid email address; using the public pool", file=sys.stderr)
    mailto = contact_email(env)
    if args.dry_run:
        print(works_url(filt, "*", min(MAX_ROWS, args.max_works), args.query, mailto, args.date_field))
        print("User-Agent:", user_agent(env))
        return 0

    keep = title_filter(args.title_word)
    client = Client(http=http, sleep=sleep, env=env)
    try:
        if not client.robots_allows("/works"):
            print("robots.txt disallows /works; stopping", file=sys.stderr)
            return 3
        out = Path(args.out_dir)
        seen_path = out / "seen_dois.txt"
        seen = load_seen(seen_path)
        items, total, reached_end = fetch(client, filt, args.query, args.date_field, args.max_works,
                                          mailto=mailto, seen=seen, keep=keep)
    except (Blocked, ApiError) as e:
        print(f"error: {e}. Nothing was written; the next run will try again.", file=sys.stderr)
        return 3

    out.mkdir(parents=True, exist_ok=True)
    kept = [i for i in items if keep is None or keep(i)]
    rows, left_over = new_rows(kept, seen, now.strftime("%Y-%m-%dT%H:%M:%SZ"), limit=args.max_works)
    write_csv(out / "new_works.csv", rows, append=False)
    write_csv(out / "works.csv", rows, append=True)
    save_seen(seen_path, seen)

    print(f"{start}..{end} by {args.date_field} date: {total} matched, {len(items)} read, "
          f"{'' if keep is None else f'{len(kept)} with the title words, '}"
          f"{len(rows)} new, {client.requests} requests, pool {'polite' if mailto else 'public'}")
    if not reached_end or left_over:
        print(f"warning: stopped at --max-works {args.max_works} new works before the end of the {total} matches. "
              f"The rest are not marked as seen, so the next run continues; raise --max-works or narrow the "
              f"filters if you need them all now.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
