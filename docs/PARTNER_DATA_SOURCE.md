# Partner reward snapshot, 2026-08-30

## Reviewed correction, 2026-08-31

The follow-up package is a one-time static correction, not a synchronizer. It
canonicalizes only the reviewed `21век` and `Unistore` variants while retaining
all existing MCC evidence and extra aliases. `21век` has Cashalot 1.01% for any
payment method, 1-2-3 2% online, Statuscard 2.5% online, and the existing Vitamin
D additional 5-point online promotion with its 100 BYN minimum, 50-point
per-operation cap, conditions, and dates. `Unistore` retains MCC 5411, Cashalot
1.01% offline, and the static Vitamin D exclusion.

All other active Statuscard partner offers are archived. No Sber, Alfa,
Paritetbank, VTB, or other StatusBank partner is added by this correction. The
cleanup rewrites a stable-keyed rule only when it still exactly matches the
2026-08-30 snapshot; a manual edit aborts the transaction. The matching seed
then inserts only missing rows, so a second cleanup-and-seed run makes no data
changes.

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

The local collector follows the linked pagination pages, normalizes exact
partner URL/image/name identities and records a SHA-256 for every response in
`snapshot.source.page_sha256`. Exact duplicates become one offer; conflicting
rates, channels or identities are held for manual review instead of choosing a
winner. The emitted card is `cactus_mtbank`, `mode=total`, `reward_kind=points`.

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
different online and offline rates remain separate rows. The local collector
uses <https://bnb.by/bonus/>, requires the page-level total/additional semantics
to be present, and records the response SHA-256 in `snapshot.source.html_sha256`.
Its output is `bnb_1_2_3`, `mode=total`, cash: the displayed rate is already the
overall rate and must not be added to BNB's base rate a second time.

### Izi

Source: <https://belarusbank.by/fizicheskim_licam/cards/bonusy/izi/>

The local collector reads the exact `var locations` map entries and their linked
detail tables. Each candidate keeps the source entity ID, legal-entity name,
store label, address, MCC and rate. Rows are grouped only within one source
entity ID and exact normalized store label; they are never fuzzy-mapped to a
local merchant. If the same visible store label belongs to multiple source
entities, the public label includes the exact legal entity so the groups remain
distinct. A complete address/store pair is offline; the explicit
`Интернет-магазин` marker is online. Missing or ambiguous detail, entity,
outlet, MCC, rate or channel evidence either aborts collection or remains a
fail-closed `action=hold` problem, never an `any`-channel offer. The listing
response is recorded as
`snapshot.source.html_sha256`, and every linked detail response as
`snapshot.source.detail_sha256`.

The dated 2026-08-30 package included only four reviewed recognizable networks.
The explicit collector is broader: it emits every safe source row, including
local legal entities and generic store names, but keeps each source entity
separate. It does not infer that a local store named Mila belongs to the
national Mila brand.

### COMBOcard and Statuscard

Sources:

- <https://www.paritetbank.by/about/news/2026/aprel/novye-partnery-kombokarty-eshchye-bolshe-vygod-kazhdyy-den/>
- <https://www.paritetbank.by/about/news/2026/iyul/novye-partnery-kombokarty-v-iyule-eshche-bolshe-vygody-kazhdyy-den/>
- <https://stbank.by/private-client/payment-cards/debetovye-karty/statuskarta-deb/>
- <https://stbank.by/upload/iblock/320/6izapivrp0mrt3z9927f7b82h1h79yrk/Usloviya-nachisleniya-manibek_-s-11.08.2026.pdf>

The package uses current April/July COMBO partner announcements and the current
StatusBank terms effective 11 August 2026: 2.5% for 21vek online. The expired
1–15 June COMBO/21vek promotion is not loaded.

The Statuscard collector reads the official maniback list at
<https://stbank.by/local/ajax/partners_maney-back.php> and the linked detail
pages (`https://stbank.by/local/ajax/our_partners_maney-back_detail.php?ELEMENT_ID={id}`). It emits only one
unambiguous `манибэк` percentage per source element as
`statusbank_statuskarta`, `mode=total`, cash; discount-only rows and changed or
incomplete detail structures are held. The list response is preserved as
`snapshot.source.html_sha256`, with each linked detail response in
`snapshot.source.detail_sha256`.

