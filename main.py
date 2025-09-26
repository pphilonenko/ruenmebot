import logging
import random
import json
import asyncio
from telegram import ForceReply, Update, BotCommand, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
from telegram.constants import ChatAction
from database import init_db, get_response, add_response
from my_info import SYSTEM_PROMPT, ERROR_RESPONSES, REACTION_RESPONSES, AI_COMMAND_RESPONSE
from config import TELEGRAM_TOKEN, FORWARD_CHAT_IDS, WEBHOOK_URL, RESPONSE_DELAY_SECONDS, TYPING_DURATION_SECONDS, ADMIN_CHAT_ID, START_BOT_MESSAGE, NON_ADMIN_MESSAGE, IP_PHONE
from grok_api import query_grok
from utils import notify_development
from aiohttp import web
from about_text import ABOUT_TEXT
from stomat import Stomat, stomat
from datetime import datetime
import re
import requests

# Регулярка для триггеров
TRIGGER_PATTERN = re.compile(
    r'(?i)\b('
    r'(жалов|жалоб|жалу|претенз|недовол|проблем|некачеств|отзыв|благодар|спасибо|довол|похвал|отличн|хорош|директор|админ|руковод|переда|сообщи|обрат|просьб|пожелан|выразить)\w*'
    r'|'
    r'(позови|вызови|мне\s+нужен|дай\s+связь|свяжи\s+с|поговорить\s+с)\s*(человека?|администратор[аом]?|менеджер[аом]?|руководител[яь])'
    r')\b',
    re.UNICODE
)

