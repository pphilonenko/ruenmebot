import aiosqlite
import logging
import re

DB_FILE = "/home/ruenmebot/responses.db"

async def init_db():
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS responses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                question TEXT NOT NULL,
                answer TEXT NOT NULL,
                pattern TEXT
            )
        ''')

        await db.execute('INSERT OR IGNORE INTO responses (question, answer, pattern) VALUES (?, ?, ?)', ("привет, как дела", "Амбивалентно. Ага. Так и живём", r"(?i)(привет|здравствуй|здорова|хей|здравствуйте).*как дела.*"))

        await db.commit()

async def get_response(question):
    async with aiosqlite.connect(DB_FILE) as db:
        cursor = await db.execute('SELECT answer FROM responses WHERE LOWER(question) = LOWER(?)', (question,))
        row = await cursor.fetchone()
        if row:
            return row[0]
        cursor = await db.execute('SELECT answer, pattern FROM responses WHERE pattern IS NOT NULL')
        rows = await cursor.fetchall()
        for answer, pattern in rows:
            try:
                if re.search(pattern, question, re.IGNORECASE):
                    return answer
            except re.error as e:
                logging.error(f"Regex error in pattern {pattern}: {str(e)}")
        return None

async def add_response(question, answer, pattern=None):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute('INSERT INTO responses (question, answer, pattern) VALUES (?, ?, ?)', (question, answer, pattern))
        await db.commit()
