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

        @bot.on('PART')
        async def on_part(**kwargs):
            # kwargs keys: nick, user, host, channel, message
            # TODO: make the system message configurable
            nick = kwargs.get('nick', '[Unknown nick]')
            reason = kwargs.get('message')
            if reason:
                reason = f' ({reason})'
            await self.put_sys_msg_if_active(f'<IRC: {nick} 已退出本频道{reason}>', **kwargs)

        @bot.on('QUIT')
        async def on_quit(**kwargs):
            # kwargs keys: nick, user, host, message
            nick = kwargs.get('nick', '[Unknown nick]')
            reason = kwargs.get('message')
            if reason:
                reason = f' ({reason})'
            await self.put_sys_msg_if_active(f'<IRC: {nick} 已离开 IRC{reason}>', **kwargs)

        @bot.on('NICK')
        async def on_nick(**kwargs):
            # kwargs keys: nick, user, host, new_nick
            nick = kwargs.get('nick', '[Unknown nick]')
            new_nick = kwargs.get('new_nick', '[Unknown new nick]')
            await self.put_sys_msg_if_active(f'<IRC: {nick} 已更名为 {new_nick}>', **kwargs)

        # TODO: add kick and kill events after https://github.com/numberoverzero/bottom/issues/43 is fixed

    async def put_sys_msg_if_active(self, message_text: str, **kwargs) -> None:
        host = kwargs.get('host')
        if not host:
            return
        groups = await db.get_active_groups_on_platform(host)
        if not groups:
            return
        if kwargs.get('channel'):
            channel = kwargs.get('channel')
            # Check if user is active in this provided channel
            if ('irc/' + channel) not in groups:
                return
            # If active, only send system message to this single channel
            await message_queue.put(await Message.create({
                'system': True,
                'text': message_text,
                # Record user info in system message for future commands against it like /ircban
                'nick': kwargs.get('nick', ''),
                'host': host,
                'group': channel,
                'created_at': datetime.utcnow(),
            }))
            return

        # Broadcast system message to all channels the user is active in
        for group in groups:
            # The results from db have platform prefix, but we don't need it to construct a Message object
            _, group_id = group.split('/', 1)
            await message_queue.put(await Message.create({
                'system': True,
                'text': message_text,
                # Record user info in system message for future commands against it like /ircban
                'nick': kwargs.get('nick', ''),
                'host': host,
                'group': group_id,
                'created_at': datetime.utcnow(),
            }))

    def construct_files(self, _):
        # Cannot upload files to IRC so leave empty
        return
