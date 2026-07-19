"""
Одноразовая миграция: раньше TEXT_MAX_CHARS был 50000 по умолчанию, и
длинные документы (примерно от 15-20 страниц и выше) молча обрезались при
extract_text(). Теперь по умолчанию ограничения нет (TEXT_MAX_CHARS=0), но
sync.py перечитывает текст файла только если у него изменился modifiedTime
в Drive — а у уже обрезанных документов он не менялся, значит сам по себе
следующий python sync.py их не тронет.

Этот скрипт помечает такие файлы как "нужно перечитать" (обнуляет
modified_time у строк, где длина text_content ровно совпадает со старым
лимитом — обрезание, а не совпадение). Дальше обычный `python sync.py`
скачает и извлечёт их текст заново, уже целиком.

Запуск (один раз, после обновления .env на TEXT_MAX_CHARS=0):

    python reset_truncated.py
    python sync.py
"""

import os

from dotenv import load_dotenv

from storage import DriveStorage

load_dotenv()

DB_PATH = os.environ.get("DB_PATH", "./data/drive.db")
OLD_CAP_CHARS = int(os.environ.get("OLD_TEXT_MAX_CHARS", "50000"))


def main():
    storage = DriveStorage(DB_PATH)
    n = storage.reset_truncated(OLD_CAP_CHARS)
    if n:
        print(f"Помечено на переизвлечение: {n} файлов (text_content ровно {OLD_CAP_CHARS} символов). "
              f"Запусти python sync.py, чтобы перечитать их целиком.")
    else:
        print(f"Файлов с text_content ровно {OLD_CAP_CHARS} символов не найдено — обрезанных документов нет "
              f"(или sync.py ещё ни разу не запускался).")


if __name__ == "__main__":
    main()
