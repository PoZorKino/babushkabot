import asyncio
import json
import logging
import random
import time
from pathlib import Path
from collections import defaultdict, deque

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, InputRichMessage, Message

from . import config
from .llm import LLMError, stream_reply
from .mdhtml import to_telegram_html
from .persona import DEFAULT_PERSONA, PERSONAS

log = logging.getLogger("babushka")

DRAFT_INTERVAL = 0.35
EDIT_INTERVAL = 1.0  # Telegram режет правки чаще ~1/сек на чат
LIMIT = 3800  # запас до 4096 на теги и курсор
RICH_LIMIT = 30000  # у rich-сообщений лимит 32768
CURSOR = " ▌"

history: dict[int, deque] = defaultdict(lambda: deque(maxlen=config.HISTORY_LIMIT))
busy: set[int] = set()

dp = Dispatcher()


STATE_FILE = Path(__file__).resolve().parent.parent / "data" / "personas.json"


def _load_personas() -> dict[int, str]:
    try:
        return {int(k): v for k, v in json.loads(STATE_FILE.read_text("utf-8")).items() if v in PERSONAS}
    except (OSError, ValueError):
        return {}


chat_persona: dict[int, str] = _load_personas()


def _save_personas() -> None:
    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(chat_persona), "utf-8")


def current_persona(chat: int) -> str:
    return chat_persona.get(chat, DEFAULT_PERSONA)


def persona_keyboard(current: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=("✓ " if key == current else "") + name, callback_data=f"persona:{key}")
                for key, (name, _, _) in PERSONAS.items()
            ]
        ]
    )


@dp.message(CommandStart())
async def start(m: Message) -> None:
    cur = current_persona(m.chat.id)
    await m.answer(
        PERSONAS[cur][1] + "\n\n/persona - сменить бабушку\n/reset - забыть разговор",
        reply_markup=persona_keyboard(cur),
    )


@dp.message(Command("persona"))
async def persona(m: Message) -> None:
    await m.answer("Кого позвать?", reply_markup=persona_keyboard(current_persona(m.chat.id)))


@dp.callback_query(F.data.startswith("persona:"))
async def pick_persona(q: CallbackQuery) -> None:
    key = q.data.split(":", 1)[1]
    if key not in PERSONAS or q.message is None:
        await q.answer()
        return
    chat = q.message.chat.id
    if key != current_persona(chat):
        chat_persona[chat] = key
        _save_personas()
        history.pop(chat, None)  # чтобы стиль прошлой бабки не просачивался
    await q.answer(PERSONAS[key][0])
    try:
        await q.message.edit_text(PERSONAS[key][1], reply_markup=persona_keyboard(key))
    except TelegramBadRequest:
        pass


@dp.message(Command("reset"))
async def reset(m: Message) -> None:
    history.pop(m.chat.id, None)
    await m.answer("Ох, и вылетело из головы всё, склероз... Давай сначала, внучок.")


