"""Offline tests: no network. Saved Crossref responses live in tests/fixtures (captured 1 November 2026)."""
import contextlib
import csv
import datetime as dt
import io
import tempfile
import unittest
from pathlib import Path

import new_works as nw
from tests.fake_http import FakeClock, FakeHTTP, fixture_json

DAY = dt.date(2026, 10, 30)
NATURE_FILTER = nw.build_filter("created", DAY, DAY, ["journal-article"], ["0028-0836"])
NOW = dt.datetime(2026, 11, 1, 16, 0, tzinfo=dt.timezone.utc)
NATURE_ARGS = ["--from-date", "2026-10-30", "--until-date", "2026-10-30", "--issn", "0028-0836"]
OK = {"x-rate-limit-limit": "1", "x-rate-limit-interval": "1s", "x-concurrency-limit": "1"}


def nature_routes(rows=nw.MAX_ROWS):
    """Two real pages of 5 + 2 Nature articles. With rows=1000 one page holds all 7, so merge them."""
    p1, p2 = fixture_json("nature_page1.json"), fixture_json("nature_page2.json")
    if rows >= 7:
        merged = fixture_json("nature_page1.json")
        merged["message"]["items"] += p2["message"]["items"]
        return {nw.works_url(NATURE_FILTER, "*", rows): (200, OK, merged)}
    return {nw.works_url(NATURE_FILTER, "*", rows): (200, OK, p1),
            nw.works_url(NATURE_FILTER, p1["message"]["next-cursor"], rows): (200, OK, p2)}


def client(http, env=None):
    clock = FakeClock()
    return nw.Client(http=http, sleep=clock.sleep, clock=clock, env=env or {}, log=lambda m: None), clock


def run_main(argv, http, out_dir, env=None, now=NOW):
    clock = FakeClock()
    err = io.StringIO()
    with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()) as out:
        code = nw.main(argv + ["--out-dir", str(out_dir)], http=http, sleep=clock.sleep, env=env or {}, now=now)
    return code, out.getvalue(), err.getvalue()


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


class Inputs(unittest.TestCase):
    def test_issn_normalized(self):
        self.assertEqual(nw.normalize_issn("00280836"), "0028-0836")
        self.assertEqual(nw.normalize_issn(" 1234-567x "), "1234-567X")
        with self.assertRaises(ValueError):
            nw.normalize_issn("12345")

    def test_dates_validated(self):
        self.assertEqual(nw.parse_date("2026-10-30"), DAY)
        for bad in ("2026/10/30", "30-10-2026", "", None):
            with self.assertRaises(ValueError):
                nw.parse_date(bad)

    def test_filter_always_sets_both_ends(self):
        f = nw.build_filter("index", DAY, DAY, [], [], ["297"], ["10.1038"], ["10.13039/100000001"])
        self.assertEqual(f, "from-index-date:2026-10-30,until-index-date:2026-10-30,member:297,"
                            "prefix:10.1038,funder:10.13039/100000001")
        with self.assertRaises(ValueError):
            nw.build_filter("created", DAY, DAY - dt.timedelta(days=1))
        with self.assertRaises(ValueError):
            nw.build_filter("published", DAY, DAY)

    def test_url_has_cursor_select_sort_and_mailto(self):
        u = nw.works_url(NATURE_FILTER, "*", 1000, "deep learning", "me@example.org")
        self.assertIn("cursor=*", u)
        self.assertIn("rows=1000", u)
        self.assertIn("select=DOI,type,title", u)
        self.assertIn("sort=created&order=asc", u)
        self.assertIn("query.bibliographic=deep+learning", u)
        self.assertIn("mailto=me%40example.org", u)
        self.assertNotIn("author", u)

    def test_window_from_days(self):
        a = nw.parse_args(["--days", "1"])
        self.assertEqual(nw.date_window(a, DAY), (DAY - dt.timedelta(days=1), DAY))
        a = nw.parse_args(["--from-date", "2026-10-01", "--until-date", "2026-10-02"])
        self.assertEqual(nw.date_window(a, DAY), (dt.date(2026, 10, 1), dt.date(2026, 10, 2)))


