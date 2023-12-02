import asyncio
import os
import logging
import shelve
import time
import traceback
import html
import time
import aiohttp
from collections import defaultdict
import openai
from telethon import TelegramClient, events, errors, functions, types

ADMIN_ID = 71863318

aclient = openai.AsyncOpenAI(
    api_key=os.getenv("OPENAI_API_KEY"),
    max_retries=0,
    timeout=120,
)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_API_ID = int(os.getenv("TELEGRAM_API_ID"))
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH")

TELEGRAM_LENGTH_LIMIT = 4096
TELEGRAM_MIN_INTERVAL = 3
OPENAI_MAX_RETRY = 3
OPENAI_RETRY_INTERVAL = 10

telegram_last_timestamp = defaultdict(lambda: None)
telegram_rate_limit_lock = defaultdict(asyncio.Lock)

def within_interval(chat_id):
    global telegram_last_timestamp
    if telegram_last_timestamp[chat_id] is None:
        return False
    remaining_time = telegram_last_timestamp[chat_id] + TELEGRAM_MIN_INTERVAL - time.time()
    return remaining_time > 0

def ensure_interval(interval=TELEGRAM_MIN_INTERVAL):
    def decorator(func):
        async def new_func(*args, **kwargs):
            chat_id = args[0]
            async with telegram_rate_limit_lock[chat_id]:
                global telegram_last_timestamp
                if telegram_last_timestamp[chat_id] is not None:
                    remaining_time = telegram_last_timestamp[chat_id] + interval - time.time()
                    if remaining_time > 0:
                        await asyncio.sleep(remaining_time)
                result = await func(*args, **kwargs)
                telegram_last_timestamp[chat_id] = time.time()
                return result
        return new_func
    return decorator

def retry(max_retry=30, interval=10):
    def decorator(func):
        async def new_func(*args, **kwargs):
            for _ in range(max_retry - 1):
                try:
                    return await func(*args, **kwargs)
                except errors.FloodWaitError as e:
                    logging.exception(e)
                    await asyncio.sleep(interval)
            return await func(*args, **kwargs)
        return new_func
    return decorator

def is_whitelist(chat_id):
    whitelist = db['whitelist']
    return chat_id in whitelist

def add_whitelist(chat_id):
    whitelist = db['whitelist']
    whitelist.add(chat_id)
    db['whitelist'] = whitelist

def del_whitelist(chat_id):
    whitelist = db['whitelist']
    whitelist.discard(chat_id)
    db['whitelist'] = whitelist

def get_whitelist():
    return db['whitelist']

def only_admin(func):
    async def new_func(message):
        if message.sender_id != ADMIN_ID:
            await send_message(message.chat_id, 'Only admin can use this command', message.id)
            return
        await func(message)
    return new_func

def only_private(func):
    async def new_func(message):
        if message.chat_id != message.sender_id:
            await send_message(message.chat_id, 'This command only works in private chat', message.id)
            return
        await func(message)
    return new_func

def only_whitelist(func):
    async def new_func(message):
        if not is_whitelist(message.chat_id):
            if message.chat_id == message.sender_id:
                await send_message(message.chat_id, 'This chat is not in whitelist', message.id)
            return
        await func(message)
    return new_func

@only_admin
async def add_whitelist_handler(message):
    if is_whitelist(message.chat_id):
        await send_message(message.chat_id, 'Already in whitelist', message.id)
        return
    add_whitelist(message.chat_id)
    await send_message(message.chat_id, 'Whitelist added', message.id)

@only_admin
async def del_whitelist_handler(message):
    if not is_whitelist(message.chat_id):
        await send_message(message.chat_id, 'Not in whitelist', message.id)
        return
    del_whitelist(message.chat_id)
    await send_message(message.chat_id, 'Whitelist deleted', message.id)

@only_admin
@only_private
async def get_whitelist_handler(message):
    await send_message(message.chat_id, str(get_whitelist()), message.id)

@retry()
@ensure_interval()
async def send_message(chat_id, text, reply_to_message_id):
    logging.info('Sending message: chat_id=%r, reply_to_message_id=%r, text=%r', chat_id, reply_to_message_id, text)
    msg = await bot.send_message(
        chat_id,
        text,
        reply_to=reply_to_message_id,
        link_preview=False,
    )
    logging.info('Message sent: chat_id=%r, reply_to_message_id=%r, message_id=%r', chat_id, reply_to_message_id, msg.id)
    return msg.id

@retry()
@ensure_interval()
async def send_photo(chat_id, caption, reply_to_message_id, photo):
    logging.info('Sending photo: chat_id=%r, reply_to_message_id=%r, caption=%r', chat_id, reply_to_message_id, caption)
    msg = await bot.send_file(
        chat_id,
        photo,
        caption=caption,
        reply_to=reply_to_message_id,
        parse_mode='html',
    )
    logging.info('Photo sent: chat_id=%r, reply_to_message_id=%r, message_id=%r', chat_id, reply_to_message_id, msg.id)
    return msg.id

