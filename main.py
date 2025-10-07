import asyncio
import os
import logging
import shelve
import time
import traceback
import html
import time
import base64
import hashlib
import contextlib
import aiohttp
from collections import defaultdict
import openai
import fal_client
from google import genai
import replicate
from telethon import TelegramClient, events, errors, functions, types

ADMIN_ID = 71863318

aclient = openai.AsyncOpenAI(
    api_key=os.getenv("OPENAI_API_KEY"),
    max_retries=0,
    timeout=600,
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
        # for msg_id, _ in self.replied_msgs:
        #     pending_reply_manager.remove((self.chat_id, msg_id))

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
                # pending_reply_manager.add((self.chat_id, msg_id))
        if len(self.replied_msgs) > len(slices):
            for i in range(len(slices), len(self.replied_msgs)):
                msg_id, _ = self.replied_msgs[i]
                await delete_message(self.chat_id, msg_id)
                # pending_reply_manager.remove((self.chat_id, msg_id))
            self.replied_msgs = self.replied_msgs[:len(slices)]

    async def update(self, text):
        self.text = text
        if not within_interval(self.chat_id):
            await self._force_update(self.text)

    async def finalize(self):
        await self._force_update(self.text)

def save_photo(photo_blob):
    h = hashlib.sha256(photo_blob).hexdigest()
    dir = f'photos/{h[:2]}/{h[2:4]}'
    path = f'{dir}/{h}.png'
    if not os.path.isfile(path):
        os.makedirs(dir, exist_ok=True)
        with open(path, 'wb') as f:
            f.write(photo_blob)
    return h

def load_photo_filename(h):
    dir = f'photos/{h[:2]}/{h[2:4]}'
    path = f'{dir}/{h}.png'
    return path

dalle_usage = """Usage: /dalle [OPTIONS] PROMPT

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
        await send_message(chat_id, f'[!] Error: {error}\n\n{dalle_usage}', msg_id)
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
            logging.info('Response: chat_id=%r, sender_id=%r, msg_id=%r, result=%s', chat_id, sender_id, msg_id, result)
            url = result.data[0].url
            revised_prompt = f"[dall-e-3] {result.data[0].revised_prompt}"
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

gpti_usage = """Usage: /gpti [OPTIONS] PROMPT

Quality:
-h --high (default): high
-m --medium: medium
-l --low: low

Size:
default: auto
-s --square: 1024x1024
-w --landscape: 1536x1024
-p --portrait: 1024x1536

Background:
default: auto
-t --transparent: transparent
-o --opaque: opaque

Example:
/gpti -h -s -o A cute cat

Note: All OPTIONS should appear before the PROMPT.
"""

@only_whitelist
async def gpti(message):
    chat_id = message.chat_id
    sender_id = message.sender_id
    msg_id = message.id
    text = message.message
    logging.info('New message: chat_id=%r, sender_id=%r, msg_id=%r, text=%r', chat_id, sender_id, msg_id, text)

    photo_message = None
    if message.is_reply:
        reply_to_message = await message.get_reply_message()
        if reply_to_message.photo is not None:
            photo_message = reply_to_message
    if message.photo is not None:
        photo_message = message
    photo_blobs = []
    if photo_message is not None:
        if photo_message.grouped_id is not None:
            grouped_id = photo_message.grouped_id
            await asyncio.sleep(3)
            if grouped_id not in albums:
                await send_message(chat_id, f'[!] Error: Historical photo album cannot be accessed by bot. Please forward or resend.', msg_id)
                return
            for msg in sorted(albums[grouped_id], key=lambda m: m.id):
                photo_blobs.append(await msg.download_media(bytes))
        else:
            photo_blobs = [await photo_message.download_media(bytes)]
    photo_hashes = []
    if photo_blobs:
        for photo_blob in photo_blobs:
            photo_hashes.append(save_photo(photo_blob))
        logging.info('Photos: chat_id=%r, sender_id=%r, msg_id=%r, photos(%r)=%r', chat_id, sender_id, msg_id, len(photo_hashes), photo_hashes)

    params = text.split()
    prompt = []
    quality = None
    size = None
    background = None
    error = None
    is_options = True
    for param in params[1:]:
        if param.startswith('-') and is_options:
            if param in ['-h', '--high']:
                if quality is None:
                    quality = 'high'
                else:
                    error = 'More than one Quality options found'
            elif param in ['-m', '--medium']:
                if quality is None:
                    quality = 'medium'
                else:
                    error = 'More than one Quality options found'
            elif param in ['-l', '--low']:
                if quality is None:
                    quality = 'low'
                else:
                    error = 'More than one Quality options found'
            elif param in ['-s', '--square']:
                if size is None:
                    size = '1024x1024'
                else:
                    error = 'More than one Size options found'
            elif param in ['-w', '--landscape']:
                if size is None:
                    size = '1536x1024'
                else:
                    error = 'More than one Size options found'
            elif param in ['-p', '--portrait']:
                if size is None:
                    size = '1024x1536'
                else:
                    error = 'More than one Size options found'
            elif param in ['-t', '--transparent']:
                if background is None:
                    background = 'transparent'
                else:
                    error = 'More than one Background options found'
            elif param in ['-o', '--opaque']:
                if background is None:
                    background = 'opaque'
                else:
                    error = 'More than one Background options found'
            else:
                error = f'Unknown option: {param}'
        else:
            prompt.append(param)
            is_options = False
    if quality is None:
        quality = 'high'
    if size is None:
        size = 'auto'
    if photo_blobs and background is not None:
        error = 'Background is not supported when editing photos'
    if background is None:
        background = 'auto'
    prompt = ' '.join(prompt)
    if not prompt:
        error = 'Prompt is empty'
    if error is not None:
        await send_message(chat_id, f'[!] Error: {error}\n\n{gpti_usage}', msg_id)
        return

    params = dict(
        model='gpt-image-1',
        prompt=prompt,
        background=background,
        moderation='low',
        quality=quality,
        size=size,
    )
    logging.info('Using gpt-image-1 API: chat_id=%r, sender_id=%r, msg_id=%r, params=%s', chat_id, sender_id, msg_id, params)
    def remove_blob(result):
        result_ = result.model_copy(deep=True)
        if hasattr(result_, 'data'):
            for item in result_.data:
                if hasattr(item, 'b64_json'):
                    item.b64_json = '...'
        return result_
    async with bot.action(chat_id, 'typing'):
        try:
            if photo_hashes:
                with contextlib.ExitStack() as stack:
                    files = [stack.enter_context(open(load_photo_filename(h), 'rb')) for h in photo_hashes]
                    params['image'] = files
                    del params['background']
                    del params['moderation']
                    result = await aclient.images.edit(**params)
            else:
                result = await aclient.images.generate(**params)
            logging.info('Response: chat_id=%r, sender_id=%r, msg_id=%r, result=%s', chat_id, sender_id, msg_id, remove_blob(result))
            image_bytes = base64.b64decode(result.data[0].b64_json)
            input_tokens = result.usage.input_tokens
            image_tokens = result.usage.input_tokens_details.image_tokens
            text_tokens = result.usage.input_tokens_details.text_tokens
            output_tokens = result.usage.output_tokens
            cost = 5e-6 * text_tokens + 10e-6 * image_tokens + 40e-6 * output_tokens
            usage_text = '[gpt-image-1]\n'
            if photo_hashes:
                usage_text += f'Input images: {len(photo_hashes)}\n'
            if input_tokens:
                usage_text += f'Input tokens: {input_tokens}\n'
            if image_tokens:
                usage_text += f'Image tokens: {image_tokens}\n'
            if text_tokens:
                usage_text += f'Text tokens: {text_tokens}\n'
            if output_tokens:
                usage_text += f'Output tokens: {output_tokens}\n'
            if cost:
                usage_text += f'Cost: ${cost:.2f}\n'
            dirname = f'images/{chat_id}'.replace('-', '_')
            filename = f'GPT_{int(time.time())}_{msg_id}.png'
            path = f'{dirname}/{filename}'
            os.makedirs(dirname, exist_ok=True)
            with open(path, 'w+b') as f:
                f.write(image_bytes)
            await send_photo(chat_id, usage_text, msg_id, path)

        except Exception as e:
            logging.exception('Error (chat_id=%r, msg_id=%r): %s', chat_id, msg_id, e)
            await send_message(chat_id, f'[!] Error: {traceback.format_exception_only(e)[-1].strip()}', msg_id)
            return

flux_usage = """Usage: /flux [OPTIONS] PROMPT

PROMPT must be English only.

Model:
--pro (default): FLUX1.1 pro
--pro1: FLUX.1 pro
--dev: FLUX.1 dev

Size:
--landscape-4-3 (default)
--square-hd
--square
--portrait-4-3
--portrait-16-9
--landscape-16-9

Example:
/flux --square-hd A cute cat

Note: All OPTIONS should appear before the PROMPT.
"""

@only_whitelist
async def flux(message):
    chat_id = message.chat_id
    sender_id = message.sender_id
    msg_id = message.id
    text = message.message
    logging.info('New message: chat_id=%r, sender_id=%r, msg_id=%r, text=%r', chat_id, sender_id, msg_id, text)
    params = text.split()
    prompt = []
    size = None
    model = None
    error = None
    is_options = True
    for param in params[1:]:
        if param.startswith('-') and is_options:
            if param in ['--landscape-4-3']:
                if size is None:
                    size = 'landscape_4_3'
                else:
                    error = 'More than one Size options found'
            elif param in ['--square-hd']:
                if size is None:
                    size = 'square_hd'
                else:
                    error = 'More than one Size options found'
            elif param in ['--square']:
                if size is None:
                    size = 'square'
                else:
                    error = 'More than one Size options found'
            elif param in ['--portrait-4-3']:
                if size is None:
                    size = 'portrait_4_3'
                else:
                    error = 'More than one Size options found'
            elif param in ['--portrait-16-9']:
                if size is None:
                    size = 'portrait_16_9'
                else:
                    error = 'More than one Size options found'
            elif param in ['--landscape-16-9']:
                if size is None:
                    size = 'landscape_16_9'
                else:
                    error = 'More than one Size options found'
            elif param in ['--pro']:
                if model is None:
                    model = 'fal-ai/flux-pro/v1.1'
                else:
                    error = 'More than one Model options found'
            elif param in ['--pro1']:
                if model is None:
                    model = 'fal-ai/flux-pro'
                else:
                    error = 'More than one Model options found'
            elif param in ['--dev']:
                if model is None:
                    model = 'fal-ai/flux/dev'
                else:
                    error = 'More than one Model options found'
            else:
                error = f'Unknown option: {param}'
        else:
            prompt.append(param)
            is_options = False
    if size is None:
        size = 'landscape_4_3'
    if model is None:
        model = 'fal-ai/flux-pro/v1.1'
    prompt = ' '.join(prompt)
    if not prompt:
        error = 'Prompt is empty'
    if any(ord(c) > 127 for c in prompt):
        error = 'Prompt is not English only'
    if error is not None:
        await send_message(chat_id, f'[!] Error: {error}\n\n{flux_usage}', msg_id)
        return

    params = dict(
        prompt=prompt,
        image_size=size,
    )
    if model == 'fal-ai/flux-pro':
        params['safety_tolerance'] = 6
    elif model == 'fal-ai/flux/dev':
        params['enable_safety_checker'] = False
    elif model == 'fal-ai/flux-pro/v1.1':
        params['safety_tolerance'] = 6
        params['enable_safety_checker'] = False

    logging.info('Using FLUX API: chat_id=%r, sender_id=%r, msg_id=%r, params=%s', chat_id, sender_id, msg_id, params)
    async with bot.action(chat_id, 'typing'):
        try:
            handler = await fal_client.submit_async(
                model,
                arguments=params,
            )

            log_index = 0
            async for event in handler.iter_events(with_logs=True):
                if isinstance(event, fal_client.InProgress):
                    new_logs = event.logs[log_index:]
                    for log in new_logs:
                        logging.info('FLUX API LOG: %s', log["message"])
                    log_index = len(event.logs)

            result = await handler.get()
            logging.info('Response: chat_id=%r, sender_id=%r, msg_id=%r, result=%s', chat_id, sender_id, msg_id, result)
            url = result['images'][0]['url']
            revised_prompt = f"[{model}] {result['prompt']}"
            download_link = f'<a href="{url}">Download</a>'
            caption = f'{download_link}\n{html.escape(revised_prompt)}'
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as session:
                async with session.get(url) as response:
                    response.raise_for_status()
                    image = await response.read()

            dirname = f'images/{chat_id}'.replace('-', '_')
            filename = f'FLUX_{int(time.time())}_{msg_id}.png'
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

qwen_usage = """Usage: /qwen [OPTIONS] PROMPT

Size:
--landscape-4-3 (default)
--square
--portrait-4-3
--portrait-16-9
--landscape-16-9

Options:
--fast: Run faster predictions with additional optimizations
--enhance: Enhance the prompt with positive magic

Example:
/qwen --square A cute cat

Note: All OPTIONS should appear before the PROMPT.
"""

@only_whitelist
async def qwen(message):
    chat_id = message.chat_id
    sender_id = message.sender_id
    msg_id = message.id
    text = message.message
    logging.info('New message: chat_id=%r, sender_id=%r, msg_id=%r, text=%r', chat_id, sender_id, msg_id, text)
    params = text.split()
    prompt = []
    size = None
    model = None
    use_fast = False
    use_enhance = False
    error = None
    is_options = True
    for param in params[1:]:
        if param.startswith('-') and is_options:
            if param in ['--landscape-4-3']:
                if size is None:
                    size = '4:3'
                else:
                    error = 'More than one Size options found'
            elif param in ['--square']:
                if size is None:
                    size = '1:1'
                else:
                    error = 'More than one Size options found'
            elif param in ['--portrait-4-3']:
                if size is None:
                    size = '3:4'
                else:
                    error = 'More than one Size options found'
            elif param in ['--portrait-16-9']:
                if size is None:
                    size = '9:16'
                else:
                    error = 'More than one Size options found'
            elif param in ['--landscape-16-9']:
                if size is None:
                    size = '16:9'
                else:
                    error = 'More than one Size options found'
            elif param in ['--fast']:
                use_fast = True
            elif param in ['--enhance']:
                use_enhance = True
            else:
                error = f'Unknown option: {param}'
        else:
            prompt.append(param)
            is_options = False
    if size is None:
        size = '4:3'
    if model is None:
        model = 'qwen/qwen-image'
    prompt = ' '.join(prompt)
    if not prompt:
        error = 'Prompt is empty'
    if error is not None:
        await send_message(chat_id, f'[!] Error: {error}\n\n{qwen_usage}', msg_id)
        return

    params = dict(
        prompt=prompt,
        go_fast=use_fast,
        aspect_ratio=size,
        output_format='png',
        enhance_prompt=use_enhance,
        disable_safety_checker=True,
    )

    logging.info('Using Replicate API: chat_id=%r, sender_id=%r, msg_id=%r, params=%s', chat_id, sender_id, msg_id, params)
    async with bot.action(chat_id, 'typing'):
        try:
            result = await replicate.predictions.async_create(
                model=model,
                input=params,
            )
            await result.async_wait()
            logging.info('Response: chat_id=%r, sender_id=%r, msg_id=%r, result=%s', chat_id, sender_id, msg_id, result)
            url = result.output[0]
            revised_prompt = f"[{model}] {prompt}"
            download_link = f'<a href="{url}">Download</a>'
            caption = f'{download_link}\n{html.escape(revised_prompt)}'
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as session:
                async with session.get(url) as response:
                    response.raise_for_status()
                    image = await response.read()

            dirname = f'images/{chat_id}'.replace('-', '_')
            filename = f'Qwen_{int(time.time())}_{msg_id}.png'
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

imagen_usage = """Usage: /imagen [OPTIONS] PROMPT

Model:
--ultra (default): Imagen 4 Ultra
--standard: Imagen 4

Size:
--square (default)
--landscape-4-3
--portrait-4-3
--portrait-16-9
--landscape-16-9

Example:
/imagen A cute cat

Note: All OPTIONS should appear before the PROMPT.
"""

@only_whitelist
async def imagen(message):
    chat_id = message.chat_id
    sender_id = message.sender_id
    msg_id = message.id
    text = message.message
    logging.info('New message: chat_id=%r, sender_id=%r, msg_id=%r, text=%r', chat_id, sender_id, msg_id, text)
    params = text.split()
    prompt = []
    size = None
    model = None
    error = None
    is_options = True
    for param in params[1:]:
        if param.startswith('-') and is_options:
            if param in ['--landscape-4-3']:
                if size is None:
                    size = '4:3'
                else:
                    error = 'More than one Size options found'
            elif param in ['--square']:
                if size is None:
                    size = '1:1'
                else:
                    error = 'More than one Size options found'
            elif param in ['--portrait-4-3']:
                if size is None:
                    size = '3:4'
                else:
                    error = 'More than one Size options found'
            elif param in ['--portrait-16-9']:
                if size is None:
                    size = '9:16'
                else:
                    error = 'More than one Size options found'
            elif param in ['--landscape-16-9']:
                if size is None:
                    size = '16:9'
                else:
                    error = 'More than one Size options found'
            elif param in ['--ultra']:
                if model is None:
                    model = 'models/imagen-4.0-ultra-generate-001'
                else:
                    error = 'More than one Model options found'
            elif param in ['--standard']:
                if model is None:
                    model = 'models/imagen-4.0-generate-001'
                else:
                    error = 'More than one Model options found'
            else:
                error = f'Unknown option: {param}'
        else:
            prompt.append(param)
            is_options = False
    if size is None:
        size = '1:1'
    if model is None:
        model = 'models/imagen-4.0-ultra-generate-001'
    prompt = ' '.join(prompt)
    if not prompt:
        error = 'Prompt is empty'
    if error is not None:
        await send_message(chat_id, f'[!] Error: {error}\n\n{imagen_usage}', msg_id)
        return

    params = dict(
        model=model,
        prompt=prompt,
        config=dict(
            number_of_images=1,
            person_generation="ALLOW_ADULT",
            aspect_ratio=size,
        ),
    )
    logging.info('Using Imagen API: chat_id=%r, sender_id=%r, msg_id=%r, params=%s', chat_id, sender_id, msg_id, params)
    async with bot.action(chat_id, 'typing'):
        try:
            client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
            result = client.models.generate_images(**params)
            def remove_response_blobs(response):
                if response.generated_images is not None and len(response.generated_images) == 1:
                    obj = response.generated_images[0]
                    if obj.image is not None and obj.image.image_bytes is not None:
                            response_new = response.model_copy(deep=True)
                            response_new.generated_images[0].image.image_bytes = b'...'
                            return response_new
                return response
            logging.info('Response: chat_id=%r, sender_id=%r, msg_id=%r, result=%s', chat_id, sender_id, msg_id, remove_response_blobs(result))
            if result.generated_images is None or len(result.generated_images) != 1:
                raise ValueError('No generated images found in response')
            image = result.generated_images[0].image.image_bytes
            prompt = f'[{model}] {prompt}'
            caption = f'{html.escape(prompt)}'
            dirname = f'images/{chat_id}'.replace('-', '_')
            filename = f'IMAGEN_{int(time.time())}_{msg_id}.png'
            path = f'{dirname}/{filename}'
            os.makedirs(dirname, exist_ok=True)
            with open(path, 'w+b') as f:
                f.write(image)
            try:
                await send_photo(chat_id, caption, msg_id, path)
            except errors.rpcerrorlist.MediaCaptionTooLongError:
                photo_msg_id = await send_photo(chat_id, '', msg_id, path)
                await send_message(chat_id, prompt, photo_msg_id)

        except Exception as e:
            logging.exception('Error (chat_id=%r, msg_id=%r): %s', chat_id, msg_id, e)
            await send_message(chat_id, f'[!] Error: {traceback.format_exception_only(e)[-1].strip()}', msg_id)
            return

seedream_usage = """Usage: /seed PROMPT

Example:
/seed A cute cat
"""

@only_whitelist
async def seedream(message):
    chat_id = message.chat_id
    sender_id = message.sender_id
    msg_id = message.id
    text = message.message
    logging.info('New message: chat_id=%r, sender_id=%r, msg_id=%r, text=%r', chat_id, sender_id, msg_id, text)

    photo_message = None
    if message.is_reply:
        reply_to_message = await message.get_reply_message()
        if reply_to_message.photo is not None:
            photo_message = reply_to_message
    if message.photo is not None:
        photo_message = message
    photo_blobs = []
    if photo_message is not None:
        if photo_message.grouped_id is not None:
            grouped_id = photo_message.grouped_id
            await asyncio.sleep(3)
            if grouped_id not in albums:
                await send_message(chat_id, f'[!] Error: Historical photo album cannot be accessed by bot. Please forward or resend.', msg_id)
                return
            for msg in sorted(albums[grouped_id], key=lambda m: m.id):
                photo_blobs.append(await msg.download_media(bytes))
        else:
            photo_blobs = [await photo_message.download_media(bytes)]
    photo_hashes = []
    if photo_blobs:
        for photo_blob in photo_blobs:
            photo_hashes.append(save_photo(photo_blob))
        logging.info('Photos: chat_id=%r, sender_id=%r, msg_id=%r, photos(%r)=%r', chat_id, sender_id, msg_id, len(photo_hashes), photo_hashes)

    params = text.split()
    prompt = []
    error = None
    is_options = True
    for param in params[1:]:
        if param.startswith('-') and is_options:
            error = f'Unknown option: {param}'
        else:
            prompt.append(param)
            is_options = False
    prompt = ' '.join(prompt)
    if not prompt:
        error = 'Prompt is empty'
    if error is not None:
        await send_message(chat_id, f'[!] Error: {error}\n\n{seedream_usage}', msg_id)
        return

    params = dict(
        model='doubao-seedream-4-0-250828',
        prompt=prompt,
        size='4K',
        response_format='b64_json',
        stream=True,
        extra_body={
            'watermark': False,
            'sequential_image_generation': 'auto',
        },
    )
    logging.info('Using seedream API: chat_id=%r, sender_id=%r, msg_id=%r, params=%s', chat_id, sender_id, msg_id, params)
    def remove_blob(event):
        event_ = event.model_copy(deep=True)
        if hasattr(event_, 'b64_json') and event_.b64_json is not None:
            event_.b64_json = '...'
        return event_
    client = openai.AsyncOpenAI(
        base_url="https://ark.cn-beijing.volces.com/api/v3",
        api_key=os.environ.get("ARK_API_KEY"),
    )
    async with bot.action(chat_id, 'typing'):
        try:
            if photo_hashes:
                params['extra_body']['image'] = []
                for h in photo_hashes:
                    with open(load_photo_filename(h), 'rb') as f:
                        params['extra_body']['image'].append('data:image/png;base64,' + base64.b64encode(f.read()).decode())
            stream = await client.images.generate(**params)
            image_index = 0
            reply_to_message_id = msg_id
            async for event in stream:
                logging.info('Response: chat_id=%r, sender_id=%r, msg_id=%r, result=%s', chat_id, sender_id, msg_id, remove_blob(event))
                if event is None:
                    continue
                elif event.type == "image_generation.partial_succeeded":
                    if event.b64_json is not None:
                        image_bytes = base64.b64decode(event.b64_json)
                        dirname = f'images/{chat_id}'.replace('-', '_')
                        filename = f'seedream_{int(time.time())}_{msg_id}_{image_index}.png'
                        image_index += 1
                        path = f'{dirname}/{filename}'
                        os.makedirs(dirname, exist_ok=True)
                        with open(path, 'w+b') as f:
                            f.write(image_bytes)
                        caption = f'[doubao-seedream-4-0-250828]\nimage_index={event.image_index}\nsize={event.size}'
                        reply_to_message_id = await send_photo(chat_id, caption, reply_to_message_id, path)
        except Exception as e:
            logging.exception('Error (chat_id=%r, msg_id=%r): %s', chat_id, msg_id, e)
            await send_message(chat_id, f'[!] Error: {traceback.format_exception_only(e)[-1].strip()}', msg_id)
            return

sora_usage = """Usage: /sora [OPTIONS] PROMPT

Model:
-2 --sora2 (default): Sora 2
-p --sora2pro: Sora 2 Pro

Size:
-h --portrait (default): 720x1280
-w --landscape: 1280x720
-H --portrait-hd: 1024x1792
-W --landscape-hd: 1792x1024

Seconds:
-4 (default): 4 seconds
-8: 8 seconds
-12 --12: 12 seconds

Example:
/sora -p -W -4 A cute cat

Note: All OPTIONS should appear before the PROMPT.
"""

@only_whitelist
async def sora(message):
    chat_id = message.chat_id
    sender_id = message.sender_id
    msg_id = message.id
    text = message.message
    logging.info('New message: chat_id=%r, sender_id=%r, msg_id=%r, text=%r', chat_id, sender_id, msg_id, text)

    photo_message = None
    remix_video_id = None
    if message.is_reply:
        reply_to_message = await message.get_reply_message()
        if repr((chat_id, reply_to_message.id)) in db:
            remix_video_id = db[repr((chat_id, reply_to_message.id))]
        if reply_to_message.photo is not None:
            photo_message = reply_to_message
    if message.photo is not None:
        photo_message = message
    photo_blobs = []
    if photo_message is not None:
        if photo_message.grouped_id is not None:
            grouped_id = photo_message.grouped_id
            await asyncio.sleep(3)
            if grouped_id not in albums:
                await send_message(chat_id, f'[!] Error: Historical photo album cannot be accessed by bot. Please forward or resend.', msg_id)
                return
            for msg in sorted(albums[grouped_id], key=lambda m: m.id):
                photo_blobs.append(await msg.download_media(bytes))
        else:
            photo_blobs = [await photo_message.download_media(bytes)]
    photo_hashes = []
    if photo_blobs:
        for photo_blob in photo_blobs:
            photo_hashes.append(save_photo(photo_blob))
        logging.info('Photos: chat_id=%r, sender_id=%r, msg_id=%r, photos(%r)=%r', chat_id, sender_id, msg_id, len(photo_hashes), photo_hashes)

    if len(photo_hashes) > 1:
        await send_message(chat_id, f'[!] Error: Only one photo is allowed for /sora command.', msg_id)
        return

    params = text.split()
    prompt = []
    model = None
    size = None
    seconds = None
    error = None
    is_options = True
    for param in params[1:]:
        if param.startswith('-') and is_options:
            if param in ['-2', '--sora2']:
                if model is None:
                    model = 'sora-2'
                else:
                    error = 'More than one Model options found'
            elif param in ['-p', '--sora2pro']:
                if model is None:
                    model = 'sora-2-pro'
                else:
                    error = 'More than one Model options found'
            elif param in ['-h', '--portrait']:
                if size is None:
                    size = '720x1280'
                else:
                    error = 'More than one Size options found'
            elif param in ['-w', '--landscape']:
                if size is None:
                    size = '1280x720'
                else:
                    error = 'More than one Size options found'
            elif param in ['-H', '--portrait-hd']:
                if size is None:
                    size = '1024x1792'
                else:
                    error = 'More than one Size options found'
            elif param in ['-W', '--landscape-hd']:
                if size is None:
                    size = '1792x1024'
                else:
                    error = 'More than one Size options found'
            elif param in ['-4']:
                if seconds is None:
                    seconds = 4
                else:
                    error = 'More than one Seconds options found'
            elif param in ['-8']:
                if seconds is None:
                    seconds = 8
                else:
                    error = 'More than one Seconds options found'
            elif param in ['-12', '--12']:
                if seconds is None:
                    seconds = 12
                else:
                    error = 'More than one Seconds options found'
            else:
                error = f'Unknown option: {param}'
        else:
            prompt.append(param)
            is_options = False
    if remix_video_id is not None:
        if model is not None:
            error = 'Model option is not allowed when remixing video'
        if size is not None:
            error = 'Size option is not allowed when remixing video'
        if seconds is not None:
            error = 'Seconds option is not allowed when remixing video'
        if photo_hashes:
            error = 'Photo is not allowed when remixing video'
    if model is None:
        model = 'sora-2'
    if size is None:
        size = '720x1280'
    if seconds is None:
        seconds = 4
    prompt = ' '.join(prompt)
    if not prompt:
        error = 'Prompt is empty'
    if error is not None:
        await send_message(chat_id, f'[!] Error: {error}\n\n{sora_usage}', msg_id)
        return

    params = dict(
        model=model,
        prompt=prompt,
        size=size,
        seconds=str(seconds),
    )
    logging.info('Using sora API: chat_id=%r, sender_id=%r, msg_id=%r, params=%s', chat_id, sender_id, msg_id, params)

    async with bot.action(chat_id, 'typing'):
        try:
            if remix_video_id is not None:
                video = await aclient.videos.remix(video_id=remix_video_id, prompt=prompt)
            elif photo_hashes:
                with open(load_photo_filename(photo_hashes[0]), 'rb') as f:
                    params['input_reference'] = f
                    video = await aclient.videos.create(**params)
            else:
                video = await aclient.videos.create(**params)
            logging.info('Response: chat_id=%r, sender_id=%r, msg_id=%r, result=%s', chat_id, sender_id, msg_id, video)

            async with BotReplyMessages(chat_id, msg_id, f'[{model}] ') as replymsgs:
                retry_count = 0
                while video.status in ["in_progress", "queued"]:
                    try:
                        video = await aclient.videos.retrieve(video.id)
                        retry_count = 0
                    except Exception as e:
                        logging.exception('Video retrieval error (chat_id=%r, msg_id=%r)', chat_id, msg_id)
                        retry_count += 1
                        if retry_count >= 10:
                            await send_message(chat_id, f'[!] Error: Video retrieval failed after 10 attempts', msg_id)
                            return
                        await asyncio.sleep(2)
                        continue

                    logging.info('Response: chat_id=%r, sender_id=%r, msg_id=%r, result=%s', chat_id, sender_id, msg_id, video)
                    status_text = "Queued" if video.status == "queued" else "Processing"
                    if video.progress is not None:
                        status_text += f" {video.progress / 100:.1%}"
                    await replymsgs.update(status_text)
                    await asyncio.sleep(2)

                if video.status == "failed":
                    message = getattr(getattr(video, "error", None), "message", "Video generation failed")
                    await replymsgs.update(f'[!] Error: {message}')
                    return

                dirname = f'images/{chat_id}'.replace('-', '_')
                filename = f'sora_{int(time.time())}_{msg_id}.mp4'
                path = f'{dirname}/{filename}'
                os.makedirs(dirname, exist_ok=True)
                content = await aclient.videos.download_content(video.id, variant="video")
                content.write_to_file(path)

                if model == 'sora-2':
                    price_per_second = 0.1
                elif size in ['720x1280', '1280x720']:
                    price_per_second = 0.3
                else:
                    price_per_second = 0.5
                cost = price_per_second * seconds
                caption = f'[{model}] {prompt}\n\nSize: {size}\nSeconds: {seconds}\nCost: ${cost:.2f}'
                try:
                    result_msg_id = await send_photo(chat_id, caption, msg_id, path)
                except errors.rpcerrorlist.MediaCaptionTooLongError:
                    result_msg_id = await send_photo(chat_id, '', msg_id, path)
                    await send_message(chat_id, caption, result_msg_id)
                db[repr((chat_id, result_msg_id))] = video.id
                await replymsgs.update(f"Completed")

        except Exception as e:
            logging.exception('Error (chat_id=%r, msg_id=%r): %s', chat_id, msg_id, e)
            await send_message(chat_id, f'[!] Error: {traceback.format_exception_only(e)[-1].strip()}', msg_id)
            return

async def ping(message):
    await send_message(message.chat_id, f'chat_id={message.chat_id} user_id={message.sender_id} is_whitelisted={is_whitelist(message.chat_id)}', message.id)

async def main():
    global bot_id, pending_reply_manager, db, bot, albums

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
        albums = defaultdict(list)
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
                if event.message.grouped_id is not None:
                    albums[event.message.grouped_id].append(event.message)
                text = event.message.message
                if text == '/ping' or text == f'/ping@{me.username}':
                    await ping(event.message)
                elif text == '/dalle' or text.startswith('/dalle ') or \
                    text == f'/dalle@{me.username}' or text.startswith(f'/dalle@{me.username} '):
                    await dalle(event.message)
                elif text == '/gpti' or text.startswith('/gpti ') or \
                    text == f'/gpti@{me.username}' or text.startswith(f'/gpti@{me.username} '):
                    await gpti(event.message)
                elif text == '/flux' or text.startswith('/flux ') or \
                    text == f'/flux@{me.username}' or text.startswith(f'/flux@{me.username} '):
                    await flux(event.message)
                elif text == '/qwen' or text.startswith('/qwen ') or \
                    text == f'/qwen@{me.username}' or text.startswith(f'/qwen@{me.username} '):
                    await qwen(event.message)
                elif text == '/imagen' or text.startswith('/imagen ') or \
                    text == f'/imagen@{me.username}' or text.startswith(f'/imagen@{me.username} '):
                    await imagen(event.message)
                elif text == '/seed' or text.startswith('/seed ') or \
                    text == f'/seed@{me.username}' or text.startswith(f'/seed@{me.username} '):
                    await seedream(event.message)
                elif text == '/sora' or text.startswith('/sora ') or \
                    text == f'/sora@{me.username}' or text.startswith(f'/sora@{me.username} '):
                    await sora(event.message)
                elif text == '/add_whitelist' or text == f'/add_whitelist@{me.username}':
                    await add_whitelist_handler(event.message)
                elif text == '/del_whitelist' or text == f'/del_whitelist@{me.username}':
                    await del_whitelist_handler(event.message)
                elif text == '/get_whitelist' or text == f'/get_whitelist@{me.username}':
                    await get_whitelist_handler(event.message)
            assert await bot(functions.bots.ResetBotCommandsRequest(
                scope=types.BotCommandScopeDefault(),
                lang_code='en',
            ))
            assert await bot(functions.bots.SetBotCommandsRequest(
                scope=types.BotCommandScopeDefault(),
                lang_code='',
                commands=[types.BotCommand(command, description) for command, description in [
                    ('ping', 'Test bot connectivity'),
                    ('add_whitelist', 'Add this group to whitelist (only admin)'),
                    ('del_whitelist', 'Delete this group from whitelist (only admin)'),
                    ('get_whitelist', 'List groups in whitelist (only admin)'),
                    ('dalle', 'Creates an image given a prompt via DALL-E'),
                    ('gpti', 'Creates an image given a prompt via gpt-image-1'),
                    ('flux', 'Creates an image given a prompt via FLUX.1'),
                    ('qwen', 'Creates an image given a prompt via qwen-image'),
                    ('imagen', 'Creates an image given a prompt via Imagen'),
                    ('seed', 'Creates an image given a prompt via Seedream'),
                    ('sora', 'Creates a video given a prompt via Sora'),
                ]]
            ))
            await bot.run_until_disconnected()

asyncio.run(main())
