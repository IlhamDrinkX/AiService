# Gmail connector

Read-only, один личный ящик через OAuth2. Первый запуск делает полную синхронизацию всей почты (кроме спама/корзины по умолчанию), последующие запуски — только новое, через History API.

## Настройка Google Cloud (один раз)

1. console.cloud.google.com → создать/выбрать проект.
2. APIs & Services → Library → **Gmail API** → Enable.
3. APIs & Services → OAuth consent screen → User type **External** → добавить scope `.../auth/gmail.readonly` → в Test users добавить свою почту.
4. APIs & Services → Credentials → Create Credentials → **OAuth client ID** → Application type **Desktop app** → скачать JSON.
5. Положить скачанный файл в эту папку как `credentials.json` (или указать путь в `.env`).

## Запуск

```bash
cd connectors/gmail
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
python sync.py
```

При первом запуске откроется браузер — войти под корп. аккаунтом, разрешить read-only доступ к почте. Дальше токен кешируется в `data/token.json`, повторный вход не нужен (пока не истечёт refresh token или не отозвать доступ вручную).

Запускать `python sync.py` периодически (по расписанию) — каждый запуск подтягивает только новые письма.

## Проверка результата

```bash
sqlite3 data/gmail.db "SELECT sender, subject, date FROM gmail_messages ORDER BY date DESC LIMIT 10;"
sqlite3 data/gmail.db "SELECT COUNT(*) FROM gmail_messages;"
```

## Ограничения этой версии

- Вложения сохраняются как метаданные (имя файла, mime-type, attachment_id), сам файл не скачивается — скачивание/OCR подключим на этапе парсинга фото/документов.
- History API у Gmail хранит историю примерно неделю — если скрипт не запускать дольше, коннектор автоматически откатится на полную пересинхронизацию.
- Хранение — локальный SQLite, миграция в общий Postgres+pgvector будет позже, вместе с остальными коннекторами.
