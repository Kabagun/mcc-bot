# Partner reward snapshot, 2026-08-30

This ledger documents the static partner package bundled as
`mcc_bot/data/partner_seed_20260830.json`. It is intentionally a reviewed,
repeatable snapshot rather than a live scraper. The bot never fetches partner
sites while answering users.

## Calculation contract

- `additional` keeps the ordinary card result and adds a separately labelled
  partner component. `3% + 5% points` ranks as 8 but is not rendered as 8%.
- `total` replaces the ordinary card result with the advertised partner total.
- `offline`, `online`, and `any` are matched to the selected store payment
  method. A plain MCC lookup never uses this dataset.
- Amount tiers, per-transaction limits, offer dates, conditions, exclusions,
  and the official source are retained.
- A normal partnership exclusion suppresses only the partner component and
  preserves the card's ordinary MCC reward. The supplied Plushki exclusions
  are explicitly marked as program-level exclusions: they suppress ordinary
  Plushki points while preserving the card's ordinary cash reward.
- Exact names and reviewed aliases are reused. Similar names are not merged by
  fuzzy matching. If a legacy database contains several exact duplicates, the
  loader prefers the requested payment method, then the primary official name,
  then the oldest stable brand ID; it does not merge those records.

## Reviewed sources and scope

### Cashalot

Source: <https://cashalot.by/stores/>

The package contains 23 current official featured/popular and reviewed partner
rows, including exact store-directory matches such as Seven Fridays and
UniStore. Rates are stored as the advertised total
cash reward. The package does not attempt to infer additional legal entities from
similar names.

### Cactus

Source: <https://www.mtbank.by/cards/cactus/part/>

The current catalog was normalized by exact/case/reordered-name deduplication.
Obvious duplicate rows become one offer. Conflicting Streetcult rates are omitted
for manual review instead of choosing one automatically.

### Vitamin D / Plushki

Sources:

- `D:\Порядок начисления бонусных баллов.pdf`
- `D:\Перечень компаний, при осуществлении операций в которых не начисляются Бонусы.pdf`

The package contains all 13 promotions listed in the supplied current rules,
including 21vek's additional 5 points only for online payment, from 100 BYN and
with the documented per-operation cap. UniStore, MTS, Belpochta, and passenger
transport exclusions are separate rows. Passenger-transport rules are global
MCC exclusions rather than a fake public store.

### 1-2-3

Sources:

- <https://bnb.by/o-lichnom/bankovskie-kartochki/1-2-3/>
- <https://bnb.by/bonus/>

The complete current partner list is included. The official catalog describes
the displayed value as the overall maximum moneyback rate, so rows use `total`
cash rather than adding the displayed rate twice. Partners with genuinely
different online and offline rates remain separate rows.

### Izi

Source: <https://belarusbank.by/fizicheskim_licam/cards/bonusy/izi/>

Only exact recognizable networks are included. The source also contains many
local legal entities and generic store names; these are deliberately not mass
imported. In particular, a local store named Mila is not linked to the national
Mila brand.

### COMBOcard and Statuscard

Sources:

- <https://www.paritetbank.by/about/news/2026/aprel/novye-partnery-kombokarty-eshchye-bolshe-vygod-kazhdyy-den/>
- <https://www.paritetbank.by/about/news/2026/iyul/novye-partnery-kombokarty-v-iyule-eshche-bolshe-vygody-kazhdyy-den/>
- <https://stbank.by/>

The package uses current April/July COMBO partner announcements and the current
StatusBank 21vek online rate. The expired 1–15 June COMBO/21vek promotion is not
loaded. The current StatusBank site supersedes an older 2.5% note with 3.2% for
21vek online.

## Repeatability and human edits

`mcc-apply-partner-seed-20260830` runs in one SQLite transaction. A stable source
key inserts each brand mapping, offer, tier, and exclusion at most once. Existing
source rows are not rewritten, so later moderator edits, archive decisions, and
audit identity survive a repeat run. The command prints only aggregate counters.

Before release, run it twice against a disposable database and verify that the
second run adds zero rows, then run `PRAGMA quick_check` and
`PRAGMA foreign_key_check`.
