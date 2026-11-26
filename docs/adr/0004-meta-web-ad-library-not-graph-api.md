# Meta ads come from the web Ad Library's GraphQL, not the Graph API

Meta's official Ad Library API (`graph.facebook.com/ads_archive`) needs a verified developer token and, outside the EU, returns only political and issue ads. Lascade's competitors run commercial ads in India and the US, so the API would return almost nothing for them. We call the same internal GraphQL the public web Ad Library uses (`AdLibrarySearchPaginationQuery`, `AdLibraryV3AdDetailsQuery`), with `fetch` inside a Playwright-controlled Chrome context, as ads-transparency does after the ADR 0002 revision. No login is needed: a logged-out browser sees the full library.

## What the capture probe established (2026-11-26, MakeMyTrip, page 120740541030)

- A POST to `/api/graphql/` with `doc_id`, `variables` and `__a=1` works through `PacedSession`'s Playwright transport, from a page on the bare `https://www.facebook.com/` origin. The `lsd` token is not checked for logged-out requests: an empty or wrong one returns normal data. A null cursor returns page 1, so the whole listing is GraphQL.
- The `doc_id`s are not in the HTML: they come from JS bundles and change when Meta deploys. The tool hard-codes them and treats `"The GraphQL document with ID … was not found."` as a distinct error that tells you to update them.
- Every response is `text/html`, and errors are HTTP 200 with an `errors` array, so success and block detection parse the body and never trust the status or content type.
- GraphQL pages hold 10 Creatives, and there is no Inventory count in them. The count (`~920 results`) appears only in the Ad Library HTML, and an in-page `fetch` of that HTML gets HTTP 403: it works only as a real navigation. So runs carry no Inventory estimate. Completeness comes from `has_next_page`.
- The `startDate` variable (`{"min": "YYYY-MM-DD", "max": "YYYY-MM-DD"}`) filters by Shown period: March 2023 returned exactly the one Creative that ran 2023-03-05 to 03-06.
- The listing already carries copy, Cards, DCO Variations, Collation group, dates, platforms and media URLs. Only EU reach needs the per-Creative Detail request. fbcdn media download with plain `requests`.

## Considered options

- **Graph API:** documented and stable, but commercial ads are missing outside the EU. Could be added later for EU-only reach if a token becomes available.
- **Scroll the web UI and intercept responses:** rejected for the same reasons as in ADR 0002 (slow, memory grows over long scrolls). The browser is used only as a same-origin `fetch` context.

## Consequences

- We don't yet know what Meta sends when it refuses a logged-out client under load: the run-to-block probe was declined so that the office IP isn't put at risk. Until a real run hits one, these are treated as a block of `www.facebook.com` (the host goes into the shared cooldown and the run stops cleanly; a rerun resumes), and the response goes to `run.log`:
  - a redirect to, or a page at, `/login` or `/checkpoint`;
  - any HTTP status of 400 or more except 429, on the origin page the browser opens or on a GraphQL request (Meta answers queries with HTTP 200, errors included). A 429 takes the back-off-and-retry path every host shares;
  - a GraphQL answer that isn't JSON, or a JSON error that reads as a rate limit;
  - three detail (EU reach) requests in a row answered with GraphQL errors (`DocNotFound` excluded). A single errors-only answer stays an ordinary failure of its queue item: a listing page with an expired cursor answers the same way, so one error can't be told apart from a block.
- A redirect to `/login` or `/checkpoint` is a block, never a challenge for `--solve-captcha`: solving it would mean logging in, and the scraper would then run under someone's Facebook account. The Meta host gets a `Challenge` only once a real logged-out checkpoint has been seen.
- When Meta changes the `doc_id`s, runs fail until the constants are updated.
- The live check (`tests/tools/meta_ad_library/test_live.py`, MakeMyTrip, region IN, active, List stage, expecting at least 100 Creatives) was written on 2026-11-26 but has not been run yet; record its result here (Creatives listed, requests, duration, any block signature) once it has.
