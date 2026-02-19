"""
VK Profile Review Bot
=====================
Бот показывает аватарки профилей ВК по одной (в случайном порядке) с кнопками ДА / НЕТ.
- ДА  → профиль сохраняется в списке одобренных
- НЕТ → профиль удаляется из profiles.txt
- Если профиль уже оценил любой пользователь — другим он больше не показывается

Зависимости:
    pip install python-telegram-bot requests playwright python-dotenv
    playwright install chromium

Файлы:
    .env          — токены
    profiles.txt  — ссылки на профили (поддерживается формат "url | Имя, возраст")

.env:
    VK_TOKEN=...
    TG_BOT_TOKEN=...
"""

import os
import random
import time
import asyncio
import logging
import requests
import tempfile
from pathlib import Path
from dotenv import load_dotenv

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

load_dotenv()

# ── Конфиг ──────────────────────────────────────────────────────────────────
VK_TOKEN      = os.getenv("VK_TOKEN", "")
VK_VERSION    = "5.199"
TG_TOKEN      = os.getenv("TG_BOT_TOKEN", "")
PROFILES_FILE = "profiles.txt"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Глобальное состояние ─────────────────────────────────────────────────────
reviewed_globally: set[str] = set()   # профили, оценённые хоть кем-то за сессию работы бота
sessions: dict[int, "Session"] = {}   # сессии по user_id


# ── Сессия пользователя ──────────────────────────────────────────────────────
class Session:
    def __init__(self):
        self.queue: list[str] = []
        self.current: str | None = None
        self.tmpdir: tempfile.TemporaryDirectory | None = None
        self.accepted: list[str] = []


def get_session(user_id: int) -> Session:
    if user_id not in sessions:
        sessions[user_id] = Session()
    return sessions[user_id]


# ── Работа с файлом профилей ─────────────────────────────────────────────────
def load_profiles() -> list[str]:
    """Читает profiles.txt, парсит URL, фильтрует уже просмотренные, перемешивает."""
    if not Path(PROFILES_FILE).exists():
        return []
    result = []
    for line in Path(PROFILES_FILE).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        url = line.split("|")[0].strip()
        if url and url not in reviewed_globally:
            result.append(url)
    random.shuffle(result)
    return result


def remove_profile(url: str):
    """Удаляет строку с данным URL из profiles.txt."""
    if not Path(PROFILES_FILE).exists():
        return
    lines = Path(PROFILES_FILE).read_text(encoding="utf-8").splitlines()
    filtered = [l for l in lines if l.split("|")[0].strip() != url]
    Path(PROFILES_FILE).write_text("\n".join(filtered) + "\n", encoding="utf-8")


def mark_reviewed(url: str):
    """Помечает профиль как просмотренный и убирает его из очередей всех пользователей."""
    reviewed_globally.add(url)
    for s in sessions.values():
        if url in s.queue:
            s.queue.remove(url)
        if s.current == url:
            s.current = None


# ── VK хелперы ───────────────────────────────────────────────────────────────
def vk_api(method: str, params: dict) -> dict:
    params.update({"access_token": VK_TOKEN, "v": VK_VERSION})
    r = requests.get(f"https://api.vk.com/method/{method}", params=params, timeout=15)
    r.raise_for_status()
    return r.json()


def get_vk_user(screen_name: str) -> dict | None:
    resp = vk_api("users.get", {
        "user_ids": screen_name,
        "fields": "photo_max_orig,photo_max,is_closed,deactivated,first_name,last_name",
    })
    users = resp.get("response")
    return users[0] if users else None


def extract_screen_name(url: str) -> str:
    return url.rstrip("/").split("/")[-1]


def download_image(url: str, dest: str) -> bool:
    try:
        r = requests.get(url, timeout=20, stream=True)
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(8192):
                f.write(chunk)
        return True
    except Exception as e:
        log.warning(f"download_image error: {e}")
        return False


def screenshot_avatar(profile_url: str, dest: str) -> bool:
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page.goto(profile_url, wait_until="networkidle", timeout=30000)
            time.sleep(2)
            for sel in [".ProfileAvatar img", ".userpic_crop img", "img.UserpicImage", ".profile_avatar img"]:
                el = page.query_selector(sel)
                if el:
                    el.screenshot(path=dest)
                    browser.close()
                    return True
            page.screenshot(path=dest, clip={"x": 0, "y": 0, "width": 480, "height": 480})
            browser.close()
            return True
    except Exception as e:
        log.warning(f"screenshot error: {e}")
        return False


def get_avatar_path(profile_url: str, tmpdir: str) -> tuple[str | None, dict | None]:
    screen_name = extract_screen_name(profile_url)
    user = get_vk_user(screen_name)
    if not user or "deactivated" in user:
        return None, user

    dest = os.path.join(tmpdir, "avatar.jpg")

    if not user.get("is_closed"):
        img_url = user.get("photo_max_orig") or user.get("photo_max")
        if img_url and download_image(img_url, dest):
            return dest, user

    if screenshot_avatar(profile_url, dest):
        return dest, user

    return None, user


