# 🚀 Деплой ALBION MVP на VPS (полный гайд)

> Актуально для v2.5 (Round 9). Проверено на коде: webhook-режим бота чинит R9-3,
> WAL-SQLite позволяет двум процессам работать с одной БД, рестарты безопасны
> (scheduler/wizard_state в БД).

---

## 0. Карта того, что будет на сервере

```
                    ┌──────────────────────────────────────────────┐
                    │                  VPS                          │
                    │                                              │
  Telegram ──HTTPS──▶ Caddy/Nginx (443, TLS) ──▶ 127.0.0.1:8443 ──▶ Бот          │
                    │        │                     (src.main       │
  MeritHub ──HTTPS──▶        └──────────▶ 127.0.0.1:8000 ──▶ uvicorn │
                    │                              (src.api.webhook)│
                    │                                              │
                    │   /opt/albion/albion.db  (WAL, общая для обоих)│
                    └──────────────────────────────────────────────┘
```

**Два процесса, одна БД:**
- **Бот** — `python -m src.main --webhook`. Слушает локальный порт 8443 (приёмник апдейтов Telegram), плюс сам ходит в Telegram API исходящими (напоминания, эскалации).
- **Вебхук-ресивер MeritHub** — `uvicorn src.api.webhook:app --port 8000`. Принимает push-события MeritHub (attendance, classStatus) и пишет их в ту же `albion.db`.

Оба процесса ходят в одну SQLite (WAL + busy_timeout=5000) — это штатно.

---

## 1. Подготовка

