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

**Region**:
The country an ad was shown in, given as an ISO code (`IN`) or `anywhere`; Google identifies it by a numeric geo ID.

**Shown period**:
The date range a query covers, matching Creatives that were *shown* at any point in it, not ones created in it.
_Avoid_: Created date, launch date

**Inventory**:
Every Creative a query matches, with the fields the listing gives for free (format, first and last shown, days shown). Always collected in full; counts, dates and start/stop conclusions come only from a complete Inventory.
_Avoid_: Sample (for the full listing)

**Message**:
A Creative's headline and description pair, normalised for case, spacing and punctuation. Two Creatives with the same Message say the same thing even if their images differ.
_Avoid_: Copy (when you mean the unit of novelty), full text

**Copy sample**:
The Creatives in a run whose copy has been read. Chosen to find as many new Messages as possible while covering every launch date and both still-running and stopped Creatives.
_Avoid_: Sample (unqualified)

**Coverage**:
The estimated share of the Advertiser's Messages the Copy sample has already found (Good–Turing: one minus the share of sampled Creatives whose Message was seen only once). Low-confidence reads don't count, because OCR misreads would look like new Messages.

**Copy status**:
What happened to a Creative's copy in a run: `read`; `not_sampled` (in the Inventory but outside the Copy sample); `pending` (chosen for the sample but not read yet); `no_copy` (the Preview carries no ad text, as with most video and app-install ads; expected, not an error); `preview_broken` (every Variation is a Broken Preview); `unreadable` (the Preview's format isn't understood, OCR and Codex both failed, or reading failed on every retry). A read that failed but will be retried on the next run stays `pending`.
_Avoid_: Blank copy (as a catch-all)

**Incomplete run**:
A run whose Inventory listing did not finish (blocked, interrupted or crashed). Its CSV is still written, but counts and start/stop conclusions must not be drawn from it until a rerun completes the listing.

## Relationships

- An **Advertiser** has many **Creatives**
- An **Advertiser** has one or more **Ads accounts**; an **Ads account** has many **Ad groups**; a **Creative** belongs to one **Ad group**
- A **Creative** has one or more **Variations**; the listing shows only the first
- Each **Variation** has exactly one **Preview**
- A query is one **Advertiser** × one **Region** × one **Shown period**

## Example dialogue

> **Dev:** "This **Creative** shows one headline in the CSV, but the client saw a different one."
> **Domain expert:** "That's another **Variation** of the same **Creative**. Rerun with variations on to get one row per **Variation**."
