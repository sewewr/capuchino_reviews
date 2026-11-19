"""
Восстановление базы из резервной копии, которую бот присылает в Telegram.

    DATABASE_URL=postgresql://... python restore_backup.py reviews-backup-2026-11-01.zip

ВНИМАНИЕ: данные в базе по DATABASE_URL заменяются данными из копии.
Скрипт показывает, что в копии, и спрашивает подтверждение.

Как пользоваться при переезде или сбое:
  1. Создать в Railway новую PostgreSQL (или взять пустую).
  2. Скопировать её DATABASE_URL (вкладка Variables → DATABASE_PUBLIC_URL,
     если запускаете со своего компьютера).
  3. Запустить этот скрипт с файлом копии.
  4. Указать боту новую базу и перезапустить.
"""

import os
import sys

import backup
import storage


def main() -> None:
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)
    path = sys.argv[1]
    data = open(path, "rb").read()
    meta, tables = backup.read_backup(data)
    print(f"Копия от {meta.get('created_at')}: {backup.describe(meta.get('counts', {}))}")

    url = os.environ.get("DATABASE_URL")
    if not url:
        print("Не задана переменная DATABASE_URL — куда восстанавливать?")
        sys.exit(1)
    host = url.split("@")[-1]
    answer = input(f"Заменить данные в базе {host}? Напишите ДА: ").strip()
    if answer != "ДА":
        print("Отменено.")
        return

    conn = storage.connect(url)
    restored = backup.restore(conn, data)
    conn.close()
    print("Готово:", ", ".join(f"{t} — {n}" for t, n in restored.items()))


if __name__ == "__main__":
    main()
