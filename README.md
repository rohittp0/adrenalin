# Adrenalin

Competitor-analysis tools for marketing teams, run as one CLI: `adrenalin <tool> [args...]`.

## ads-transparency

Exports every creative an advertiser showed on [Google Ads Transparency](https://adstransparency.google.com) to a CSV: format, region, dates shown, landing page, headline, description, sitelinks, video title and the preview image. Copy comes from Google's `content.js` previews where possible and from OCR of the preview image otherwise (Apple Vision on macOS, Tesseract elsewhere, Codex for images OCR cannot read). Runs are paced to avoid blocks and can be resumed by rerunning the same command.

## Install

Requires Python 3.13 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
brew install tesseract   # required on non-macOS (apt/dnf install tesseract-ocr on Linux); on macOS only a fallback to Apple Vision
```

Codex (`codex` CLI) is optional; it is only called for images OCR fails on, or for all images with `--vision`.

## Usage

```bash
uv run adrenalin ads-transparency AR04752318142645010433 --region IN --days 7
```

| Flag | Meaning |
|---|---|
| `--region IN` | ISO country code or `anywhere` (default) |
| `--days N` / `--start` / `--end` | Shown period (ads shown at any point in it) |
| `--variations` | One row per variation, not just the first |
| `--vision`, `--vision-limit N` | Send every image to Codex; cap Codex calls (default 200) |
| `--wait-on-block` | Wait out a Google block instead of exiting |
| `--stage list\|fetch\|extract\|export` | Run one stage only |
| `--proxy URL` | Proxy, repeatable to rotate |
| `--out DIR` | Output root (default `out/`) |

Exit code 2 means Google blocked the run; rerun later to continue. Run `uv run adrenalin ads-transparency --help` for everything.

## Output

```
out/ads-transparency/<advertiser-slug>/
  images/                        downloaded preview images
  runs/<REGION>_<start>_<end>/
    ads.csv                      the result
    state.sqlite3                resumable run state
out/.cache/                      cooldown and extraction caches
```

See `CONTEXT.md` for terminology and `docs/adr/` for design decisions. Tests: `uv run pytest`.
