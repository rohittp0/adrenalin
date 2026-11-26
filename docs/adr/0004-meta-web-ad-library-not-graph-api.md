# Meta ads come from the web Ad Library's GraphQL, not the Graph API

Meta's official Ad Library API (`graph.facebook.com/ads_archive`) needs a verified developer token and, outside the EU, returns only political and issue ads. Lascade's competitors run commercial ads in India and the US, so the API would return almost nothing for them. We call the same internal GraphQL the public web Ad Library uses (`AdLibrarySearchPaginationQuery`, `AdLibraryV3AdDetailsQuery`), with `fetch` inside a Playwright-controlled Chrome context, as ads-transparency does after the ADR 0002 revision. No login is needed: a logged-out browser sees the full library.

## What the capture probe established (2026-11-26, MakeMyTrip, page 120740541030)

- A POST to `/api/graphql/` with only `lsd`, `doc_id` and `variables` (plus `__a=1`) works from a page on www.facebook.com. The `lsd` token is in the Ad Library HTML. The `doc_id`s are not: they come from JS bundles and change when Meta deploys, so the tool hard-codes them and treats `"The GraphQL document with ID … was not found."` as a distinct error that tells you to update them.
- Every response is `text/html`, and errors are HTTP 200 with an `errors` array, so success and block detection parse the body and never trust the status or content type.
- GraphQL pages hold 10 Creatives. The Inventory count (`count`) is only in the Ad Library HTML, which also embeds the first 30 Creatives and their cursor.
- The listing already carries copy, Cards, DCO Variations, Collation group, dates, platforms and media URLs. Only EU reach needs the per-Creative Detail request.

## Considered options

- **Graph API:** documented and stable, but commercial ads are missing outside the EU. Could be added later for EU-only reach if a token becomes available.
- **Scroll the web UI and intercept responses:** rejected for the same reasons as in ADR 0002 (slow, memory grows over long scrolls). The browser is used only as a same-origin `fetch` context.

## Consequences

- We don't yet know what Meta sends when it refuses a logged-out client under load: the run-to-block probe was declined so that the office IP isn't put at risk. Until a real run hits one, any GraphQL answer that isn't a JSON `data` object, and any redirect off the Ad Library, is treated as a block, and the full response goes to `run.log`.
- When Meta changes the `doc_id`s, runs fail until the constants are updated.
