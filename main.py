import asyncio
import logging
import os
import sqlite3
import datetime
import json
from typing import List, Dict, Any, Optional
import sys
from openai import OpenAI
from maxapi import Bot, Dispatcher
from dotenv import load_dotenv
from maxapi.types import BotStarted, Command, MessageCreated

load_dotenv()
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_URL = "https://openrouter.ai/api/v1"
MAXBOT_TOKEN = os.getenv("MAXBOT_TOKEN")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

db_connection = sqlite3.connect(
    "my_database.db",
    check_same_thread=False,
    detect_types=sqlite3.PARSE_DECLTYPES,
)
db_connection.row_factory = sqlite3.Row


def _parse_iso_datetime(value: Optional[str]) -> Optional[datetime.datetime]:
    if not value:
        return None
    try:
        return datetime.datetime.fromisoformat(value)
    except Exception:
        try:
            return datetime.datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
        except Exception:
            logger.debug("Failed to parse datetime: %s", value)
            return None


def _format_reminders(reminders: List[Dict[str, Any]]) -> str:
    if not reminders:
        return "— Нет напоминаний —"
    header = f"{'ID':>4} | {'Время':<16} | Текст"
    sep = "-" * 4 + "-+-" + "-" * 16 + "-+-" + "-" * 20
    lines = [header, sep]
    for r in reminders:
        ts = r.get("timestamp")
        if isinstance(ts, str):
            ts_display = ts
        elif isinstance(ts, datetime.datetime):
            ts_display = ts.isoformat(sep=" ", timespec="minutes")
        else:
            ts_display = str(ts)
        rid = r.get("id")
        text = r.get("text")
        lines.append(f"{rid:>4} | {ts_display:<16} | {text}")
    return "\n".join(lines)


async def db_setup() -> None:
    def _setup():
        cur = db_connection.cursor()
        cur.execute(
            """
CREATE TABLE IF NOT EXISTS Reminders (
    id INTEGER PRIMARY KEY,
    userid INTEGER NOT NULL,
    timestamp DATETIME NOT NULL,
    text TEXT NOT NULL
)
"""
        )
        db_connection.commit()

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _setup)


async def db_fetch_user_reminders(userid: int) -> List[Dict[str, Any]]:
    def _fetch():
        cur = db_connection.cursor()
        cur.execute(
            "SELECT id, timestamp, text, userid FROM Reminders WHERE userid = ?",
            (userid,),
        )
        return [dict(row) for row in cur.fetchall()]

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _fetch)


async def db_fetch_all_reminders() -> List[Dict[str, Any]]:
    def _fetch_all():
        cur = db_connection.cursor()
        cur.execute("SELECT id, timestamp, text, userid FROM Reminders")
        return [dict(row) for row in cur.fetchall()]

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _fetch_all)


async def db_execute(sql: str, params: tuple = ()) -> int:
    def _exec():
        cur = db_connection.cursor()
        cur.execute(sql, params)
        db_connection.commit()
        return cur.lastrowid

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _exec)


if not OPENROUTER_API_KEY:
    logger.error(
        "Environment variable OPENROUTER_API_KEY is not set. AI calls will fail."
    )

ai_client: Optional[OpenAI]
if OPENROUTER_API_KEY:
    ai_client = OpenAI(base_url=OPENROUTER_URL, api_key=OPENROUTER_API_KEY)
else:
    ai_client = None


