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

RELAY_DAILY_LIMIT = int(os.getenv("RELAY_DAILY_LIMIT", "30"))  # сообщений гостя жильцам в день

RULES = (
    "У нас в комнате 311 карантин: зайти можно только по заявке, которую одобрили жильцы.\n"
    f"• Заявку рассматривают {len(ADMIN_IDS)} жильцов, нужно {REQUIRED_APPROVALS} одобрения.\n"
    f"• Принимаем с {VISIT_FROM} до {VISIT_TO}, заявку нужно подать минимум за {MIN_LEAD_MINUTES} мин.\n"
    f"• Не больше {MAX_REQUESTS_PER_DAY} заявок в день и одна активная заявка за раз.\n"
    "• После одобрения придёт 4-значный код: покажи его у двери.\n"
    "• Жильцы могут написать причину решения — она придёт тебе вместе с ответом."
)
