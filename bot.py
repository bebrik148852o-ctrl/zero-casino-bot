import asyncio
import hashlib
import hmac
import json
import math
import os
import secrets
import sqlite3
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from urllib.parse import parse_qsl, urlsplit

import psycopg2
from aiogram import Bot, Dispatcher, types
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import Command
from aiogram.types import WebAppInfo
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiohttp import web


TOKEN = os.environ.get("TOKEN")
if not TOKEN:
    raise RuntimeError("Установите переменную окружения TOKEN.")

WEB_APP_URL = "https://still-bonus-3827.bebrik1488-52o.workers.dev"
WEB_APP_ORIGIN = f"{urlsplit(WEB_APP_URL).scheme}://{urlsplit(WEB_APP_URL).netloc}"
DATABASE_PATH = os.environ.get("DATABASE_PATH", "casino.db")
DATABASE_URL = os.environ.get("DATABASE_URL")
PROMO_CODE = "ZEROWIN"
PROMO_AMOUNT_CENTS = 10_000
PROMO_CODES = {
    PROMO_CODE: PROMO_AMOUNT_CENTS,
    "ANTON67": 6_700,
    "PANTERA": 77_700,
    "KIZARU1488": 148_800,
}
PROMO_MESSAGES = {
    "ANTON67": "Промокод ANTON67 активирован! +67 ₽",
    "PANTERA": "Промокод PANTERA активирован! +777 ₽",
    "KIZARU1488": "Промокод для своих активирован! +1488 ₽",
}
MAX_AUTH_AGE_SECONDS = 24 * 60 * 60
PLINKO_ROWS = 8
PLINKO_MULTIPLIERS = (
    Decimal("10"),
    Decimal("3"),
    Decimal("1.5"),
    Decimal("0.5"),
    Decimal("0.2"),
    Decimal("0.5"),
    Decimal("1.5"),
    Decimal("3"),
    Decimal("10"),
)
ANTON_SLOT_MIN_STAKE = Decimal("1000000")
ANTON_SLOT_MAX_STAKE = Decimal("100000000")
ANTON_SLOT_MULTIPLIERS = (3, 10, 50)
MEMEDICE_MIN_STAKE = Decimal("1000000")
MEMEDICE_MAX_STAKE = Decimal("100000000")
ADMIN_ID = 8716395955

session = AiohttpSession()
bot = Bot(token=TOKEN, session=session)
dp = Dispatcher()


class DatabaseConnection:
    def __init__(self, connection, is_postgres):
        self.connection = connection
        self.is_postgres = is_postgres

    async def fetchone(self, sqlite_query, postgres_query, parameters=()):
        query = postgres_query if self.is_postgres else sqlite_query
        return await asyncio.to_thread(self._fetchone, query, parameters)

    async def fetchall(self, sqlite_query, postgres_query, parameters=()):
        query = postgres_query if self.is_postgres else sqlite_query
        return await asyncio.to_thread(self._fetchall, query, parameters)

    async def execute(self, sqlite_query, postgres_query, parameters=()):
        query = postgres_query if self.is_postgres else sqlite_query
        return await asyncio.to_thread(self._execute, query, parameters)

    def _fetchone(self, query, parameters):
        cursor = self.connection.cursor()
        try:
            cursor.execute(query, parameters)
            return cursor.fetchone()
        finally:
            cursor.close()

    def _fetchall(self, query, parameters):
        cursor = self.connection.cursor()
        try:
            cursor.execute(query, parameters)
            return cursor.fetchall()
        finally:
            cursor.close()

    def _execute(self, query, parameters):
        cursor = self.connection.cursor()
        try:
            cursor.execute(query, parameters)
            return cursor.rowcount
        finally:
            cursor.close()