async def ai_request(reminders: List[Dict[str, Any]], user_input: str) -> str:
    reminders_text = json.dumps(reminders, default=str, ensure_ascii=False)
    if not ai_client:
        raise RuntimeError("AI client not configured (OPENROUTER_API_KEY missing)")

    def _call_ai() -> str:
        resp = ai_client.responses.create(
            model="tngtech/deepseek-r1t2-chimera:free",
            input=[
                {
                    "role": "system",
                    "content": f"""
Ты — цифровой секретарь (assistant), управляющий личными напоминаниями пользователя. Твоя единственная задача — проанализировать входящее сообщение пользователя и вернуть строго валидный JSON-объект (только JSON, без пояснений, без обёрток), соответствующий указанной схеме.

Контекст:
- Текущие напоминания пользователя: {reminders_text}
- Локальное текущее время: {datetime.datetime.now()}

Общие правила:
- Всегда отвечай ТОЛЬКО JSON-объектом, который соответствует схеме: поля `type`, `confidence`, `text`, `datetime`, `id` (см. схему ниже).
- Если ты не уверен в действии — выбери `Help` и в поле `text` кратко объясни, какую именно дополнительную информацию нужно от пользователя.
- Поле `confidence` должно быть числом 0..1, отражающим уверенность твоего вывода (0 — нет уверенности, 1 — полностью уверен).
- Поле `datetime` должно быть в формате ISO 8601 (пример: "2025-11-13T18:30:00" или с часовым поясом). Если дата/время отсутствуют для типа действия, укажи пустую строку "".
- Поле `id` используется только для операций, связанных с существующим напоминанием (Edit, Delete). Если id не применим — укажи 0.
- Не включай никаких дополнительных полей, комментариев или текста вне JSON.

Типы действий (детально с примерами):

- "Add": пользователь хочет создать новое напоминание.
    Когда использовать: если в сообщении явно указана новая задача или время, и она не совпадает с существующими напоминаниями.
    Примеры пользовательских фраз:
        • "Напомни завтра в 09:00 позвонить маме"
        • "Добавь встречу с Петей в пятницу в 15:00"
    Пример JSON:
        {{"type":"Add","confidence":0.95,"text":"Позвонить маме","datetime":"2025-11-14T09:00:00","id":0}}

- "Edit": пользователь изменяет существующее напоминание.
    Когда использовать: если в сообщении указано изменение времени/текста конкретного напоминания, или если текст пользователя совпадает (полностью или частично) с существующим напоминанием и присутствует новая дата/время.
    Правила сопоставления: сравни ключевые слова/фразы в запросе с текстами текущих напоминаний; при частичном совпадении предпочти Edit.
    Примеры пользовательских фраз:
        • "Перенеси встречу с Петей на понедельник в 11:00"
        • "Психология — сейчас в 14:30 вместо 13:00"
    Пример JSON (если найдено напоминание с id 42):
        {{"type":"Edit","confidence":0.9,"text":"Встреча с Петей","datetime":"2025-11-17T11:00:00","id":42}}

- "Delete": пользователь просит удалить существующее напоминание.
    Когда использовать: если есть команды типа "удали", "стереть", "отмени напоминание про ..." или при явном указании id/полного совпадения текста.
    Примеры пользовательских фраз:
        • "Удали напоминание про врача"
        • "Отмени встречу с Анной"
    Пример JSON (если удаляем напоминание id=7):
        {{"type":"Delete","confidence":0.98,"text":"Удалить напоминание про врача","datetime":"","id":7}}

- "List": пользователь хочет получить список напоминаний (все или за период/день).
    Когда использовать: фразы типа "что у меня сегодня", "покажи все", "покажи напоминания на завтра".
    Пример JSON:
        {{"type":"List","confidence":0.9,"text":"Показать напоминания на сегодня","datetime":"","id":0}}

- "Help": запрос не ясен, не связан с напоминаниями, или модель не уверена в интерпретации.
    Когда использовать: неполные, двусмысленные или не относящиеся к напоминаниям запросы.
    Примеры:
        • "Что ты умеешь?"
        • "Сделай мне кофе" (не про напоминания)
    Пример JSON:
        {{"type":"Help","confidence":0.4,"text":"Пожалуйста, уточните: хотите добавить, изменить, удалить или посмотреть напоминания? Укажите текст и желаемое время.","datetime":"","id":0}}

Дополнительные указания для разбора:
- Если пользователь указывает только время, но есть ровно одно схожее напоминание — выбери Edit и подставь id этого напоминания.
- Если пользователь даёт новый текст, который не совпадает с существующими, — выбери Add.
- Всегда пытайся извлечь точную дату/время. Если невозможно — оставь поле `datetime` пустым ("") и понизь `confidence`.

Формат JSON (обязательное соответствие схеме):
{{
    "type": "List|Edit|Add|Delete|Help",
    "confidence": 0.0-1.0,
    "text": "строка, краткое описание действия/текста напоминания",
    "datetime": "строка в ISO 8601 или пустая строка",
    "id": integer (id напоминания или 0)
}}

Примеры: (несколько типичных входных ситуаций и ожидаемые JSON-ответы)
1) Текущие напоминания: [{{"id":3,"text":"Врач","timestamp":"2025-11-13T10:00:00"}}]
     Вход: "Перенеси врача на 15:00"
    Ответ: {{"type":"Edit","confidence":0.9,"text":"Врач","datetime":"2025-11-13T15:00:00","id":3}}

2) Текущие напоминания: []
     Вход: "Напомни завтра купить хлеб"
    Ответ: {{"type":"Add","confidence":0.95,"text":"Купить хлеб","datetime":"2025-11-14T09:00:00","id":0}}

3) Текущие напоминания: [...] Вход: "покажи все"
    Ответ: {{"type":"List","confidence":0.8,"text":"Показать все напоминания","datetime":"","id":0}}

4) Непонятный запрос: "А где мои ключи?"
    Ответ: {{"type":"Help","confidence":0.3,"text":"Неясно, какое действие с напоминаниями вы хотите: добавить, изменить, удалить или посмотреть?","datetime":"","id":0}}

ВАЖНО: Ещё раз — возвращай ТОЛЬКО JSON-объект без объяснений и без дополнительных символов. Если модель не уверена, используй `Help`.
""",
                },
                {"role": "user", "content": user_input},
            ],
            text={
                "format": {
                    "type": "json_schema",
                    "name": "my_response",
                    "schema": {
                        "type": "object",
                        "properties": {
                            "type": {
                                "type": "string",
                                "enum": ["List", "Edit", "Add", "Delete", "Help"],
                            },
                            "confidence": {
                                "type": "number",
                                "minimum": 0,
                                "maximum": 1,
                            },
                            "text": {"type": "string"},
                            "datetime": {"type": "string", "format": "date-time"},
                            "id": {"type": "integer"},
                        },
                        "required": ["type", "confidence", "text", "datetime", "id"],
                        "additionalProperties": False,
                    },
                    "strict": True,
                }
            },
        )
        return getattr(resp, "output_text", str(resp))

    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(None, _call_ai)
    except Exception as exc:
        logger.exception("AI request failed: %s", exc)
        raise RuntimeError("AI request failed") from exc


