import asyncio
import hashlib
import hmac
import json
import os
import sqlite3
import time
from contextlib import closing
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qsl, urlsplit

from aiogram import Bot, Dispatcher, types
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import Command
from aiogram.types import WebAppInfo
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiohttp import web


TOKEN = os.environ.get("TOKEN")
if not TOKEN:
    raise RuntimeError("Установите переменную окружения TOKEN.")

WEB_APP_URL = "https://peaceful-duckanoo-a4cbaf.netlify.app/"
WEB_APP_ORIGIN = f"{urlsplit(WEB_APP_URL).scheme}://{urlsplit(WEB_APP_URL).netloc}"
DATABASE_PATH = os.environ.get("DATABASE_PATH", "balances.sqlite3")
PROMO_CODE = "ZEROWIN"
PROMO_AMOUNT_CENTS = 10_000
MAX_AUTH_AGE_SECONDS = 24 * 60 * 60
ADMIN_ID = 8716395955

session = AiohttpSession()
bot = Bot(token=TOKEN, session=session)
dp = Dispatcher()


def initialize_database():
    with closing(sqlite3.connect(DATABASE_PATH)) as connection:
        with connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS balances (
                    user_id INTEGER PRIMARY KEY,
                    balance_cents INTEGER NOT NULL DEFAULT 0
                        CHECK (balance_cents >= 0)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS promo_redemptions (
                    user_id INTEGER NOT NULL,
                    promo_code TEXT NOT NULL,
                    PRIMARY KEY (user_id, promo_code)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS stats (
                    user_id INTEGER PRIMARY KEY,
                    total_bets INTEGER NOT NULL DEFAULT 0,
                    total_won INTEGER NOT NULL DEFAULT 0
                )
                """
            )


def get_balance(user_id):
    with closing(sqlite3.connect(DATABASE_PATH)) as connection:
        row = connection.execute(
            "SELECT balance_cents FROM balances WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    return Decimal(row[0]) / 100 if row else Decimal(0)


def update_balance(user_id, amount):
    amount_cents = amount * 100
    if amount_cents != amount_cents.to_integral_value():
        raise ValueError("Сумма должна быть указана с точностью до копеек.")
    delta_cents = int(amount_cents)

    with closing(sqlite3.connect(DATABASE_PATH)) as connection:
        with connection:
            if delta_cents > 0:
                connection.execute(
                    "INSERT OR IGNORE INTO balances (user_id) VALUES (?)",
                    (user_id,),
                )
            result = connection.execute(
                """
                UPDATE balances
                SET balance_cents = balance_cents + ?
                WHERE user_id = ? AND balance_cents + ? >= 0
                """,
                (delta_cents, user_id, delta_cents),
            )
            if result.rowcount == 0:
                return None
            row = connection.execute(
                "SELECT balance_cents FROM balances WHERE user_id = ?",
                (user_id,),
            ).fetchone()
    return Decimal(row[0]) / 100


def update_balance_and_stats(user_id, amount, stat_column, stat_increment):
    if stat_column not in {"total_bets", "total_won"}:
        raise ValueError("Недопустимый столбец статистики.")

    amount_cents = amount * 100
    if amount_cents != amount_cents.to_integral_value():
        raise ValueError("Сумма должна быть указана с точностью до копеек.")
    delta_cents = int(amount_cents)

    with closing(sqlite3.connect(DATABASE_PATH)) as connection:
        with connection:
            if delta_cents > 0:
                connection.execute(
                    "INSERT OR IGNORE INTO balances (user_id) VALUES (?)",
                    (user_id,),
                )
            result = connection.execute(
                """
                UPDATE balances
                SET balance_cents = balance_cents + ?
                WHERE user_id = ? AND balance_cents + ? >= 0
                """,
                (delta_cents, user_id, delta_cents),
            )
            if result.rowcount == 0:
                return None

            connection.execute(
                "INSERT OR IGNORE INTO stats (user_id) VALUES (?)",
                (user_id,),
            )
            connection.execute(
                f"UPDATE stats SET {stat_column} = {stat_column} + ? WHERE user_id = ?",
                (stat_increment, user_id),
            )
            row = connection.execute(
                "SELECT balance_cents FROM balances WHERE user_id = ?",
                (user_id,),
            ).fetchone()
    return Decimal(row[0]) / 100


def get_stats(user_id):
    with closing(sqlite3.connect(DATABASE_PATH)) as connection:
        row = connection.execute(
            "SELECT total_bets, total_won FROM stats WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    return row if row else (0, 0)


def validate_auth(auth_data):
    if not isinstance(auth_data, str) or not auth_data:
        return None

    try:
        pairs = parse_qsl(auth_data, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        return None

    if len(dict(pairs)) != len(pairs):
        return None

    values = dict(pairs)
    received_hash = values.pop("hash", None)
    if not received_hash:
        return None

    data_check_string = "\n".join(
        f"{key}={value}" for key, value in sorted(values.items())
    )
    secret_key = hmac.new(
        b"WebAppData",
        TOKEN.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    expected_hash = hmac.new(
        secret_key,
        data_check_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(received_hash, expected_hash):
        return None

    try:
        auth_date = int(values["auth_date"])
        user = json.loads(values["user"])
    except (KeyError, ValueError, json.JSONDecodeError):
        return None

    now = int(time.time())
    if auth_date > now + 60 or now - auth_date > MAX_AUTH_AGE_SECONDS:
        return None
    if not isinstance(user, dict):
        return None

    user_id = user.get("id")
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
        return None
    return user_id


def amount_from_payload(payload):
    value = payload.get("amount")
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        return None
    if not amount.is_finite() or amount <= 0:
        return None
    if amount * 100 != (amount * 100).to_integral_value():
        return None
    return amount


async def read_json(request):
    try:
        payload = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def api_error(message, status=400):
    return web.json_response({"ok": False, "error": message}, status=status)


async def api_user(request):
    if request.method == "POST":
        payload = await read_json(request)
        if payload is None:
            return api_error("Ожидается JSON с полем _auth.")
        auth_data = payload.get("_auth", "")
    else:
        auth_data = request.query.get("_auth", "")

    user_id = validate_auth(auth_data)
    if user_id is None:
        return api_error("Недействительная авторизация", status=401)
    return web.json_response({"ok": True, "balance": float(get_balance(user_id))})


async def api_bet(request):
    payload = await read_json(request)
    if payload is None:
        return api_error("Ожидается JSON с полями _auth и amount.")

    user_id = validate_auth(payload.get("_auth", ""))
    if user_id is None:
        return api_error("Недействительная авторизация", status=401)

    amount = amount_from_payload(payload)
    if amount is None:
        return api_error("Некорректная сумма.")

    if get_balance(user_id) < amount:
        return web.json_response({"ok": False, "error": "Мало денег"})

    balance = update_balance_and_stats(user_id, -amount, "total_bets", 1)
    if balance is None:
        return web.json_response({"ok": False, "error": "Мало денег"})
    return web.json_response({"ok": True, "balance": float(balance)})


async def api_win(request):
    payload = await read_json(request)
    if payload is None:
        return api_error("Ожидается JSON с полями _auth и amount.")

    user_id = validate_auth(payload.get("_auth", ""))
    if user_id is None:
        return api_error("Недействительная авторизация", status=401)

    amount = amount_from_payload(payload)
    if amount is None:
        return api_error("Некорректная сумма.")

    amount_cents = int(amount * 100)
    balance = update_balance_and_stats(user_id, amount, "total_won", amount_cents)
    return web.json_response({"ok": True, "balance": float(balance)})


async def api_stats(request):
    payload = await read_json(request)
    if payload is None:
        return api_error("Ожидается JSON с полем _auth.")

    user_id = validate_auth(payload.get("_auth", ""))
    if user_id is None:
        return api_error("Недействительная авторизация", status=401)

    total_bets, total_won_cents = get_stats(user_id)
    total_won = Decimal(total_won_cents) / 100
    return web.json_response({
        "ok": True,
        "total_bets": total_bets,
        "total_won": float(total_won),
    })


async def api_winners(request):
    with closing(sqlite3.connect(DATABASE_PATH)) as connection:
        rows = connection.execute(
            """
            SELECT user_id, total_won
            FROM stats
            WHERE total_won > 0
            ORDER BY total_won DESC, user_id ASC
            LIMIT 3
            """
        ).fetchall()

    winners = [
        {
            "name": f"Игрок [{user_id}]",
            "amount": float(Decimal(total_won_cents) / 100),
        }
        for user_id, total_won_cents in rows
    ]
    return web.json_response({"ok": True, "winners": winners})


def redeem_promo(user_id):
    with closing(sqlite3.connect(DATABASE_PATH)) as connection:
        with connection:
            try:
                connection.execute(
                    "INSERT INTO promo_redemptions (user_id, promo_code) VALUES (?, ?)",
                    (user_id, PROMO_CODE),
                )
            except sqlite3.IntegrityError:
                return None

            connection.execute(
                """
                INSERT INTO balances (user_id, balance_cents)
                VALUES (?, ?)
                ON CONFLICT(user_id)
                DO UPDATE SET balance_cents = balance_cents + excluded.balance_cents
                """,
                (user_id, PROMO_AMOUNT_CENTS),
            )
            row = connection.execute(
                "SELECT balance_cents FROM balances WHERE user_id = ?",
                (user_id,),
            ).fetchone()
    return Decimal(row[0]) / 100


async def api_promo(request):
    payload = await read_json(request)
    if payload is None:
        return api_error("Ожидается JSON с полями _auth и promo.")

    user_id = validate_auth(payload.get("_auth", ""))
    if user_id is None:
        return api_error("Недействительная авторизация", status=401)

    promo = payload.get("promo")
    if not isinstance(promo, str) or promo.strip().upper() != PROMO_CODE:
        return api_error("Неверный промокод.")

    balance = redeem_promo(user_id)
    if balance is None:
        return api_error("Промокод уже активирован.")
    return web.json_response({"ok": True, "balance": float(balance)})


async def health_check(request):
    return web.Response(text="Bot is alive")


@web.middleware
async def cors_middleware(request, handler):
    if request.method == "OPTIONS":
        response = web.Response(status=204)
    else:
        response = await handler(request)

    if request.headers.get("Origin") == WEB_APP_ORIGIN:
        response.headers["Access-Control-Allow-Origin"] = WEB_APP_ORIGIN
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
        response.headers["Vary"] = "Origin"
    return response


app = web.Application(middlewares=[cors_middleware])
app.router.add_get("/", health_check)
app.router.add_get("/health", health_check)
app.router.add_get("/api/user", api_user)
app.router.add_post("/api/user", api_user)
app.router.add_post("/api/bet", api_bet)
app.router.add_post("/api/win", api_win)
app.router.add_post("/api/stats", api_stats)
app.router.add_post("/api/winners", api_winners)
app.router.add_post("/api/promo", api_promo)


@dp.message(Command("start"))
async def start(message: types.Message):
    builder = InlineKeyboardBuilder()
    builder.button(
        text="🎰 Открыть ZeroCasino",
        web_app=WebAppInfo(url=WEB_APP_URL),
    )
    await message.answer(
        f"Привет, {message.from_user.first_name}! 🦆\n"
        "Добро пожаловать в закрытый клуб ZeroCasino.\n"
        "Нажми на кнопку ниже, чтобы открыть приложение.",
        reply_markup=builder.as_markup(),
    )


def parse_admin_command_amount(message: types.Message):
    if not message.text:
        return None

    parts = message.text.split()
    if len(parts) != 3:
        return None

    try:
        user_id = int(parts[1])
        amount = Decimal(parts[2])
    except (ValueError, InvalidOperation):
        return None

    if user_id <= 0 or not amount.is_finite():
        return None
    if amount * 100 != (amount * 100).to_integral_value():
        return None
    return user_id, amount


@dp.message(Command("give"))
async def give_command(message: types.Message):
    if message.from_user is None or message.from_user.id != ADMIN_ID:
        return

    parsed = parse_admin_command_amount(message)
    if parsed is None or parsed[1] <= 0:
        await message.answer("Формат: /give ID_игрока СУММА (сумма должна быть положительной).")
        return

    user_id, amount = parsed
    balance = update_balance(user_id, amount)
    if balance is None:
        await message.answer("Не удалось начислить сумму игроку.")
        return
    await message.answer(
        f"Игроку {user_id} начислено {amount:.2f} ₽. Баланс: {balance:.2f} ₽."
    )


@dp.message(Command("setbal"))
async def set_balance_command(message: types.Message):
    if message.from_user is None or message.from_user.id != ADMIN_ID:
        return

    parsed = parse_admin_command_amount(message)
    if parsed is None or parsed[1] < 0:
        await message.answer("Формат: /setbal ID_игрока СУММА (сумма не может быть отрицательной).")
        return

    user_id, amount = parsed
    amount_cents = int(amount * 100)
    with closing(sqlite3.connect(DATABASE_PATH)) as connection:
        with connection:
            connection.execute(
                "INSERT OR IGNORE INTO balances (user_id) VALUES (?)",
                (user_id,),
            )
            connection.execute(
                "UPDATE balances SET balance_cents = ? WHERE user_id = ?",
                (amount_cents, user_id),
            )

    await message.answer(f"Баланс игрока {user_id} установлен: {amount:.2f} ₽.")


async def main():
    initialize_database()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(os.environ.get("PORT", 8080)))
    await site.start()
    print("Бот и API запущены.")
    try:
        await dp.start_polling(bot)
    finally:
        await runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
