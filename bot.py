import asyncio
import hashlib
import hmac
import logging
import os
import json
import random
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
from urllib.parse import parse_qsl

import motor.motor_asyncio
import redis.asyncio as redis
from aiogram import Bot, Dispatcher, types, F
from aiogram.types import (
    InlineKeyboardMarkup, 
    InlineKeyboardButton, 
    WebAppInfo
)
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder
import aiohttp
from quart import Quart, request, jsonify

# Настройки из переменных окружения
BOT_TOKEN = os.getenv("BOT_TOKEN")
CRYPTO_PAY_TOKEN = os.getenv("CRYPTO_PAY_TOKEN")
WEB_APP_URL = os.getenv("WEB_APP_URL")
ADMIN_IDS = list(map(int, os.getenv("ADMIN_IDS", "").split(",")))
MONGODB_URI = os.getenv("MONGODB_URI")
REDIS_URL = os.getenv("REDIS_URL")
MIN_DEPOSIT = float(os.getenv("MIN_DEPOSIT", "1"))
MIN_WITHDRAW = float(os.getenv("MIN_WITHDRAW", "1"))
MAX_BET = float(os.getenv("MAX_BET", "100"))

# Логирование
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Инициализации
app = Quart(__name__)

# FSM состояния
class Form(StatesGroup):
    deposit = State()
    withdraw_amount = State()

