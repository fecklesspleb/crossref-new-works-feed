"""Live checks against api.crossref.org. Skipped unless LIVE=1. They make about 8 requests at 1 per second."""
import datetime as dt
import os
import unittest

import new_works as nw


@unittest.skipUnless(os.environ.get("LIVE") == "1", "set LIVE=1 to run against api.crossref.org")
class Live(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = nw.Client()
        cls.day = dt.date.today() - dt.timedelta(days=3)

    def test_robots_allows_works(self):
        self.assertTrue(self.client.robots_allows("/works"))

    def test_cursor_paging_matches_one_page(self):
        f = nw.build_filter("created", self.day, self.day, ["journal-article"], ["0028-0836"])
        whole, total, complete = nw.fetch(self.client, f)
        self.assertTrue(complete)
        if total < 3:
            self.skipTest(f"only {total} Nature articles that day")
        paged, _, _ = nw.fetch(self.client, f, rows=2)
        self.assertEqual({i["DOI"] for i in paged}, {i["DOI"] for i in whole})
        self.assertEqual(len(paged), total)

    def test_created_dates_fall_inside_the_window(self):
        f = nw.build_filter("created", self.day, self.day, ["journal-article"], ["0028-0836"])
        items, _, _ = nw.fetch(self.client, f, max_works=20)
        for i in items:
            self.assertTrue(i["created"]["date-time"].startswith(self.day.isoformat()), i["created"])

    def test_index_date_matches_far_more_than_created(self):
        created = self.client.get(nw.works_url(nw.build_filter("created", self.day, self.day), "*", 0))
        indexed = self.client.get(nw.works_url(nw.build_filter("index", self.day, self.day), "*", 0))
        self.assertGreater(indexed["message"]["total-results"], created["message"]["total-results"])

    def test_bad_filter_is_a_400(self):
        with self.assertRaises(nw.ApiError):
            self.client.get(nw.WORKS_URL + "?filter=from-nonsense-date:2026-01-01&rows=0")


if __name__ == "__main__":
    unittest.main()