@asynccontextmanager
async def database_transaction(immediate=False):
    is_postgres = bool(DATABASE_URL)
    if is_postgres:
        database_url = DATABASE_URL
        if database_url.startswith("postgres://"):
            database_url = "postgresql://" + database_url[len("postgres://"):]
        connection = await asyncio.to_thread(psycopg2.connect, database_url)
    else:
        connection = await asyncio.to_thread(
            sqlite3.connect,
            DATABASE_PATH,
            check_same_thread=False,
        )
        begin_statement = "BEGIN IMMEDIATE" if immediate else "BEGIN"
        await asyncio.to_thread(connection.execute, begin_statement)

    try:
        yield DatabaseConnection(connection, is_postgres)
        await asyncio.to_thread(connection.commit)
    except BaseException:
        await asyncio.to_thread(connection.rollback)
        raise
    finally:
        await asyncio.to_thread(connection.close)


async def initialize_database():
    async with database_transaction() as connection:
        await connection.execute(
            """
            CREATE TABLE IF NOT EXISTS balances (
                user_id BIGINT PRIMARY KEY,
                balance_cents BIGINT NOT NULL DEFAULT 0
                    CHECK (balance_cents >= 0)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS balances (
                user_id BIGINT PRIMARY KEY,
                balance_cents BIGINT NOT NULL DEFAULT 0
                    CHECK (balance_cents >= 0)
            )
            """,
        )
        await connection.execute(
            """
            CREATE TABLE IF NOT EXISTS promo_redemptions (
                user_id BIGINT NOT NULL,
                promo_code TEXT NOT NULL,
                PRIMARY KEY (user_id, promo_code)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS promo_redemptions (
                user_id BIGINT NOT NULL,
                promo_code TEXT NOT NULL,
                PRIMARY KEY (user_id, promo_code)
            )
            """,
        )
        await connection.execute(
            """
            CREATE TABLE IF NOT EXISTS stats (
                user_id BIGINT PRIMARY KEY,
                total_bets BIGINT NOT NULL DEFAULT 0,
                total_won BIGINT NOT NULL DEFAULT 0
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS stats (
                user_id BIGINT PRIMARY KEY,
                total_bets BIGINT NOT NULL DEFAULT 0,
                total_won BIGINT NOT NULL DEFAULT 0
            )
            """,
        )
        await connection.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                referrer_id BIGINT,
                last_daily DATETIME,
                last_spin DATETIME
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                referrer_id BIGINT,
                last_daily TIMESTAMP,
                last_spin TIMESTAMP
            )
            """,
        )
        columns = await connection.fetchall(
            "PRAGMA table_info(users)",
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = current_schema() AND table_name = 'users'
            """,
        )
        has_last_daily = (
            any(column[1] == "last_daily" for column in columns)
            if not connection.is_postgres
            else any(column[0] == "last_daily" for column in columns)
        )
        if not has_last_daily:
            await connection.execute(
                "ALTER TABLE users ADD COLUMN last_daily DATETIME",
                "ALTER TABLE users ADD COLUMN last_daily TIMESTAMP",
            )
        has_last_spin = (
            any(column[1] == "last_spin" for column in columns)
            if not connection.is_postgres
            else any(column[0] == "last_spin" for column in columns)
        )
        if not has_last_spin:
            await connection.execute(
                "ALTER TABLE users ADD COLUMN last_spin DATETIME",
                "ALTER TABLE users ADD COLUMN last_spin TIMESTAMP",
            )
        await connection.execute(
            """
            INSERT INTO users (user_id)
            SELECT user_id FROM balances WHERE 1 = 1
            ON CONFLICT(user_id) DO NOTHING
            """,
            """
            INSERT INTO users (user_id)
            SELECT user_id FROM balances WHERE 1 = 1
            ON CONFLICT(user_id) DO NOTHING
            """,
        )


async def register_user(user_id, referrer_id=None):
    async with database_transaction() as connection:
        if referrer_id is not None:
            inviter = await connection.fetchone(
                "SELECT user_id FROM users WHERE user_id = ?",
                "SELECT user_id FROM users WHERE user_id = %s",
                (referrer_id,),
            )
            if inviter is None:
                referrer_id = None

        inserted = await connection.execute(
            "INSERT INTO users (user_id, referrer_id) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO NOTHING",
            "INSERT INTO users (user_id, referrer_id) VALUES (%s, %s) "
            "ON CONFLICT(user_id) DO NOTHING",
            (user_id, referrer_id),
        )
    return referrer_id if inserted == 1 else None


