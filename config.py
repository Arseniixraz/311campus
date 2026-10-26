import hashlib
import os

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]

# "id:Имя,id:Имя,id:Имя" — три владельца комнаты
_admins = [a.split(":", 1) for a in os.environ["ADMINS"].split(",")]
ADMIN_IDS = [int(a[0]) for a in _admins]
ADMIN_NAMES = {int(a[0]): (a[1] if len(a) > 1 else a[0]) for a in _admins}

REQUIRED_APPROVALS = int(os.getenv("REQUIRED_APPROVALS", "2"))
TIMEZONE = os.getenv("TIMEZONE", "Europe/Moscow")
VISIT_FROM = os.getenv("VISIT_FROM", "10:00")
VISIT_TO = os.getenv("VISIT_TO", "22:00")
MIN_LEAD_MINUTES = int(os.getenv("MIN_LEAD_MINUTES", "30"))
MAX_REQUESTS_PER_DAY = int(os.getenv("MAX_REQUESTS_PER_DAY", "2"))
REMIND_MINUTES = int(os.getenv("REMIND_MINUTES", "15"))
DB_PATH = os.getenv("DB_PATH", "bot.db")

# на Render: Postgres вместо файла (там диск стирается) и веб-хук вместо опроса
DATABASE_URL = os.getenv("DATABASE_URL")
WEBHOOK_BASE_URL = os.getenv("WEBHOOK_BASE_URL") or os.getenv("RENDER_EXTERNAL_URL")
WEBHOOK_SECRET = hashlib.sha256(BOT_TOKEN.encode()).hexdigest()
PORT = int(os.getenv("PORT", "10000"))
