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
  ordinary seed rows prefer the requested payment method, then the primary
  official name, then the oldest stable brand ID; they do not merge those
  records.
- Cashalot and Statuscard rows are catalog-bound. They require one unambiguous,
  active brand with at least one active MCC fact matching any declared channel
  and MCC selectors. A missing or ambiguous match is counted and skipped; these
  rows never create a partner-only store. Existing seed mappings are rechecked
  against the same active-MCC contract and repaired when one replacement exists.

## Reviewed sources and scope

### Cashalot

Source: <https://cashalot.by/stores/>

The package contains 21 approved featured/popular and reviewed partner rows,
including exact store-directory matches such as Seven Fridays and UniStore.
Physical stores are limited to offline payment; 21vek and 7745.by are online.
Rates are stored as the advertised total cash reward. 7 Karat and Xistore are
not loaded because the store catalog has no unambiguous exact active-MCC brand
for them. The package does not infer additional legal entities from similar
names.

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
- <https://stbank.by/private-client/payment-cards/debetovye-karty/statuskarta-deb/>
- <https://stbank.by/upload/iblock/320/6izapivrp0mrt3z9927f7b82h1h79yrk/Usloviya-nachisleniya-manibek_-s-11.08.2026.pdf>

The package uses current April/July COMBO partner announcements and the current
StatusBank terms effective 11 August 2026: 2.5% for 21vek online. The expired
1–15 June COMBO/21vek promotion is not loaded.

## Repeatability and human edits

`mcc-apply-partner-seed-20260830` runs in one SQLite transaction. A stable source
key inserts each brand mapping, offer, tier, and exclusion at most once. Existing
source rows are not rewritten, so later moderator edits, archive decisions, and
audit identity survive a repeat run. Catalog-bound rows may repair only their
seed-to-brand mapping; they do not rewrite an existing offer. The command prints
only aggregate counters, including missing and ambiguous catalog matches.

Before release, run it twice against a disposable database and verify that the
second run adds zero rows, then run `PRAGMA quick_check` and
`PRAGMA foreign_key_check`.