class Etiquette(unittest.TestCase):
    def test_user_agent_from_env(self):
        self.assertEqual(nw.user_agent({}), "crossref-new-works-feed/1.0 (+https://github.com/fecklesspleb/crossref-new-works-feed)")
        self.assertIn("mailto:me@example.org", nw.user_agent({"CROSSREF_MAILTO": "me@example.org"}))
        self.assertEqual(nw.user_agent({"CROSSREF_USER_AGENT": "MyLab/2 (mailto:a@b.org)"}), "MyLab/2 (mailto:a@b.org)")

    def test_invalid_mailto_ignored(self):
        self.assertIsNone(nw.contact_email({"CROSSREF_MAILTO": "not-an-email"}))
        self.assertNotIn("mailto", nw.user_agent({"CROSSREF_MAILTO": "not-an-email"}))

    def test_one_request_per_second(self):
        http = FakeHTTP(nature_routes(rows=5))
        c, clock = client(http)
        items, total, complete = nw.fetch(c, NATURE_FILTER, rows=5)
        self.assertEqual((len(items), total, complete), (7, 7, True))
        self.assertEqual(len(http.calls), 2)
        self.assertEqual(clock.sleeps, [1.0])  # waited a full second before the second page

    def test_slower_when_server_says_so(self):
        c, _ = client(FakeHTTP())
        c._adapt({"x-rate-limit-limit": "1", "x-rate-limit-interval": "2s"})
        self.assertEqual(c.interval, 2.0)
        c._adapt({"x-rate-limit-limit": "50", "x-rate-limit-interval": "1s"})
        self.assertEqual(c.interval, 1.0)  # never faster than 1/s

    def test_429_honours_retry_after_then_succeeds(self):
        url = nw.works_url(NATURE_FILTER, "*", 1000)
        ok = nature_routes()[url]
        http = FakeHTTP({url: [(429, {"retry-after": "7"}, "slow down"), ok]})
        c, clock = client(http)
        items, _, _ = nw.fetch(c, NATURE_FILTER)
        self.assertEqual(len(items), 7)
        self.assertIn(7.0, clock.sleeps)

    def test_retry_after_http_date(self):
        self.assertEqual(nw.retry_after_seconds("Sun, 01 Nov 2026 16:00:30 GMT", now=NOW.timestamp()), 30.0)
        self.assertIsNone(nw.retry_after_seconds("soon"))

    def test_500_backs_off_and_gives_up(self):
        url = nw.works_url(NATURE_FILTER, "*", 1000)
        c, clock = client(FakeHTTP({url: (503, {}, "down")}))
        with self.assertRaises(nw.ApiError):
            nw.fetch(c, NATURE_FILTER)
        self.assertEqual(c.requests, nw.MAX_TRIES)
        self.assertEqual([s for s in clock.sleeps if s >= 2], [2.0, 4.0, 8.0, 16.0])

    def test_403_stops_without_retry(self):
        url = nw.works_url(NATURE_FILTER, "*", 1000)
        c, _ = client(FakeHTTP({url: (403, {}, "blocked")}))
        with self.assertRaises(nw.Blocked):
            nw.fetch(c, NATURE_FILTER)
        self.assertEqual(c.requests, 1)

    def test_non_json_stops(self):
        url = nw.works_url(NATURE_FILTER, "*", 1000)
        c, _ = client(FakeHTTP({url: (200, {}, "<html>challenge</html>")}))
        with self.assertRaises(nw.Blocked):
            nw.fetch(c, NATURE_FILTER)

    def test_400_reports_crossref_message(self):
        url = nw.works_url(NATURE_FILTER, "*", 1000)
        c, _ = client(FakeHTTP({url: (400, {}, '{"status":"failed","message":[{"message":"bad filter"}]}')}))
        with self.assertRaisesRegex(nw.ApiError, "bad filter"):
            nw.fetch(c, NATURE_FILTER)

    def test_robots_disallow_stops_before_any_data_request(self):
        http = FakeHTTP({nw.API + "/robots.txt": (200, {}, "User-agent: *\nDisallow: /works\n")})
        with tempfile.TemporaryDirectory() as d:
            code, _, err = run_main(NATURE_ARGS, http, d)
        self.assertEqual(code, 3)
        self.assertIn("robots.txt disallows", err)
        self.assertEqual(http.data_calls(), [])