async def _send_reminder_to_user(reminder: Dict[str, Any]) -> bool:
    uid = reminder.get("userid")
    text = reminder.get("text")
    ts = reminder.get("timestamp")
    if isinstance(ts, str):
        ts_display = ts
    elif isinstance(ts, datetime.datetime):
        ts_display = ts.isoformat(sep=" ", timespec="minutes")
    else:
        ts_display = str(ts)

    message = f"⏰ Напоминание:\n{ts_display} — {text}"
    try:
        await max_bot.send_message(chat_id=uid, text=message)
        return True
    except Exception:
        logger.exception("Failed to send reminder to user %s", uid)
        return False


async def check_due_reminders_loop(poll_interval_seconds: int = 5):
    logger.info("Starting reminders checker (interval=%s sec)", poll_interval_seconds)
    try:
        while True:
            try:
                now = datetime.datetime.now()
                now = now.replace(tzinfo=datetime.timezone.utc)
                all_reminders = await db_fetch_all_reminders()
                due = []
                for r in all_reminders:
                    ts = r.get("timestamp")
                    # Parse timestamp if needed
                    if isinstance(ts, str):
                        parsed = _parse_iso_datetime(ts)
                    elif isinstance(ts, datetime.datetime):
                        parsed = ts
                    else:
                        parsed = None
                    if parsed and parsed <= now:
                        due.append((r, parsed))

                if due:
                    logger.info("Found %d due reminders", len(due))
                for r, parsed in due:
                    sent = await _send_reminder_to_user(r)
                    if sent:
                        await db_execute(
                            "DELETE FROM Reminders WHERE id = ?", (r.get("id"),)
                        )
                        logger.info(
                            "Sent and deleted reminder id=%s for user=%s",
                            r.get("id"),
                            r.get("userid"),
                        )
            except Exception:
                logger.exception("Error in reminders checker iteration")

            await asyncio.sleep(poll_interval_seconds)
    except asyncio.CancelledError:
        logger.info("Reminders checker cancelled")
        raise


