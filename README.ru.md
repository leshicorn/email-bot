# Email Digest Agent

[English](README.md) · **Русский**

Self-hosted Telegram-бот, который превращает ваши ящики в ежедневный AI-дайджест.
Читает IMAP (Gmail, mail.ru, Яндекс), суммаризирует только новое моделью через
OpenRouter и присылает категоризированную сводку в чат по расписанию — или по
команде `/digest`.

Рассчитан на одного пользователя. Почта не покидает ваш сервер никуда, кроме
промпта тому провайдеру модели, которого вы сами указали.

## Как это работает

```
              ┌──────────── на каждый ящик ─────────┐
IMAP  ───────►│ watermark → fetch → parse → thread  │
(Gmail,       │ → поиск рассылок → дедуп (SQLite)   │
 mail.ru,     └───────────────┬────────────────────┘
 Яндекс)                      ▼
                    LLM (чанками, цепочка моделей)
                              ▼
                 рендер → outbox → Telegram
```

Три свойства важнее списка функций:

1. **Ничего не теряется.** У каждого ящика свой watermark, поэтому сломанный
   ящик не сдвигает окно выборки здорового. Неудачный фетч никогда не двигает
   watermark. `Message-ID` записывается *до* вызова модели.
2. **Ничего не теряется при доставке.** Рендер идёт через outbox: дайджест,
   который Telegram не принял (лимит, сбой, >4096 символов), доотправляется
   фоновой задачей, а `/last` может его повторить.
3. **Модели не доверяем.** Её JSON проверяется pydantic-схемой, битые ответы
   чинятся, а затем уходят на следующую модель цепочки.

## Что внутри

| Область | Что получаете |
|---|---|
| Ящики | Gmail, mail.ru, Яндекс; IMAP поверх TLS с app-паролями; папка настраивается и проверяется |
| LLM | OpenRouter (цепочка бесплатных моделей) и **GonkaRouter как запасной провайдер**; OpenAI, Gemini, Ollama и GigaChat тоже поддерживаются |
| Надёжность | watermark на ящик, двухфазный дедуп, деградация по чанкам, починка JSON, ретраи с backoff |
| Доставка | outbox с ретраями, безопасное разбиение под 4096, `/last`, `/important` |
| Разбор почты | склейка тредов, `.ics`-приглашения, имена вложений, `\Seen`/`\Flagged`, `List-Unsubscribe` |
| Дайджест | категории (важное / рабочее / прочее), задачи, секция календаря, счётчики рассылок и спама |
| Стоимость | учёт токенов и цен по каталогу OpenRouter; модели `:free` отмечаются как бесплатные |
| Эксплуатация | ротация логов, heartbeat-файл, single-instance lock, `--check`, `--once`, `--dry-run`, Docker |

## Быстрый старт

```bash
git clone https://github.com/leshicorn/email-bot.git
cd email-bot
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp .env.example .env
chmod 600 .env          # внутри app-пароли
$EDITOR .env

.venv/bin/python main.py --check    # проверяет IMAP-логины и ключ LLM
.venv/bin/python main.py --dry-run  # один дайджест в консоль, ничего не отправляется
.venv/bin/python main.py            # бот + планировщик
```

### Где взять доступы

