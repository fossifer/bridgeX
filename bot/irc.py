import asyncio
import bottom
import logging
from . import utils
from .config import Config
from .database import MongoDB
from datetime import datetime
from .im import MessagingPlatform
from .message import Message

config = Config('bridge.yaml')
logger = logging.getLogger(__name__)
try:
    logger.setLevel(config.get_nowait('Logging', 'level', default='INFO'))
except ValueError:
    # Unknown level, use default INFO level
    pass
db = MongoDB()
msg_collection = db.collection
message_queue = utils.message_queue

class IRC(MessagingPlatform):
    """
    The singleton IRC listener.
    """

    def __init__(self):
        if not hasattr(self, 'bot'):
            self.bot = bottom.Client(
                host=config.get_nowait('IRC', 'host'),
                port=config.get_nowait('IRC', 'port'),
                ssl=config.get_nowait('IRC', 'ssl')
            )
            self.register_listeners()

    def download_media(self, _):
        # There are no attachments on IRC, so do nothing
        return []

    def register_listeners(self):
        bot = self.bot

        @bot.on('CLIENT_CONNECT')
        async def connect(**kwargs):
            nick = await config.get('IRC', 'nick')
            bot.send('NICK', nick=nick)
            bot.send('USER', user=nick, realname=await config.get('IRC', 'real_name', default=''))

            # Don't try to join channels until the server has
            # sent the MOTD, or signaled that there's no MOTD.
            done, pending = await asyncio.wait(
                [bot.wait("RPL_ENDOFMOTD"),
                 bot.wait("ERR_NOMOTD")],
                return_when=asyncio.FIRST_COMPLETED
            )

            # Login to account and do not join channels before successfully logged in.
            bot.send('PRIVMSG',
                         target='NickServ',
                         message=f'identify {nick} {await config.get("IRC", "password", default="")}')
            await asyncio.sleep(0.5)

            # Cancel whichever waiter's event didn't come in.
            for future in pending:
                future.cancel()

            # Join all channels required to be connected slowly.
            async for c in utils.get_groups('IRC'):
                bot.send('JOIN', channel=c)
                await asyncio.sleep(0.2)

        @bot.on('PING')
        def keepalive(message, **kwargs):
            bot.send('PONG', message=message)

        @bot.on('PRIVMSG')
        async def message(nick, target, message, **kwargs):
            """
            IRC listener serves as a producer to add new messages to queue.
            """
            # kwargs keys: user, host
            # IRC does not send us the time when a message is received
            # so just use the current UTC time
            received_at = datetime.utcnow()
            mynick = await config.get('IRC', 'nick')
            # Don't echo self
            if nick == mynick:
                return
            # Must be in bridge map
            if 'irc/' + target not in (await utils.get_bridge_map()):
                return

            await message_queue.put(await Message.create({
                'group': target,
                'host': kwargs.get('host', ''),
                'nick': nick,
                'text': message,
                'created_at': received_at,
            }))

    def construct_files(self, _):
        # Cannot upload files to IRC so leave empty
        return
