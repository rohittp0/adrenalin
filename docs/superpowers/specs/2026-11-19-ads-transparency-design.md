# ads-transparency: design

Agreed in a design discussion on 2026-11-19. Terms are defined in `CONTEXT.md`. The decisions behind the run model are in `docs/adr/0001` and `0002`.

## Purpose

`adrenalin ads-transparency <advertiser_id>` exports every Creative that one Google Ads Transparency Advertiser showed in one Region and one Shown period to a CSV, including the ad copy.

## Inputs

| Flag | Meaning |
|---|---|
| `advertiser_id` (positional) | e.g. `AR04752318142645010433` |
| `--region` | ISO alpha-2 (`IN`, `US`) or `anywhere` (default). Mapped to Google geo ID = 2000 + ISO numeric (India → 2356). |
| `--start`, `--end` | Inclusive `YYYY-MM-DD`. Shown-period semantics. Default: no date filter. |
| `--days N` | Shortcut: `end = today`, `start = today - N + 1`. Mutually exclusive with `--start/--end`. |
| `--variations` | One row per Variation (one `GetCreativeById` call per Creative). Default: one row per Creative from the listing only. |
| `--vision` | Send every image Preview to Codex instead of only OCR failures. |
| `--vision-model` | Default `gpt-5.6-luna` at reasoning effort `low` (its lightest level), or `$ADRENALIN_VISION_MODEL`. `gpt-5-mini` is rejected for ChatGPT-account Codex; `gpt-6-luna` and `gpt-6.1-terra` also work. |
| `--vision-limit` | Max Codex calls per run, default 200. |
| `--wait-on-block` | Wait out `/sorry/` cooldowns instead of exiting. |
| `--stage` | `list`, `fetch`, `extract` or `export`: run one stage only. |
| `--out` | Output root, default `out/` (git-ignored). |
| `--proxy` | Repeatable. Rotated per request. Default none. |

## Output

```
out/ads-transparency/<advertiser-name-slug>/
  entity.json                    # {"id": "AR…", "name": "Lascade LLC"}; a different id in an existing folder is an error
  images/<simgad_id>.png         # shared across runs of this advertiser
  runs/<region>_<start|all>_<end|all>[_variations]/
    ads.csv
    state.sqlite3                # queue + checkpoint + the options the run was started with
out/.cache/
  extract.sqlite3                # (image_id, method, model) → extracted fields; shared by all tools
  cooldown.sqlite3               # per-host cooldown; shared by all runs
  advertisers.json               # advertiser_id → name, avoids a lookup call per run
```

CSV columns, in order:
`transparency_url, creative_id, variation, format, region, first_shown, last_shown, days_shown, website, landing_url, display_url, brand, headline, description, cta, sitelinks, full_text, video_id, video_url, video_title, preview_url, image_file, image_width, image_height, logo_url, image_urls, extraction_method, extraction_model, extraction_confidence`

- No advertiser columns: the folder is the advertiser.
- `transparency_url` = `https://adstransparency.google.com/advertiser/{AR}/creative/{CR}?region={region}`.
- `variation` is 1-based. It is always 1 without `--variations`. Variations with an identical Preview (same `simgad` ID or same `content.js` URL) are collapsed into one, because Google often repeats the same render.
- `landing_url` is the decoded `adurl=` from `content.js`, for example a Play Store link for app-install ads. It is empty for `simgad` ads.
- `sitelinks` and `image_urls` are joined with ` | `.
- `extraction_method` is one of `content_js`, `apple_vision`, `tesseract`, `codex` or empty. `extraction_confidence` is a number from 0 to 1. Codex confidence is self-reported.

## Where copy comes from

| Preview | Source | Method |
|---|---|---|
| `content.js`, video / app install | YouTube ID from `ytimg.com/vi/<id>`, the markup strings (brand, description, CTA such as `Install`), `adurl=` → `landing_url`; `video_title` from YouTube oEmbed (any error → blank) | `content_js` |
| `content.js`, image/Demand Gen | `google_template_data.adData`: `headline`, `body`, `advertiser`, `logo`, `squareImage`/`landscapeImage` | `content_js` |
| `content.js`, text | Markup strings: brand, display URL, headline, description, sitelinks | `content_js` |
| `simgad` image | OCR (Apple Vision on macOS, else Tesseract) + layout rules | `apple_vision` / `tesseract` |
| `simgad`, OCR failed or `--vision` | `codex exec -m gpt-5.6-luna -c model_reasoning_effort=low -i <img> --output-schema`, stdin closed (it otherwise waits for input) | `codex` |

- OCR fails when there is no text, the mean confidence is below 0.6, or a Text-format image doesn't match brand → display URL → headline → description.
- Codex runs at most 2 calls at a time and 200 per run. Over the cap, the OCR result is kept and the run ends with a warning giving the skipped count.
- `content.js` parsing is additive: video ID, `adData`, markup strings and `adurl` are all collected, not first-match-wins.
- The `overlay=` query parameter of a `content.js` URL is gzipped protobuf containing `headline`, `description` and `visurl`. Use it when the fetched script yields no copy.
- If both find nothing, the row is left blank with a warning. No headless browser.

## Run model

The stages are List (Transparency RPC), Detail (only with `--variations`; RPC), Fetch (CDN: images and `content.js`), Extract (OCR/Codex/oEmbed) and Export (CSV). They run concurrently in one process from a SQLite queue.

- **Backpressure:** List pauses while Detail + Fetch + Extract backlog > 200. Detail pauses while Fetch + Extract backlog > 50. Fetch pauses while the Extract backlog > 50.
- **Pacing, per host, as minimums:** `adstransparency.google.com` 6 s ± 2 s jitter, CDN hosts and `youtube.com` 0.5 s.
- **`429`:** backoff `min(60·2^attempt, 900)` s or `Retry-After` if larger, up to 3 retries, then treated as blocked.
- **`/sorry/` or reCAPTCHA HTML:** 900 s cooldown saved in `cooldown.sqlite3`. The stage stops and its items stay pending. Downstream stages drain, the CSV is exported, and the tool exits with code 2 and "blocked until HH:MM, rerun to continue". With `--wait-on-block`, it sleeps until the cooldown ends and retries.
- Rerunning the same command resumes. Items left running by a crash return to pending.
- Failed items are retried on the next run, up to 3 attempts in total. A run that ends with failed items exits 1, after the CSV is written.
- If a resumed list cursor is rejected (error or non-JSON), listing restarts from page 1. Creatives already queued are not duplicated. Cursors were confirmed valid after 11 minutes; longer gaps are untested.
- Rerunning a finished run whose window is open (no `--end`, or `--end` ≥ the day the listing finished) lists again from page 1. That updates `last_shown` and `days_shown` and queues new creatives, without re-extracting existing ones.
- `--vision` on a rerun re-queues image rows that weren't extracted by Codex. Rows skipped because of the Codex limit are re-queued on the next run automatically.

## Out of scope

The counts-only mode, Playwright, several regions per run, variation counts without `--variations`, and OCR of images inside `content.js` ads.