| Доступ | Где взять |
|---|---|
| `TELEGRAM_BOT_TOKEN` | [@BotFather](https://t.me/BotFather) → `/newbot` |
| `TELEGRAM_CHAT_ID` | [@userinfobot](https://t.me/userinfobot) |
| Gmail app password | Google Account → Security → 2-Step Verification → App passwords |
| mail.ru app password | Настройки → Безопасность → Пароли для внешних приложений |
| Яндекс app password | passport.yandex.ru → Безопасность → Пароли приложений |
| `OPENROUTER_API_KEY` | [openrouter.ai/keys](https://openrouter.ai/keys) |

## OpenRouter и бесплатные модели

Задайте провайдера и ключ:

```env
LLM_PROVIDER=openrouter
OPENROUTER_API_KEY=sk-or-v1-…
OPENROUTER_MODEL=particle-ai/particle-1:free
OPENROUTER_FALLBACK_MODELS=dots-studio/dots-3-note-preview:free,google/gemma-4-31b-it:free,openrouter/free
```

Бот пробует `OPENROUTER_MODEL`, а затем идёт по `OPENROUTER_FALLBACK_MODELS`,
если модель упёрлась в rate limit, недоступна, вернула битый JSON или пустой ответ.

> **Бесплатные модели не взаимозаменяемы.** Многие `:free` reasoning-модели
> тратят весь `max_tokens` на скрытые рассуждения и отдают пустой `content` с
> `finish_reason=length`. Ровно для этого и нужна цепочка.

Замеры на реальном payload из 15 писем (один чанк дайджеста), ноябрь 2026:

| Модель | Время | Токены | Результат |
|---|---|---|---|
| `particle-ai/particle-1:free` | 16,7 с | 3 229 | ✅ валидный JSON, адекватные категории |
| `dots-studio/dots-3-note-preview:free` | 18,8 с | 2 840 | ✅ валидный JSON |
| `google/gemma-4-31b-it:free` | 50,9 с | 2 286 | ✅ медленно, нужен `response_format` |
| `baidu/ernie-5.5-flash-preview:free` | 16,1 с | 4 116 | ⚠️ норм на коротком промпте, обрезается на 15 письмах |
| `nvidia/nemotron-3.5-lightning:free` | 22,1 с | 2 711 | ❌ пустой content (reasoning съел бюджет) |
| `liquid/lfm-3-1.7b:free`, `nvidia/nemotron-3-ultra-550b-a55b:free` | — | — | ❌ обрезанный или битый JSON |
| `thinkingmachines/inkling-2-small:free` | — | — | ❌ 403 вне своего региона |

Перед выбором модели стоит перепроверить каталог:

```bash
.venv/bin/python main.py --check          # цепочка моделей + IMAP + ключ
curl -s https://openrouter.ai/api/v1/models \
  | python3 -c 'import json,sys; [print(m["id"]) for m in json.load(sys.stdin)["data"] if m["pricing"]["prompt"]=="0"]'
```

Если целый чанк не осилила ни одна модель, дайджест всё равно придёт: этот чанк
деградирует в простой список с пометкой «AI недоступен», а сами письма уйдут на
повтор в следующем запуске (до трёх попыток), а не потеряются.

### У бесплатного тарифа жёсткий дневной лимит

OpenRouter даёт аккаунту **50 запросов к бесплатным моделям в сутки**, пока вы не
купили 10 кредитов (тогда — 1000). Лимит считается на *аккаунт*, а не на модель,
поэтому цепочка моделей его не спасает — и бот не делает вид, что спасает:

- `429` с `limit_source=openrouter_free_tier_daily` сразу останавливает цепочку,
  а не сжигает оставшиеся модели (один запрос вместо двенадцати);
- **запуск по расписанию** не присылает заглушку из одних тем — он откладывается
  на время чуть позже сброса (03:02 МСК при сбросе в 00:00 UTC), отправляет одну
  *беззвучную* заметку с объяснением и оставляет письма в очереди;
- **ручной** `/digest` отвечает сразу простым списком с пометкой
  `⚠️ лимит бесплатных моделей`, потому что вы попросили *сейчас*;
- в обоих случаях недосуммаризированные письма остаются в статусе **`pending`**,
  а watermark ящиков откатывается к реально прочитанному окну, чтобы повтор их
  подхватил, а не потерял.

Дневному дайджесту хватает 1–3 запросов, так что 50 в сутки — с запасом. Тесно
становится, если дёргать `/digest` подряд; в день написания этого файла лимит
выжгли мои же замеры моделей. `.venv/bin/python main.py --check` печатает
`Квота бесплатных моделей: used/limit, remaining`.

Если нужен запас — купите 10 кредитов один раз (1000 бесплатных запросов в сутки)
или поставьте в `OPENROUTER_MODEL` дешёвую платную модель: бот посчитает её
стоимость по каталогу.

### Запасной провайдер: GonkaRouter

Когда OpenRouter в rate limit, без квоты или просто молчит, бот уходит на
[GonkaRouter](https://gonkarouter.io) — он тоже OpenAI-совместимый. Квота
считается **по провайдеру**, поэтому исчерпанный аккаунт OpenRouter больше не
стоит вам всего дайджеста.

```env
GONKAROUTER_ENABLED=true
GONKAROUTER_API_KEY=sk-…
GONKAROUTER_MODEL=MiniMaxAI/MiniMax-M2.7
GONKAROUTER_FALLBACK_MODELS=zai-org/GLM-5.3-Flash,deepseek-ai/DeepSeek-V4-Flash-0731
```

Замеры на том же payload из 15 писем:

| Модель | JSON mode | Время | Токены | Результат |
|---|---|---|---|---|
| `MiniMaxAI/MiniMax-M2.7` | **выкл** | 15,7 с | 3 004 | ✅ лучшая: ровно 3 верных «важных» письма |
| `MiniMaxAI/MiniMax-M2.7` | вкл | 32,6 с | 5 156 | ❌ 19 601 символ прозы, без JSON |
| `zai-org/GLM-5.3-Flash` | вкл | 83 с | 3 351 | ✅ но пере-классифицирует (6 «важных») |
| `zai-org/GLM-5.3-Flash` | выкл | — | — | ❌ HTTP 502 |
| `deepseek-ai/DeepSeek-V4-Flash-0731` | вкл | 35,4 с | 4 682 | ✅ работает, самый медленный |
| `deepseek-ai/DeepSeek-V4-Flash-0731` | выкл | — | — | ❌ reasoning обрывается, content пустой |

Две особенности, которые бот уже обходит:

- **`response_format` зависит от модели.** MiniMax отвечает прозой, если попросить
  JSON-объект, а GLM отказывается отвечать без него. Поэтому у провайдера есть
  «лестница» режимов: OpenRouter сначала пробует строгий JSON, GonkaRouter —
  свободный формат, и не-JSON ответ переводит на следующую ступень, а не роняет
  весь чанк.
- **Cloudflare блокирует дефолтный Python user-agent.** `python-urllib` и
  дефолтный UA `requests` получают `403 error code: 1010`, поэтому бот отправляет
  `LLM_USER_AGENT` (по умолчанию `EmailDigestAgent/2.0`).

GonkaRouter не публикует цены в `/models`, поэтому бот их не выдумывает: в футере
видны токены и имя модели, а значок «бесплатно 🎉» достаётся только модели с
`:free` в имени.

Сервис известен тем, что недоступен из части сетей (нужен маршрут, которого нет у
российских провайдеров). `main.py --check` покажет, отвечает ли он оттуда, откуда
вы запускаете бота.

## Команды

| Команда | Действие |
|---|---|
| `/digest` | собрать и отправить дайджест прямо сейчас |
| `/last` | повторить последний дайджест |
| `/important` | только важное из последнего дайджеста |
| `/status` | расписание, следующий запуск, состояние ящиков, очередь |
| `/stats` | токены и расход за последние 7 дней |
| `/search СЛОВО` | поиск по истории дайджестов |
| `/pause 3d` | пауза (`3d`, `12h`, `30m`, `1w`) |
| `/resume` | снять паузу |
| `/time 08:30` | изменить время без правки `.env` |
| `/retry` | доотправить очередь |
| `/help` | список команд |

В сообщениях с дайджестом есть инлайн-кнопки: **Только важное**, **Ещё раз**, **Статус**.

## Конфигурация

Все настройки берутся из `.env` через pydantic-settings; полный список с
комментариями — в `.env.example`. Самое полезное:

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `LLM_PROVIDER` | `openrouter` | `openrouter`, `gonkarouter`, `openai`, `gemini`, `ollama`, `gigachat` |
| `OPENROUTER_MODEL` | `particle-ai/particle-1:free` | основная модель |
| `OPENROUTER_FALLBACK_MODELS` | `dots-studio/…,google/gemma-4-31b-it:free,…` | цепочка через запятую |
| `GONKAROUTER_ENABLED` | `true` | использовать запасной провайдер |
| `GONKAROUTER_MODEL` | `MiniMaxAI/MiniMax-M2.7` | основная модель отката |
| `GONKAROUTER_FALLBACK_MODELS` | `zai-org/GLM-5.3-Flash,…` | цепочка внутри GonkaRouter |
| `LLM_USER_AGENT` | `EmailDigestAgent/2.0` | заголовок UA (Cloudflare режет дефолтные) |
| `DIGEST_HOUR` / `DIGEST_MINUTE` | `21:00` | время в `TIMEZONE` |
| `TIMEZONE` | `Europe/Moscow` | имя IANA |
| `WEEKLY_DIGEST_ENABLED` | `true` | сводка за неделю по воскресеньям |
| `QUIET_WHEN_EMPTY` | `true` | не присылать, когда нечего сообщать |
| `MAX_BODY_CHARS` | `3000` | обрезка тела письма до промпта |
| `LLM_CHUNK_SIZE` | `12` | писем на один вызов модели |
| `LLM_MAX_TOKENS` | `4000` | бюджет ответа (reasoning-моделям нужно больше) |
| `SKIP_SENDERS_CONTAINING` | `noreply, newsletter, …` | подстроки отправителей, считающихся рассылкой |
| `NEWSLETTER_COUNT_ONLY` | `true` | считать рассылки, не тратя на них токены |
| `YANDEX_DISPLAY_NAME` / `YANDEX_IMAP_FOLDER` | `Яндекс` / `INBOX` | имя в дайджесте и реальная папка — разные поля |
| `DB_PATH` | `data/digest.db` | путь к SQLite |
| `LOG_FILE` | `bot.log` | лог с ротацией (5 МБ × 5) |

## Пример дайджеста

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

## Запуск как сервис

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
# Секреты лежат в .env рядом с кодом, поэтому EnvironmentFile не нужен.
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

Файл `data/heartbeat` перезаписывается после каждого запуска — наведите мониторинг
на его mtime, чтобы замечать молча умершего бота.

## Docker

```bash
docker compose up -d --build
docker compose logs -f
```

`docker-compose.yml` монтирует `./data` под SQLite, heartbeat и lock-файл, а `.env`
читает из корня проекта (в образ он не попадает).

## Разработка

```bash
.venv/bin/ruff check .
.venv/bin/python -m pytest -q
.venv/bin/python main.py --dry-run   # БД в памяти, дайджест в консоль
```

Пайплайн получает фетчеры и суммаризатор аргументами, поэтому тесты идут без сети
и реальных доступов.

## Ограничения

- IMAP `SINCE` оперирует датой; точная фильтрация по времени идёт на клиенте,
  поэтому загружается немного лишних заголовков.
- Одна папка на ящик.
- Письма больше 5 МБ суммаризируются только по заголовкам (вложения не качаются).
- У бесплатных моделей OpenRouter дневной лимит; GonkaRouter закрывает разрыв, но
  доступен не из всех сетей.
- GonkaRouter не публикует цены за токен, поэтому в его дайджестах нет суммы.
- Оценка стоимости берётся из каталога OpenRouter и меняется вместе с ним.
- Рассчитан на одного пользователя — это личный ассистент, а не многопользовательский сервис.
