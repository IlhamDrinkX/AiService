"""
Одноразовая миграция (2026-07-19): до этого коммита Google Sheets
экспортировались через Drive API как CSV, что физически возвращает только
первый лист — остальные листы многолистовых таблиц никогда не попадали в
text_content. Google Slides экспортировались как plain text, что отдаёт
только видимый текст на слайдах — заметки докладчика никогда не
извлекались. Оба случая теперь читаются через отдельные API (Sheets API /
Slides API, см. extract.py), но уже сохранённые записи в базе останутся
неполными, пока их не перечитать заново — sync.py пропускает файлы, у
которых не изменился modifiedTime, а у уже засинканных Sheets/Slides он не
менялся.

Этот скрипт обнуляет modified_time у ВСЕХ файлов типа Google Sheets и
Google Slides (не только "подозрительно усечённых" по длине, как
reset_truncated.py для TEXT_MAX_CHARS — здесь неполнота была структурной,
не связанной с длиной текста). Дальше обычный `python sync.py` перечитает
их уже полностью (все листы / текст + заметки).

Запуск (один раз, после обновления кода и переустановки зависимостей):

    pip install -r requirements.txt
    python reset_sheets_slides.py
    python sync.py   # первый запуск после смены scopes откроет браузер для повторного согласия
"""

import os

from dotenv import load_dotenv

from extract import GOOGLE_SHEETS_MIME, GOOGLE_SLIDES_MIME
from storage import DriveStorage

load_dotenv()

DB_PATH = os.environ.get("DB_PATH", "./data/drive.db")


def main():
    storage = DriveStorage(DB_PATH)
    n = storage.reset_by_mime([GOOGLE_SHEETS_MIME, GOOGLE_SLIDES_MIME])
    if n:
        print(f"Помечено на переизвлечение: {n} файлов (Google Sheets/Slides). "
              f"Запусти python sync.py, чтобы перечитать их полностью (все листы / текст+заметки).")
    else:
        print("Файлов Google Sheets/Slides в базе не найдено — sync.py ещё ни разу не запускался, либо их просто нет на диске.")


if __name__ == "__main__":
    main()