@retry()
@ensure_interval()
async def edit_message(chat_id, text, message_id):
    logging.info('Editing message: chat_id=%r, message_id=%r, text=%r', chat_id, message_id, text)
    try:
        await bot.edit_message(
            chat_id,
            message_id,
            text,
            link_preview=False,
        )
    except errors.MessageNotModifiedError as e:
        logging.info('Message not modified: chat_id=%r, message_id=%r', chat_id, message_id)
    else:
        logging.info('Message edited: chat_id=%r, message_id=%r', chat_id, message_id)

@retry()
@ensure_interval()
async def delete_message(chat_id, message_id):
    logging.info('Deleting message: chat_id=%r, message_id=%r', chat_id, message_id)
    await bot.delete_messages(
        chat_id,
        message_id,
    )
    logging.info('Message deleted: chat_id=%r, message_id=%r', chat_id, message_id)

class BotReplyMessages:
    def __init__(self, chat_id, orig_msg_id, prefix):
        self.prefix = prefix
        self.msg_len = TELEGRAM_LENGTH_LIMIT - len(prefix)
        assert self.msg_len > 0
        self.chat_id = chat_id
        self.orig_msg_id = orig_msg_id
        self.replied_msgs = []
        self.text = ''

    async def __aenter__(self):
        return self

    async def __aexit__(self, type, value, tb):
        await self.finalize()
        for msg_id, _ in self.replied_msgs:
            pending_reply_manager.remove((self.chat_id, msg_id))

    async def _force_update(self, text):
        slices = []
        while len(text) > self.msg_len:
            slices.append(text[:self.msg_len])
            text = text[self.msg_len:]
        if text:
            slices.append(text)
        if not slices:
            slices = [''] # deal with empty message

        for i in range(min(len(slices), len(self.replied_msgs))):
            msg_id, msg_text = self.replied_msgs[i]
            if slices[i] != msg_text:
                await edit_message(self.chat_id, self.prefix + slices[i], msg_id)
                self.replied_msgs[i] = (msg_id, slices[i])
        if len(slices) > len(self.replied_msgs):
            for i in range(len(self.replied_msgs), len(slices)):
                if i == 0:
                    reply_to = self.orig_msg_id
                else:
                    reply_to, _ = self.replied_msgs[i - 1]
                msg_id = await send_message(self.chat_id, self.prefix + slices[i], reply_to)
                self.replied_msgs.append((msg_id, slices[i]))
                pending_reply_manager.add((self.chat_id, msg_id))
        if len(self.replied_msgs) > len(slices):
            for i in range(len(slices), len(self.replied_msgs)):
                msg_id, _ = self.replied_msgs[i]
                await delete_message(self.chat_id, msg_id)
                pending_reply_manager.remove((self.chat_id, msg_id))
            self.replied_msgs = self.replied_msgs[:len(slices)]

    async def update(self, text):
        self.text = text
        if not within_interval(self.chat_id):
            await self._force_update(self.text)

    async def finalize(self):
        await self._force_update(self.text)

usage = """Usage: /dalle [OPTIONS] PROMPT

Quality:
-s --standard (default)
-h --hd: hd creates images with finer details and greater consistency across the image.

Style:
-v --vivid (default): Vivid causes the model to lean towards generating hyper-real and dramatic images.
-n --natural: Natural causes the model to produce more natural, less hyper-real looking images.

Size:
--square (default): 1024x1024
-w --wide: 1792x1024
-t --tall: 1024x1792

Example:
/dalle -h -n A cute cat

Note: All OPTIONS should appear before the PROMPT.
"""