class Records(unittest.TestCase):
    def test_date_parts_variants(self):
        self.assertEqual(nw.date_parts({"date-parts": [[2026]]}), "2026")
        self.assertEqual(nw.date_parts({"date-parts": [[2026, 1]]}), "2026-01")
        self.assertEqual(nw.date_parts({"date-parts": [[2026, 1, 5]]}), "2026-01-05")
        self.assertEqual(nw.date_parts({"date-parts": [[None]]}), "")
        self.assertEqual(nw.date_parts(None), "")

    def test_row_from_real_record(self):
        item = fixture_json("nature_page1.json")["message"]["items"][0]
        row = nw.to_row(item, "2026-11-01T16:00:00Z")
        self.assertEqual(row["doi"], "10.1038/d41586-026-03368-6")
        self.assertEqual(row["journal"], "Nature")
        self.assertEqual(row["issn"], "0028-0836|1476-4687")
        self.assertEqual(row["created"], "2026-10-30T10:02:02Z")
        self.assertEqual(row["url"], "https://doi.org/10.1038/d41586-026-03368-6")
        self.assertEqual(list(row), nw.COLUMNS)

    def test_duplicates_and_case_inside_one_run(self):
        items = [{"DOI": "10.1/ABC"}, {"DOI": "10.1/abc"}, {"DOI": ""}, {"DOI": "10.1/def"}]
        rows, left = nw.new_rows(items, set(), "t")
        self.assertEqual([r["doi"] for r in rows], ["10.1/ABC", "10.1/def"])
        self.assertEqual(left, 0)

    def test_limit_leaves_the_rest_unseen(self):
        seen = set()
        rows, left = nw.new_rows([{"DOI": "10.1/a"}, {"DOI": "10.1/b"}, {"DOI": "10.1/c"}], seen, "t", limit=2)
        self.assertEqual((len(rows), left, seen), (2, 1, {"10.1/a", "10.1/b"}))


class TitleWords(unittest.TestCase):
    def test_all_words_must_be_in_the_title(self):
        keep = nw.title_filter(["Perovskite", "solar"])
        self.assertTrue(keep({"title": ["Stable perovskite solar cells"]}))
        self.assertFalse(keep({"title": ["Solar wind at 1 AU"]}))
        self.assertFalse(keep({}))
        self.assertIsNone(nw.title_filter([]))

    def test_rejected_titles_are_not_written_or_marked_seen(self):
        with tempfile.TemporaryDirectory() as d:
            code, out, _ = run_main(NATURE_ARGS + ["--title-word", "burrito"], FakeHTTP(nature_routes()), d)
            self.assertEqual(code, 0)
            self.assertIn("7 read, 1 with the title words, 1 new", out)
            self.assertEqual((Path(d) / "seen_dois.txt").read_text().split(), ["10.1038/d41586-026-03368-6"])