class CasinoBot:
    def __init__(self):
        self.bot = Bot(token=BOT_TOKEN)
        self.redis = redis.from_url(REDIS_URL, decode_responses=True)
        self.storage = RedisStorage(redis=self.redis)
        self.dp = Dispatcher(storage=self.storage)
        self.db: Optional[motor.motor_asyncio.AsyncIOMotorDatabase] = None
        self.session: Optional[aiohttp.ClientSession] = None
        self.app = app
        
    async def init_db(self):
        """Инициализация MongoDB"""
        client = motor.motor_asyncio.AsyncIOMotorClient(MONGODB_URI)
        self.db = client.casino
        
        # Создание индексов
        await self.db.users.create_index("user_id", unique=True)
        await self.db.transactions.create_index("invoice_id", unique=True)
        await self.db.games_history.create_index([("user_id", 1), ("created_at", -1)])
        
        logger.info("MongoDB initialized")

    async def init_session(self):
        """Инициализация aiohttp сессии"""
        self.session = aiohttp.ClientSession()

    def validate_telegram_data(self, init_data: str) -> bool:
        """Безопасная валидация данных от Telegram"""
        try:
            # Парсим query string в словарь
            data = dict(parse_qsl(init_data))
            received_hash = data.pop('hash', '')
            
            # Сортируем ключи и формируем строку
            data_check_string = '\n'.join(sorted([f"{k}={v}" for k, v in data.items()]))
            
            secret_key = hmac.new(
                b"WebAppData", 
                BOT_TOKEN.encode(), 
                hashlib.sha256
            ).digest()
            
            calculated_hash = hmac.new(
                secret_key, 
                data_check_string.encode(), 
                hashlib.sha256
            ).hexdigest()
            
            return calculated_hash == received_hash
        except Exception as e:
            logger.error(f"Validation error: {e}")
            return False

    def extract_user_id(self, init_data: str) -> Optional[int]:
        """Безопасное извлечение user_id"""
        try:
            data = dict(parse_qsl(init_data))
            user_data = json.loads(data.get('user', '{}'))
            return user_data.get('id')
        except Exception as e:
            logger.error(f"Extract user_id error: {e}")
        return None

    # Web_app API endpoints
    @app.route('/api/webapp/init', methods=['POST'])
    async def webapp_init(self):
        """Инициализация мини-приложения"""
        data = await request.get_json()
        init_data = data.get('initData')
        
        if not init_data or not self.validate_telegram_data(init_data):
            return jsonify({'error': 'Invalid authentication'}), 403
        
        user_id = self.extract_user_id(init_data)
        if not user_id:
            return jsonify({'error': 'Invalid user'}), 400
        
        # Обновляем активность
        await self.db.users.update_one(
            {"user_id": user_id},
            {"$set": {"last_active": datetime.utcnow()}}
        )
        
        # Получаем данные пользователя
        user_data = await self.db.users.find_one({"user_id": user_id})
        if not user_data:
            user_data = {
                "user_id": user_id,
                "balance": 0.0,
                "username": f"user_{user_id}",
                "created_at": datetime.utcnow(),
                "last_active": datetime.utcnow()
            }
            await self.db.users.insert_one(user_data)
        
        # Статистика
        stats_pipeline = [
            {"$match": {"user_id": user_id, "created_at": {"$gt": datetime.utcnow() - timedelta(days=7)}}},
            {"$group": {
                "_id": None,
                "games": {"$sum": 1},
                "total_bet": {"$sum": "$bet_amount"},
                "total_win": {"$sum": "$win_amount"}
            }}
        ]
        
        stats_result = await self.db.games_history.aggregate(stats_pipeline).to_list(1)
        stats = stats_result[0] if stats_result else {}
        
        return jsonify({
            'success': True,
            'balance': user_data['balance'],
            'username': user_data['username'],
            'stats': {
                'games': stats.get('games', 0),
                'total_bet': stats.get('total_bet', 0.0),
                'total_win': stats.get('total_win', 0.0)
            }
        })

    @app.route('/api/game/play', methods=['POST'])
    async def game_play(self):
        """Обработка игры"""
        data = await request.get_json()
        init_data = data.get('initData')
        game_type = data.get('gameType', 'slots')
        bet_amount = float(data.get('betAmount', 0))
        
        if not init_data or not self.validate_telegram_data(init_data):
            return jsonify({'error': 'Invalid authentication'}), 403
        
        user_id = self.extract_user_id(init_data)
        if not user_id:
            return jsonify({'error': 'Invalid user'}), 400
        
        if not (0.1 <= bet_amount <= MAX_BET):
            return jsonify({'error': 'Invalid bet amount'}), 400
        
        # Проверяем баланс и списываем ставку
        user = await self.db.users.find_one({"user_id": user_id})
        if not user or user['balance'] < bet_amount:
            return jsonify({'error': 'Insufficient balance'}), 400
        
        # Списываем ставку
        await self.db.users.update_one(
            {"user_id": user_id},
            {"$inc": {"balance": -bet_amount}}
        )
        
        # Игра
        result = await self.process_game(game_type, bet_amount)
        
        # Начисляем выигрыш
        await self.db.users.update_one(
            {"user_id": user_id},
            {"$inc": {"balance": result['win_amount']}}
        )
        
        # Логируем игру
        await self.db.games_history.insert_one({
            "user_id": user_id,
            "game_type": game_type,
            "bet_amount": bet_amount,
            "win_amount": result['win_amount'],
            "result": result,
            "created_at": datetime.utcnow()
        })
        
        new_balance = await self.db.users.find_one({"user_id": user_id})
        
        return jsonify({
            'success': True,
            'result': result,
            'new_balance': new_balance['balance']
        })

    async def process_game(self, game_type: str, bet_amount: float) -> Dict[str, Any]:
        """Обработка логики игры"""
        if game_type == 'slots':
            return await self.slots_game(bet_amount, random)
        elif game_type == 'roulette':
            return await self.roulette_game(bet_amount, random)
        else:
            return await self.slots_game(bet_amount, random)

    async def slots_game(self, bet_amount: float, random) -> Dict[str, Any]:
        """Слоты с улучшенной математикой"""
        symbols = ['🍒', '🍋', '🍉', '⭐', '💎', '7️⃣']
        reels = [symbols[i] for i in [random.randint(0, len(symbols)-1) for _ in range(3)]]
        
        # Расчет выигрыша
        multiplier = 0
        if reels[0] == reels[1] == reels[2]:
            if reels[0] == '7️⃣':
                multiplier = 10  # Джекпот
            elif reels[0] == '💎':
                multiplier = 5
            else:
                multiplier = 3
        elif reels[0] == reels[1] or reels[1] == reels[2]:
            multiplier = 1.5
        
        win_amount = bet_amount * multiplier
        
        return {
            'reels': reels,
            'multiplier': multiplier,
            'win_amount': win_amount,
            'is_win': win_amount > bet_amount
        }

    async def roulette_game(self, bet_amount: float, random) -> Dict[str, Any]:
        """Рулетка"""
        number = random.randint(0, 36)
        color = 'red' if number in [1,3,5,7,9,12,14,16,18,19,21,23,25,27,30,32,34,36] else 'black'
        if number == 0: color = 'green'
        
        is_win = random.random() < 0.48
        multiplier = 2 if is_win else 0
        
        return {
            'number': number,
            'color': color,
            'multiplier': multiplier,
            'win_amount': bet_amount * multiplier,
            'is_win': is_win
        }

    # Telegram bot handlers
    def setup_handlers(self):
        """Настройка обработчиков"""
        
        @self.dp.message(CommandStart())
        async def start(message: types.Message):
            # Создание/обновление пользователя
            await self.db.users.update_one(
                {"user_id": message.from_user.id},
                {
                    "$setOnInsert": {
                        "user_id": message.from_user.id,
                        "balance": 0.0,
                        "created_at": datetime.utcnow(),
                    },
                    "$set": {
                        "username": message.from_user.username,
                        "last_active": datetime.utcnow()
                    }
                },
                upsert=True
            )
            
            welcome_text = (
                "🎰 <b>Добро пожаловать в Premium Casino!</b>\n\n"
                "🚀 Быстрые выплаты через чеки\n"
                "💎 Моментальные депозиты\n"
                "🎁 Минимальный вывод: 1 USDT\n"
                "📊 Прозрачная статистика\n\n"
                "<i>Нажмите «Играть» для запуска мини-приложения</i>"
            )
            
            await message.answer(
                welcome_text,
                reply_markup=self.main_menu(),
                parse_mode="HTML"
            )

        @self.dp.callback_query(F.data == "main")
        async def back_main(callback: CallbackQuery):
            await callback.message.edit_text(
                "Главное меню",
                reply_markup=self.main_menu()
            )

        @self.dp.callback_query(F.data == "profile")
        async def profile(callback: CallbackQuery):
            user_data = await self.db.users.find_one({"user_id": callback.from_user.id})
            
            # Статистика
            stats_pipeline = [
                {"$match": {"user_id": callback.from_user.id}},
                {"$group": {
                    "_id": None,
                    "games": {"$sum": 1},
                    "total_bet": {"$sum": "$bet_amount"},
                    "avg_rtp": {"$avg": {"$divide": ["$win_amount", "$bet_amount"]}}
                }}
            ]
            
            stats_result = await self.db.games_history.aggregate(stats_pipeline).to_list(1)
            stats = stats_result[0] if stats_result else {}
            
            text = (
                f"👤 <b>Ваш профиль</b>\n\n"
                f"💰 Баланс: <code>{user_data['balance']:.2f} USDT</code>\n"
                f"📅 Дата регистрации: {user_data['created_at'].strftime('%d.%m.%Y')}\n\n"
                f"🎮 Сыграно игр: {stats.get('games', 0)}\n"
                f"💸 Общая ставка: {stats.get('total_bet', 0.0):.2f} USDT\n"
                f"📈 RTP: {stats.get('avg_rtp', 0.0) * 100:.2f}%"
            )
            
            await callback.message.edit_text(
                text,
                reply_markup=self.profile_menu(),
                parse_mode="HTML"
            )

        @self.dp.callback_query(F.data == "deposit")
        async def deposit(callback: CallbackQuery, state: FSMContext):
            await callback.message.edit_text(
                "💳 <b>Пополнение баланса</b>\n\n"
                f"Минимальная сумма: <code>{MIN_DEPOSIT} USDT</code>\n"
                "Введите сумму для пополнения:",
                parse_mode="HTML"
            )
            await state.set_state(Form.deposit)

        @self.dp.message(Form.deposit)
        async def process_deposit(message: types.Message, state: FSMContext):
            try:
                amount = float(message.text)
                if amount < MIN_DEPOSIT:
                    await message.answer(
                        f"⚠️ Минимальная сумма: {MIN_DEPOSIT} USDT",
                        reply_markup=self.back_keyboard()
                    )
                    await state.clear()
                    return
                
                # Создание инвойса
                async with self.session.post(
                    "https://pay.crypt.bot/api/createInvoice",
                    json={
                        "asset": "USDT",
                        "amount": str(amount),
                        "description": f"Deposit user_{message.from_user.id}",
                        "paid_btn_name": "openBot",
                        "paid_btn_url": f"https://t.me/{(await self.bot.get_me()).username}"
                    },
                    headers={"Crypto-Pay-Api-Token": CRYPTO_PAY_TOKEN}
                ) as resp:
                    result = await resp.json()
                
                if result.get("ok"):
                    invoice = result["result"]
                    
                    # Сохраняем транзакцию
                    await self.db.transactions.insert_one({
                        "user_id": message.from_user.id,
                        "type": "deposit",
                        "amount": amount,
                        "status": "pending",
                        "invoice_id": invoice["invoice_id"],
                        "created_at": datetime.utcnow()
                    })
                    
                    keyboard = InlineKeyboardBuilder()
                    keyboard.button(text="💳 Оплатить", url=invoice["pay_url"])
                    keyboard.button(
                        text="✅ Проверить",
                        callback_data=f"check_dep_{invoice['invoice_id']}"
                    )
                    keyboard.button(text="🔙 Назад", callback_data="profile")
                    keyboard.adjust(1, 2)
                    
                    await message.answer(
                        f"📨 <b>Инвойс создан</b>\n"
                        f"Сумма: <code>{amount} USDT</code>\n\n"
                        f"ID: <code>{invoice['invoice_id']}</code>",
                        reply_markup=keyboard.as_markup(),
                        parse_mode="HTML"
                    )
                else:
                    await message.answer(
                        "❌ Ошибка создания инвойса. Попробуйте позже.",
                        reply_markup=self.back_keyboard()
                    )
            except ValueError:
                await message.answer(
                    "⚠️ Введите корректное число",
                    reply_markup=self.back_keyboard()
                )
            finally:
                await state.clear()

        @self.dp.callback_query(F.data.startswith("check_dep_"))
        async def check_deposit(callback: CallbackQuery):
            invoice_id = callback.data.split("_")[2]
            
            async with self.session.get(
                f"https://pay.crypt.bot/api/getInvoices?invoice_ids={invoice_id}",
                headers={"Crypto-Pay-Api-Token": CRYPTO_PAY_TOKEN}
            ) as resp:
                result = await resp.json()
            
            if result.get("ok"):
                invoice = result["result"]["items"][0]
                
                if invoice["status"] == "paid":
                    amount = float(invoice["amount"])
                    
                    # Проверяем, не обработан ли уже
                    exists = await self.db.transactions.find_one({
                        "invoice_id": invoice_id,
                        "status": "completed"
                    })
                    
                    if not exists:
                        # Обновляем баланс
                        await self.db.users.update_one(
                            {"user_id": callback.from_user.id},
                            {"$inc": {"balance": amount}}
                        )
                        
                        # Обновляем статус транзакции
                        await self.db.transactions.update_one(
                            {"invoice_id": invoice_id},
                            {"$set": {"status": "completed"}}
                        )
                        
                        # Уведомление админу
                        for admin_id in ADMIN_IDS:
                            try:
                                await self.bot.send_message(
                                    admin_id,
                                    f"💰 <b>Новый депозит</b>\n\n"
                                    f"User: @{callback.from_user.username}\n"
                                    f"Amount: <code>{amount} USDT</code>",
                                    parse_mode="HTML"
                                )
                            except:
                                pass
                    
                    await callback.message.edit_text(
                        f"✅ <b>Пополнение успешно!</b>\n\n"
                        f"Баланс пополнен на <code>{amount} USDT</code>",
                        parse_mode="HTML",
                        reply_markup=self.back_keyboard()
                    )
                else:
                    await callback.answer("⏳ Ожидание оплаты...", show_alert=True)
            else:
                await callback.answer("❌ Ошибка проверки", show_alert=True)

        @self.dp.callback_query(F.data == "withdraw")
        async def withdraw(callback: CallbackQuery, state: FSMContext):
            user_data = await self.db.users.find_one({"user_id": callback.from_user.id})
            
            await callback.message.edit_text(
                f"📤 <b>Вывод средств</b>\n\n"
                f"Доступный баланс: <code>{user_data['balance']:.2f} USDT</code>\n"
                f"Минимальная сумма: <code>{MIN_WITHDRAW} USDT</code>\n\n"
                "⚠️ <i>Вывод будет доступен в виде чека Crypto Pay</i>\n\n"
                "Введите сумму для вывода:",
                parse_mode="HTML"
            )
            await state.set_state(Form.withdraw_amount)

        @self.dp.message(Form.withdraw_amount)
        async def process_withdraw(message: types.Message, state: FSMContext):
            try:
                amount = float(message.text)
                user_id = message.from_user.id
                
                # Получаем актуальный баланс
                user_data = await self.db.users.find_one({"user_id": user_id})
                
                if amount < MIN_WITHDRAW:
                    await message.answer(
                        f"⚠️ Минимальная сумма: {MIN_WITHDRAW} USDT",
                        reply_markup=self.back_keyboard()
                    )
                    await state.clear()
                    return
                
                if amount > user_data['balance']:
                    await message.answer(
                        "⚠️ Недостаточно средств",
                        reply_markup=self.back_keyboard()
                    )
                    await state.clear()
                    return
                
                # Создание чека через Crypto Pay
                async with self.session.post(
                    "https://pay.crypt.bot/api/createCheck",
                    json={
                        "asset": "USDT",
                        "amount": str(amount),
                    },
                    headers={"Crypto-Pay-Api-Token": CRYPTO_PAY_TOKEN}
                ) as resp:
                    result = await resp.json()
                
                if result.get("ok"):
                    check = result["result"]
                    
                    # Снимаем со счета и логируем
                    await self.db.users.update_one(
                        {"user_id": user_id},
                        {"$inc": {"balance": -amount}}
                    )
                    
                    await self.db.transactions.insert_one({
                        "user_id": user_id,
                        "type": "withdraw",
                        "amount": amount,
                        "status": "completed",
                        "check_url": check['bot_check_url'],
                        "created_at": datetime.utcnow()
                    })
                    
                    await message.answer(
                        f"✅ <b>Вывод успешен!</b>\n\n"
                        f"Сумма: <code>{amount} USDT</code>\n"
                        f"Чек: {check['bot_check_url']}\n\n"
                        f"⚠️ <i>Активируйте чек в течение 24 часов</i>\n\n"
                        f"Код чека: <code>{check['check_id']}</code>",
                        parse_mode="HTML",
                        reply_markup=self.back_keyboard()
                    )
                    
                    # Уведомление админу
                    for admin_id in ADMIN_IDS:
                        try:
                            await self.bot.send_message(
                                admin_id,
                                f"📤 <b>Новый вывод</b>\n\n"
                                f"User: @{message.from_user.username}\n"
                                f"Amount: <code>{amount} USDT</code>\n"
                                f"Check ID: <code>{check['check_id']}</code>",
                                parse_mode="HTML"
                            )
                        except:
                            pass
                else:
                    await message.answer(
                        "❌ Ошибка создания чека. Попробуйте позже.",
                        reply_markup=self.back_keyboard()
                    )
            except ValueError:
                await message.answer(
                    "⚠️ Введите корректное число",
                    reply_markup=self.back_keyboard()
                )
            finally:
                await state.clear()

        @self.dp.message(Command("stats"))
        async def stats(message: types.Message):
            if message.from_user.id not in ADMIN_IDS:
                return
            
            # Агрегация статистики за 24 часа
            pipeline = [
                {"$match": {"created_at": {"$gte": datetime.utcnow() - timedelta(hours=24)}}},
                {"$group": {
                    "_id": None,
                    "users": {"$addToSet": "$user_id"},
                    "deposits": {"$sum": {"$cond": [{"$eq": ["$type", "deposit"]}, "$amount", 0]}},
                    "withdraws": {"$sum": {"$cond": [{"$eq": ["$type", "withdraw"]}, "$amount", 0]}},
                    "transactions": {"$sum": 1}
                }}
            ]
            
            result = await self.db.transactions.aggregate(pipeline).to_list(1)
            stats = result[0] if result else {}
            
            text = (
                f"📊 <b>Статистика за 24 часа</b>\n\n"
                f"👥 Пользователей: {len(stats.get('users', []))}\n"
                f"💰 Депозитов: {stats.get('deposits', 0.0):.2f} USDT\n"
                f"📤 Выводов: {stats.get('withdraws', 0.0):.2f} USDT\n"
                f"🔄 Транзакций: {stats.get('transactions', 0)}"
            )
            
            await message.answer(text, parse_mode="HTML")

    def main_menu(self):
        keyboard = InlineKeyboardBuilder()
        keyboard.button(text="🎰 Играть", web_app=WebAppInfo(url=WEB_APP_URL))
        keyboard.button(text="💼 Профиль", callback_data="profile")
        keyboard.button(text="📞 Поддержка", url=f"https://t.me/your_support")
        keyboard.adjust(1, 2)
        return keyboard.as_markup()

    def profile_menu(self):
        keyboard = InlineKeyboardBuilder()
        keyboard.button(text="📥 Пополнить", callback_data="deposit")
        keyboard.button(text="📤 Вывести", callback_data="withdraw")
        keyboard.button(text="📊 История", callback_data="history")
        keyboard.button(text="🔙 Главное меню", callback_data="main")
        keyboard.adjust(2, 1)
        return keyboard.as_markup()

    def back_keyboard(self):
        keyboard = InlineKeyboardBuilder()
        keyboard.button(text="🔙 Назад", callback_data="profile")
        return keyboard.as_markup()

    async def start(self):
        """Запуск бота и веб-сервера"""
        await self.init_db()
        await self.init_session()
        self.setup_handlers()
        
        # Запуск веб-сервера
        port = int(os.getenv("PORT", 5000))
        
        from hypercorn.asyncio import serve
        from hypercorn.config import Config
        
        config = Config()
        config.bind = [f"0.0.0.0:{port}"]
        
        await asyncio.gather(
            serve(self.app, config),
            self.dp.start_polling(self.bot)
        )

if __name__ == '__main__':
    import uvloop
    uvloop.install()
    
    bot_app = CasinoBot()
    asyncio.run(bot_app.start())
