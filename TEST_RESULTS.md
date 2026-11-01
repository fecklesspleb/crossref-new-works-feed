# Test results

Recorded on Sunday 1 November 2026 between 11:36 and 11:41 EST, with Python 3.13.5 on Linux, from the public pool (no `CROSSREF_MAILTO` set).

## Offline tests

```
$ python3 -m unittest
Ran 35 tests in 0.012s
OK (skipped=5)
```

That is 30 offline tests passing. The 5 skipped tests are the live tests, which only run when `LIVE=1` is set.

## Live tests

```
$ LIVE=1 python3 -m unittest tests.test_live -v
test_bad_filter_is_a_400 ... ok
test_created_dates_fall_inside_the_window ... ok
test_cursor_paging_matches_one_page ... HTTP 429; waiting 2s (try 1 of 5)
ok
test_index_date_matches_far_more_than_created ... ok
test_robots_allows_works ... ok
Ran 5 tests in 10.088s
OK
```

These tests also passed 5 of 5 on an earlier run at 11:36 EST, in about 8 seconds. During the 11:41 run, Crossref returned one HTTP 429 even though the client was pacing itself at 1 request per second. The retry worked as designed.

## Workflow lint

```
$ actionlint .github/workflows/new-works.yml
(no output, exit 0)
```

This used actionlint 1.7.12.

## Live runs of new_works.py

| Command | Output |
|---|---|
| `--issn 0028-0836 --days 7` | `2026-10-25..2026-11-01 by created date: 88 matched, 88 read, 88 new, 2 requests, pool public` |
| the same, run again | `... 88 matched, 88 read, 0 new, 2 requests, pool public` |
| `--days 0 --max-works 1000` (all journal articles created on 1 Nov so far) | 8,117 matched. It wrote 1,000 new works and warned that it stopped at `--max-works`. |
| the same, run again | 2,000 read, 1,000 new (the next 1,000, not the same ones), 3 requests |
| `--query "perovskite solar" --days 2` | 246 matched, 246 new; only 28 had both words in the title |
| the same plus `--title-word perovskite --title-word solar` | 29 kept, all new (this ran a few minutes later than the row above) |

At 11:37 EST, the first Nature run hit one HTTP 429. That happened because one process started immediately after another had finished. It waited 2 seconds and then succeeded.

## Cursor paging with sort

On 30 October 2026, I requested Nature (ISSN 0028-0836) journal articles with `sort=created&order=asc`:

- three cursor pages with `rows=2`;
- one request with `rows=6`.

Both returned the same 6 DOIs in the same ascending `created` order.