max_bot = Bot(MAXBOT_TOKEN)
max_dispatcher = Dispatcher()


def _validate_startup_config() -> None:
    bot_token = MAXBOT_TOKEN
    if not bot_token:
        logger.error("MAXBOT_TOKEN is not set. Exiting.")
        print("ERROR: please set MAXBOT_TOKEN environment variable.")
        sys.exit(2)
        
    if not OPENROUTER_API_KEY:
        logger.warning("OPENROUTER_API_KEY not set — AI features will be disabled.")


@max_dispatcher.bot_started()
async def max_on_bot_start(event: BotStarted):
    await event.bot.send_message(chat_id=event.chat_id, text="✅ Бот запущен")


@max_dispatcher.message_created(Command("start"))
async def max_on_start_command(event: MessageCreated):
    await event.message.answer("✅ Команда /start принята")


@max_dispatcher.message_created()
async def max_on_message(event: MessageCreated):
    reminders = await db_fetch_user_reminders(event.chat.chat_id)

    try:
        response = await ai_request(reminders, event.message.body.text)
    except Exception as e:
        await event.message.answer(f"❗ Ошибка при обращении к AI: {e}")
        return

    await event.message.answer(response)

    try:
        j = json.loads(response)
    except json.JSONDecodeError:
        await event.message.answer("⚠️ Не удалось разобрать ответ AI (ожидался JSON).")
        return

    action_type = j.get("type")

    if action_type == "List":
        formatted = _format_reminders(reminders)
        await event.message.answer(f"📋 Напоминания ({len(reminders)}):\n\n{formatted}")
        return

    try:
        if action_type == "Add":
            dt = j.get("datetime")
            text = j.get("text", "")
            if not dt:
                await event.message.answer(
                    "❌ Невозможно добавить — отсутствует поле datetime."
                )
                return
            timestamp = _parse_iso_datetime(dt)
            if not timestamp:
                await event.message.answer(
                    "Невозможно добавить — неверный формат datetime."
                )
                return
            await db_execute(
                "INSERT INTO Reminders (timestamp, text, userid) VALUES (?, ?, ?)",
                (timestamp, text, event.chat.chat_id),
            )
            await event.message.answer("✅ Напоминание добавлено.")
            return

        if action_type == "Delete":
            rid = j.get("id")
            if not rid:
                await event.message.answer("❌ Невозможно удалить — не указан id.")
                return
            await db_execute(
                "DELETE FROM Reminders WHERE id = ? AND userid = ?",
                (rid, event.message.sender.user_id),
            )
            await event.message.answer("✅ Напоминание удалено.")
            return

        if action_type == "Edit":
            rid = j.get("id")
            dt = j.get("datetime")
            text = j.get("text", "")
            if not (rid and dt):
                await event.message.answer(
                    "Невозможно изменить — не указан id или datetime."
                )
                return
            timestamp = _parse_iso_datetime(dt)
            if not timestamp:
                await event.message.answer(
                    "Невозможно изменить — неверный формат datetime."
                )
                return
            await db_execute(
                "UPDATE Reminders SET timestamp = ?, text = ? WHERE id = ? AND userid = ?",
                (timestamp, text, rid, event.message.sender.user_id),
            )
            await event.message.answer("✅ Напоминание обновлено.")
            return

        if action_type == "Help":
            await event.message.answer(j.get("text", "Пожалуйста, уточните запрос."))
            return

        await event.message.answer("⚠️ Неизвестный тип действия в ответе AI.")
    except Exception:
        logger.exception("Ошибка при применении действий с БД")
        await event.message.answer("❗ Произошла ошибка при изменении напоминаний.")


async def main():
    _validate_startup_config()
    await db_setup()

    checker_task = asyncio.create_task(check_due_reminders_loop())

    try:
        await max_dispatcher.start_polling(max_bot)
    finally:
        checker_task.cancel()
        try:
            await checker_task
        except asyncio.CancelledError:
            pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        max_bot.close_session()
        ai_client.close()
        try:
            db_connection.commit()
            db_connection.close()
        except Exception:
            logger.exception("Ошибка при закрытии БД")