class EndToEnd(unittest.TestCase):
    def test_second_run_writes_only_new_dois(self):
        with tempfile.TemporaryDirectory() as d:
            code, out, _ = run_main(NATURE_ARGS, FakeHTTP(nature_routes()), d)
            self.assertEqual(code, 0)
            self.assertIn("7 matched, 7 read, 7 new", out)
            self.assertEqual(len(read_csv(Path(d) / "new_works.csv")), 7)

            # Second run: one extra work appears, everything else was seen before.
            routes = nature_routes()
            url = next(iter(routes))
            extra = dict(fixture_json("nature_page1.json")["message"]["items"][0], DOI="10.1038/NEW-ONE")
            routes[url][2]["message"]["items"].append(extra)
            routes[url][2]["message"]["total-results"] = 8
            code, out, _ = run_main(NATURE_ARGS, FakeHTTP(routes), d)
            self.assertEqual(code, 0)
            self.assertIn("8 matched, 8 read, 1 new", out)
            self.assertEqual([r["doi"] for r in read_csv(Path(d) / "new_works.csv")], ["10.1038/NEW-ONE"])
            self.assertEqual(len(read_csv(Path(d) / "works.csv")), 8)
            seen = (Path(d) / "seen_dois.txt").read_text().split()
            self.assertEqual(len(seen), 8)
            self.assertIn("10.1038/new-one", seen)

    def test_quiet_day_still_writes_header(self):
        empty = fixture_json("nature_page3.json")
        with tempfile.TemporaryDirectory() as d:
            url = nw.works_url(NATURE_FILTER, "*", 1000)
            code, out, _ = run_main(NATURE_ARGS, FakeHTTP({url: (200, OK, empty)}), d)
            self.assertEqual(code, 0)
            self.assertEqual((Path(d) / "new_works.csv").read_text().strip(), ",".join(nw.COLUMNS))

    def test_max_works_warns_when_incomplete(self):
        p1 = fixture_json("nature_page1.json")
        url = nw.works_url(NATURE_FILTER, "*", 5)
        with tempfile.TemporaryDirectory() as d:
            code, out, err = run_main(NATURE_ARGS + ["--max-works", "5"], FakeHTTP({url: (200, OK, p1)}), d)
        self.assertEqual(code, 0)
        self.assertIn("7 matched, 5 read, 5 new", out)
        self.assertIn("stopped at --max-works 5 new works before the end of the 7 matches", err)

    def test_capped_run_carries_on_next_time(self):
        with tempfile.TemporaryDirectory() as d:
            code, out, _ = run_main(NATURE_ARGS + ["--max-works", "5"], FakeHTTP(nature_routes(rows=5)), d)
            self.assertIn("5 new", out)
            http = FakeHTTP(nature_routes(rows=5))
            code, out, err = run_main(NATURE_ARGS + ["--max-works", "5"], http, d)
            self.assertEqual(code, 0)
            self.assertIn("7 matched, 7 read, 2 new", out)  # skipped the 5 seen ones and paged on
            self.assertEqual(len(http.data_calls()), 2)
            self.assertEqual(err, "")
            self.assertEqual(len(read_csv(Path(d) / "works.csv")), 7)

    def test_failure_writes_nothing(self):
        url = nw.works_url(NATURE_FILTER, "*", 1000)
        with tempfile.TemporaryDirectory() as d:
            code, _, err = run_main(NATURE_ARGS, FakeHTTP({url: (403, {}, "no")}), d)
            self.assertEqual(code, 3)
            self.assertIn("Nothing was written", err)
            self.assertEqual(list(Path(d).iterdir()), [])

    def test_dry_run_makes_no_request(self):
        http = FakeHTTP()
        with tempfile.TemporaryDirectory() as d:
            code, out, _ = run_main(NATURE_ARGS + ["--dry-run"], http, d, env={"CROSSREF_MAILTO": "me@example.org"})
        self.assertEqual(code, 0)
        self.assertEqual(http.calls, [])
        self.assertIn("mailto=me%40example.org", out)

    def test_mailto_sent_in_query_and_user_agent(self):
        env = {"CROSSREF_MAILTO": "me@example.org"}
        routes = {nw.works_url(NATURE_FILTER, "*", 1000, mailto="me@example.org"): next(iter(nature_routes().values()))}
        http = FakeHTTP(routes)
        with tempfile.TemporaryDirectory() as d:
            code, out, _ = run_main(NATURE_ARGS, http, d, env=env)
        self.assertEqual(code, 0)
        self.assertIn("pool polite", out)
        self.assertTrue(all("mailto:me@example.org" in h["User-Agent"] for _, h in http.calls))

    def test_bad_issn_is_an_input_error(self):
        with tempfile.TemporaryDirectory() as d:
            code, _, err = run_main(["--issn", "nope"], FakeHTTP(), d)
        self.assertEqual(code, 2)
        self.assertIn("not an ISSN", err)


if __name__ == "__main__":
    unittest.main()