async def get_balance(user_id):
    async with database_transaction() as connection:
        row = await connection.fetchone(
            "SELECT balance_cents FROM balances WHERE user_id = ?",
            "SELECT balance_cents FROM balances WHERE user_id = %s",
            (user_id,),
        )
    return Decimal(row[0]) / 100 if row else Decimal(0)


async def update_balance_in_transaction(connection, user_id, amount):
    amount_cents = amount * 100
    if amount_cents != amount_cents.to_integral_value():
        raise ValueError("Сумма должна быть указана с точностью до копеек.")
    delta_cents = int(amount_cents)

    if delta_cents > 0:
        await connection.execute(
            "INSERT INTO balances (user_id) VALUES (?) ON CONFLICT(user_id) DO NOTHING",
            "INSERT INTO balances (user_id) VALUES (%s) ON CONFLICT(user_id) DO NOTHING",
            (user_id,),
        )
    updated = await connection.execute(
        """
        UPDATE balances
        SET balance_cents = balance_cents + ?
        WHERE user_id = ? AND balance_cents + ? >= 0
        """,
        """
        UPDATE balances
        SET balance_cents = balance_cents + %s
        WHERE user_id = %s AND balance_cents + %s >= 0
        """,
        (delta_cents, user_id, delta_cents),
    )
    if updated == 0:
        return None
    row = await connection.fetchone(
        "SELECT balance_cents FROM balances WHERE user_id = ?",
        "SELECT balance_cents FROM balances WHERE user_id = %s",
        (user_id,),
    )
    return Decimal(row[0]) / 100


async def update_balance(user_id, amount):
    async with database_transaction() as connection:
        return await update_balance_in_transaction(connection, user_id, amount)


async def claim_daily_bonus(user_id):
    async with database_transaction(immediate=True) as connection:
        await connection.execute(
            "INSERT INTO users (user_id) VALUES (?) ON CONFLICT(user_id) DO NOTHING",
            "INSERT INTO users (user_id) VALUES (%s) ON CONFLICT(user_id) DO NOTHING",
            (user_id,),
        )

        lock_suffix = "" if not connection.is_postgres else " FOR UPDATE"
        row = await connection.fetchone(
            f"SELECT last_daily FROM users WHERE user_id = ?{lock_suffix}",
            f"SELECT last_daily FROM users WHERE user_id = %s{lock_suffix}",
            (user_id,),
        )

        now = datetime.now(timezone.utc)
        last_daily = row[0]
        if isinstance(last_daily, str):
            last_daily = datetime.fromisoformat(last_daily)
        if last_daily is not None:
            if last_daily.tzinfo is None:
                last_daily = last_daily.replace(tzinfo=timezone.utc)
            else:
                last_daily = last_daily.astimezone(timezone.utc)
            elapsed = now - last_daily
            cooldown = timedelta(hours=24)
            if elapsed <= cooldown:
                remaining_seconds = (cooldown - elapsed).total_seconds()
                return None, max(1, math.ceil(remaining_seconds / 3600))

        balance = await update_balance_in_transaction(
            connection,
            user_id,
            Decimal("50"),
        )
        if balance is None:
            raise RuntimeError(f"Не удалось начислить ежедневный бонус пользователю {user_id}.")

        await connection.execute(
            "UPDATE users SET last_daily = ? WHERE user_id = ?",
            "UPDATE users SET last_daily = %s WHERE user_id = %s",
            (now.replace(tzinfo=None), user_id),
        )
    return balance, None