## Bounded Paritet page collector, 2026-09-10

The official Paritet partner page is server-rendered HTML, not a bot runtime
endpoint. The explicit collector in `mcc_bot.paritet_source` reads either a
saved response or a URL and writes a reviewed v1 snapshot plus a deterministic
JSON report. It reads partner records only from `#all`; the category panels are
expected copies and are counted as ignored. A duplicate inside `#all`, an
unknown or conflicting category copy, a missing Bitrix ID, or a changed rate
structure aborts the run before either output is written.

The command normalizes only the first visible percentage row (the existing
`paritet_combo` card, `mode=total`, cash) and uses stable keys containing the
numeric Bitrix ID, for example
`paritet:111407:paritet_combo:offline`. Payment conditions classify records as
`online`, `offline`, or `any`; no MCC is inferred. ORO (Bitrix ID 104700) and
Adrenalin (111408) are retained as structured `hold` problems because their
visible terms cannot safely be represented by one ordinary offer. They are not
emitted as offers.

For a saved response, use a caller-owned output path:

```powershell
.\.venv\Scripts\python.exe -m mcc_bot.paritet_source `
  --html temp/paritet-partners.html `
  --output temp/paritet-snapshot.json `
  --report temp/paritet-report.json
```

`mcc-collect-paritet` is the equivalent installed command. Passing `--url
https://www.paritetbank.by/private/partners/` performs one explicit bounded
fetch; no periodic collection or startup fetch is configured. The generated
snapshot is suitable for a later reviewed local partner-batch preview. It does
not change SQLite, publish data, or silently resolve held problems. Its
`snapshot.source.html_sha256` records the exact response body used for the
snapshot; the report repeats the same source provenance.

## Local collector and approval-batch contract

The five adapters above are explicit local review tools, not bot runtime
refreshers. `mcc-collect-official-partners --source cactus|bnb|izi|statuscard`
and `mcc-collect-paritet` write only caller-selected snapshot/report files.
Each output is deterministic and retains response SHA-256 provenance: Cactus
uses `page_sha256`; BNB and Paritet use `html_sha256`; Izi and Statuscard use
`html_sha256` for the listing plus `detail_sha256` for linked pages. A missing
selector, changed reward structure, duplicate identity or unsafe mapping is
reported as a hold or aborts the collector; it is never guessed into a partner
row.

Cashalot and Vitamin D/Plushki remain reviewed static fallbacks in the dated
seed packages. They are intentionally outside the live official collectors;
the static seed remains the source for those offers and exclusions.

For a snapshot that passed review, run a read-only plan first:

```powershell
.\.venv\Scripts\python.exe -m mcc_bot.partner_batch `
  --database var/stores.sqlite3 `
  --snapshot temp/paritet-snapshot.json
```

Preview uses SQLite query-only mode and prints a deterministic `plan_sha256`;
it does not write the database. Review its counters and conflicts, then invoke
apply only as explicit approval with the exact unchanged plan SHA, a positive
actor ID (`BOT_OWNER_TELEGRAM_ID` may supply the owner ID), and a new backup
path:

```powershell
$planSha = '<copy plan_sha256 from the preview JSON>'
.\.venv\Scripts\python.exe -m mcc_bot.partner_batch `
  --database var/stores.sqlite3 `
  --snapshot temp/paritet-snapshot.json `
  --apply `
  --expected-plan-sha256 $planSha `
  --backup temp/stores-before-paritet.sqlite3 `
  --actor-id $env:BOT_OWNER_TELEGRAM_ID
```

Apply creates the backup before changing SQLite, rechecks the database and
plan fingerprints, inserts only safe new rows, and archives only explicitly
approved exact retirement rows. Inserts and archives share one transaction and
roll back together on failure; existing partner rows are not silently updated.
The backup path must be new and separate from the database. A stale plan aborts
before mutation. Apply one reviewed snapshot at a time; that snapshot may be
one source or an explicitly combined atomic batch. If several snapshots are
applied sequentially, make a fresh preview before each one because the database
fingerprint changes. This is a local snapshot/review/database workflow only.

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

The 2026-08-31 correction uses the same guarantees through
`mcc-reconcile-partners-20260831` followed by
`mcc-apply-partner-seed-20260831`.
