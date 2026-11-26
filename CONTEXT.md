# Adrenalin

Competitor-analysis tools for marketing teams. Each tool collects public data about a competitor's marketing (ads, listings, etc.) and turns it into tabular output.

## Language

### Shared by all ad tools

**Creative**:
One ad, as the ad platform identifies it: a Google creative ID such as `CR03912763628117295105`, or a Meta Library ID (`ad_archive_id`). One CSV row by default.
_Avoid_: Ad (when you mean a single record)

**Variation**:
One distinct version of a Creative that a viewer may be shown instead of another: a headline and description combination of a Google responsive search ad, or one version of a Meta dynamic-creative ad. Identical renders are the same Variation. `--variations` gives one row per Variation.
_Avoid_: Version, render

**Region**:
The country a query filters on, given as an ISO code (`IN`) or `anywhere`; Google identifies it by a numeric geo ID; Meta by the ISO code, with `anywhere` meaning `ALL`. It says which Creatives a query matches, not where each one ran: for that, see Shown country (Google) and EU reach (Meta).
_Avoid_: Shown country

**Shown period**:
The date range a query covers, matching Creatives that were *shown* at any point in it, not ones created in it.
_Avoid_: Created date, launch date

**Inventory**:
Every Creative a query matches, with the fields the listing gives for free (format, first and last shown, days shown). Always collected in full; counts, dates and start/stop conclusions come only from a complete Inventory.
_Avoid_: Sample (for the full listing)

**Incomplete run**:
A run whose Inventory listing did not finish (blocked, interrupted or crashed). Its CSV is still written, but counts and start/stop conclusions must not be drawn from it until a rerun completes the listing.

### Google Ads Transparency

**Advertiser**:
A verified entity on Google Ads Transparency, identified by an advertiser ID such as `AR04752318142645010433`.
_Avoid_: Company, account (one company can have several Advertisers)

