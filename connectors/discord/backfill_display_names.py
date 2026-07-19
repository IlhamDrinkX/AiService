"""
Одноразовая миграция: обновляет author_name на текущий display_name (ник на
сервере, если задан, иначе глобальное имя) для ВСЕХ уже сохранённых
сообщений в discord.db.

Зачем: до этой правки бот писал str(message.author) — технический юзернейм
(вроде "vanche5102"), а не то имя, что видно в самом Discord. bot.py уже
исправлен и новые сообщения сразу пишут display_name — но это не помогает
задним числом тем, кто уже есть в базе и давно не писал. Этот скрипт чинит
существующие данные одним прогоном: для каждого уникального author_id уже в
базе запрашивает текущий display_name через guild.fetch_member() (обычный
REST-запрос, не требует привилегированного Members-интента) и переписывает
author_name во ВСЕХ его сообщениях.

Не часть обычного sync.py/bot.py — запускать вручную один раз после
обновления bot.py (см. PROGRESS.md, "по просьбе пользователя, 2026-07-19").
Безопасно запускать повторно (идемпотентно — просто освежит имена).

Запуск: python backfill_display_names.py (тот же venv, что у bot.py).
"""

import asyncio
import os
import sqlite3
from pathlib import Path

import discord
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

TOKEN = os.environ["DISCORD_BOT_TOKEN"]
DB_PATH = Path(os.environ.get("DB_PATH", str(ROOT / "data" / "discord.db")))


async def main():
    if not DB_PATH.exists():
        print(f"База не найдена: {DB_PATH}")
        return

    conn = sqlite3.connect(DB_PATH)
    author_ids = [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT author_id FROM discord_messages WHERE author_id IS NOT NULL"
        )
    ]
    print(f"Уникальных авторов в базе: {len(author_ids)}")

    intents = discord.Intents.default()
    intents.guilds = True
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        try:
            if not client.guilds:
                print("Бот не состоит ни в одном сервере — нечего резолвить.")
                return
            guild = client.guilds[0]
            print(f"Сервер: {guild.name} ({guild.id})")

            resolved = 0
            not_found = 0
            for uid in author_ids:
                try:
                    member = await guild.fetch_member(int(uid))
                except discord.NotFound:
                    not_found += 1
                    continue
                except (discord.HTTPException, ValueError) as e:
                    print(f"  {uid}: пропущен ({e})")
                    continue
                conn.execute(
                    "UPDATE discord_messages SET author_name = ? WHERE author_id = ?",
                    (member.display_name, uid),
                )
                resolved += 1
                await asyncio.sleep(0.15)  # не долбить REST лимиты подряд

            conn.commit()
            print(f"Обновлено: {resolved}, не найдено на сервере (ушли?): {not_found}")
        finally:
            conn.close()
            await client.close()

    await client.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
