import asyncio
import bottom
import logging
from config import Config
from message import Message
from motor.motor_asyncio import AsyncIOMotorClient
from telethon import TelegramClient, events

# Load configs from file
config = Config('bridge.yaml')

logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    filename=config.get_nowait('Logging', 'path', default=''),
    filemode='w'
)
logger = logging.getLogger(__name__)
try:
    logger.setLevel(config.get_nowait('Logging', 'level', default='INFO'))
except ValueError:
    # Unknown level, use default INFO level
    pass

# Get mappings
bridge_cfg: 'list[list[str]]' = config.get_nowait('Bridge', default=[])
bridge_map: 'dict[str, list[str]]' = dict()
for groups in bridge_cfg:
    for group in groups:
        if group in bridge_map:
            logger.warning(f'duplicate mapping in config: {group} - previous mapping will be overwritten')
        # Map each group with other connected groups
        bridge_map[group] = [g for g in groups if g != group]

def get_groups(platform: str):
    """
    Generate all group ids of the given platform, without platform prefix
    """
    for group in bridge_map.keys():
        if group.startswith(platform.lower()):
            yield group.split('/', 1)[1]

irc_bot = bottom.Client(
    host=config.get_nowait('IRC', 'host'),
    port=config.get_nowait('IRC', 'port'),
    ssl=config.get_nowait('IRC', 'ssl')
)
tg_bot = TelegramClient(
    config.get_nowait('Telegram', 'session', default='bridge'),
    config.get_nowait('Telegram', 'api_id'),
    config.get_nowait('Telegram', 'api_hash')
)
tg_bot.start(bot_token=config.get_nowait('Telegram', 'bot_token'))

# Initialize MongoDB client
# mongo_client = AsyncIOMotorClient(config.get_nowait('Mongo', 'uri'))
# db = mongo_client[config.get_nowait('Mongo', 'database_name')]
# messages_collection = db[config.get_nowait('Mongo', 'collection_name')]

# Initialize the asyncio.Queue
message_queue = asyncio.Queue()


@irc_bot.on('CLIENT_CONNECT')
async def connect(**kwargs):
    nick = await config.get('IRC', 'nick')
    irc_bot.send('NICK', nick=nick)
    irc_bot.send('USER', user=nick, realname=await config.get('IRC', 'real_name', default=''))

    # Don't try to join channels until the server has
    # sent the MOTD, or signaled that there's no MOTD.
    done, pending = await asyncio.wait(
        [irc_bot.wait("RPL_ENDOFMOTD"),
         irc_bot.wait("ERR_NOMOTD")],
        return_when=asyncio.FIRST_COMPLETED
    )

    # Login to account and do not join channels before successfully logged in.
    irc_bot.send('PRIVMSG',
                 target='NickServ',
                 message=f'identify {nick} {await config.get("IRC", "password", default="")}')
    await asyncio.sleep(0.5)

    # Cancel whichever waiter's event didn't come in.
    for future in pending:
        future.cancel()

    # Join all channels required to be connected slowly.
    for c in get_groups('IRC'):
        irc_bot.send('JOIN', channel=c)
        await asyncio.sleep(0.2)

@irc_bot.on('PING')
def keepalive(message, **kwargs):
    irc_bot.send('PONG', message=message)

@irc_bot.on('PRIVMSG')
async def message(nick, target, message, **kwargs):
    """
    IRC listener serves as a producer to add new messages to queue.
    """
    # kwargs keys: user, host
    mynick = await config.get('IRC', 'nick')
    # Don't echo self
    if nick == mynick:
        return
    # Must be in bridge map
    if 'irc/' + target not in bridge_map:
        return

    await message_queue.put(await Message.create({
        'group': target,
        'nick': nick,
        'text': message,
    }))

@tg_bot.on(events.NewMessage(incoming=True))
async def telegram_listener(event):
    """
    Telegram listener serves as a producer to add new messages to queue.
    """
    if ('telegram/' + str(event.chat_id)) not in bridge_map:
        return
    logger.info(f'Telegram {event.chat_id} incoming message: ' + str(event.message))
    await message_queue.put(await Message.create(event.message))

async def worker():
    """
    The consumer will fetch messages from queue, insert into database and
    relay the message to other platform.
    """
    while True:
        message = await message_queue.get()
        logger.info(f'outgoing message: ' + str(message))
        from_group = message.from_prefix + message.from_group
        relay_message_text = f'[{message.platform_prefix} - {message.from_nick}] {message.text}'
        for group_to_send in bridge_map.get(from_group, []):
            # TODO: make this a helper function
            platform, group_id = group_to_send.split('/', 1)
            if platform == 'irc':
                # TODO: if message is too long, send in multiple times
                irc_bot.send('PRIVMSG', target=group_id, message=relay_message_text)
            elif platform == 'telegram':
                # Telethon only accept int group ids
                await tg_bot.send_message(int(group_id), relay_message_text, parse_mode='md')
            else:
                # Unknown platform
                pass

async def main():
    await irc_bot.connect()
    await worker()
    await tg_bot.run_until_disconnected()

try:
    tg_bot.loop.run_until_complete(main())
except KeyboardInterrupt:
    exit(0)
