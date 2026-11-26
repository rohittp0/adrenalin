# Call Google's internal RPCs over plain HTTP, not a headless browser

The Ads Transparency site is an empty JS shell that loads data from internal RPCs (`SearchService/SearchCreatives`, `LookupService/GetCreativeById`). We call those RPCs directly with `requests` instead of driving Playwright. The earlier `gads.py` script collected a year of daily data this way without being blocked, and throughput is set by our own pacing either way, so a browser would only add weight (minutes for small advertisers, hours for large ones) without adding data.

## Considered Options

- **Playwright:** looks more like a real user, but is slower and its memory grows during long scrolls. Revisit only if plain HTTP starts getting blocked despite pacing.
- **Playwright to render `content.js` Previews that fail to parse:** rejected; those rows are left blank with a warning instead.

## Revision: Playwright-controlled Chrome transport

The user approved Playwright after Python requests and curl-cffi continued to
receive `/sorry/`. The inspection tool's HTTP client worked, but the installed
local Playwright APIRequestContext was also blocked. Fresh local headless Chrome
loaded the site successfully.

Transparency RPCs now use `fetch` inside a fresh Playwright-controlled Chrome
context. This does not scroll or scrape the UI, or import browser-profile cookies.
RPC fetches use same-origin cookies from the scraper-owned context. Chrome supplies its own network fingerprint
and browser headers. Other hosts retain requests. Pacing, proxies, persisted
cooldowns and block handling remain in PacedSession.

A dedicated owner thread serializes Playwright calls because pipeline stages
run on different threads. Browser contexts are reused per proxy, with a page per
origin. The origin is loaded once to establish a same-origin fetch context.
Closing the session closes browser contexts and Chrome, stops Playwright and
joins its owner thread. Derived sessions own separate lazy transports.

Google Chrome must be installed. The earlier curl-cffi experiment is removed;
it did not unblock the endpoint.

## Manual CAPTCHA handoff

`--solve-captcha` normally runs Chrome headlessly and opens a visible browser
only for a challenge. After completion it closes that window and restarts
headlessly, transferring per-proxy cookies and local storage in memory. A `/sorry/` response triggers a service-root navigation and a
bounded ten-minute wait for the browser to return to the original origin. The
same RPC is retried once; if still blocked, normal cooldown handling applies.
No CAPTCHA is solved automatically. The unexpired `GOOGLE_ABUSE_EXEMPTION` cookie
is persisted in owner-only files under the output cache, separated by proxy, and
restored on restart. Other cookies and local storage are not persisted. Expiry is
checked before restore; Google can still reject an exemption, for example after an IP change.

Read-only RPCs retry timeouts and connection failures up to three total attempts,
through the normal pacing and cooldown checks. TLS errors are not retried. A browser
`Failed to fetch` discards the affected page, keeping its context and cookies; the next
attempt loads the service origin again so a stale page or CAPTCHA can be handled.
