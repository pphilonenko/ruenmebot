# через бота
# /add_response Как настроение? | Прикольно, всё ок. А у тебя как? | (как настроение|настроение как).*
# /add_response Го гулять? | Сорян, завален делами. Давай на днях? | (го гулять|пойдём гулять|погуляем).*
# или
import asyncio
from database import add_response

async def main():
    responses = [
        ("Пойдёшь на тусу?", "Не, я пас, тусы не моё. Лучше дома чилить.", r"(пойдёшь на тусу|туса|на вечеринку).*"),
        ("Чё по работе?", "Всё норм, пашу как вол. А ты как?", r"(ч[её] по работе|как работа|работа как).*"),
    ]
    for question, answer, pattern in responses:
        await add_response(question, answer, pattern)

if __name__ == "__main__":
    asyncio.run(main()) 