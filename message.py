import telethon
from config import Config

config = Config('bridge.yaml')

# Util functions
async def get_tg_nick(sender):
    """
    Get the display name from a telethon sender object, depending on config.
    """
    username = sender.username
    first = sender.first_name + ' ' or ' '
    last = sender.last_name or ''
    first_last = (first + last).strip()
    if (await config.get('Telegram', 'nick_style', default='username')) == 'username':
        # Use username over first_name last_name
        return username or first_last
    else:
        return first_last or username

class Message:
    """
    The universal message object used internally to be sent to all platforms.
    """

    def __init__(self):
        self.text = None
        self.from_nick = None
        self.from_group = None
        self.from_prefix = None
        self.platform_prefix = None

    @classmethod
    async def create(cls, message):
        """
        Process message objects from each platform to make it universal.

        message: Message object from any platform
        """
        self = cls()
        if type(message) is telethon.tl.types.Message:
            # Telegram message
            # Message(id=673, peer_id=PeerChannel(channel_id=1389787734), date=datetime.datetime(2023, 11, 20, 5, 26, 49, tzinfo=datetime.timezone.utc), message='test', out=False, mentioned=False, media_unread=False, silent=False, post=False, from_scheduled=False, legacy=False, edit_hide=False, pinned=False, noforwards=False, invert_media=False, from_id=PeerUser(user_id=314797898), fwd_from=None, via_bot_id=None, reply_to=None, media=None, reply_markup=None, entities=[], views=None, forwards=None, replies=None, edit_date=None, post_author=None, grouped_id=None, reactions=None, restriction_reason=[], ttl_period=None)
            self.text = message.text
            # TODO: anonymous sender
            self.from_nick = (await get_tg_nick(message.sender)) or 'Anonymous'
            self.from_group = str(message.chat_id)
            self.from_prefix = 'telegram/'
            self.platform_prefix = config.get_nowait('Telegram', 'platform_prefix', default='T')
        elif type(message) is dict:
            # IRC message
            self.text = message.get('text', '')
            self.from_nick = message.get('nick', '')
            self.from_group = message.get('group', '')
            self.from_prefix = 'irc/'
            self.platform_prefix = config.get_nowait('IRC', 'platform_prefix', default='I')
        else:
            raise TypeError('Unknown message type')
        return self

    def __str__(self):
        return f'{self.from_prefix}{self.from_group} -> [{self.platform_prefix} - {self.from_nick}] {self.text}'
