# MCC Moneyback Telegram Bot

This bot accepts a four-digit merchant category code (MCC) and replies with
the configured cards for that category, ordered from the largest moneyback
value to the smallest. It uses Telegram long polling, so no public HTTP
endpoint is required.

The structure follows the small, testable layout used by the public
[gippo-bot](https://github.com/Kabagun/gippo-bot): environment-based settings,
`python-telegram-bot`, a catalog/domain module kept separate from Telegram
handlers, and a local validation command. The catalog is kept as normalized JSON
so card rates can be reviewed and updated without code
changes. The current catalog contains eight cards and 2,507 explicit offers.

## Local setup

Python 3.11 or newer is required. PowerShell 7 on Windows:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

Edit `.env` and set `TELEGRAM_BOT_TOKEN`. Keep access restricted by putting the
allowed Telegram numeric IDs in `TELEGRAM_ALLOWED_USER_IDS`; set
`TELEGRAM_OPEN_ACCESS=true` only when the bot is intentionally public. The
process refuses to start if both open access and the allow-list are disabled.
The bot loads `.env` from its current working directory before reading settings;
variables already provided by the process take precedence.

Validate the catalog without starting Telegram:

```powershell
.\.venv\Scripts\python.exe -m mcc_bot.cli
.\.venv\Scripts\python.exe -m mcc_bot.cli --mcc 5411
```

Start the bot:

```powershell
.\.venv\Scripts\python.exe -m mcc_bot
```

The same commands work on Linux with `.venv/bin/python`.

## Telegram usage

- `/start` or `/help` shows instructions.
- `/mcc 5411` performs a lookup.
- Sending `5411` as a normal message performs the same lookup.

Only the four-digit code is accepted. Friendly forms such as `MCC 5411` and
`mcc:5411` are also accepted. An invalid code receives a short validation
message rather than a catalog query.

## Catalog contract

`MCC_CATALOG_PATH` points to a UTF-8 JSON file and defaults to
`data/cards.json`. The root object must contain the explicit `version: 1` field
so a future schema cannot be mistaken for the current one:

```json
{
  "version": 1,
  "cards": [
    {
      "id": "stable-card-id",
      "name": "Card shown to users",
      "issuer": "Bank name",
      "notes": "Optional card-level condition",
      "default_offer": {"moneyback": 2, "unit": "percent"},
      "excluded_mccs": ["7999"],
      "offers": [
        {"mcc": "5411", "moneyback": 1, "unit": "percent", "notes": "Selected category"},
        {"mcc": "5812", "moneyback": 0, "unit": "percent"}
      ]
    }
  ]
}
```

Each card ID and MCC offer must be unique. `offers` may be omitted when a card
only uses `default_offer`; otherwise it is an array. `moneyback` is a non-negative JSON
number (not a quoted string). MCC values should be four-digit strings so leading
zeros such as `"0742"` are preserved. `unit` defaults to `percent`; use `currency` for
an absolute amount and provide a three-letter `currency` code, for example:

```json
{"mcc": "5411", "moneyback": 2.00, "unit": "currency", "currency": "BYN"}
```

`default_offer` is optional and applies to every MCC not covered by an
explicit `offers` entry or `excluded_mccs`. An explicit offer wins over both a
default and an exclusion (so an explicit zero is a visible zero-moneyback
result); an excluded MCC with no explicit offer returns no card. `notes` is an
optional string on either a card or an offer and is shown beside the result for
conditional products such as `MTКАРТА — only 3 connected groups` or
`Кактус — one selected group`.

The lookup sorts the numeric moneyback value descending and uses card name and
ID as deterministic tie-breakers. Every offer for one MCC must use the same
`(unit, currency)` pair across cards; the bot rejects mixed rates or currencies
at startup instead of producing a misleading order. It does not convert
currencies or rates. Cards without an offer for the requested MCC are omitted.
After editing the catalog, restart the bot so it loads the validated file at
startup.

Percentage moneyback is shown as gross and net when it exceeds 2%. The first
2 percentage points are tax-free; only the remainder is reduced by 13%, so
`3%` is displayed as `3% (2.87% after tax)`. Sorting always uses the gross
percentage, which preserves the same descending order as the net value. Net
percentages are rounded to two decimal places for display.

## Configuration

| Variable | Required | Description |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | bot only | Token issued by BotFather |
| `TELEGRAM_OPEN_ACCESS` | no | `true` to allow every Telegram user; defaults to `false` |
| `TELEGRAM_ALLOWED_USER_IDS` | restricted mode | Comma- or semicolon-separated numeric IDs |
| `MCC_CATALOG_PATH` | no | Catalog JSON path; defaults to `data/cards.json` |
| `LOG_LEVEL` | no | Python log level; defaults to `INFO` |

Do not commit `.env`; it contains the Telegram token. The bot does not log
tokens, message URLs, or catalog contents beyond the configured path and card
count.

## Linux deployment

The generic user-level unit in `deploy/mcc-bot.service` follows the same layout
as the reference bot: checkout at `/srv/bots/mcc-bot/app`, virtual environment
at `/srv/bots/mcc-bot/venv`, and owner-readable secrets at
`/srv/bots/mcc-bot/.env`. From the checkout host:

```bash
python3 -m venv /srv/bots/mcc-bot/venv
/srv/bots/mcc-bot/venv/bin/python -m pip install /srv/bots/mcc-bot/app
chmod 600 /srv/bots/mcc-bot/.env
mkdir -p ~/.config/systemd/user
cp /srv/bots/mcc-bot/app/deploy/mcc-bot.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now mcc-bot.service
```

The service expects `EnvironmentFile=/srv/bots/mcc-bot/.env`; create that file
out of band and never put its token in Git. Check it with
`systemctl --user status mcc-bot.service` and
`journalctl --user -u mcc-bot.service`.

## Tests and lint

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
```