async def claim_wheel_spin(user_id):
    async with database_transaction(immediate=True) as connection:
        await connection.execute(
            "INSERT INTO users (user_id) VALUES (?) ON CONFLICT(user_id) DO NOTHING",
            "INSERT INTO users (user_id) VALUES (%s) ON CONFLICT(user_id) DO NOTHING",
            (user_id,),
        )

        lock_suffix = "" if not connection.is_postgres else " FOR UPDATE"
        row = await connection.fetchone(
            f"SELECT last_spin FROM users WHERE user_id = ?{lock_suffix}",
            f"SELECT last_spin FROM users WHERE user_id = %s{lock_suffix}",
            (user_id,),
        )

        now = datetime.now(timezone.utc)
        last_spin = row[0]
        if isinstance(last_spin, str):
            last_spin = datetime.fromisoformat(last_spin)
        if last_spin is not None:
            if last_spin.tzinfo is None:
                last_spin = last_spin.replace(tzinfo=timezone.utc)
            else:
                last_spin = last_spin.astimezone(timezone.utc)
            cooldown = timedelta(hours=24)
            elapsed = now - last_spin
            if elapsed < cooldown:
                remaining_seconds = (cooldown - elapsed).total_seconds()
                return None, max(1, math.ceil(remaining_seconds / 3600))

        await connection.execute(
            "UPDATE users SET last_spin = ? WHERE user_id = ?",
            "UPDATE users SET last_spin = %s WHERE user_id = %s",
            (now.replace(tzinfo=None), user_id),
        )
    return True, None


async def update_balance_and_stats(user_id, amount, stat_column, stat_increment):
    if stat_column not in {"total_bets", "total_won"}:
        raise ValueError("Недопустимый столбец статистики.")

    amount_cents = amount * 100
    if amount_cents != amount_cents.to_integral_value():
        raise ValueError("Сумма должна быть указана с точностью до копеек.")
    delta_cents = int(amount_cents)

    async with database_transaction() as connection:
        if delta_cents > 0:
            await connection.execute(
                "INSERT INTO balances (user_id) VALUES (?) ON CONFLICT(user_id) DO NOTHING",
                "INSERT INTO balances (user_id) VALUES (%s) ON CONFLICT(user_id) DO NOTHING",
                (user_id,),
            )
        updated = await connection.execute(
            """
            UPDATE balances
            SET balance_cents = balance_cents + ?
            WHERE user_id = ? AND balance_cents + ? >= 0
            """,
            """
            UPDATE balances
            SET balance_cents = balance_cents + %s
            WHERE user_id = %s AND balance_cents + %s >= 0
            """,
            (delta_cents, user_id, delta_cents),
        )
        if updated == 0:
            return None

        await connection.execute(
            "INSERT INTO stats (user_id) VALUES (?) ON CONFLICT(user_id) DO NOTHING",
            "INSERT INTO stats (user_id) VALUES (%s) ON CONFLICT(user_id) DO NOTHING",
            (user_id,),
        )
        await connection.execute(
            f"UPDATE stats SET {stat_column} = {stat_column} + ? WHERE user_id = ?",
            f"UPDATE stats SET {stat_column} = {stat_column} + %s WHERE user_id = %s",
            (stat_increment, user_id),
        )
        row = await connection.fetchone(
            "SELECT balance_cents FROM balances WHERE user_id = ?",
            "SELECT balance_cents FROM balances WHERE user_id = %s",
            (user_id,),
        )
    return Decimal(row[0]) / 100


async def get_stats(user_id):
    async with database_transaction() as connection:
        row = await connection.fetchone(
            "SELECT total_bets, total_won FROM stats WHERE user_id = ?",
            "SELECT total_bets, total_won FROM stats WHERE user_id = %s",
            (user_id,),
        )
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
    return web.json_response({
        "ok": True,
        "balance": float(await get_balance(user_id)),
    })


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

    if await get_balance(user_id) < amount:
        return web.json_response({"ok": False, "error": "Мало денег"})

    balance = await update_balance_and_stats(user_id, -amount, "total_bets", 1)
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
    balance = await update_balance_and_stats(
        user_id,
        amount,
        "total_won",
        amount_cents,
    )
    return web.json_response({"ok": True, "balance": float(balance)})