class Streamer:
    """Один «живой» ответ.

    В личке - sendMessageDraft (нативный стриминг Telegram, анимированный
    черновик), в итоге уходит обычным сообщением. В группах черновиков нет,
    поэтому правим сообщение через editMessageText.
    """

    def __init__(self, source: Message) -> None:
        self.source = source
        self.use_draft = source.chat.type == "private"
        # rich-сообщения (Bot API 10.2) понимают Markdown сами, включая таблицы
        self.rich = self.use_draft
        self.limit = RICH_LIMIT if self.rich else LIMIT
        self.draft_id = random.randint(1, 2**31 - 1)
        self.msg: Message | None = None
        self.offset = 0  # сколько символов уже ушло в предыдущие сообщения
        self.interval = DRAFT_INTERVAL if self.use_draft else EDIT_INTERVAL
        self.last_edit = 0.0
        self.last_sent = ""
        self.last_draft = ""

    async def _draft(self, text: str) -> None:
        bot, chat = self.source.bot, self.source.chat.id
        if self.rich:
            try:
                await bot.send_rich_message_draft(chat, self.draft_id, InputRichMessage(markdown=text))
                return
            except TelegramBadRequest as e:
                log.warning("rich draft rejected: %s", e)  # недописанная разметка - пробуем обычный драфт
        try:
            await bot.send_message_draft(chat, self.draft_id, text=to_telegram_html(text), parse_mode="HTML")
        except TelegramBadRequest:
            await bot.send_message_draft(chat, self.draft_id, text=text, parse_mode=None)

    async def _send(self, text: str, final: bool) -> None:
        if self.use_draft and not final:
            if text == self.last_draft:
                return
            try:
                await self._draft(text)
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after)
                return
            except TelegramBadRequest:
                self.use_draft = False  # драфты недоступны - откатываемся на правки
                return
            self.last_draft = text
            self.last_edit = time.monotonic()
            return

        if self.rich and final:
            try:
                await self.source.bot.send_rich_message(self.source.chat.id, InputRichMessage(markdown=text))
                self.last_draft = ""
                return
            except TelegramBadRequest as e:
                log.warning("rich message rejected, fallback to html: %s", e)
                self.rich = False
                self.limit = LIMIT

        body = to_telegram_html(text) + ("" if final else CURSOR)
        if body == self.last_sent:
            return
        try:
            if self.msg is None:
                self.msg = await self.source.answer(body, parse_mode="HTML")
            else:
                await self.msg.edit_text(body, parse_mode="HTML")
        except TelegramBadRequest as e:
            if "not modified" in str(e):
                return
            # кривая разметка на недописанном тексте - шлём как есть
            plain = text + ("" if final else CURSOR)
            if self.msg is None:
                self.msg = await self.source.answer(plain, parse_mode=None)
            else:
                try:
                    await self.msg.edit_text(plain, parse_mode=None)
                except TelegramBadRequest:
                    pass
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
            return
        self.last_sent = body
        self.last_edit = time.monotonic()
        if final and self.use_draft:
            self.msg = None  # черновик закрыт настоящим сообщением

    async def update(self, full: str) -> None:
        if time.monotonic() - self.last_edit < self.interval:
            return
        # слишком длинно: закрываем текущее сообщение на границе строки и начинаем новое
        while len(full) - self.offset > self.limit:
            chunk = full[self.offset : self.offset + self.limit]
            cut = chunk.rfind("\n")
            cut = cut if cut > self.limit // 2 else self.limit
            await self._send(full[self.offset : self.offset + cut], final=True)
            self.offset += cut
            self.msg, self.last_sent = None, ""
        await self._send(full[self.offset :], final=False)

    async def finish(self, full: str) -> None:
        await self.update_force(full)

    async def update_force(self, full: str) -> None:
        self.last_edit = 0.0
        await self.update(full)
        await self._send(full[self.offset :], final=True)


@dp.message(F.text & ~F.text.startswith("/"))
async def talk(m: Message, bot: Bot) -> None:
    chat = m.chat.id
    if chat in busy:
        await m.answer("Погоди, внучок, я ещё предыдущее договариваю...")
        return
    busy.add(chat)
    try:
        history[chat].append({"role": "user", "content": m.text})
        messages = [{"role": "system", "content": PERSONAS[current_persona(chat)][2]}, *history[chat]]
        streamer = Streamer(m)
        full = ""
        await bot.send_chat_action(chat, "typing")
        try:
            async for piece in stream_reply(messages):
                full += piece
                await streamer.update(full)
        except LLMError as e:
            log.error("llm failed: %s", e)
            if not full:
                history[chat].pop()
                await m.answer("Ой, внучок, что-то у меня в голове шумит, не соображу. Спроси чуть погодя.")
                return
        except Exception:
            log.exception("stream broke")
            if not full:
                history[chat].pop()
                await m.answer("Ой, связь пропала, внучок. Повтори, пожалуйста.")
                return
        if full.strip():
            await streamer.finish(full)
            history[chat].append({"role": "assistant", "content": full})
    finally:
        busy.discard(chat)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    bot = Bot(config.BOT_TOKEN, default=DefaultBotProperties(link_preview_is_disabled=True))
    me = await bot.get_me()
    log.info("запущен как @%s, модели: %s", me.username, ", ".join(config.MODELS))
    await dp.start_polling(bot)
