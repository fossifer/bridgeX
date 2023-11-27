import discord
import telethon
from config import Config
from uuid import uuid4

# Maximum number of media files per message, other files will be ignored
# Currently this is the limit by both Telegram (albums) and Discord
MAX_FILES_PER_MSG = 10

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
    
async def get_relay_message(message, target_platform: str) -> str:
    """
    Add a prefix to the message from other platforms to indicate the source.
    """
    bold_char = ''
    if target_platform in {'telegram', 'discord'}:
        bold_char = '**'
    elif target_platform == 'irc':
        bold_char = '\u0002'
    # Show file attributes; only IRC needs to see the url
    file_str = ''
    if target_platform == 'irc':
        file_str = ''.join([file.__str__(with_url=(target_platform == 'irc')) for file in message.files])
    else:
        if len(message.files) > 1:
            # Album, just show general info
            file_str = f'<album: {len(message.files)} files>'
        elif len(message.files) == 1:
            file_str = message.files[0].__str__(with_url=False)
    if file_str: file_str += ' '
    # TODO: make the message format configurable
    return f'[{message.platform_prefix} - {bold_char}{message.from_nick}{bold_char}] {file_str}{message.text}'

async def get_edited_message(old_message: dict, new_message) -> str:
    """
    A text notice to send exclusively for IRC when bridged messages are edited.
    """
    # Truncate old message
    old_text = old_message.get("text", "An unknown message")
    if len(old_text) > 50:
        old_text = old_text[:50] + '...'
    # Old message text <blue,bold>was edited to:</blue,bold> New message text
    return f'\u001E{old_text}\u001E \u0002\u000312was edited to:\u0003\u0002 {new_message.text}'

async def get_deleted_message(old_messages: list[dict]) -> str:
    """
    A text notice to send exclusively for IRC when bridged messages are deleted.
    """
    # Truncate too long messages; only show text of first message
    if not old_messages: return
    old_text = old_messages[0].get("text", "An unknown message")
    if len(old_text) > 200:
        old_text = old_text[:200] + '...'
    verb = 'was'
    more_text = ''
    if len(old_messages) > 1:
        more_text = f' and {len(old_messages) - 1} more messages'
        verb = 'were'
    # <s>Old message text</s> <red,bold>was deleted</red,bold>
    return f'\u001E{old_text}\u001E{more_text} \u0002\u000304{verb} deleted\u0003\u0002'

class File:
    """
    The media files used in message attachments.
    """
    def __init__(self, type: str, path: str, ext: str='', **kwargs):
        self.type = type
        # Location of this file in internal storage
        self.path = path
        # The URL to this file accessable by users (hopefully!)
        self.url = self.path
        self.ext = ext
        if not self.ext:
            # Infer from path
            self.ext = self.path.split('.')[-1] if '.' in self.path else ''
        self.metadata = kwargs

    def __repr__(self) -> str:
        return self.__str__()

    def __str__(self, with_url=True):
        size_str = ''
        if self.metadata.get('width') and self.metadata.get('height'):
            size_str = f'{self.metadata["width"]}x{self.metadata["height"]}'
        if self.metadata.get('size'):
            size_str += f'{", " if size_str else ""}{self.metadata["size"] / 1024.0} KB'
        if self.metadata.get('duration'):
            minutes = int(self.metadata.get('duration') // 60)
            seconds = int(self.metadata.get('duration') - 60 * minutes)
            size_str += f'{", " if size_str else ""}{minutes:02d}:{seconds:02d}'
        if size_str:
            size_str = ': ' + size_str
        url_str = ''
        if with_url:
            url_str = f' {self.url}'
        return f'{self.metadata.get("alt", "")}<{self.type}{size_str}>{url_str} '

    def is_empty(self) -> bool:
        if self.path:
            return False
        return True
    
    def is_image(self) -> bool:
        # Allowed by telegram album
        return self.type in {'image', 'photo', 'video'}

    async def upload(self) -> bool:
        """
        Depending on configuration, upload to a media hosting website or just serve it with a web server.
        """
        # TODO
        self.url = self.path
        return True

    @staticmethod
    def generate_name(dir: str='', ext: str='') -> str:
        """
        Generate a random filename in given directory with given file extension.
        """
        # Normalize arguments
        if dir and not dir.endswith('/'):
            dir += '/'
        if ext and not ext.startswith('.'):
            ext = '.' + ext
        # Generate a random name
        filename = uuid4().hex
        return dir + filename + ext

class Message:
    """
    The universal message object used internally to be sent to all platforms.
    """

    def __init__(self):
        self.deleted = False
        self.text = None
        self.from_user_id = None
        self.from_nick = None
        self.from_group = None
        self.from_message_id = None
        self.platform_prefix = None
        self.created_at = None
        self.edited_at = None
        self.deleted_at = None
        self.files: list[File] = []

    @classmethod
    async def create(cls, message, files=[]):
        """
        Process message objects from each platform to make it universal.

        message: Message object from any platform
        """
        self = cls()
        self.files = files[:MAX_FILES_PER_MSG]
        if type(message) is telethon.tl.types.Message:
            # Telegram message
            self.text = message.text
            # TODO: Make nickname of anonymous sender configurable
            self.from_user_id = message.sender_id
            self.from_nick = (await get_tg_nick(message.sender)) or 'Anonymous'
            self.from_group = 'telegram/' + str(message.chat_id)
            self.from_message_id = message.id
            self.platform_prefix = await config.get('Telegram', 'platform_prefix', default='T')
            self.created_at = message.date
            self.edited_at = message.edit_date
        elif type(message) is discord.Message:
            # Discord message
            self.text = message.content
            self.from_user_id = message.author.id
            if (await config.get('Discord', 'nick_style', default='nickname')) == 'nickname':
                self.from_nick = message.author.display_name
            else:
                self.from_nick = message.author.name
            self.from_group = 'discord/' + str(message.channel.id)
            self.from_message_id = message.id
            self.platform_prefix = await config.get('Discord', 'platform_prefix', default='D')
            self.created_at = message.created_at
            self.edited_at = message.edited_at
        elif type(message) is dict:
            # IRC message
            self.text = message.get('text', '')
            self.from_user_id = message.get('host', '')
            self.from_nick = message.get('nick', '')
            self.from_group = 'irc/' + message.get('group', '')
            self.from_message_id = None  # IRC does not have message ids
            self.platform_prefix = await config.get('IRC', 'platform_prefix', default='I')
            self.created_at = message.get('created_at')
        else:
            raise TypeError('Unknown message type')
        return self

    def __repr__(self) -> str:
        return self.__str__()

    def __str__(self):
        return f'[{self.created_at.isoformat()}] {self.from_group}:{self.from_message_id} -> [{self.platform_prefix} - {self.from_nick} ({self.from_user_id})] {self.text} [{len(self.files)} file(s): {self.files}]'