async def api_plinko(request):
    payload = await read_json(request)
    if payload is None:
        return api_error("Ожидается JSON с полями _auth и amount.")

    user_id = validate_auth(payload.get("_auth", ""))
    if user_id is None:
        return api_error("Недействительная авторизация", status=401)

    amount = amount_from_payload(payload)
    if amount is None:
        return api_error("Некорректная сумма.")
    if amount < Decimal("1") or amount > Decimal("10000"):
        return api_error("Ставка должна быть от 1 до 10 000 ₽.")

    path = [secrets.randbelow(2) for _ in range(PLINKO_ROWS)]
    slot_index = sum(path)
    multiplier = PLINKO_MULTIPLIERS[slot_index]
    payout = (amount * multiplier).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    payout_cents = int(payout * 100)

    async with database_transaction(immediate=True) as connection:
        balance_after_bet = await update_balance_in_transaction(
            connection,
            user_id,
            -amount,
        )
        if balance_after_bet is None:
            return web.json_response({"ok": False, "error": "Мало денег"})

        balance = await update_balance_in_transaction(connection, user_id, payout)
        if balance is None:
            raise RuntimeError(f"Не удалось начислить Plinko-выплату пользователю {user_id}.")

        await connection.execute(
            "INSERT INTO stats (user_id) VALUES (?) ON CONFLICT(user_id) DO NOTHING",
            "INSERT INTO stats (user_id) VALUES (%s) ON CONFLICT(user_id) DO NOTHING",
            (user_id,),
        )
        await connection.execute(
            "UPDATE stats SET total_bets = total_bets + 1, total_won = total_won + ? WHERE user_id = ?",
            "UPDATE stats SET total_bets = total_bets + 1, total_won = total_won + %s WHERE user_id = %s",
            (payout_cents, user_id),
        )

    return web.json_response({
        "ok": True,
        "path": path,
        "slot_index": slot_index,
        "multiplier": float(multiplier),
        "payout": float(payout),
        "balance_after_bet": float(balance_after_bet),
        "balance": float(balance),
    })


async def api_anton_slots(request):
    payload = await read_json(request)
    if payload is None:
        return api_error("Ожидается JSON с полями _auth и amount.")

    user_id = validate_auth(payload.get("_auth", ""))
    if user_id is None:
        return api_error("Недействительная авторизация", status=401)

    amount = amount_from_payload(payload)
    if (
        amount is None
        or amount < ANTON_SLOT_MIN_STAKE
        or amount > ANTON_SLOT_MAX_STAKE
        or amount != amount.to_integral_value()
    ):
        return api_error("Ставка должна быть целым числом от 1 000 000 до 100 000 000 ₽.")

    symbols = [secrets.randbelow(3) for _ in range(9)]
    winning_rows = [
        row for row in range(3)
        if symbols[row * 3] == symbols[row * 3 + 1] == symbols[row * 3 + 2]
    ]
    multiplier = secrets.choice(ANTON_SLOT_MULTIPLIERS) if winning_rows else 0
    payout = amount * multiplier
    payout_cents = int(payout * 100)

    async with database_transaction(immediate=True) as connection:
        balance_after_bet = await update_balance_in_transaction(
            connection,
            user_id,
            -amount,
        )
        if balance_after_bet is None:
            return web.json_response({"ok": False, "error": "Мало денег"})

        balance = balance_after_bet
        if payout > 0:
            balance = await update_balance_in_transaction(connection, user_id, payout)
            if balance is None:
                raise RuntimeError(f"Не удалось начислить ANTON SLOTS-выигрыш пользователю {user_id}.")

        await connection.execute(
            "INSERT INTO stats (user_id) VALUES (?) ON CONFLICT(user_id) DO NOTHING",
            "INSERT INTO stats (user_id) VALUES (%s) ON CONFLICT(user_id) DO NOTHING",
            (user_id,),
        )
        await connection.execute(
            "UPDATE stats SET total_bets = total_bets + 1, total_won = total_won + ? WHERE user_id = ?",
            "UPDATE stats SET total_bets = total_bets + 1, total_won = total_won + %s WHERE user_id = %s",
            (payout_cents, user_id),
        )

    return web.json_response({
        "ok": True,
        "symbols": symbols,
        "winning_rows": winning_rows,
        "multiplier": multiplier,
        "payout": float(payout),
        "balance_after_bet": float(balance_after_bet),
        "balance": float(balance),
    })


