"""Бобр Койн: Telegram-бот + игра + баланс в онлайн-базе Postgres."""
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from pathlib import Path
from urllib.parse import parse_qsl

import asyncpg
from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.filters import Command, CommandStart
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

BOT_TOKEN = os.environ["BOT_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]
WEBAPP_URL = (os.environ.get("WEBAPP_URL") or os.environ.get("RENDER_EXTERNAL_URL", "")).rstrip("/")
PORT = int(os.environ.get("PORT", "8080"))
BASE = Path(__file__).parent

WEBHOOK_PATH = "/webhook"
WEBHOOK_SECRET = hashlib.sha256(("hook" + BOT_TOKEN).encode()).hexdigest()[:32]

logging.basicConfig(level=logging.INFO)
pool = None


async def init_db(app=None):
    global pool
    pool = await asyncpg.create_pool(
        DATABASE_URL,
        min_size=1,
        max_size=3,
        statement_cache_size=0,
        max_inactive_connection_lifetime=30,
    )
    await pool.execute(
        """CREATE TABLE IF NOT EXISTS users (
            id BIGINT PRIMARY KEY,
            name TEXT NOT NULL DEFAULT '',
            coins BIGINT NOT NULL DEFAULT 0,
            best INT NOT NULL DEFAULT 0,
            created BIGINT NOT NULL
        )"""
    )


async def touch_user(user):
    name = (user.get("first_name") or user.get("username") or "Бобр")[:40]
    return await pool.fetchrow(
        """INSERT INTO users (id, name, created) VALUES ($1, $2, $3)
           ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name
           RETURNING coins, best""",
        user["id"], name, int(time.time()),
    )


def check_init_data(init_data):
    try:
        data = dict(parse_qsl(init_data, keep_blank_values=True))
        got_hash = data.pop("hash", None)
        if not got_hash:
            return None
        check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, got_hash):
            return None
        if time.time() - int(data.get("auth_date", "0")) > 86400:
            return None
        return json.loads(data["user"])
    except Exception:
        return None


RUNS = {}


async def read(request):
    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest()
    user = check_init_data(str(body.get("initData", "")))
    if not user:
        raise web.HTTPUnauthorized()
    return user, body


async def api_me(request):
    user, _ = await read(request)
    row = await touch_user(user)
    return web.json_response({"coins": row["coins"], "best": row["best"]})


async def api_start(request):
    user, _ = await read(request)
    await touch_user(user)
    now = time.time()
    for k in [k for k, (_, ts) in RUNS.items() if now - ts > 3600]:
        RUNS.pop(k, None)
    run_id = secrets.token_urlsafe(12)
    RUNS[run_id] = (user["id"], now)
    return web.json_response({"run_id": run_id})


async def api_finish(request):
    user, body = await read(request)
    row = await touch_user(user)
    run = RUNS.pop(str(body.get("run_id", "")), None)
    if not run or run[0] != user["id"]:
        return web.json_response({"coins": row["coins"], "best": row["best"]})

    elapsed = time.time() - run[1]
    max_dist = (330 * elapsed + 3 * elapsed ** 2) / 40 * 1.15 + 10
    try:
        dist = max(0, min(int(body.get("dist", 0)), int(max_dist)))
        coins = max(0, int(body.get("coins", 0)))
    except (TypeError, ValueError):
        dist, coins = 0, 0
    coins = min(coins, int(dist * 0.35) + 6)

    row = await pool.fetchrow(
        "UPDATE users SET coins = coins + $1, best = GREATEST(best, $2) WHERE id = $3 RETURNING coins, best",
        coins, dist, user["id"],
    )
    return web.json_response({"coins": row["coins"], "best": row["best"]})


async def index(request):
    return web.FileResponse(BASE / "index.html")


async def health(request):
    return web.Response(text="ok")


dp = Dispatcher()


def play_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🦫 Играть", web_app=WebAppInfo(url=WEBAPP_URL))]]
    )


@dp.message(CommandStart())
async def cmd_start(message: Message):
    await touch_user({"id": message.from_user.id, "first_name": message.from_user.first_name})
    await message.answer(
        "Привет! Это Бобр Койн.\n\n"
        "Бобр едет на машине: прыгай через брёвна и камни и собирай Бобр койны. "
        "Баланс сохраняется в твоём аккаунте навсегда.",
        reply_markup=play_keyboard(),
    )


@dp.message(Command("balance"))
async def cmd_balance(message: Message):
    row = await touch_user({"id": message.from_user.id, "first_name": message.from_user.first_name})
    await message.answer(
        f"Баланс: {row['coins']} Бобр койнов\nЛучший заезд: {row['best']} м",
        reply_markup=play_keyboard(),
    )


@dp.message(Command("top"))
async def cmd_top(message: Message):
    rows = await pool.fetch("SELECT name, coins FROM users ORDER BY coins DESC LIMIT 10")
    if not rows:
        await message.answer("Пока никого нет. Будь первым!", reply_markup=play_keyboard())
        return
    lines = [f"{i}. {r['name']}: {r['coins']}" for i, r in enumerate(rows, 1)]
    await message.answer("Топ по Бобр койнам:\n" + "\n".join(lines), reply_markup=play_keyboard())


async def on_startup(bot: Bot):
    await bot.set_webhook(WEBAPP_URL + WEBHOOK_PATH, secret_token=WEBHOOK_SECRET)
    logging.info("Вебхук установлен: %s", WEBAPP_URL + WEBHOOK_PATH)


def make_app():
    if not WEBAPP_URL:
        raise RuntimeError("Не задан WEBAPP_URL")
    bot = Bot(BOT_TOKEN)
    dp.startup.register(on_startup)

    app = web.Application()
    app.on_startup.append(init_db)
    app.router.add_get("/", index)
    app.router.add_get("/health", health)
    app.router.add_post("/api/me", api_me)
    app.router.add_post("/api/start", api_start)
    app.router.add_post("/api/finish", api_finish)
    SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=WEBHOOK_SECRET).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)
    return app


if __name__ == "__main__":
    web.run_app(make_app(), host="0.0.0.0", port=PORT)