**Что нужно:**
- VPS: Ubuntu 22.04/24.04 (Debian тоже ок), минимум 1 CPU / 1 GB RAM, белый IP.
- Домен (или поддомен), например `albion.example.com`, с A-записью на IP сервера.
- Порты наружу: **22** (SSH), **80** (для Let's Encrypt), **443** (HTTPS). Порты 8000/8443 наружу открывать НЕ нужно — их закрывает реверс-прокси.

```bash
# Базовая настройка сервера
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3 python3-venv git ufw
sudo ufw allow 22/tcp && sudo ufw allow 80/tcp && sudo ufw allow 443/tcp
sudo ufw enable
```

## 2. Код и окружение

```bash
sudo mkdir -p /opt/albion
sudo chown $USER:$USER /opt/albion
cd /opt/albion
git clone <ваш-репозиторий> .        # или rsync-перенос с машины
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## 3. Файл `.env` (прод)

```bash
cd /opt/albion
cp .env.example .env
nano .env
```

Минимум для прода:

```ini
TELEGRAM_BOT_TOKEN=ваш_токен
# ⚠️ ОБЯЗАТЕЛЬНО сгенерируйте свой секрет (не test_secret!):
#   python3 -c "import secrets; print(secrets.token_hex(32))"
TELEGRAM_WEBHOOK_SECRET=<случайная строка>
TELEGRAM_WEBHOOK_URL=https://albion.example.com/tg
TELEGRAM_WEBHOOK_HOST=127.0.0.1
TELEGRAM_WEBHOOK_PORT=8443

DATABASE_URL=sqlite+aiosqlite:////opt/albion/albion.db
LOG_LEVEL=INFO

ALBION_ADMIN_TELEGRAM_IDS=<ваши TG ID через запятую>
ALBION_ORG_TIMEZONE=Europe/London
ALBION_DEMO_MODE=false

# Тайминги (по вкусу)
ALBION_NOTIFY_PARENT_DELAY_MIN=5
ALBION_ESCALATE_DELAY_MIN=15
ALBION_SCHEDULER_INTERVAL_SEC=5

# MeritHub API (если подключаем реальный):
MERITHUB_CLIENT_ID=
MERITHUB_CLIENT_SECRET=
# MeritHub webhook (путь совпадает с Caddy-правилом ниже):
MERITHUB_WEBHOOK_SECRET=
MERITHUB_WEBHOOK_PORT=8000
MERITHUB_WEBHOOK_PATH=/merithub/webhook
```

> `TELEGRAM_WEBHOOK_HOST=127.0.0.1` — безопаснее, чем 0.0.0.0: прокси на том же
> хосте, наружу порт не торчит. `0.0.0.0` нужен только если прокси в Docker/на другом хосте.

## 4. База данных

```bash
# Вариант A: перенос с текущей машины (сохраняет пользователей, учеников, классы)
scp albion.db user@vps:/opt/albion/albion.db

# Вариант B: старт с нуля — БД создастся сама при первом запуске (init_db).

# Бэкап (WAL-safe, можно на живой БД):
sqlite3 /opt/albion/albion.db ".backup /backup/albion-$(date +%F).db"
```

## 5. systemd-сервисы

**`/etc/systemd/system/albion-bot.service`:**
```ini
[Unit]
Description=ALBION Telegram bot (webhook mode)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/albion
EnvironmentFile=/opt/albion/.env
ExecStart=/opt/albion/.venv/bin/python -m src.main --webhook
Restart=always
RestartSec=5
# Логи в journald (+ дублируются в /opt/albion/albion.log)

[Install]
WantedBy=multi-user.target
```

**`/etc/systemd/system/albion-webhook.service`:**
```ini
[Unit]
Description=ALBION MeritHub webhook receiver
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/albion
EnvironmentFile=/opt/albion/.env
ExecStart=/opt/albion/.venv/bin/uvicorn src.api.webhook:app --host 127.0.0.1 --port 8000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now albion-bot albion-webhook
```

> ⚠️ Запустите оба сервиса **до** настройки прокси — так вы поймёте, что процессы
> поднялись (`systemctl status`), прежде чем впускать внешний трафик.

## 6. Реверс-прокси + HTTPS

### Вариант 1 (рекомендую): Caddy — авто-HTTPS, 3 строки

**`/etc/caddy/Caddyfile`:**
```
albion.example.com {
    handle /merithub/* {
        reverse_proxy 127.0.0.1:8000
    }
    handle /tg* {
        reverse_proxy 127.0.0.1:8443
    }
    handle {
        reverse_proxy 127.0.0.1:8443
    }
}
```

```bash
sudo apt install -y caddy
sudo systemctl enable --now caddy
```

Caddy сам выпустит и продлит Let's Encrypt-сертификат. DNS A-запись должна уже указывать на сервер.

### Вариант 2: Nginx + certbot

```bash
sudo apt install -y nginx certbot python3-certbot-nginx
```

**`/etc/nginx/sites-available/albion`:**
```nginx
server {
    listen 80;
    server_name albion.example.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    server_name albion.example.com;

    location /merithub/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
    }
    location /tg {
        proxy_pass http://127.0.0.1:8443;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
    location / {
        proxy_pass http://127.0.0.1:8443;
        proxy_set_header Host $host;
    }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/albion /etc/nginx/sites-enabled/
sudo certbot --nginx -d albion.example.com
sudo nginx -t && sudo systemctl reload nginx
```

> Пути: Telegram webhook живёт на `/tg`, MeritHub — на `/merithub/webhook`.
> Путь Telegram берётся из `TELEGRAM_WEBHOOK_URL` автоматически (R9-3): если
> URL `https://albion.example.com/tg`, бот слушает ровно `/tg`.

## 7. Проверка после запуска

```bash
# Сервисы живы?
systemctl status albion-bot albion-webhook

# Логи бота: должно быть
#   "Webhook: https://albion.example.com/tg (listening 127.0.0.1:8443/tg)"
journalctl -u albion-bot -f

# Вебхук-ресивер отвечает?
curl https://albion.example.com/health
# → {"status":"ok","service":"albion-merithub-webhook"}

# Telegram принял webhook?
curl -s "https://api.telegram.org/bot<TOKEN>/getWebhookInfo" | python3 -m json.tool
# → "url": "https://albion.example.com/tg", "pending_update_count": 0

# Продуктовая проверка: напишите боту /start → он отвечает.
# Затем /status (админ) → "Ожидает: N задач", Kill Switch: Полностью.
```

**Проверка связки с MeritHub** (когда подключите):
1. В панели MeritHub → Webhook Url вставьте `https://albion.example.com/merithub/webhook`.
2. Дёрните тестовое событие → в боте `/mh_events` появится захваченный payload.

## 8. Обновление и откат

```bash
cd /opt/albion
git pull                       # новая версия
.venv/bin/pip install -r requirements.txt   # если менялись зависимости
sudo systemctl restart albion-bot albion-webhook
```

Откат: `git checkout <предыдущий-коммит> && sudo systemctl restart ...`

**Перед обновлением всегда бэкап БД** (см. п.4). Миграции идемпотентны (`init_db`),
старые БД дополняются ALTER'ами автоматически.

## 9. Безопасность и грабли

| Грабли | Что делать |
|---|---|
| **Kill switch живёт в памяти** (H6) | После каждого рестарта уровень = 2 (Полностью). Проверьте `/status` и при необходимости настройте снова. |
| **Двойной `SafeStreamHandler`** (R9-8) | Косметика, не влияет на работу. |
| **Polling + webhook одновременно** | Нельзя: Telegram отдаёт 409. Перед переездом остановите локальный бот (или просто не запускайте его после `set_webhook` на VPS). |
| **Docker-вариант** | В `docker-compose.yml` `albion.db` монтируется файлом (H5): при отсутствии файла Docker создаст **каталог** и бот упадёт. Либо создайте пустой файл заранее, либо используйте systemd (рекомендую). |
| **`test_secret`** | Если не задать `TELEGRAM_WEBHOOK_SECRET` — подставится дефолт из кода. Сгенерируйте свой. |
| **Фаервол** | Наружу только 22/80/443. 8000 и 8443 слушают на 127.0.0.1. |
| **Бэкапы** | Ежедневный cron: `sqlite3 ... ".backup ..."` + копия `.env` (секреты!). |
| **Время** | Сервер в UTC — не важно: вся логика времени в org-зоне (`ALBION_ORG_TIMEZONE`). |

## 10. Чек-лист перед переездом (с локальной машины)

- [ ] `git push` всех фиксов (у вас уже: R9-1..R9-14, 230/230 тестов)
- [ ] `scp albion.db` на сервер (или готовность начать с нуля)
- [ ] Сгенерирован `TELEGRAM_WEBHOOK_SECRET`
- [ ] A-запись домена → IP VPS
- [ ] .env заполнен (см. п.3)
- [ ] Оба systemd-сервиса `active (running)`
- [ ] `getWebhookInfo` показывает ваш URL, бот отвечает на /start
- [ ] Старый локальный бот остановлен