# ── Telegram хендлеры ────────────────────────────────────────────────────────
async def send_next(update_or_query, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    session = get_session(user_id)

    if hasattr(update_or_query, "message") and update_or_query.message:
        chat_id = update_or_query.message.chat_id
    else:
        chat_id = update_or_query.message.chat_id

    if not session.queue:
        total = len(session.accepted)
        msg = (
            f"✅ Просмотр завершён!\n"
            f"Одобрено профилей: {total}\n\n"
            + ("\n".join(session.accepted) if session.accepted else "Ни одного.")
        )
        await context.bot.send_message(chat_id=chat_id, text=msg)
        return

    url = session.queue[0]
    session.current = url
    screen_name = extract_screen_name(url)

    await context.bot.send_message(chat_id=chat_id, text=f"⏳ Загружаю профиль @{screen_name}...")

    if session.tmpdir:
        try:
            session.tmpdir.cleanup()
        except Exception:
            pass
    session.tmpdir = tempfile.TemporaryDirectory()

    loop = asyncio.get_event_loop()
    img_path, user = await loop.run_in_executor(
        None, get_avatar_path, url, session.tmpdir.name
    )

    if user:
        name = f"{user.get('first_name', '')} {user.get('last_name', '')}".strip()
        closed = "🔒 " if user.get("is_closed") else ""
        caption = f"{closed}{name}\n{url}\n\n{len(session.queue)} осталось в очереди"
    else:
        caption = f"{url}\n\n{len(session.queue)} осталось в очереди"

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ ДА",  callback_data=f"yes:{user_id}"),
        InlineKeyboardButton("❌ НЕТ", callback_data=f"no:{user_id}"),
    ]])

    if img_path and os.path.exists(img_path):
        with open(img_path, "rb") as f:
            await context.bot.send_photo(
                chat_id=chat_id, photo=f,
                caption=caption, reply_markup=keyboard,
            )
    else:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"🖼 Не удалось получить аватарку\n{caption}",
            reply_markup=keyboard,
        )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    session = get_session(user_id)
    profiles = load_profiles()

    if not profiles:
        await update.message.reply_text(
            f"📂 Нет доступных профилей.\n"
            f"Либо `{PROFILES_FILE}` пуст, либо все профили уже были оценены.",
            parse_mode="Markdown"
        )
        return

    session.queue = profiles
    session.accepted = []
    session.current = None

    await update.message.reply_text(
        f"🚀 Начинаем! Профилей в очереди: {len(profiles)}\n"
        "Нажимай ДА / НЕТ для каждого."
    )
    await send_next(update, context, user_id)


async def cmd_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Пропустить текущий без оценки и без удаления из файла."""
    user_id = update.effective_user.id
    session = get_session(user_id)
    if session.queue:
        session.queue.pop(0)
    await send_next(update, context, user_id)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session = get_session(update.effective_user.id)
    await update.message.reply_text(
        f"📊 В очереди: {len(session.queue)}\n"
        f"Одобрено: {len(session.accepted)}\n"
        f"Оценено глобально: {len(reviewed_globally)}"
    )


async def cmd_accepted(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session = get_session(update.effective_user.id)
    if not session.accepted:
        await update.message.reply_text("Пока ни одного одобренного.")
        return
    await update.message.reply_text("✅ Одобренные профили:\n\n" + "\n".join(session.accepted))


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    parts = query.data.split(":")
    action = parts[0]
    owner_id = int(parts[1]) if len(parts) > 1 else query.from_user.id

    # кнопки может нажать только владелец сессии
    if query.from_user.id != owner_id:
        await query.answer("Это не твоя сессия!", show_alert=True)
        return

    session = get_session(owner_id)

    if not session.current:
        return

    url = session.current

    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass

    if action == "yes":
        session.accepted.append(url)
        mark_reviewed(url)
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=f"✅ Добавлено!\n{url}",
        )

    elif action == "no":
        mark_reviewed(url)
        remove_profile(url)
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="❌ Удалено из списка.",
        )

    await send_next(query, context, owner_id)


# ── Точка входа ──────────────────────────────────────────────────────────────
def main():
    if not TG_TOKEN:
        print("❌ Укажи TG_BOT_TOKEN в .env")
        return

    app = Application.builder().token(TG_TOKEN).build()
    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("skip",     cmd_skip))
    app.add_handler(CommandHandler("status",   cmd_status))
    app.add_handler(CommandHandler("accepted", cmd_accepted))
    app.add_handler(CallbackQueryHandler(on_callback))

    print("✅ Бот запущен. Команды: /start /skip /status /accepted")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
