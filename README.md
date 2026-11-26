# Adrenalin

Competitor-analysis tools for marketing teams, run as one CLI: `adrenalin <tool> [args...]`.

## ads-transparency

Exports every creative an advertiser showed on [Google Ads Transparency](https://adstransparency.google.com) to a CSV: format, region, dates shown, landing page, headline, description, sitelinks, video title and the preview image. Copy comes from Google's `content.js` previews where possible and from OCR of the preview image otherwise (Apple Vision on macOS, Tesseract elsewhere, Codex for images OCR cannot read). Domains read by OCR are checked with one `GET https://<host>/` per host; one that doesn't resolve in DNS (with or without `www.`) is treated as misread, dropped, and the image goes to Codex. Runs are paced to avoid blocks and can be resumed by rerunning the same command.

## meta-ad-library

Exports every Creative a Facebook Page showed in the [Meta Ad Library](https://www.facebook.com/ads/library/) to a CSV: status, dates shown, platforms, page, copy, Cards, DCO Variations, link and media URLs, and optionally EU reach, payer and beneficiary. It calls the Ad Library's own GraphQL (see `docs/adr/0004-meta-web-ad-library-not-graph-api.md`), logged out, and downloads media with plain requests. Runs are paced and resumable like ads-transparency.

## Install

Requires Python 3.13 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
brew install tesseract   # Linux: apt/dnf install tesseract-ocr
```

Tesseract is required on Linux and Windows. On macOS it is optional: Apple Vision is used, and Tesseract only if Apple Vision is unavailable. With no OCR engine, image previews can only be read by Codex; images neither can read fail and are retried on the next run.

Codex (`codex` CLI) is optional; it is only called for images OCR fails on, or for all images with `--vision`.

## Usage

```bash
uv run adrenalin ads-transparency AR04752318142645010433 --region IN --days 7
```

| Flag | Meaning |
|---|---|
| `--region IN` | ISO country code or `anywhere` (default) |
| `--days N` / `--start` / `--end` | Shown period (ads shown at any point in it). `--days N` becomes concrete dates when you run it, so rerunning after midnight starts a new run folder (downloaded images and the extraction cache are reused) |
| `--variations` | One row per variation, not just the first |
| `--vision`, `--vision-limit N` | Send every image to Codex; cap Codex calls (default 200) |
| `--sampling stratified` | Default: representative random Copy sample across Ads account × launch period × running/stopped. Default target 2,000 Creatives, minimum 3 per group; waits for complete Inventory before selection |
| `--sampling novelty` | Explicit exploratory discovery using the legacy novelty allocator. Its repetition score is not population Coverage |
| `--coverage X` | Novelty mode only: legacy repetition-score threshold; ignored for stratified selection |
| `--max-ads N` | Stratified target size (default 2,000); minimum representation can exceed it. Novelty mode: approximate copy-read cap |
| `--all` | Read every Creative's copy, however large the Inventory |
| `--wait-on-block` | Wait out a Google block instead of exiting |
| `--phase SECONDS` | Pin listing/country/detail request spacing to any positive finite number of seconds (e.g. `--phase 1.5`), without jitter or adaptation. Overrides the adaptive floor; cooldowns still apply. Omit for adaptive pacing. |
| `--stage list\|fetch\|extract\|export` | Run one stage only |
| `--proxy URL` | Proxy, repeatable to rotate |
| `-p`, `--progress` | Progress bars: creatives done (Google's estimated total until the listing finishes, then exact) with rate and ETA, plus one bar per stage |
| `--out DIR` | Output root (default `out/`) |

Exit codes (the same for every tool): `0` done; `1` some items failed or the run hit an error (the CSV is still written; rerun to retry them); `2` the run was blocked (rerun later to continue); `3` otherwise done, but the listing did not finish (an Incomplete run: draw no counts or start/stop conclusions until a rerun completes it). `-v` prints debug messages, and every run writes `run.log` in its run folder; the log describes the response that was taken as a block. For ads-transparency, a block on one Google host stops only the work that depends on it (e.g. image downloads continue while `content.js` downloads are blocked). Run `uv run adrenalin ads-transparency --help` for everything.

### meta-ad-library usage

```bash
uv run adrenalin meta-ad-library 120740541030 --region IN --status active
uv run adrenalin meta-ad-library "https://www.facebook.com/ads/library/?view_all_page_id=120740541030" --days 30
```

The page argument is a numeric Page ID or an Ad Library URL containing `view_all_page_id=`.

| Flag | Meaning |
|---|---|
| `--region IN` | ISO country code or `anywhere` (default) |
| `--days N` / `--start` / `--end` | Shown period, as for ads-transparency |
| `--status all\|active\|inactive` | Which Creatives to list by running status (default `all`) |
| `--variations` | One row per Variation of a DCO Creative, not just the first |
| `--eu-reach` | EU reach, payer and beneficiary. Costs one extra request per Creative |
| `--ocr` | Read the text in each Creative's downloaded images |
| `--stage list\|fetch\|export` | Run one stage only |
| `--wait-on-block`, `--proxy URL`, `-p`, `--out DIR`, `-v` | As for ads-transparency |
| `--solve-captcha` | Accepted, but never applies to a login or checkpoint redirect: those are blocks (ADR 0004) |

Caveats:

- Inactive Creatives outside the EU are only partly listed by Meta, so don't read stopped counts as complete.
- There is no Inventory estimate (the count is only in the Ad Library HTML), so completeness comes from the listing reaching its last page. Exit code `3` means it did not.
- Meta's query IDs are hard-coded. A "GraphQL document not found" error means Meta changed them: update `SEARCH_DOC_ID` / `DETAIL_DOC_ID` in `src/adrenalin/tools/meta_ad_library/client.py`.
- Meta requests run in a Playwright-controlled Chrome, so install Google Chrome. A redirect to a login or checkpoint page is treated as a block, never as a challenge to solve, as is any HTTP error from `www.facebook.com` except 429 (which backs off and retries) and three EU reach requests in a row answered with GraphQL errors.
- A rerun of a finished run lists the Page again from page 1 when the Shown period is still open and the last listing was before today, so at most once a day. Media files already downloaded and EU reach already fetched are kept. Adding `--eu-reach` to a finished run fetches EU reach for the rows that lack it.
- If a run lists no Creatives at all, check the Page ID: the Ad Library ID of a single ad is not a Page ID.

Output goes to `out/meta-ad-library/<page-slug>-<page-id>/runs/<REGION>_<status>_<start>_<end>[_variations]/ads.csv` (e.g. `out/meta-ad-library/makemytrip-120740541030/runs/IN_active_all_all/`: the Page ID keeps two Pages with the same name apart, and start and end print `all` when unset), with `summary.json` and `state.sqlite3` beside it.

## Output

```
out/ads-transparency/<advertiser-slug>/
  images/                        downloaded preview images
  runs/<REGION>_<start>_<end>/
    ads.csv                      the result
    summary.json                 counts per copy_status, Coverage and sampling groups, Ads accounts, stage times
    state.sqlite3                resumable run state
out/.cache/                      cooldown and extraction caches
```

See `CONTEXT.md` for terminology and `docs/adr/` for design decisions. Tests: `uv run pytest`.

Transparency RPCs use a fresh headless Google Chrome session controlled by Playwright. Install Google Chrome before running the tool; no signed-in profile is required. The browser sends RPC requests directly and closes at the end of each run. Other downloads use Python requests. All HTTP transports retain the configured pacing and block cooldowns.

If Google requires a CAPTCHA, rerun with `--solve-captcha --wait-on-block`.
Chrome opens a visible window only when a challenge is encountered. Complete
the challenge manually in that scraper-owned window; when it returns to Ads Transparency, the blocked RPC is
retried automatically with the same cursor. The window closes after completion;
headless Chrome continues using the session cookies held in memory and opens a
visible window again only if needed. The handoff waits up to 10 minutes.
Existing saved cooldowns are still honored before requests are sent.
Only the unexpired `GOOGLE_ABUSE_EXEMPTION` cookie is saved under `out/.cache/captcha/`
in owner-only files, separately per proxy, and restored on restart. Other cookies and
local storage remain in memory; your regular Chrome profile is never imported.
Google may still require another challenge if your IP changes or the exemption is rejected.

Script downloads use three concurrent workers with shared adaptive host pacing:
a 2.5-second base interval decreases toward 1.5 seconds after clean responses and
increases immediately on HTTP 429/CAPTCHA, or after three consecutive network
failures/5xx responses, up to 15 seconds (with jitter). Clean responses reset the
failure streak. Isolated disconnects and timeouts get two paced retries with
per-request backoff without slowing the entire host; blocks still trigger cooldowns.

Progress separates `inventory` (Creatives listed) from `creatives queued` (copy
work currently admitted). The queued-copy ETA covers only that queue; later
sampling rounds may add work. Sampling targets and limits are unchanged.

### Representative sample and existing runs

The default `stratified` mode allocates three slots per group (or its full size
if smaller), then allocates the remaining slots proportionally to group Inventory.
Unknown Ads accounts stay in an explicit unknown-account group; accounts are
not inferred from copy after selection. A stored random seed ranks every
Creative within its group. Repeating the same run and target keeps selection
stable. Failed reads remain selected and are retried, never replaced with easier
reads. `--all` still reads every Creative.

CSV `sample_role` separates `representative`, `exploratory`, `not_sampled`, and
`census`. On migration, existing copy is retained; only missing selected reads
are added. Old novelty-selected reads outside the representative design get no
representative weight. The default target is not a 95% confidence or messaging
completeness guarantee. There is no Coverage stopping threshold in this mode.

For representative rows, `sample_weight` is group Inventory / selected Creatives,
including selected rows with missing copy. This is a design weight, not an
adjustment for unreadable copy. Keep missing-copy outcomes explicit. Each
Variation has its Creative's weight; use Variation 1 when counting Creatives.
Legacy novelty weights are descriptive expansion ratios only, not probability
sample weights. Summary JSON records the selected IDs, quotas, seed, pending
reads, and retained exploratory reads. Stop/restart the current process with the
same command to adopt the new default and resume the existing run.
