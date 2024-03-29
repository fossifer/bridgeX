import discord
import logging
from . import utils
from .config import Config
from .database import MongoDB
from .im import MessagingPlatform
from .message import File, Message
from discord import app_commands
from discord.ext.commands import is_owner, Bot, Context

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

class Discord(MessagingPlatform):
    """
    The singleton Discord listener.
    """

    def __init__(self):
        if not hasattr(self, 'bot'):
            intents = discord.Intents.default()
            intents.message_content = True
            #self.bot = discord.Client(intents=intents)
            self.bot = Bot(command_prefix='!', intents=intents)
            self.register_listeners()

    async def download_media(self, message: discord.Message) -> list[File]:
        """
        Helper method to download media from a message and save the media type.

        Return: list of File contains type and path of the media.
        Filenames will be generated randomly to avoid duplicate names.
        """
        ret = []
        for attachment in message.attachments:
            # Content type is like 'image/png' so we can infer file extension from it
            if attachment.content_type:
                media_type, ext = attachment.content_type.split('/', 1)
            else:
                media_type, ext = '', ''
            directory = await config.get('Files', 'path', default='')
            path = File.generate_name(directory, ext)
            try:
                await attachment.save(path)
            except (discord.HTTPException, discord.NotFound) as e:
                logger.warning(f'Downloading discord attachment {attachment} from {message} failed: {e}')
                path = ''
            file = File(media_type, path, ext, filename=attachment.filename,
                        size=attachment.size, height=attachment.height, width=attachment.width,
                        duration=attachment.duration, description=attachment.description,
                        is_spoiler=attachment.is_spoiler(), is_voice=attachment.is_voice_message())
            logger.info(f'Downloaded one Discord file: {file}, path: {path}, is_empty: {file.is_empty()}, metadata: {file.metadata}')
            if not file.is_empty():
                # Only add non-empty files (i.e. download succeeded)
                ret.append(file)
                if not (await file.upload()):
                    logger.info(f'Warning: failed to upload Discord file at {path}')
        logger.info(f'Downloaded Discord files: {ret}')
        return ret

    def register_listeners(self):
        bot = self.bot

        @bot.event
        async def on_ready():
            synced = await bot.tree.sync()
            logging.info(f'Discord: we have logged in as {bot.user}, {len(synced)} commands synced')

        @bot.tree.command(description="列出 IRC 频道所有用户，或查看目标是否在频道中")
        @app_commands.describe(target="要查看是否在线的昵称，可选")
        async def ircnames(interaction: discord.Interaction, target: str=''):
            if ('discord/' + str(interaction.channel.id)) not in (await utils.get_bridge_map()):
                return
            logger.info(f'Discord {interaction.channel.id} incoming /ircnames: ' + str(interaction.message))
            await message_queue.put({
                'action': 'ircnames',
                'target': target,
                'event': interaction,
                'from_group': 'discord/' + str(interaction.channel.id),
            })

        @bot.tree.command(description="查看 IRC 在线用户的 WHOIS 信息")
        @app_commands.describe(target="要查看的昵称，必须在线")
        async def ircwhois(interaction: discord.Interaction, target: str):
            if ('discord/' + str(interaction.channel.id)) not in (await utils.get_bridge_map()):
                return
            logger.info(f'Discord {interaction.channel.id} incoming /ircwhois: ' + str(interaction.message))
            await message_queue.put({
                'action': 'ircwhois',
                'target': target,
                'event': interaction,
                'from_group': 'discord/' + str(interaction.channel.id),
            })

        @bot.tree.command(description="查看 IRC 离线用户的 WHOWAS 信息")
        @app_commands.describe(target="要查看的昵称，必须离线")
        async def ircwhowas(interaction: discord.Interaction, target: str):
            if ('discord/' + str(interaction.channel.id)) not in (await utils.get_bridge_map()):
                return
            logger.info(f'Discord {interaction.channel.id} incoming /ircwhowas: ' + str(interaction.message))
            await message_queue.put({
                'action': 'ircwhowas',
                'target': target,
                'event': interaction,
                'from_group': 'discord/' + str(interaction.channel.id),
            })

        @bot.event
        async def on_message(message):
            """
            Discord listener serves as a producer to add new messages to queue.
            """
            # Don't echo self
            if message.author == bot.user:
                return
            if ('discord/' + str(message.channel.id)) not in (await utils.get_bridge_map()):
                return
            logger.info(f'Discord {message.channel.id} incoming message: ' + str(message))
            files = await self.download_media(message)
            await message_queue.put(await Message.create(message, files=files))

        @bot.event
        async def on_message_delete(message):
            """
            Discord listener that detects when a message is deleted.
            I am glad that it is much more reliable than the telegram listener.
            """
            group = 'discord/' + str(message.channel.id)
            if group not in (await utils.get_bridge_map()):
                return
            logger.info(f'Discord message {message.id} were deleted in {message.channel.id}')
            msg_doc = await db.find_bridged_messages_to_update(group, message.id)
            if not msg_doc or msg_doc.get('deleted') or not msg_doc.get('bridge_messages'):
                return
            logger.info(f'Messages to be deleted in bridged groups: {msg_doc.get("bridge_messages")}')
            # Put the request into queue for workers to actually delete messages
            await message_queue.put({'action': 'delete', 'body': msg_doc})
            await db.delete_message_record(msg_doc)

        @bot.event
        async def on_bulk_message_delete(messages):
            """
            Discord listener that detects when messages are bulk deleted.
            This may happen when e.g. an admin banned a member and deletes all their messages.
            """
            for message in messages:
                group = 'discord/' + str(message.channel.id)
                if group not in (await utils.get_bridge_map()):
                    continue
                logger.info(f'Discord message {message.id} were bulk deleted in {message.channel.id}')
                msg_doc = await db.find_bridged_messages_to_update(group, message.id)
                if not msg_doc or msg_doc.get('deleted') or not msg_doc.get('bridge_messages'):
                    continue
                logger.info(f'Messages to be deleted in bridged groups: {msg_doc.get("bridge_messages")}')
                # Put the request into queue for workers to actually delete messages
                await message_queue.put({'action': 'delete', 'body': msg_doc})
                await db.delete_message_record(msg_doc)

        @bot.event
        async def on_message_edit(_, message):
            """
            Discord listener that detects when a message is edited.

            It takes two arguments: before and after. We do not need the before one.
            """
            group = 'discord/' + str(message.channel.id)
            if group not in (await utils.get_bridge_map()):
                return
            if message.author == bot.user:
                return
            logger.info(f'Discord message {message.id} were edited in {message.channel.id}')
            msg_doc = await db.find_bridged_messages_to_update(group, message.id)
            if not msg_doc or not msg_doc.get('bridge_messages'):
                return

            files = await self.download_media(message)
            # Update the message internally
            await msg_collection.update_one({'_id': msg_doc.get('_id')}, {
                '$set': {
                    'edited_at': message.edited_at,
                    'text': message.content,
                    'files': [vars(file) for file in files],
                }
            })
            logger.info(f'Messages to be edited in bridged groups: {msg_doc.get("bridge_messages")}')
            new_message = await Message.create(message, files=files)
            await message_queue.put({'action': 'edit', 'body': {'to_edit': msg_doc, 'new_message': new_message}})

    def construct_files(self, files: list[File]) -> list[discord.File]:
        ret = []
        for file in files:
            if file.is_empty(): continue
            ret.append(discord.File(
                file.path,
                spoiler=file.metadata.get('is_spoiler', False),
                description=file.metadata.get('description', '')
            ))
        return ret
