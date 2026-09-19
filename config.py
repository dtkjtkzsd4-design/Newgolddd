import os

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
MAIN_ADMIN_ID = int(os.getenv("MAIN_ADMIN_ID", "0") or "0")

# Необязательно: чат/канал, куда бот пишет лог сделок (бот должен быть там админом).
LOG_CHAT_ID = int(os.getenv("LOG_CHAT_ID", "0") or "0")

# На Railway при подключённом Volume эта переменная выставляется автоматически.
VOLUME_PATH = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "")

# Путь к базе: DB_PATH из окружения -> файл на Railway Volume -> локальный файл.
DB_PATH = os.getenv("DB_PATH") or (
    os.path.join(VOLUME_PATH, "ishopgold.db") if VOLUME_PATH else "ishopgold.db"
)

ON_RAILWAY = bool(os.getenv("RAILWAY_PROJECT_ID") or os.getenv("RAILWAY_ENVIRONMENT_NAME"))
