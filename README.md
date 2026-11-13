# Email Digest Agent

Self-hosted Telegram bot that turns your mailboxes into a daily AI digest.
It reads IMAP (Gmail, mail.ru, Yandex), summarises only what is new with an
OpenRouter model, and delivers a categorised digest to your chat on a schedule —
or on demand with `/digest`.

Single-user by design. Your mail never leaves your server except as a prompt to
the model provider you configure.

## How it works

```
              ┌──────────── per mailbox ────────────┐
IMAP  ───────►│ watermark → fetch → parse → thread   │
(Gmail,       │ → bulk detect → dedup (SQLite)       │
 mail.ru,     └───────────────┬─────────────────────┘
 Yandex)                      ▼
                    LLM (chunked, model chain)
                              ▼
                 render → outbox → Telegram
```

Three properties matter more than features:

1. **Nothing is lost.** Each mailbox has its own watermark, so a broken mailbox
   does not shift the window of a healthy one. Failed fetches never advance the
   watermark. Message IDs are written *before* the model is called.
2. **Nothing is silently dropped.** Rendering goes through an outbox; a digest
   that Telegram refused (rate limit, outage, >4096 characters) is retried by a
   background job, and `/last` can replay it.
3. **The model is not trusted.** Its JSON is validated by a pydantic schema,
   malformed replies are repaired and then retried on the next model in the
   chain.

## Highlights

| Area | What you get |
|---|---|
| Mailboxes | Gmail, mail.ru, Yandex; IMAP over TLS with app passwords; folder is configurable and validated |
| LLM | OpenRouter with a fallback **chain** of free models; OpenAI, Gemini, Ollama and GigaChat still supported |
| Reliability | per-mailbox watermark, two-phase dedup, per-chunk degradation, JSON repair, retries with backoff |
| Delivery | outbox with retry, 4096-safe splitting, `/last`, `/important` |
| Email parsing | thread collapsing, `.ics` invites, attachment names, `\Seen`/`\Flagged`, `List-Unsubscribe` |
| Digest | categories (важное / рабочее / прочее), action items, calendar section, newsletter & spam counters |
| Cost | per-model token accounting via the OpenRouter catalogue; `:free` models report as free |
| Ops | rotating file logs, heartbeat file, single-instance lock, `--check`, `--once`, `--dry-run`, Docker |

## Quick start

```bash
git clone https://github.com/leshicorn/email-bot.git
cd email-bot
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp .env.example .env
chmod 600 .env          # it holds app passwords
$EDITOR .env

.venv/bin/python main.py --check    # verifies IMAP logins and the LLM key
.venv/bin/python main.py --dry-run  # one digest, printed, nothing sent
.venv/bin/python main.py            # bot + scheduler
```

### Credentials

