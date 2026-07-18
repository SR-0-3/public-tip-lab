# Forward-test methodology

## Research question

If every eligible public recommendation from the three configured subreddits receives the same one-unit paper stake at its first captured quoted price, is the combined realised return positive over a sufficiently long forward sample?

This is a prospective observational experiment, not a historical backtest. The clock begins only when the collector is first run.

## Unit of observation

The primary observation is a **Reddit-recommended slip**.

- A single is one slip with one leg and one unit staked.
- A parlay/acca is one slip with two or more legs and one unit staked on the whole quoted price.
- Two different Reddit users recommending the same selection are two observations because the aim is to measure the Reddit recommendation stream. The same Reddit source ID is never ingested twice.

Secondary analyses may aggregate by user, subreddit, sport, league, market, or slip type.

## Inclusion criteria

An automatically accepted slip must have:

1. a first-seen source timestamp;
2. an explicit selection;
3. usable quoted odds greater than 1.00 in decimal form after conversion;
4. a uniquely matched externally listed event;
5. an event start later than actual placement time plus the lead-time buffer;
6. an externally matched standard market and a quoted price within tolerance;
7. sufficiently high parser confidence.

Manual acceptance must satisfy items 1–5 and occurs at the real review time, not the earlier collection time.

## Reddit access and retention

Live collection is disabled until the owner confirms explicit Reddit Data API
approval in `.env`. Raw Reddit bodies, titles, links, revisions, and public
usernames are minimized after the configured retention window (48 hours by
default). The body hash and structured bet record remain. Public usernames are
pseudonymized at ingestion so longitudinal grouping can continue without storing
the username. Any stricter conditions in Reddit's approval control.

## Exclusion criteria

- results, recaps, winning-slip celebrations, and other obviously retrospective content;
- already started or completed events;
- missing odds;
- image-only tips in version 0.1;
- ambiguous events, selections, prices, or parlay boundaries that are not manually resolved before start;
- duplicate Reddit source IDs.

## Payoff rule

For stake `s` and quoted decimal odds `d`:

- win: `s × (d − 1)` units profit;
- loss: `−s` units profit;
- push or void: `0` units profit.

For a parlay with a pushed or voided leg, that leg is removed and the prices of
the surviving winning legs are multiplied only when every surviving leg has a
separately captured quoted price. If those prices were not posted, the slip waits
for documented manual settlement; the program never invents an adjusted payout.

The default is `s = 1`. A user's own claimed stake or confidence is ignored.

## Primary outcome

`ROI = total realised profit / total settled stake`

Open slips do not enter realised ROI. They are reported as open liability.

## Supporting outcomes

- cumulative realised P/L and paper bankroll;
- hit rate among decisive wins/losses;
- maximum drawdown;
- ROI by subreddit, sport, market, slip type, and quoted-odds band;
- individual tipster records subject to a minimum sample threshold;
- bootstrap interval for ROI;
- observed wins versus raw implied-odds expectation.

## Interpretation guardrails

- Do not stop the experiment because current performance looks unusually good or bad.
- Choose a minimum horizon before drawing a conclusion, preferably both a time target and a settled-slip target.
- Treat subgroup and tipster rankings as exploratory unless independently replicated.
- Report collection downtime, manual-review exclusions, provider gaps, and the count of later Reddit edits.
- Inspect the rejected-record feed so parser and review exclusions remain visible rather than disappearing from the sample.
- Never merge demo records with the live experiment database.