async def api_memedice(request):
    payload = await read_json(request)
    if payload is None:
        return api_error("Ожидается JSON с полями _auth и amount.")

    user_id = validate_auth(payload.get("_auth", ""))
    if user_id is None:
        return api_error("Недействительная авторизация", status=401)

    amount = amount_from_payload(payload)
    if (
        amount is None
        or amount < MEMEDICE_MIN_STAKE
        or amount > MEMEDICE_MAX_STAKE
        or amount != amount.to_integral_value()
    ):
        return api_error("Ставка должна быть целым числом от 1 000 000 до 100 000 000 ₽.")

    dice = [secrets.randbelow(6) + 1 for _ in range(2)]
    total = sum(dice)
    multiplier = 10 if total == 12 else 2 if total == 7 else 0
    payout = amount * multiplier
    payout_cents = int(payout * 100)

    async with database_transaction(immediate=True) as connection:
        balance_after_bet = await update_balance_in_transaction(
            connection,
            user_id,
            -amount,
        )
        if balance_after_bet is None:
            return web.json_response({"ok": False, "error": "Мало денег"})

        balance = balance_after_bet
        if payout > 0:
            balance = await update_balance_in_transaction(connection, user_id, payout)
            if balance is None:
                raise RuntimeError(f"Не удалось начислить Мемные кости-выигрыш пользователю {user_id}.")

        await connection.execute(
            "INSERT INTO stats (user_id) VALUES (?) ON CONFLICT(user_id) DO NOTHING",
            "INSERT INTO stats (user_id) VALUES (%s) ON CONFLICT(user_id) DO NOTHING",
            (user_id,),
        )
        await connection.execute(
            "UPDATE stats SET total_bets = total_bets + 1, total_won = total_won + ? WHERE user_id = ?",
            "UPDATE stats SET total_bets = total_bets + 1, total_won = total_won + %s WHERE user_id = %s",
            (payout_cents, user_id),
        )

    return web.json_response({
        "ok": True,
        "dice": dice,
        "total": total,
        "multiplier": multiplier,
        "payout": float(payout),
        "balance_after_bet": float(balance_after_bet),
        "balance": float(balance),
    })


async def api_stats(request):
    payload = await read_json(request)
    if payload is None:
        return api_error("Ожидается JSON с полем _auth.")

    user_id = validate_auth(payload.get("_auth", ""))
    if user_id is None:
        return api_error("Недействительная авторизация", status=401)

    total_bets, total_won_cents = await get_stats(user_id)
    total_won = Decimal(total_won_cents) / 100
    return web.json_response({
        "ok": True,
        "total_bets": total_bets,
        "total_won": float(total_won),
    })


async def api_winners(request):
    async with database_transaction() as connection:
        rows = await connection.fetchall(
            """
            SELECT user_id, total_won
            FROM stats
            WHERE total_won > 0
            ORDER BY total_won DESC, user_id ASC
            LIMIT 3
            """,
            """
            SELECT user_id, total_won
            FROM stats
            WHERE total_won > 0
            ORDER BY total_won DESC, user_id ASC
            LIMIT 3
            """,
        )

    winners = []
    for row in rows:
        user_id, total_won_cents = row[0], row[1]
        winners.append({
            "name": f"Игрок [{user_id}]",
            "amount": float(Decimal(total_won_cents) / 100),
        })
    return web.json_response({"ok": True, "winners": winners})


async def redeem_promo(user_id, promo_code, amount_cents):
    async with database_transaction(immediate=True) as connection:
        inserted = await connection.execute(
            "INSERT INTO promo_redemptions (user_id, promo_code) VALUES (?, ?) "
            "ON CONFLICT(user_id, promo_code) DO NOTHING",
            "INSERT INTO promo_redemptions (user_id, promo_code) VALUES (%s, %s) "
            "ON CONFLICT(user_id, promo_code) DO NOTHING",
            (user_id, promo_code),
        )
        if inserted == 0:
            return None

        balance = await update_balance_in_transaction(
            connection,
            user_id,
            Decimal(amount_cents) / 100,
        )
        if balance is None:
            raise RuntimeError(f"Не удалось начислить промокод пользователю {user_id}.")
    return balance