**Ads account**:
A Google Ads account an Advertiser runs Creatives from. One Advertiser can have many (Rio: at least 17, one website each). Google exposes only an obfuscated ID, and only on `content.js` Previews; the ID is stable and maps one-to-one to real accounts (checked against Lascade's own). For other Creatives it is inferred when their website belongs to exactly one known Ads account, and marked as inferred.
_Avoid_: Account (unqualified), Advertiser

**Ad group**:
The group inside an Ads account that a Creative belongs to. Exposed only on `content.js` Previews, never inferred. Transparency shows too few per Ad group to count Creatives per Ad group: Lascade's Hotels70 search has about 3 ads per Ad group, while Transparency suggested 1.

**Format**:
The Creative's kind as Google classifies it: Text, Image or Video.
_Avoid_: Ad type (except as the CSV column label)

**Preview**:
Google's rendering of a Variation: either a static image (`simgad`), whose copy must be read by OCR, or a `content.js` script that carries the copy as text.
_Avoid_: Creative (when you mean its rendering), asset

**Broken Preview**:
A Preview that Google archived as a picture of its own error page ("500. That's an error."). It is permanent: downloading it again returns the same image. Copy is then read from another Variation of the same Creative when one is not broken.

**Sitelink**:
An extra link or callout shown under a Text ad: a link pill, a row of a link list, or a tagged callout line. Read from `content.js` Previews, or from a `simgad` image by OCR layout rules or Codex.

**Days shown**:
How many days Google reports a Creative as having run, taken directly from the listing.

**Shown country**:
A country Google reports a Creative was actually shown in during its life, with the last date it was shown there. For EU countries Google also gives the first date shown there and an impression range (such as 10K–15K), the only reach figure Transparency publishes. Checked against Lascade's own targeting: an ad's Shown countries fall inside what its campaign targeted (55 of 59 checked ads; the other 4 fit targeting changed after they ran), and a worldwide campaign shows only the few countries that actually served it.
_Avoid_: Region, targeting

**Message**:
A Creative's headline and description pair, normalised for case, spacing and punctuation. Two Creatives with the same Message say the same thing even if their images differ.
_Avoid_: Copy (when you mean the unit of novelty), full text

**Copy sample**:
The Creatives selected for copy reading. By default a stratified random sample covers observable Ads accounts, launch periods, and running/stopped groups. Existing reads outside the representative design remain exploratory. Explicit novelty mode hunts for new Messages separately.
_Avoid_: Sample (unqualified)

**Coverage**:
A legacy field containing one minus the singleton fraction of countable Messages in novelty mode. Because those reads are adaptively selected, it is a repetition diagnostic, not a validated population Coverage estimate or percentage of distinct Messages discovered. Representative sampling leaves Coverage unset and stops after its saved design is processed.

**Copy status**:
What happened to a Creative's copy in a run: `read`; `not_sampled` (in the Inventory but outside the Copy sample); `pending` (chosen for the sample but not read yet); `no_copy` (the Preview carries no ad text, as with most video and app-install ads; expected, not an error); `preview_broken` (every Variation is a Broken Preview); `unreadable` (the Preview's format isn't understood, OCR and Codex both failed, or reading failed on every retry). A read that failed but will be retried on the next run stays `pending`.
_Avoid_: Blank copy (as a catch-all)

### Meta Ad Library

**Page**:
A Facebook Page that runs ads, identified by the page ID in an Ad Library URL (`view_all_page_id=...`). The Meta counterpart of an Advertiser: one company can have several Pages, and one run covers one Page.
_Avoid_: Advertiser (for Meta), account

**Display format**:
The Creative's kind as Meta classifies it: Image, Video, Carousel, DCO (dynamic creative: Meta picks one of several Variations per viewer) or DPA (catalog ad: products filled in from a catalog). Seen on MakeMyTrip (IN, 2026-11-26): DCO and DPA made up two thirds of the listing.
_Avoid_: Format (Google's Text/Image/Video), ad type

**Card**:
One panel of a Carousel or DPA Creative. A viewer sees all the Cards of a Variation together, so Cards are part of one Variation, never Variations of their own, and go into columns rather than rows. Meta's listing puts both Cards and DCO Variations in the same `cards` list; the Display format says which they are.
_Avoid_: Variation, version

**Template copy**:
Catalog placeholders such as `{{product.brand}}` that Meta shows in place of copy on DPA and DCO Creatives. Never real copy: the copy is read from the Variations or Cards instead.

**Collation group**:
Meta's grouping of Creatives that use the same creative and text ("12 ads use this creative and text"). Each Creative in it keeps its own Library ID, dates and platforms, so the group is a column, not a row.
_Avoid_: Duplicate, Message (which is normalised headline and description text, across any Creatives)

**Inactive Creative**:
A Meta Creative that has stopped running. Meta lists only some of them outside the EU: MakeMyTrip (IN, 2026-11-26) showed 54 inactive Creatives against about 870 active, back to 2023. So outside the EU, counts of stopped Creatives and start/stop conclusions are not complete even when the Inventory is.
_Avoid_: Stopped (as if every stopped Creative were listed)

**EU reach**:
For a Meta Creative delivered in the EU (or the UK), the reach Meta publishes: total reach, and reach by country, age and gender, with the payer and beneficiary. It is the only reach figure Meta publishes for commercial ads (the Meta counterpart of a Shown country's impression range). It comes from the Creative's detail, one extra request per Creative, so it is fetched only on request (`--eu-reach`).
_Avoid_: Impressions, audience size

## Relationships

- An **Advertiser** has many **Creatives** (Google)
- An **Advertiser** has one or more **Ads accounts**; an **Ads account** has many **Ad groups**; a **Creative** belongs to one **Ad group**
- A Google **Creative** has one or more **Variations**; the listing shows only the first
- Each **Variation** has exactly one **Preview**
- A Google query is one **Advertiser** × one **Region** × one **Shown period**
- A **Page** has many **Creatives**; a Meta query is one **Page** × one **Region** × one **Shown period**
- A Meta **Creative** belongs to at most one **Collation group**, and has one or more **Variations** (several only for DCO); a **Variation** has zero or more **Cards**

## Example dialogue

> **Dev:** "This **Creative** shows one headline in the CSV, but the client saw a different one."
> **Domain expert:** "That's another **Variation** of the same **Creative**. Rerun with variations on to get one row per **Variation**."
