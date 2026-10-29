import asyncio
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import WebAppInfo
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.client.session.aiohttp import AiohttpSession

# Твой токен от BotFather
TOKEN = "9221238859:AAENx-jshXV0Ui6bXTvLlbs4gk36c23vta4"

# Ссылка на твое казино
WEB_APP_URL = "https://peaceful-duckanoo-a4cbaf.netlify.app/"

# Настройка локального прокси (который сейчас работает в соседнем терминале)
session = AiohttpSession(proxy="http://127.0.0.1:8080")

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
    