import discord
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
            self.text = message.text
            # TODO: anonymous sender
            self.from_nick = (await get_tg_nick(message.sender)) or 'Anonymous'
            self.from_group = str(message.chat_id)
            self.from_prefix = 'telegram/'
            self.platform_prefix = await config.get('Telegram', 'platform_prefix', default='T')
        elif type(message) is discord.Message:
            # Discord message
            self.text = message.content
            if (await config.get('Discord', 'nick_style', default='nickname')) == 'nickname':
                self.from_nick = message.author.display_name
            else:
                self.from_nick = message.author.name
            self.from_group = str(message.channel.id)
            self.from_prefix = 'discord/'
            self.platform_prefix = await config.get('Discord', 'platform_prefix', default='D')
        elif type(message) is dict:
            # IRC message
            self.text = message.get('text', '')
            self.from_nick = message.get('nick', '')
            self.from_group = message.get('group', '')
            self.from_prefix = 'irc/'
            self.platform_prefix = await config.get('IRC', 'platform_prefix', default='I')
        else:
            raise TypeError('Unknown message type')
        return self

    def __str__(self):
        return f'{self.from_prefix}{self.from_group} -> [{self.platform_prefix} - {self.from_nick}] {self.text}'
