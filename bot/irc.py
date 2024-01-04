import asyncio
import pydle
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

class IRCBot(pydle.Client):
    async def on_connect(self):
        # Join all channels in config
        async for c in utils.get_groups('IRC'):
            await self.join(c)
            #await asyncio.sleep(0.2)

    async def on_message(self, target: str, source: str, message: str):
        """
        IRC listener serves as a producer to add new messages to queue.
        """
        # IRC does not send us the time when a message is received
        # so just use the current UTC time
        received_at = datetime.utcnow()
        mynick = await config.get('IRC', 'nick')
        # Don't echo self
        if source == mynick:
            return
        # Must be in bridge map
        if 'irc/' + target not in (await utils.get_bridge_map()):
            return

        await message_queue.put(await Message.create({
            'group': target,
            'host': self.users[source]['hostname'],
            'nick': source,
            'text': message,
            'created_at': received_at,
        }))

    async def on_part(self, channel: str, user: str, message: str='') -> None:
        # TODO: make the system message configurable
        if message:
            message = f' ({message})'
        await self.put_sys_msg_if_active(f'<IRC: {user} 已退出本频道{message}>',
                                         nick=user, channel=channel, host=self.users[user]['hostname'])

    async def on_quit(self, user: str, message: str='') -> None:
        if message:
            message = f' ({message})'
        await self.put_sys_msg_if_active(f'<IRC: {user} 已离开 IRC{message}>',
                                         nick=user, host=self.users[user]['hostname'])

    async def on_kick(self, channel: str, target: str, by: str, reason: str='') -> None:
        if reason:
            reason = f' ({reason})'
        await self.put_sys_msg_if_active(f'<IRC: {target} 已被 {by} 踢出本频道{reason}>',
                                         nick=target, channel=channel, host=self.users[target]['hostname'])

    async def on_kill(self, target: str, by: str, reason: str='') -> None:
        if reason:
            reason = f' ({reason})'
        await self.put_sys_msg_if_active(f'<IRC: {target} 已被 {by} 踢出服务器{reason}>',
                                         nick=target, host=self.users[target]['hostname'])

    async def on_nick_change(self, old: str, new: str) -> None:
        # This is called after parent's on_raw_nick(), so look up hostname with new nick
        await self.put_sys_msg_if_active(f'<IRC: {old} 已更名为 {new}>',
                                         nick=old, host=self.users[new]['hostname'])

    async def my_whois(self, nickname):
        """
        A wrapper of super().whois() that adds a timeout, and won't return empty results.
        """
        # Can be rewritten in 3.11 with asyncio.timeout
        # Discord interaction's time limit is 3 seconds, so timeout should be less than 3
        try:
            ret = await asyncio.wait_for(super().whois(nickname), timeout=2)
            return ret if ret else 'Error: no such user'
        except asyncio.TimeoutError:
            return 'Error: server response timed out (likely an issue in pydle, not the fault of mine!)'

    async def my_whowas(self, nickname):
        try:
            ret = await asyncio.wait_for(super().whowas(nickname), timeout=2)
            return ret if ret else 'Error: no such user'
        except asyncio.TimeoutError:
            return 'Error: server response timed out (likely an issue in pydle, not the fault of mine!)'

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

class IRC(MessagingPlatform):
    """
    The singleton IRC listener.
    """

    def __init__(self):
        if not hasattr(self, 'bot'):
            self.bot = IRCBot(
                nickname=config.get_nowait('IRC', 'nick'),
                sasl_username=config.get_nowait("IRC", "username", default=""),
                sasl_password=config.get_nowait("IRC", "password", default=""),
                sasl_identity='NickServ',
                realname=config.get_nowait('IRC', 'real_name', default='')
            )
            self.register_listeners()

    async def connect(self):
        await self.bot.connect(
            hostname=config.get_nowait('IRC', 'host'),
            port=config.get_nowait('IRC', 'port'),
            tls=config.get_nowait('IRC', 'ssl'),
        )

    def download_media(self, _):
        # There are no attachments on IRC, so do nothing
        return []

    def register_listeners(self):
        # Done in IRCBot class
        pass

    def construct_files(self, _):
        # Cannot upload files to IRC so leave empty
        return