async def api_promo(request):
    payload = await read_json(request)
    if payload is None:
        return api_error("Ожидается JSON с полями _auth и promo.")

    user_id = validate_auth(payload.get("_auth", ""))
    if user_id is None:
        return api_error("Недействительная авторизация", status=401)

    promo = payload.get("promo")
    if not isinstance(promo, str):
        return api_error("Неверный промокод.")
    promo_code = promo.strip().upper()
    amount_cents = PROMO_CODES.get(promo_code)
    if amount_cents is None:
        return api_error("Неверный промокод.")

    balance = await redeem_promo(user_id, promo_code, amount_cents)
    if balance is None:
        return api_error("Промокод уже активирован.")
    response = {"ok": True, "balance": float(balance)}
    message = PROMO_MESSAGES.get(promo_code)
    if message is not None:
        response["message"] = message
    return web.json_response(response)


async def api_daily(request):
    payload = await read_json(request)
    if payload is None:
        return api_error("Ожидается JSON с полем _auth.")

    user_id = validate_auth(payload.get("_auth", ""))
    if user_id is None:
        return api_error("Недействительная авторизация", status=401)

    balance, hours_remaining = await claim_daily_bonus(user_id)
    if hours_remaining is not None:
        return web.json_response({
            "ok": False,
            "message": f"Бонус будет доступен через {hours_remaining} часов",
        })
    return web.json_response({
        "ok": True,
        "message": "Бонус получен!",
        "balance": float(balance),
    })


async def api_spin(request):
    payload = await read_json(request)
    if payload is None:
        return api_error("Ожидается JSON с полем _auth.")

    user_id = validate_auth(payload.get("_auth", ""))
    if user_id is None:
        return api_error("Недействительная авторизация", status=401)

    _, hours_remaining = await claim_wheel_spin(user_id)
    if hours_remaining is not None:
        return web.json_response({
            "ok": False,
            "error": f"Колесо будет доступно через {hours_remaining} часов",
        })
    return web.json_response({"ok": True, "message": "Можно крутить!"})


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
app.router.add_post("/api/plinko", api_plinko)
app.router.add_post("/api/anton-slots", api_anton_slots)
app.router.add_post("/api/memedice", api_memedice)
app.router.add_post("/api/stats", api_stats)
app.router.add_post("/api/winners", api_winners)
app.router.add_post("/api/promo", api_promo)
app.router.add_post("/api/daily", api_daily)
app.router.add_post("/api/spin", api_spin)


@dp.message(Command("start"))
async def start(message: types.Message):
    if message.from_user is None:
        return

    referrer_id = None
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) == 2 and parts[1].startswith("ref_"):
        referrer_text = parts[1][4:]
        if referrer_text.isascii() and referrer_text.isdigit():
            candidate_id = int(referrer_text)
            if candidate_id > 0 and candidate_id != message.from_user.id:
                referrer_id = candidate_id

    new_referrer_id = await register_user(message.from_user.id, referrer_id)

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

    if new_referrer_id is not None:
        balance = await update_balance(new_referrer_id, Decimal("100"))
        if balance is None:
            raise RuntimeError(f"Не удалось начислить реферальный бонус пользователю {new_referrer_id}.")
        await bot.send_message(
            new_referrer_id,
            "🎉 По твоей ссылке зашел новый игрок! Тебе начислено 100 ₽",
        )


@dp.message(Command("invite"))
async def invite_command(message: types.Message):
    if message.from_user is None:
        return

    await register_user(message.from_user.id)
    invite_link = (
        f"https://t.me/ZeroCasinoX_bot?start=ref_{message.from_user.id}"
    )
    await message.answer(f"Твоя персональная ссылка для приглашения:\n{invite_link}")


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
    balance = await update_balance(user_id, amount)
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
    async with database_transaction() as connection:
        await connection.execute(
            "INSERT INTO balances (user_id) VALUES (?) ON CONFLICT(user_id) DO NOTHING",
            "INSERT INTO balances (user_id) VALUES (%s) ON CONFLICT(user_id) DO NOTHING",
            (user_id,),
        )
        await connection.execute(
            "UPDATE balances SET balance_cents = ? WHERE user_id = ?",
            "UPDATE balances SET balance_cents = %s WHERE user_id = %s",
            (amount_cents, user_id),
        )

    await message.answer(f"Баланс игрока {user_id} установлен: {amount:.2f} ₽.")


async def main():
    await initialize_database()
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
