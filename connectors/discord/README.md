# Discord connector

Read-only. Забирает историю сообщений (backfill) и слушает новые в реальном времени, складывает всё в локальный SQLite. Ничего не пишет обратно в Discord.

Покрытие: обычные текстовые каналы, треды, форум-посты (включая архивные), текстовый чат в голосовых каналах. Отдельно отслеживаются правки (с историей старых версий) и удаления (контент не стирается, помечается как удалённый).

## Запуск

```bash
cd connectors/discord
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
# открой .env и вставь DISCORD_BOT_TOKEN
python bot.py
```

При старте бот:
1. Подключится и залогируется под именем бота.
2. Для каждого сервера, куда он добавлен, пройдёт по текстовым и голосовым каналам, тредам (активным и архивным) и форум-постам (или только по указанным в `CHANNEL_IDS`) и подтянет последние `BACKFILL_LIMIT`/`ARCHIVED_THREAD_LIMIT` сообщений.
3. Дальше будет слушать всё перечисленное живьём: новые сообщения, правки, удаления, а также новые треды/форум-посты — подключается к ним и бэкфиллит на лету, без перезапуска.

Что читаем, включается/выключается через `.env` (`INCLUDE_THREADS`, `INCLUDE_ARCHIVED_THREADS`, `INCLUDE_FORUM`, `INCLUDE_VOICE_TEXT`) — см. `.env.example`.

## Проверка результата

```bash
sqlite3 data/discord.db "SELECT channel_name, channel_type, author_name, content FROM discord_messages LIMIT 10;"
sqlite3 data/discord.db "SELECT COUNT(*) FROM discord_messages;"
sqlite3 data/discord.db "SELECT COUNT(*) FROM discord_messages WHERE deleted_at IS NOT NULL;"
sqlite3 data/discord.db "SELECT * FROM discord_message_edits ORDER BY edited_at DESC LIMIT 10;"
```

## Права боту нужны

В Developer Portal → Bot: `Message Content Intent` включён (уже требуется предыдущей версией). Права на сервере — как минимум `View Channels`, `Read Message History`, и `Read Message History` внутри тредов (обычно даётся автоматически вместе с доступом к родительскому каналу). Если бот не видит какой-то канал — просто пропускает его с предупреждением в логе, не падает.

## Ограничения этой версии

- Вложения сохраняются как ссылки (URL), не скачиваются — скачивание/OCR фото подключим на этапе парсинга фото по общему плану.
- Правки: если правка приходит без контента в событии (редкий случай, например изменился только embed), бот дозапрашивает сообщение через REST — если оно к этому моменту уже удалено, правка не логируется.
- Реакции (эмодзи) не отслеживаются — только сами сообщения.
- Хранение — локальный файл SQLite, под миграцию в общий Postgres+pgvector по мере готовности остальной инфраструктуры. Существующие `data/discord.db` мигрируются автоматически при первом запуске новой версии (добавляются недостающие колонки).
