# Data dictionary

## Core states

| State | Meaning |
|---|---|
| `REVIEW` | Parsed but not paper-placed |
| `OPEN` | Accepted before the earliest event start |
| `IN_PLAY` | Earliest event has started and the slip is unresolved |
| `NEEDS_SETTLEMENT` | A completed event or parlay adjustment needs owner evidence |
| `WON`, `LOST`, `PUSH`, `VOID` | Final paper result |
| `REJECTED` | Permanently excluded from the experiment |

## Main tables

| Table | Purpose |
|---|---|
| `source_items` | Immutable first-seen Reddit or demo snapshot |
| `source_revisions` | Later changed body observed without rewriting the first snapshot |
| `slips` | One fixed-stake single or parlay observation |
| `legs` | Selections and provider-event mappings inside each slip |
| `odds_snapshots` | External outcome prices seen during validation |
| `runs` | Collector, validation, and settlement run outcomes |
| `audit_log` | Material state transitions and manual actions |

## Important timestamps

| Field | Meaning |
|---|---|
| `created_at` on source | Reddit's reported source creation time |
| `collected_at` | First time this program saw the source |
| `placed_at` | Time the slip actually entered the paper experiment |
| `event_start_at` | Earliest provider start across the slip's legs |
| `settled_at` | Time the final paper result was recorded |

All stored timestamps use UTC ISO 8601 form.
