# Adrenalin

Competitor-analysis tools for marketing teams, run as one CLI: `adrenalin <tool> [args...]`.

## ads-transparency

Exports every creative an advertiser showed on [Google Ads Transparency](https://adstransparency.google.com) to a CSV: format, region, dates shown, landing page, headline, description, sitelinks, video title and the preview image. Copy comes from Google's `content.js` previews where possible and from OCR of the preview image otherwise (Apple Vision on macOS, Tesseract elsewhere, Codex for images OCR cannot read). Domains read by OCR are checked with one `GET https://<host>/` per host; one that doesn't resolve in DNS (with or without `www.`) is treated as misread, dropped, and the image goes to Codex. Runs are paced to avoid blocks and can be resumed by rerunning the same command.

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
| `--coverage X` | Large Inventories (Google estimates over 2,000 Creatives) read copy for a Copy sample only: every bucket (launch week (month before the Shown period) × running/stopped, split by Ads account where Google exposes it) gets at least 3 reads, then sampling continues where new Messages turn up until Coverage reaches X (default 0.95, 0 < X < 1), or stops when no readable Messages turn up. Unread Creatives get `copy_status` `not_sampled`; processed rows (copy reading finished, whether or not copy was found) carry a `sample_weight` (their bucket's Inventory / Creatives processed). With `--variations` the weight is per Creative: all its rows carry it, and Variation 1 rows' weights sum to the Inventory. Rerun with a higher X to read more |
| `--max-ads N` | Read copy for about N Creatives, even when the Inventory is small enough to read in full (this also shrinks the random head start). Every launch week × running/stopped × Ads account group still gets up to 3 reads, which can exceed N by a lot: Rio's first 3,200 listed Creatives fall into 110 groups, so at least 258 reads (the run warns when this happens). Recommended for very large Advertisers, where reading to the Coverage target can take many hours. If Google gives no count estimate, every listed Creative is read anyway. `--all` wins over it. The head start is fixed by a run folder's first run: a rerun with another N or `--coverage` changes only the reads after it |
| `--all` | Read every Creative's copy, however large the Inventory |
| `--wait-on-block` | Wait out a Google block instead of exiting |
| `--stage list\|fetch\|extract\|export` | Run one stage only |
| `--proxy URL` | Proxy, repeatable to rotate |
| `-p`, `--progress` | Progress bars: creatives done (Google's estimated total until the listing finishes, then exact) with rate and ETA, plus one bar per stage |
| `--out DIR` | Output root (default `out/`) |

Exit codes: `0` done; `1` some items failed or the run hit an error (the CSV is still written; rerun to retry them); `2` Google blocked the run (rerun later to continue); `3` otherwise done, but the listing did not finish (an Incomplete run: draw no counts or start/stop conclusions until a rerun completes it). A block on one Google host stops only the work that depends on it (e.g. image downloads continue while `content.js` downloads are blocked), and the log describes the response that was taken as a block. Run `uv run adrenalin ads-transparency --help` for everything.

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
