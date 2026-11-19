# Adrenalin

Competitor-analysis tools for marketing teams. Each tool collects public data about a competitor's marketing (ads, listings, etc.) and turns it into tabular output.

## Language

### Google Ads Transparency

**Advertiser**:
A verified entity on Google Ads Transparency, identified by an advertiser ID such as `AR04752318142645010433`.
_Avoid_: Company, account (one company can have several Advertisers)

**Creative**:
One ad an Advertiser ran, identified by a creative ID such as `CR03912763628117295105`.
_Avoid_: Ad (when you mean a single record)

**Variation**:
One distinct rendered version of a Creative, such as a particular headline and description combination of a responsive search ad. Google often repeats an identical render; repeats are the same Variation.
_Avoid_: Version, render

**Format**:
The Creative's kind as Google classifies it: Text, Image or Video.
_Avoid_: Ad type (except as the CSV column label)

**Preview**:
Google's rendering of a Variation: either a static image (`simgad`), whose copy must be read by OCR, or a `content.js` script that carries the copy as text.
_Avoid_: Creative (when you mean its rendering), asset

**Sitelink**:
An extra link or callout shown under a Text ad: a link pill, a row of a link list, or a tagged callout line. Read from `content.js` Previews, or from a `simgad` image by OCR layout rules or Codex.

**Days shown**:
How many days Google reports a Creative as having run, taken directly from the listing.

**Region**:
The country an ad was shown in, given as an ISO code (`IN`) or `anywhere`; Google identifies it by a numeric geo ID.

**Shown period**:
The date range a query covers, matching Creatives that were *shown* at any point in it, not ones created in it.
_Avoid_: Created date, launch date

## Relationships

- An **Advertiser** has many **Creatives**
- A **Creative** has one or more **Variations**; the listing shows only the first
- Each **Variation** has exactly one **Preview**
- A query is one **Advertiser** × one **Region** × one **Shown period**

## Example dialogue

> **Dev:** "This **Creative** shows one headline in the CSV, but the client saw a different one."
> **Domain expert:** "That's another **Variation** of the same **Creative**. Rerun with variations on to get one row per **Variation**."