logging.basicConfig(filename='/home/ruenmebot/logs/ruenmebot.log', level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s')
# Глобальная переменная для хранения состояния и данных
user_data = {}
booking_state = {}
booking_datetime = 0
mysql_booking_datetime = 0

async def handle_callback(update, context):
    query = update.callback_query
    await query.answer()  # Подтверждаем обработку
    
    if query.data == 'belousov':
        text = await stomat.get_belousov_info(app)
        await query.edit_message_text(text=text, reply_markup=query.message.reply_markup, parse_mode='HTML')

    elif query.data == 'simonova':
        result = await stomat.get_simonova_info(app)
        if isinstance(result, dict):
            text = result.get("text", "Нет данных")
            await query.edit_message_text(text=text, parse_mode='HTML')
            # await stomat.send_booking_options(update, app)
        else:
            logging.error("Unexpected result type from get_simonova_info: %s", type(result))
            await query.edit_message_text(text="Ошибка обработки данных.", reply_markup=query.message.reply_markup, parse_mode='HTML')

    elif query.data.startswith("next_day_"):
        next_idx = int(query.data.split("_")[2])
        logging.info(f"next_day_ with index: {next_idx}")
        await stomat.send_booking_options_next(update, app, current_day_idx=next_idx)

    elif query.data.startswith("prev_day_"):
        prev_idx = int(query.data.split("_")[2])
        logging.info(f"prev_day_ with index: {prev_idx}")
        await stomat.send_booking_options_next(update, app, current_day_idx=prev_idx)

    elif query.data.startswith("first_day_"):
        first_idx = int(query.data.split("_")[2])
        logging.info(f"first_day_ with index: {first_idx}")
        await stomat.send_booking_options_next(update, app, current_day_idx=first_idx)

    elif query.data.startswith("last_day_"):
        last_idx = int(query.data.split("_")[2])
        logging.info(f"last_day_ with index: {last_idx}")
        await stomat.send_booking_options_next(update, app, current_day_idx=last_idx)

    elif query.data.startswith("book_"):
        parts = query.data.split("_")
        day = parts[1]  # "02 сентября"
        time = parts[2]  # "11:30"
        logging.info(f"book_ with day: {day}, time: {time}")  # Исправлено логирование
        # Преобразование в формат "2025-09-02 11:30:00"
        month_map = {
            "января": "01", "февраля": "02", "марта": "03", "апреля": "04",
            "мая": "05", "июня": "06", "июля": "07", "августа": "08",
            "сентября": "09", "октября": "10", "ноября": "11", "декабря": "12"
        }
        day_parts = day.split()
        day_num = day_parts[0]
        month = month_map[day_parts[1]]
        year = "2025"  # Учитывая текущую дату (июнь 2025)
        mysql_booking_datetime = f"{year}-{month}-{day_num} {time}:00"
        booking_datetime = f"{day} {time}"

        # Сохранение в user_data с отладкой
        user_id = query.from_user.id
        logging.info(f"Before save user_data for {user_id}: {user_data.get(user_id, {})}")
        user_data[user_id] = user_data.get(user_id, {})
        user_data[user_id]["mysql_booking_datetime"] = mysql_booking_datetime
        user_data[user_id]["booking_datetime"] = booking_datetime
        logging.info(f"After save user_data for {user_id}: {user_data[user_id]}")
        
        # Инициализируем состояние для пользователя
        booking_state[user_id] = "waiting_for_full_name"
        await query.message.reply_text(
            "Пожалуйста, введите ваше полное ФИО:",
            reply_markup=ForceReply(selective=True)
        )    
        # await query.edit_message_text(f"Вы записаны на {booking_datetime}.", parse_mode='HTML')

async def get_business_response(query, user_info=None, bot=None):
    try:
        answer = await get_response(query)
        if answer:
            return answer
        answer = await query_grok(query, system_prompt=SYSTEM_PROMPT, user_info=user_info, bot=bot)
        return answer
    except Exception as e:
        return random.choice(ERROR_RESPONSES)

async def forward_feedback_message(app, user_id, username, user_message):
    """Пересылает сообщение в другой бот и на email."""
    formatted_message = f"Сообщение от @{username} (ID: {user_id}):\n{user_message}"
    try:
        target_app = Application.builder().token(TELEGRAM_TOKEN).build()
        await target_app.initialize()
        for chat_id in FORWARD_CHAT_IDS:
            try:
                await target_app.bot.send_message(
                    chat_id=chat_id,
                    text=formatted_message
                )
                # logging.info(f"Message successfully forwarded to Telegram chat {chat_id} from user {user_id}")
            except Exception as e:
                logging.error(f"Failed to forward message to Telegram chat {chat_id}: {str(e)}")
        await target_app.shutdown()
        await stomat.send_email(formatted_message)
        # logging.info(f"Feedback forwarded from user {user_id}")
    except Exception as e:
        logging.error(f"Error forwarding feedback: {str(e)}")
        # Не прерываем выполнение, чтобы бот не упал

async def webhook(request: web.Request):
    app = request.app['telegram_app']
    try:
        data = await request.json()
        update = Update.de_json(data, app.bot)
        if not update:
            return web.Response(status=200)
        if update.message and update.message.text and update.message.text.startswith('/'):
            await app.process_update(update)
        elif update.business_message:
            business_connection_id = data.get('business_message', {}).get('business_connection_id')
            if business_connection_id:
                # response_text = await get_business_response(update.business_message.text, user_info={
                #     'username': update.business_message.from_user.username,
                #     'first_name': update.business_message.from_user.first_name,
                #     'last_name': update.business_message.from_user.last_name
                # }, bot=app.bot)

                # Задержка и печатает текст
                await asyncio.sleep(RESPONSE_DELAY_SECONDS)
                await app.bot.send_chat_action(chat_id=update.business_message.chat.id, action=ChatAction.TYPING, business_connection_id=business_connection_id)
                await asyncio.sleep(TYPING_DURATION_SECONDS)
                
                # Еще раз задержка и печатает текст
                await asyncio.sleep(RESPONSE_DELAY_SECONDS)
                await app.bot.send_chat_action(chat_id=update.business_message.chat.id, action=ChatAction.TYPING, business_connection_id=business_connection_id)
                await asyncio.sleep(TYPING_DURATION_SECONDS)

                response_text = await stomat.process_message(user_id, update.message.text)
                await app.bot.send_message(chat_id=update.business_message.chat.id, text=response_text, business_connection_id=business_connection_id)
            else:
                pass
        elif data.get('business_message_reaction'):
            chat_id = data['business_message_reaction']['chat']['id']
            reaction = data['business_message_reaction']['new_reaction']
            business_connection_id = data['business_message_reaction'].get('business_connection_id', None)
            if reaction and isinstance(reaction, list) and reaction[0].get('emoji') in REACTION_RESPONSES:
                response_text = REACTION_RESPONSES[reaction[0]['emoji']]
                try:
                    await app.bot.send_message(chat_id=chat_id, text=response_text, business_connection_id=business_connection_id)
                except Exception as e:
                    logging.error(f"Failed to send business reaction response: {str(e)}")
        elif update.message_reaction:
            chat_id = update.message_reaction.chat.id
            reaction = update.message_reaction.new_reaction[0].emoji if update.message_reaction.new_reaction else None
            if reaction in REACTION_RESPONSES:
                response_text = REACTION_RESPONSES[reaction]
                business_connection_id = None
                if chat_id == 6575779900:
                    business_connection_id = "Ii_7B4uxoEnzDgAA-XW2qPt-vBo"
                try:
                    await app.bot.send_message(chat_id=chat_id, text=response_text, business_connection_id=business_connection_id)
                except Exception as e:
                    logging.error(f"Failed to send reaction response: {str(e)}")
        elif update.callback_query:
            await handle_callback(update, app)
        elif update.message:
            user_id = update.message.from_user.id
            global booking_state
            logging.info(f"Webhook checking booking state for user {user_id}: {booking_state.get(user_id)}")
            if user_id in booking_state:
                await app.process_update(update)  # Передаём в handle_message
            else:
                # Проверяем, является ли это нажатием кнопки (только обычные клавиши)
                if re.search(r'^(📅 Запись|💰 Цены|☎️ Контакты|🏥 Школа|🧠 ИИ|🗓 Моя запись|♻️)$', update.message.text):
                    if update.message.text == '🏥 Школа':
                        keyboard = [
                            ['📅 Запись', '💰 Цены'],
                            ['☎️ Контакты','🗓 Моя запись'],
                            ['🧠 ИИ','♻️']
                        ]
                        reply_markup = ReplyKeyboardMarkup(
                            keyboard=keyboard,
                            resize_keyboard=True,
                            one_time_keyboard=False,
                            selective=True
                        )
                        await app.bot.send_message(chat_id=update.message.chat.id, text=ABOUT_TEXT, reply_markup=reply_markup, parse_mode='markdown')
                    elif update.message.text == '🧠 ИИ':
                        keyboard = [
                            ['📅 Запись', '💰 Цены'],
                            ['☎️ Контакты','🗓 Моя запись'],
                            ['🏥 Школа','♻️']
                        ]
                        reply_markup = ReplyKeyboardMarkup(
                            keyboard=keyboard,
                            resize_keyboard=True,
                            one_time_keyboard=False,
                            selective=True
                        )
                        await app.bot.send_message(chat_id=update.message.chat.id, text=AI_COMMAND_RESPONSE, reply_markup=reply_markup, parse_mode='markdown')
                    elif update.message.text == '♻️':
                        keyboard = [
                            ['📅 Запись', '💰 Цены'],
                            ['☎️ Контакты', '🏥 Школа'],
                            ['🧠 ИИ','🗓 Моя запись']
                        ]
                        reply_markup = ReplyKeyboardMarkup(
                            keyboard=keyboard,
                            resize_keyboard=True,
                            one_time_keyboard=False,
                            selective=True
                        )
                        await app.bot.send_message(chat_id=update.message.chat.id, text="отмена", reply_markup=reply_markup)
                    elif update.message.text == '☎️ Контакты':
                        contacts = await stomat.get_contacts(app)
                        if contacts:
                            
                            for row in contacts:
                                text = f"<b>{row[1]}</b>\n"  # note
                                text += f"Email: school@izdenta.com\n"
                                text +=f"Лицензия: 12345 бессрочно\n"
                                text +=f"Директор Михеева Екатерина Романовна\n"
                                text += "\nИжевск, Бородина, 21, оф.417\n"
                                text += f"как проехать {row[2]}\n"  # adress
                            await app.bot.send_message(chat_id=update.message.chat.id, text=text, parse_mode='HTML')
                        else:
                            await app.bot.send_message(chat_id=update.message.chat.id, text="Ошибка при загрузке контактов. Попробуйте позже.")
                    elif update.message.text == '💰 Цены':
                        text = (
                            "Задайте свой вопрос в свободной форме (например, «Сколько стоит занятие?»), и я сразу отвечу!\n"
                            "Полный прайс-лист доступен на нашем официальном сайте:\n🌐 <a href='https://ruenme.com/pricelist'>ruenme.com/pricelist</a>"
                        )
                        await app.bot.send_message(chat_id=update.message.chat.id, text=text, parse_mode='HTML')
                    elif update.message.text == '📅 Запись':

                        text = f"Выберите преподавателя"
                        keyboard = [
                            [InlineKeyboardButton("Михеева Е.Р.", callback_data='belousov'),
                            InlineKeyboardButton("Михеева Е.Р.", callback_data='simonova')],
                        ]
                        reply_markup = InlineKeyboardMarkup(keyboard)

                        await app.bot.send_message(chat_id=update.message.chat.id, text=text or "Нет данных", reply_markup=reply_markup, parse_mode='HTML')
                    elif update.message.text == '🗓 Моя запись':
                        await stomat.handle_my_appointment(update, app)
                else:
                    is_trigger = TRIGGER_PATTERN.search(update.message.text)
                    # Проверяем стоматологические запросы
                    pattern = r"(?i)(?:прайс|все\s+цены|покажи\s+все\s+цены|покажи\s+прайс|покажи\s+цены|цены)(?:\s+на)?\s+([\w\s]+)|" \
                            r"(?:сколько\s+(?:стоит|стоят)\s+(\w+)|поч[её]м(?:\s+у\s+вас)?\s+(\w+)|стоимость\s+(\w+)|" \
                            r"(?:скажите|подскажите|скажи)\s+(?:цена|цену|цены|стоимость)\s+(?:на)?\s+([а-яё]+)|" \
                            r"(\w+)\s+(?:сколько\s+(?:стоит|стоят)|поч[её]м(?:\s+у\s+вас)?|стоимость)|" \
                            r"(\w+)\s+(?:скажите|подскажите|скажи)\s+(?:цена|цену|цены|стоимость))"
                    if re.search(pattern, update.message.text.lower()):
                        handled = await stomat.handle_price_query(update, app, TYPING_DURATION_SECONDS)
                        if not handled:
                            await app.bot.send_message(chat_id=update.message.chat.id, text="Такую услугу наша Школа не оказывает, приносим извинения.")
                    else:
                        # response = await get_business_response(update.message.text, user_info={
                        #     'username': update.message.from_user.username,
                        #     'first_name': update.message.from_user.first_name,
                        #     'last_name': update.message.from_user.last_name
                        # }, bot=app.bot)
                        
                        # Задержка и печатает текст
                        await asyncio.sleep(RESPONSE_DELAY_SECONDS)
                        await app.bot.send_chat_action(chat_id=update.message.chat.id, action=ChatAction.TYPING)
                        await asyncio.sleep(TYPING_DURATION_SECONDS)

                        # Еще раз задержка и печатает текст
                        await asyncio.sleep(RESPONSE_DELAY_SECONDS)
                        await app.bot.send_chat_action(chat_id=update.message.chat.id, action=ChatAction.TYPING)
                        await asyncio.sleep(TYPING_DURATION_SECONDS)
                        
                        response = await stomat.process_message(user_id, update.message.text)
                        if is_trigger:
                            response += "\n\nЯ передал Ваше сообщение в администрацию."
                            username = update.message.from_user.username or "Не указан"
                            asyncio.create_task(forward_feedback_message(app, user_id, username, update.message.text))
                        await app.bot.send_message(chat_id=update.message.chat.id, text=response)
        return web.Response(status=200)
    except Exception as e:
        logging.error(f"Webhook error: {str(e)}")
        return web.Response(status=200)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user_id = update.message.from_user.id
    text = update.message.text

    # Проверка состояния бронирования с отладкой
    global booking_state, user_data
    logging.info(f"Processing message for user {user_id}, booking_state: {booking_state.get(user_id)}, user_data: {user_data.get(user_id, {})}")
    logging.info(f"Checking booking state for user {user_id}: {booking_state.get(user_id)}")
    if user_id in booking_state:
        if text == '/cancel' or text == '♻️':
            await update.message.reply_text(
                "Отмена бронирования. Вы вернулись в обычный режим.",
                reply_markup=ReplyKeyboardMarkup(
                    keyboard=[
                        ['📅 Запись', '💰 Цены'],
                        ['☎️ Контакты', '🏥 Школа'],
                        ['🧠 ИИ','🗓 Моя запись']
                    ],
                    resize_keyboard=True,
                    one_time_keyboard=False,
                    selective=True
                )
            )
            del booking_state[user_id]
            if user_id in user_data:
                    del user_data[user_id]
            return
        if booking_state[user_id] == "waiting_for_full_name":
            # Проверка ФИО: 3 или 4 слова, каждое с заглавной буквы
            if not re.match(r'^[А-ЯЁ][а-яё]+\s[А-ЯЁ][а-яё]+\s[А-ЯЁ][а-яё]+(\s[А-ЯЁ][а-яё]+)?$', text):
                await update.message.reply_text(
                    "Ошибка: ФИО должно содержать 3 или 4 слова (Фамилия, Имя, Отчество) с заглавной буквы. Попробуйте снова.",
                    reply_markup=ForceReply(selective=True)
                )
                return
            user_data[user_id] = {"full_name": text}
            await update.message.reply_text(
                "Введите вашу дату рождения (в формате ДД.ММ.ГГГГ):",
                reply_markup=ForceReply(selective=True)
            )
            booking_state[user_id] = "waiting_for_birth_date"
        elif booking_state[user_id] == "waiting_for_birth_date":
            if text == '/cancel' or text == '♻️':
                await update.message.reply_text(
                    "Отмена бронирования. Вы вернулись в обычный режим.",
                    reply_markup=ReplyKeyboardMarkup(
                        keyboard=[
                            ['📅 Запись', '💰 Цены'],
                            ['☎️ Контакты', '🏥 Школа'],
                            ['🧠 ИИ','🗓 Моя запись']
                        ],
                        resize_keyboard=True,
                        one_time_keyboard=False,
                        selective=True
                    )
                )
                del booking_state[user_id]
                if user_id in user_data:
                    del user_data[user_id]
                return
            # Проверка и преобразование даты в YYYY-MM-DD
            try:
                birth_date = datetime.strptime(text, "%d.%m.%Y").strftime("%Y-%m-%d")
                user_data[user_id]["birth_date"] = birth_date
            except ValueError:
                await update.message.reply_text(
                    "Ошибка: Введите дату в формате ДД.ММ.ГГГГ (например, 01.01.1990). Попробуйте снова.",
                    reply_markup=ForceReply(selective=True)
                )
                return
            await update.message.reply_text(
                "Введите ваш номер телефона (в формате +7XXXXXXXXXX):",
                reply_markup=ForceReply(selective=True)
            )
            booking_state[user_id] = "waiting_for_phone"
        elif booking_state[user_id] == "waiting_for_phone":
            if text == '/cancel' or text == '♻️':
                await update.message.reply_text(
                    "Отмена бронирования. Вы вернулись в обычный режим.",
                    reply_markup=ReplyKeyboardMarkup(
                        keyboard=[
                            ['📅 Запись', '💰 Цены'],
                            ['☎️ Контакты', '🏥 Школа'],
                            ['🧠 ИИ','🗓 Моя запись']
                        ],
                        resize_keyboard=True,
                        one_time_keyboard=False,
                        selective=True
                    )
                )
                del booking_state[user_id]
                if user_id in user_data:
                    del user_data[user_id]
                return
            # Проверка номера телефона: +7XXXXXXXXXX
            if not re.match(r'^\+7\d{10}$', text):
                await update.message.reply_text(
                    "Ошибка: Введите номер в формате +7XXXXXXXXXX (например, +71234567890). Попробуйте снова.",
                    reply_markup=ForceReply(selective=True)
                )
                return
            # Преобразование в +7 HHH OOO-UU-KK
            phone = f"+7 {text[2:5]} {text[5:8]}-{text[8:10]}-{text[10:12]}"
            user_data[user_id]["phone"] = phone
            await update.message.reply_text(
                "Введите примечание (или оставьте пустым):",
                reply_markup=ForceReply(selective=True)
            )
            booking_state[user_id] = "waiting_for_note"
        elif booking_state[user_id] == "waiting_for_note":
            if text == '/cancel' or text == '♻️':
                await update.message.reply_text(
                    "Отмена бронирования. Вы вернулись в обычный режим.",
                    reply_markup=ReplyKeyboardMarkup(
                        keyboard=[
                            ['📅 Запись', '💰 Цены'],
                            ['☎️ Контакты', '🏥 Школа'],
                            ['🧠 ИИ','🗓 Моя запись']
                        ],
                        resize_keyboard=True,
                        one_time_keyboard=False,
                        selective=True
                    )
                )
                del booking_state[user_id]
                if user_id in user_data:
                    del user_data[user_id]
                return
            user_data[user_id]["note"] = text if text.strip() else "нет"
            full_name = user_data[user_id]["full_name"]
            birth_date = user_data[user_id]["birth_date"]
            phone = user_data[user_id]["phone"]
            note = user_data[user_id]["note"]
            # mysql_booking_datetime = user_data[user_id]["mysql_booking_datetime"]
            # booking_datetime = user_data[user_id]["booking_datetime"]
            # Проверка наличия ключей
            mysql_booking_datetime = user_data[user_id].get("mysql_booking_datetime", "не указано")
            booking_datetime = user_data[user_id].get("booking_datetime", "не указано")
            logging.info(f"Retrieved user_data for {user_id}: {user_data[user_id]}")
            logging.info(f"Using mysql_booking_datetime: {mysql_booking_datetime} for user {user_id}")
            keyboard = [
                ['📅 Запись', '💰 Цены'],
                ['☎️ Контакты', '🏥 Школа'],
                ['🧠 ИИ','🗓 Моя запись']
            ]
            reply_markup = ReplyKeyboardMarkup(
                keyboard=keyboard,
                resize_keyboard=True,
                one_time_keyboard=False,
                selective=True
            )
            await update.message.reply_text(
                f"ФИО: {full_name}, дата рождения: {birth_date}, номер телефона: {phone}, примечание: {note} процесс записи на прием {booking_datetime}",
                reply_markup=reply_markup,
                parse_mode='HTML'
            )

            # Вызов функции с сохранением и получение результата
            status, message = await add_appoint(full_name=full_name, birth_date=birth_date, phone=phone, note=note, booking_datetime=mysql_booking_datetime)
            # Отправка сообщения об успехе или ошибке
            await update.message.reply_text(message)

            # Например: await save_to_database(user_id, full_name, birth_date, phone, note, booking_datetime)
            del booking_state[user_id]
            if user_id in user_data:
                del user_data[user_id]
            return
        return  # Прерываем выполнение после обработки любого состояния

    # Обычная обработка текста, если нет состояния бронирования
    # Задержка и печатает текст
    await asyncio.sleep(RESPONSE_DELAY_SECONDS)
    await context.bot.send_chat_action(
        chat_id=update.message.chat.id,
        action=ChatAction.TYPING
    )
    await asyncio.sleep(TYPING_DURATION_SECONDS)

    # Еще раз задержка и печатает текст
    await asyncio.sleep(RESPONSE_DELAY_SECONDS)
    await context.bot.send_chat_action(
        chat_id=update.message.chat.id,
        action=ChatAction.TYPING
    )
    await asyncio.sleep(TYPING_DURATION_SECONDS)

    response = await get_response(text)
    if response:
        logging.info(f"DB response for regular: {response}")
    else:
        response = await query_grok(text, system_prompt=SYSTEM_PROMPT, user_info=None, bot=context.bot)
    await update.message.reply_text(response)

async def start_biz_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.text.split()[-1].replace("bizChat", "")
    await update.message.reply_text(f"Бот активирован для бизнес-чата {chat_id}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id == ADMIN_CHAT_ID:
        #await context.bot.send_message(chat_id=update.effective_chat.id, text=START_BOT_MESSAGE, reply_markup=reply_markup)
        await stomat.handle_start(update, context.application)
    else:
        await stomat.handle_start(update, context.application)

async def add_response_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id == ADMIN_CHAT_ID:
        if not context.args:
            await update.message.reply_text("Формат: /add_response <question> | <answer> | <pattern>")
            return
        try:
            input_text = " ".join(context.args)
            parts = input_text.split("|")
            if len(parts) < 3:
                raise ValueError("Недостаточно частей")
            question = parts[0].strip()
            pattern = parts[-1].strip()
            answer = "|".join(parts[1:-1]).strip()
            await add_response(question, answer, pattern)
            await update.message.reply_text(f"Добавлено: {question} -> {answer} (pattern: {pattern})")
        except ValueError:
            await update.message.reply_text("Ошибка! Формат: /add_response <question> | <answer> | <pattern>")
        except Exception as e:
            logging.error(f"Error adding response: {str(e)}")
            await update.message.reply_text("Что-то пошло не так, сорян!")
    else:
        await update.message.reply_text(NON_ADMIN_MESSAGE)

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id == ADMIN_CHAT_ID:
        await update.message.reply_text("Статистика пока в разработке, ага.")
    else:
        await update.message.reply_text(NON_ADMIN_MESSAGE)

async def confirm_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await stomat.handle_confirm(update, context.application)

async def cancelvisit_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await stomat.handle_cancelvisit(update, context.application)

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.error(f"Update {update} caused error {context.error}")

async def help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id == ADMIN_CHAT_ID:
        commands = [
            "/start - Запустить бота",
            "/add_response <вопрос> | <ответ> | <шаблон> - Добавить ответ (админ)",
            "/stats - Показать статистику (в разработке)",
            "/help - Показать команды (админ)"
        ]
        await update.message.reply_text("Вот мои команды:\n" + "\n".join(commands))
    else:
        await update.message.reply_text(NON_ADMIN_MESSAGE)

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    global booking_state, user_data
    if user_id in booking_state:
        await update.message.reply_text(
            "Отмена бронирования. Вы вернулись в обычный режим.",
            reply_markup=ReplyKeyboardMarkup(
                keyboard=[
                    ['📅 Запись', '💰 Цены'],
                    ['☎️ Контакты', '🏥 Школа'],
                    ['🧠 ИИ','🗓 Моя запись']
                ],
                resize_keyboard=True,
                one_time_keyboard=False,
                selective=True
            )
        )
        del booking_state[user_id]
        del user_data[user_id]
    else:
        await update.message.reply_text(
            "Нет активного процесса для отмены.",
            reply_markup=ReplyKeyboardMarkup(
                keyboard=[
                    ['📅 Запись', '💰 Цены'],
                    ['☎️ Контакты', '🏥 Школа'],
                    ['🧠 ИИ','🗓 Моя запись']
                ],
                resize_keyboard=True,
                one_time_keyboard=False,
                selective=True
            )
        )

# сохраняем запись на прием в базу
async def add_appoint(full_name, birth_date, phone, note, booking_datetime):
    url = "https://ruenme.com/appoint/calendar"
    payload = {
        "fullname": full_name,
        "dr": birth_date,
        "tel": phone,
        "note": note,
        "id_spec": 13,
        "id_patient": 2,
        "data_app": booking_datetime
    }
    response = requests.post(url, data=payload)
    if response.status_code == 200:
        logging.info(f"Appointment saved for user {full_name}")
        return "success", "Запись успешно сохранена!"
    else:
        logging.error(f"Failed to save appointment: {response.status_code} - {response.text}")
        return "error", f"Ошибка при записи: {response.status_code} - {response.text}"

async def main():
    await init_db()
    global app
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start_biz_chat, filters.Regex(r'^/start\s+bizChat')))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("add_response", add_response_command))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("help", help))
    app.add_handler(CommandHandler("confirm", confirm_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CommandHandler("cancelvisit", cancelvisit_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_error_handler(error_handler)
    await app.initialize()

    # Настройка меню бота
    commands = [
        BotCommand(command="start", description="Запустить бота"),
        BotCommand(command="cancel", description="Отменить")
    ]
    await app.bot.set_my_commands(commands)

    await app.bot.set_webhook(
        url=WEBHOOK_URL,
        allowed_updates=["message", "callback_query"]
    )

    web_app = web.Application()
    web_app['telegram_app'] = app
    web_app.add_routes([web.post('/ruenmebot', webhook)])
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', 8446)
    await site.start()
    await app.start()
    try:
        await asyncio.Event().wait()
    finally:
        await app.stop()
        await runner.cleanup()

if __name__ == '__main__':
    asyncio.run(main())