@only_whitelist
async def dalle(message):
    chat_id = message.chat_id
    sender_id = message.sender_id
    msg_id = message.id
    text = message.message
    logging.info('New message: chat_id=%r, sender_id=%r, msg_id=%r, text=%r', chat_id, sender_id, msg_id, text)
    params = text.split()
    prompt = []
    quality = None
    style = None
    size = None
    error = None
    is_options = True
    for param in params[1:]:
        if param.startswith('-') and is_options:
            if param in ['-s', '--standard']:
                if quality is None:
                    quality = 'standard'
                else:
                    error = 'More than one Quality options found'
            elif param in ['-h', '--hd']:
                if quality is None:
                    quality = 'hd'
                else:
                    error = 'More than one Quality options found'
            elif param in ['-v', '--vivid']:
                if style is None:
                    style = 'vivid'
                else:
                    error = 'More than one Style options found'
            elif param in ['-n', '--natural']:
                if style is None:
                    style = 'natural'
                else:
                    error = 'More than one Style options found'
            elif param in ['--square']:
                if size is None:
                    size = '1024x1024'
                else:
                    error = 'More than one Size options found'
            elif param in ['-w', '--wide']:
                if size is None:
                    size = '1792x1024'
                else:
                    error = 'More than one Size options found'
            elif param in ['-t', '--tall']:
                if size is None:
                    size = '1024x1792'
                else:
                    error = 'More than one Size options found'
            else:
                error = f'Unknown option: {param}'
        else:
            prompt.append(param)
            is_options = False
    if quality is None:
        quality = 'standard'
    if style is None:
        style = 'vivid'
    if size is None:
        size = '1024x1024'
    prompt = ' '.join(prompt)
    if not prompt:
        error = 'Prompt is empty'
    if error is not None:
        await send_message(chat_id, f'[!] Error: {error}\n\n{usage}', msg_id)
        return

    params = dict(
        model='dall-e-3',
        prompt=prompt,
        size=size,
        quality=quality,
        style=style,
    )
    logging.info('Using DALL-E 3 API: chat_id=%r, sender_id=%r, msg_id=%r, params=%s', chat_id, sender_id, msg_id, params)
    async with bot.action(chat_id, 'typing'):
        try:
            result = await aclient.images.generate(**params)
            logging.info('Responce: chat_id=%r, sender_id=%r, msg_id=%r, result=%s', chat_id, sender_id, msg_id, result)
            url = result.data[0].url
            revised_prompt = result.data[0].revised_prompt
            download_link = f'<a href="{url}">Download</a>'
            caption = f'{download_link}\n{html.escape(revised_prompt)}'
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as session:
                async with session.get(url) as response:
                    response.raise_for_status()
                    image = await response.read()

            dirname = f'images/{chat_id}'.replace('-', '_')
            filename = f'{int(time.time())}_{msg_id}.png'
            path = f'{dirname}/{filename}'
            os.makedirs(dirname, exist_ok=True)
            with open(path, 'w+b') as f:
                f.write(image)
            try:
                await send_photo(chat_id, caption, msg_id, path)
            except errors.rpcerrorlist.MediaCaptionTooLongError:
                photo_msg_id = await send_photo(chat_id, download_link, msg_id, path)
                await send_message(chat_id, revised_prompt, photo_msg_id)

        except Exception as e:
            logging.exception('Error (chat_id=%r, msg_id=%r): %s', chat_id, msg_id, e)
            await send_message(chat_id, f'[!] Error: {traceback.format_exception_only(e)[-1].strip()}', msg_id)
            return

async def ping(message):
    await send_message(message.chat_id, f'chat_id={message.chat_id} user_id={message.sender_id} is_whitelisted={is_whitelist(message.chat_id)}', message.id)

async def main():
    global bot_id, pending_reply_manager, db, bot

    logFormatter = logging.Formatter("%(asctime)s %(process)d %(levelname)s %(message)s")

    rootLogger = logging.getLogger()
    rootLogger.setLevel(logging.INFO)

    fileHandler = logging.FileHandler(__file__ + ".log")
    fileHandler.setFormatter(logFormatter)
    rootLogger.addHandler(fileHandler)

    consoleHandler = logging.StreamHandler()
    consoleHandler.setFormatter(logFormatter)
    rootLogger.addHandler(consoleHandler)

    with shelve.open('db') as db:
        # db['whitelist'] = set(whitelist_chat_ids)
        if 'whitelist' not in db:
            db['whitelist'] = {ADMIN_ID}
        bot_id = int(TELEGRAM_BOT_TOKEN.split(':')[0])
        async with await TelegramClient('bot', TELEGRAM_API_ID, TELEGRAM_API_HASH).start(bot_token=TELEGRAM_BOT_TOKEN) as bot:
            bot.parse_mode = None
            me = await bot.get_me()
            @bot.on(events.NewMessage)
            async def process(event):
                if event.message.chat_id is None:
                    return
                if event.message.sender_id is None:
                    return
                if event.message.message is None:
                    return
                text = event.message.message
                if text == '/ping' or text == f'/ping@{me.username}':
                    await ping(event.message)
                elif text == '/dalle' or text.startswith('/dalle ') or \
                    text == f'/dalle@{me.username}' or text.startswith(f'/dalle@{me.username} '):
                    await dalle(event.message)
                elif text == '/add_whitelist' or text == f'/add_whitelist@{me.username}':
                    await add_whitelist_handler(event.message)
                elif text == '/del_whitelist' or text == f'/del_whitelist@{me.username}':
                    await del_whitelist_handler(event.message)
                elif text == '/get_whitelist' or text == f'/get_whitelist@{me.username}':
                    await get_whitelist_handler(event.message)
            assert await bot(functions.bots.SetBotCommandsRequest(
                scope=types.BotCommandScopeDefault(),
                lang_code='en',
                commands=[types.BotCommand(command, description) for command, description in [
                    ('ping', 'Test bot connectivity'),
                    ('add_whitelist', 'Add this group to whitelist (only admin)'),
                    ('del_whitelist', 'Delete this group from whitelist (only admin)'),
                    ('get_whitelist', 'List groups in whitelist (only admin)'),
                    ('dalle', 'Creates an image given a prompt')
                ]]
            ))
            await bot.run_until_disconnected()

asyncio.run(main())
