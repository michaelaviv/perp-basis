---
name: basis
description: Print the latest pairwise basis (perp_mark vs CME) across all venues for the most recent snapshot. Optional argument is a product name (gold | wti) to filter.
---

Argument: optional product (`gold` or `wti`). Defaults to showing both.

When invoked:

1. Read `/Users/michael/Elwood - work/perp-basis/data/latest.json` (Read tool).
2. Build a small table: for each (product, perp_venue) compute
   `basis_bps = (perp.mark_price - cme.last_price) / cme.last_price * 10_000`,
   pulling the matching CME row (`venue == "cme_yahoo"`) for the same product.
3. Print as a markdown table with columns:
   `product | venue | mark | cme | basis (bps) | funding | 24h quote vol`.
4. If the user passed an argument, filter to rows where `product` matches.
5. Note the snapshot timestamp from `latest.json["ts"]`.

If `latest.json` is missing or empty, suggest the user run `/snapshot-now` first.
