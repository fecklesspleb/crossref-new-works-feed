# crossref-new-works-feed

A small Python script and a GitHub Actions schedule that ask the free [Crossref REST API](https://api.crossref.org) once a day for newly registered scholarly works (journal articles, preprints, chapters, datasets) and write **only the DOIs you have not seen before** to CSV.

- `data/new_works.csv`: works first seen on the latest run (overwritten each run).
- `data/works.csv`: every work ever seen, one row per DOI (appended).
- `data/seen_dois.txt`: the DOIs already written, lower-cased, one per line. DOIs are case-insensitive, so `10.1038/ABC` and `10.1038/abc` count as one work.

No API key, no account, no packages: Python standard library only.

## What it reads

One endpoint, `https://api.crossref.org/works`, with a filter like this:

```
filter=from-created-date:2026-10-31,until-created-date:2026-11-01,type:journal-article,issn:0028-0836
&rows=1000&cursor=*&select=DOI,type,title,container-title,ISSN,publisher,member,created,published
&sort=created&order=asc
```

- **Dates.** `created` is when a DOI was first registered, so it is the right date for "new works". Both ends of the range are always set (inclusive, UTC).
- **Paging.** Results are read oldest first with Crossref's `cursor` deep paging, up to 1,000 rows per request.
- **Fields.** `select=` asks only for the fields that go in the CSV. No author names, ORCID iDs or abstracts are requested or stored.

## Quickstart

Tested with Python 3.13.

```bash
git clone https://github.com/fecklesspleb/crossref-new-works-feed
cd crossref-new-works-feed
export CROSSREF_MAILTO=you@example.org       # optional but polite; see below
python3 new_works.py --issn 0028-0836 --days 7
python3 new_works.py --issn 0028-0836 --days 7   # second run: 0 new
```

What that printed on 1 November 2026 (Nature's print ISSN, 25 October to 1 November):

```
2026-10-25..2026-11-01 by created date: 88 matched, 88 read, 88 new, 2 requests, pool public
2026-10-25..2026-11-01 by created date: 88 matched, 88 read, 0 new, 2 requests, pool public
```

The 2 requests are `robots.txt` (currently a 404, which means no rules) and one data page.

Options:

```
--days N              look back N days before today, plus today, UTC (default 1 = yesterday and today)
--from-date DATE      start date YYYY-MM-DD, inclusive (overrides --days)
--until-date DATE     end date, inclusive (default today, UTC)
--date-field F        created (default) | index | update
--type T              Crossref work type, repeatable (default journal-article; "all" for every type)
--issn ISSN           journal ISSN, repeatable; 00280836 and 0028-0836 both work
--member ID           Crossref member ID (publisher), repeatable, e.g. 297
--prefix P            DOI prefix, repeatable, e.g. 10.1038
--funder ID           funder ID, repeatable, e.g. 10.13039/100000001
--query WORDS         bibliographic keywords
--title-word WORD     keep only titles containing WORD, repeatable (all must match)
--max-works N         stop after N new works (default 5000); the next run carries on
--out-dir PATH        default data
--source public|apify default public (or env WORKS_SOURCE)
--max-usd 0.50        apify only: hard cap on what one run can charge (or env APIFY_MAX_USD)
--dry-run             print the first request URL and the User-Agent (public), or the Actor request (apify), no network
```

Filters of the same kind are combined with OR (two ISSNs means either journal). Different kinds are combined with AND.

### Keywords are fuzzy

`--query` is Crossref's relevance search, not an exact phrase match, and the results stay in date order. On 1 November 2026, `--query "perovskite solar" --days 2` matched 246 works, but only 29 had both words in the title. Add `--title-word perovskite --title-word solar` to keep just those; works rejected that way are not marked as seen.

### Finding IDs

- **Journal ISSN:** on the journal's website, or `https://api.crossref.org/journals?query=<name>`.
- **Publisher member ID:** `https://api.crossref.org/members?query=<name>`.
- **Funder ID:** `https://api.crossref.org/funders?query=<name>`, then prefix the `id` with `10.13039/`.

## Run it every day with GitHub Actions

`.github/workflows/new-works.yml` runs at 06:23 UTC every day, and on demand from the Actions tab. It:

1. runs the offline tests;
2. runs `new_works.py --days 1`;
3. commits the three files in `data/` when they change.

To set it up:

1. Fork or copy this repository.
2. Under Settings > Secrets and variables > Actions > Variables, optionally set:
   - `ISSNS`: comma separated. The default is `0028-0836` (Nature) so the first run is small.
   - `QUERY` and `TITLE_WORDS` (comma separated).
   - `MAX_WORKS`.
   - `CROSSREF_MAILTO`: your contact email.
   - To use the hosted Actor instead of the Crossref API: variable `WORKS_SOURCE` = `apify`, secret `APIFY_TOKEN`, and optionally variable `APIFY_MAX_USD` (default 0.50).
3. Open the Actions tab, enable workflows if asked, and run **Crossref new works feed** once by hand.

Things to know:
- **Keep the feed narrow.** A weekday brings roughly 27,000 to 30,000 new journal articles (Crossref counts for 28 to 30 October 2026). Each CSV row is about 330 bytes, so an unfiltered feed would add about 9 MB a day to `works.csv`.
- **Look-back.** The default look-back of yesterday plus today overlaps the previous run, so works that reach the API late are still caught, and the seen list stops repeats.
- **Scheduled workflows can switch off.** GitHub disables scheduled workflows in public repositories after 60 days with no repository activity, and scheduled workflows are off by default in a fork.


## Or run it on Apify instead

The script above is free and runs anywhere Python does. If you'd rather not keep a runner or a cron job alive, the same feed is on the Apify Store as **crossref-new-works-feed**. Apify runs it on a schedule, keeps the results as a dataset and can send them on to email, Slack, Google Sheets, n8n, Make or Zapier.

You can also call the hosted version from this repo:

```bash
export APIFY_TOKEN=...   # from Apify Console > Settings > API & Integrations
python3 new_works.py --source apify --query "climate adaptation" --days 1 --dry-run   # check the request first
python3 new_works.py --source apify --query "climate adaptation" --days 1
```

With `--source apify`, the script starts the Apify Actor, waits for it to finish and writes the same output files as a local run (`data/new_works.csv`, `data/works.csv`, `data/seen_dois.txt`). The token is sent in the `Authorization` header, never in the URL. `--max-usd` (default 0.50, or env `APIFY_MAX_USD`) is sent as `maxTotalChargeUsd`, which stops the run at your cap. The Actor costs $0.001 per work returned (pay per event, event `work`); there is no separate start fee.

`--date-field update` is not available with `--source apify` (the Actor has no equivalent). `--date-field index` is sent as the Actor's `indexed`. `--title-word` is applied after the Actor returns, so dropped works may still have been charged. Without `--source apify`, nothing changes: the script calls the Crossref API directly, for free.

Which one to pick:
- **Local (default):** free, and you own the scheduling and the storage.
- **Apify:** pay-per-result on Apify's pricing. Scheduling, retries, storage and integrations are handled for you.

Apify Store page: https://apify.com/fecklesspleb/crossref-new-works-feed

## Being polite to Crossref

- **Request rate.** At most 1 request per second, one at a time. Crossref's current limits for list and filter requests are 1 per second in the public pool and 3 per second in the polite pool (announced July 2026). If the `x-rate-limit-limit` and `x-rate-limit-interval` headers ask for something slower, the script slows down; it never goes faster than 1 per second.
- **Polite pool.** `CROSSREF_MAILTO` adds `mailto=` to every request and puts the address in the User-Agent:
  `crossref-new-works-feed/1.0 (+https://github.com/fecklesspleb/crossref-new-works-feed; mailto:you@example.org)`.
  - Crossref counts polite-pool limits per email address, so use your own.
  - An address that does not look like an email is ignored (Crossref may block invalid ones).
  - To send your own User-Agent instead, set `CROSSREF_USER_AGENT`.
- **Errors.**
  - On HTTP 429 the script waits for `Retry-After`, or backs off 2, 4, 8 and 16 seconds, up to 5 tries. It also retries 5xx and network errors.
  - On 403, or an answer that is not JSON, it stops at once and writes nothing, so the next run starts clean.
- **robots.txt.** The script reads `robots.txt` first and stops if it ever disallows `/works`.

## Output

Columns: `doi, type, title, journal, issn, publisher, member, published, created, url, first_seen`

```
doi,type,title,journal,issn,publisher,member,published,created,url,first_seen
10.1038/s41586-026-11328-3,journal-article,ctDNA clearance and outcomes for resectable lung cancer from NeoCOAST-2,Nature,0028-0836|1476-4687,Springer Science and Business Media LLC,297,2026-10-25,2026-10-25T07:01:31Z,https://doi.org/10.1038/s41586-026-11328-3,2026-11-01T16:37:25Z
```

- `published` is `YYYY`, `YYYY-MM` or `YYYY-MM-DD`, whichever the publisher deposited.
- `created` and `first_seen` are UTC.
- `issn` joins a journal's ISSNs with `|`.

## Limits

- **Late arrivals.** A new DOI can take minutes to a few hours to show up in the API after it is registered. That is why the look-back overlaps.
- **Metadata quality.** It is only as complete as each publisher's deposit. Titles can be missing, and a print date can be months ahead of the online date.
- **`--date-field index`** matches any change to a record, including citation-count updates, so it returns far more than `created`. On 30 October 2026 that was about 789,000 records against 86,622 newly created ones.
- **`--max-works`** counts new works. When a run stops at the limit, the works it did not write are not marked as seen, so the next run continues. Only once a work falls out of the look-back window can it be missed.

Disclosure: if you'd rather not host it, I built and sell a paid hosted version: [Crossref New Works Feed on Apify](https://apify.com/fecklesspleb/crossref-new-works-feed), $0.001 per work, no start fee.

## Tests

```bash
python3 -m unittest -v                         # offline: saved Crossref responses in tests/fixtures
LIVE=1 python3 -m unittest tests.test_live -v  # also about 8 requests to api.crossref.org
```

**Offline tests** never touch the network. They check:
- the filter and URL (both date ends, cursor, `select`, `mailto`);
- ISSN and date validation;
- one request per second;
- 429 with `Retry-After`, 5xx backoff, and stopping on 403 and non-JSON;
- `robots.txt`;
- date-parts parsing;
- deduplication across two runs and within one run;
- the `--max-works` carry-over and the title-word filter;
- the exact Apify request (URL, cost cap, header, body fields against the Actor's input schema), that non-work records are dropped, the 408 timeout path, and that the default `--source public` path is unchanged.

**Live tests** check:
- that cursor paging with 2 rows returns the same DOIs as one page;
- that `created` dates fall inside the window;
- that `index` matches more than `created`;
- that a bad filter returns 400.

## License

MIT
