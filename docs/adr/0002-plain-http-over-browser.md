# Call Google's internal RPCs over plain HTTP, not a headless browser

The Ads Transparency site is an empty JS shell that loads data from internal RPCs (`SearchService/SearchCreatives`, `LookupService/GetCreativeById`). We call those RPCs directly with `requests` instead of driving Playwright. The earlier `gads.py` script collected a year of daily data this way without being blocked, and throughput is set by our own pacing either way, so a browser would only add weight (minutes for small advertisers, hours for large ones) without adding data.

## Considered Options

- **Playwright:** looks more like a real user, but is slower and its memory grows during long scrolls. Revisit only if plain HTTP starts getting blocked despite pacing.
- **Playwright to render `content.js` Previews that fail to parse:** rejected; those rows are left blank with a warning instead.