| Credential | Where to get it |
|---|---|
| `TELEGRAM_BOT_TOKEN` | [@BotFather](https://t.me/BotFather) → `/newbot` |
| `TELEGRAM_CHAT_ID` | [@userinfobot](https://t.me/userinfobot) |
| Gmail app password | Google Account → Security → 2-Step Verification → App passwords |
| mail.ru app password | Настройки → Безопасность → Пароли для внешних приложений |
| Yandex app password | passport.yandex.ru → Безопасность → Пароли приложений |
| `OPENROUTER_API_KEY` | [openrouter.ai/keys](https://openrouter.ai/keys) |

## OpenRouter and free models

Set the provider and the key:

```env
LLM_PROVIDER=openrouter
OPENROUTER_API_KEY=sk-or-v1-…
OPENROUTER_MODEL=particle-ai/particle-1:free
OPENROUTER_FALLBACK_MODELS=dots-studio/dots-3-note-preview:free,google/gemma-4-31b-it:free,openrouter/free
```

The bot tries `OPENROUTER_MODEL` first and walks down `OPENROUTER_FALLBACK_MODELS`
when a model is rate-limited, unavailable, returns broken JSON, or answers with
an empty message.

> **Free models are not interchangeable.** Many of the `:free` reasoning models
> spend the whole `max_tokens` budget on hidden reasoning and return an empty
> `content` with `finish_reason=length`. That is what the chain is for.

Measured on a realistic 15-email payload (one digest chunk), November 2026:

| Model | Time | Tokens | Result |
|---|---|---|---|
| `particle-ai/particle-1:free` | 16.7 s | 3 229 | ✅ valid JSON, sensible categories |
| `dots-studio/dots-3-note-preview:free` | 18.8 s | 2 840 | ✅ valid JSON |
| `google/gemma-4-31b-it:free` | 50.9 s | 2 286 | ✅ slow, needs `response_format` |
| `baidu/ernie-5.5-flash-preview:free` | 16.1 s | 4 116 | ⚠️ fine on tiny prompts, truncates on 15 emails |
| `nvidia/nemotron-3.5-lightning:free` | 22.1 s | 2 711 | ❌ empty content (reasoning ate the budget) |
| `liquid/lfm-3-1.7b:free`, `nvidia/nemotron-3-ultra-550b-a55b:free` | — | — | ❌ truncated or broken JSON |
| `thinkingmachines/inkling-2-small:free` | — | — | ❌ 403 outside its region |

Re-check the catalogue before picking a model:

```bash
.venv/bin/python main.py --check          # model chain + IMAP + key, offline-safe
curl -s https://openrouter.ai/api/v1/models \
  | python3 -c 'import json,sys; [print(m["id"]) for m in json.load(sys.stdin)["data"] if m["pricing"]["prompt"]=="0"]'
```

If a whole chunk of emails fails on every model, the digest still arrives: that
chunk degrades to a plain listing marked “AI недоступен”, and those messages are
retried on the next run (up to three attempts) instead of being lost.

### The free tier has a hard daily cap

OpenRouter gives an account **50 free-model requests per day** until you have
bought 10 credits (then it becomes 1000). The cap is per *account*, not per
model — so the fallback chain cannot rescue it, and the bot does not pretend
otherwise:

- a `429` carrying `limit_source=openrouter_free_tier_daily` stops the chain
  immediately instead of burning the remaining models (one request, not twelve);
- a **scheduled** run does not send a subject-only stub — it postpones itself to
  just after the reset (03:02 MSK for a 00:00 UTC reset), sends one *silent*
  note explaining the delay, and keeps the mail queued;
- a **manual** `/digest` answers immediately with a plain listing and a
  `⚠️ лимит бесплатных моделей` note, since you asked for it *now*;
- either way the barely-summarised messages stay **`pending`** and their mailbox
  watermarks are rewound to the window that was actually read, so the retry
  summarises them instead of losing them.

A daily digest needs 1–3 requests, so 50/day is comfortable. It becomes tight if
you mash `/digest`, and my own model benchmarking is what exhausted it on the day
this was written. `.venv/bin/python main.py --check` prints
`Квота бесплатных моделей: used/limit, remaining`.

If you need headroom, either buy 10 credits once (1000 free requests/day), or set
`OPENROUTER_MODEL` to a cheap paid model — the bot prices it from the catalogue.

## Commands

| Command | Action |
|---|---|
| `/digest` | build and send a digest right now |
| `/last` | replay the last digest |
| `/important` | only the important part of the last digest |
| `/status` | schedule, next run, mailbox health, queue size |
| `/stats` | tokens and spending for the last 7 days |
| `/search СЛОВО` | search the digest history |
| `/pause 3d` | pause digests (`3d`, `12h`, `30m`, `1w`) |
| `/resume` | clear the pause |
| `/time 08:30` | change the daily time without editing `.env` |
| `/retry` | flush the delivery queue |
| `/help` | command list |

Digest messages also carry inline buttons: **Только важное**, **Ещё раз**, **Статус**.

## Configuration

All settings come from `.env` via pydantic-settings; `.env.example` documents
every key. The ones worth knowing:

| Variable | Default | Purpose |
|---|---|---|
| `LLM_PROVIDER` | `openrouter` | `openrouter`, `openai`, `gemini`, `ollama`, `gigachat` |
| `OPENROUTER_MODEL` | `particle-ai/particle-1:free` | primary model |
| `OPENROUTER_FALLBACK_MODELS` | `dots-studio/…,google/gemma-4-31b-it:free,…` | comma-separated chain |
| `DIGEST_HOUR` / `DIGEST_MINUTE` | `21:00` | daily time in `TIMEZONE` |
| `TIMEZONE` | `Europe/Moscow` | IANA name |
| `WEEKLY_DIGEST_ENABLED` | `true` | Sunday summary of the week's stats |
| `QUIET_WHEN_EMPTY` | `true` | skip the message when there is nothing to report |
| `MAX_BODY_CHARS` | `3000` | per-email body truncation before the prompt |
| `LLM_CHUNK_SIZE` | `12` | emails per model call |
| `LLM_MAX_TOKENS` | `4000` | output budget (raise it for reasoning models) |
| `SKIP_SENDERS_CONTAINING` | `noreply, newsletter, …` | sender substrings treated as bulk |
| `NEWSLETTER_COUNT_ONLY` | `true` | count bulk mail instead of spending tokens on it |
| `YANDEX_DISPLAY_NAME` / `YANDEX_IMAP_FOLDER` | `Яндекс` / `INBOX` | display name and real folder are separate |
| `DB_PATH` | `data/digest.db` | SQLite location |
| `LOG_FILE` | `bot.log` | rotating log (5 MB × 5) |

## Example digest

```
📧 Email-дайджест · 13 ноября 2026

📬 Gmail
📌 Важное (2)
  • Списание 5 000 ₽ с карты *4321
    от noreply@sberbank.ru
    Операция прошла успешно. Если вы её не совершали — звоните 900.

  • Вход в приложение с нового устройства
    от noreply@tinkoff.ru
    Требуется подтверждение или блокировка карты.

💼 Рабочее (1)
  • Отчёт за квартал: правки в разделе 3
    от ivan@company.ru
    Нужна новая версия до пятницы.

🗑 Пропущено — рассылок: 12, спама: 1

📅 События:
  • 18 ноя 10:30–11:30 · каб. 204 · Защита диплома

⚡ Требуют внимания:
  • Поправить раздел 3 и прислать новую версию до пятницы

📊 Обработано 15 писем · particle-ai/particle-1:free · 🪙 4812 токенов (вход 3900 / выход 912) · бесплатно 🎉
```

## Running as a service

`/etc/systemd/system/email-bot.service`:

```ini
[Unit]
Description=Email Digest Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=YOURUSER
WorkingDirectory=/home/YOURUSER/email-bot
ExecStart=/home/YOURUSER/email-bot/.venv/bin/python main.py
Restart=always
RestartSec=5
# Secrets live in .env next to the code, so no EnvironmentFile is needed.
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=/home/YOURUSER/email-bot/data /home/YOURUSER/email-bot/bot.log

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now email-bot
journalctl -u email-bot -f
```

The heartbeat file (`data/heartbeat`) is rewritten after every run — point your
monitoring at its mtime to notice a silently dead bot.

## Docker

```bash
docker compose up -d --build
docker compose logs -f
```

`docker-compose.yml` mounts `./data` for SQLite, the heartbeat and the lock file,
and reads `.env` from the project root (never baked into the image).

## Development

```bash
.venv/bin/ruff check .
.venv/bin/python -m pytest -q
.venv/bin/python main.py --dry-run   # in-memory DB, prints the digest
```

The pipeline takes its fetchers and summariser as arguments, so tests run without
network access or real credentials.

## Limitations

- IMAP `SINCE` is date-granular; exact-time filtering happens client-side, so a
  bit of extra header data is fetched.
- One folder per mailbox.
- Bodies larger than 5 MB are summarised from headers only (attachments are not
  downloaded).
- Free OpenRouter models have daily request caps; a very busy mailbox may need a
  second model in the chain or a paid one.
- Cost figures come from the OpenRouter catalogue and change with it.
- Single-user by design — this is a personal assistant, not a multi-tenant service.
