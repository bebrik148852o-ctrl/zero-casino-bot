import asyncio
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import WebAppInfo
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.client.session.aiohttp import AiohttpSession

# Твой токен от BotFather (можешь оставить так или брать из переменных окружения Render)
TOKEN = os.environ.get("TOKEN", "9221238859:AAENx-jshXV0Ui6bXTvLlbs4gk36c23vta4")

# Ссылка на твое казино
WEB_APP_URL = "https://peaceful-duckanoo-a4cbaf.netlify.app/"

# --- ЗАГЛУШКА ДЛЯ RENDER (чтобы сервис не засыпал) ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is alive")

def run_health_check():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(('0.0.0.0', port), HealthCheckHandler)
    server.serve_forever()

# Запускаем сервер в фоновом потоке
threading.Thread(target=run_health_check, daemon=True).start()
# ----------------------------------------------------

# Создаем сессию (без прокси, так как Render находится за границей)
session = AiohttpSession() 
bot = Bot(token=TOKEN, session=session)
dp = Dispatcher()

@dp.message(Command("start"))
async def start(message: types.Message):
    builder = InlineKeyboardBuilder()
    builder.button(
        text="🎰 Открыть ZeroCasino", 
        web_app=WebAppInfo(url=WEB_APP_URL)
    )
    
    await message.answer(
        f"Привет, {message.from_user.first_name}! 🦆\n"
        "Добро пожаловать в закрытый клуб ZeroCasino.\n"
        "Нажми на кнопку ниже, чтобы открыть приложение.", 
        reply_markup=builder.as_markup()
    )

async def main():
    print("Бот запущен! Проверь кнопку в Телеграме.")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
    
    
