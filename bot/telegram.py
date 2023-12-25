import asyncio
import logging
from . import utils
from .config import Config
from .database import MongoDB
from .im import MessagingPlatform
from .message import File, Message
from telethon import TelegramClient, events, errors, functions, types
from typing import Optional

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

class Telegram(MessagingPlatform):
    """
    The singleton Telegram listener.
    """

    def __init__(self):
        if not hasattr(self, 'bot'):
            self.bot = TelegramClient(
                config.get_nowait('Telegram', 'session', default='bridge'),
                config.get_nowait('Telegram', 'api_id'),
                config.get_nowait('Telegram', 'api_hash')
            )
            self.bot.start(bot_token=config.get_nowait('Telegram', 'bot_token'))
            self.register_listeners()

    async def download_media(self, message: types.Message) -> Optional[File]:
        """
        Helper method to download media from a message and save the media type.

        Return: downloaded File object. None if no medias at all or download failed.
        """
        media_type = ''
        metadata = {}
        # Some common attributes for MessageMediaDocument
        if message.media:
            metadata['is_spoiler'] = True if hasattr(message.media, 'spoiler') and message.media.spoiler else False
            if hasattr(message.media, 'document') and message.media.document:
                document = message.media.document
                metadata['size'] = document.size
                for attr in document.attributes:
                    if hasattr(attr, 'alt'): metadata['alt'] = attr.alt
                    if hasattr(attr, 'w'): metadata['width'] = attr.w
                    if hasattr(attr, 'h'): metadata['height'] = attr.h
                    if hasattr(attr, 'size'): metadata['size'] = attr.size
                    if hasattr(attr, 'duration'): metadata['duration'] = attr.duration
                    if hasattr(attr, 'file_name'): metadata['filename'] = attr.file_name
        if message.photo:
            media_type = 'photo'
            # MessageMediaPhoto attributes
            for size in message.photo.sizes:
                if hasattr(size, 'w'): metadata['width'] = size.w
                if hasattr(size, 'h'): metadata['height'] = size.h
                if hasattr(size, 'size'): metadata['size'] = size.size
        elif message.sticker:
            media_type = 'sticker'
        elif message.gif:
            media_type = 'gif'
        elif message.video:
            media_type = 'video'
        elif message.voice:
            media_type = 'voice'
        elif message.document:
            # This is a fallback type so should be placed near the end
            media_type = 'document'
        elif message.media:
            # TODO: (probably) support geo, poll, dice, invoice etc.
            media_type = 'unsupported'
        else:
            return
        path = ''
        if media_type != 'unsupported':
            try:
                path = await message.download_media(await config.get('Files', 'path', default=''))
            except errors.RPCError as e:
                logger.warning(f'Downloading telegram attachment from {message} failed: {e}')
                path = ''
        ret = File(media_type, path, **metadata)
        logger.info(f'Downloaded Telegram file: {ret}, path: {path}, is_empty: {ret.is_empty()}, metadata: {ret.metadata}')
        if ret.is_empty():
            return None
        return ret

    def register_listeners(self):
        bot = self.bot

        @bot.on(events.NewMessage(incoming=True))
        async def listener(event):
            """
            Telegram listener serves as a producer to add new messages to queue.
            """
            # Telegram bots cannot see self messages so we are fine
            if ('telegram/' + str(event.chat_id)) not in (await utils.get_bridge_map()):
                return
            # Albums are handled otherwise
            if event.grouped_id:
                return
            logger.info(f'Telegram {event.chat_id} incoming message: ' + str(event.message))
            file = await self.download_media(event.message)
            logger.info(f'files arg={([file] if file else [])}')
            await message_queue.put(await Message.create(event.message, files=([file] if file else [])))

        @bot.on(events.Album)
        async def album_listener(event):
            """
            Since albums in Telegram are sent to client as consecutive new message updates,
            we need a standalone handler for this instead of inventing wheels to deal with that
            """
            if ('telegram/' + str(event.chat_id)) not in (await utils.get_bridge_map()):
                return

            # Counting how many photos or videos the album has
            logger.info(f'Telegram {event.chat_id} incoming album with {len(event)} items: {event.text}')

            files = []
            for i in range(len(event)):
                # Download media for each album message
                file = await self.download_media(event.messages[i])
                if file:
                    if event.messages[i].message:
                        file.metadata['description'] = event.messages[i].message
                    files.append(file)

            # Note: only caption of the first message is retained. Others are discarded.
            # TODO: record all message ids of the album for delete/edit
            await message_queue.put(await Message.create(event.messages[0], files=files))

        @bot.on(events.MessageDeleted)
        async def deleted_listener(event):
            """
            Telegram listener that detects when messages are deleted.
            From telethon doc it isn't 100% reliable. Actually it works like only 1% of the time.
            So we have to implement the polling method as well ¯\_(ツ)_/¯
            """
            group = 'telegram/' + str(event.chat_id)
            if group not in (await utils.get_bridge_map()):
                return
            logger.info(f'Telegram message {event.deleted_ids} were deleted in {event.chat_id}')
            to_delete = []
            for msg_id in event.deleted_ids:
                msg_doc = await db.find_bridged_messages_to_update(group, msg_id)
                if not msg_doc or msg_doc.get('deleted') or not msg_doc.get('bridge_messages'):
                    continue
                to_delete.append(msg_doc)
                await db.delete_message_record(msg_doc)

            if not to_delete:
                return
            logger.info(f'Messages to be deleted in bridged groups: {to_delete}')
            await message_queue.put({'action': 'delete', 'body': to_delete})

        # TODO: compare old/new messages, do not re-download and re-upload same files
        @bot.on(events.MessageEdited)
        async def edited_listener(event):
            """
            Telegram listener that detects when messages are edited.
            """
            group = 'telegram/' + str(event.chat_id)
            if group not in (await utils.get_bridge_map()):
                return
            logger.info(f'Telegram message {event.message.id} were edited in {event.chat_id}')
            msg_doc = await db.find_bridged_messages_to_update(group, event.message.id)
            if not msg_doc or not msg_doc.get('bridge_messages'):
                return

            file = await self.download_media(event.message)
            # Update the message internally
            await msg_collection.update_one({'_id': msg_doc.get('_id')}, {
                '$set': {
                    'edited_at': event.message.edit_date,
                    'text': event.message.text,
                    'files': [vars(file) if file else []],
                }
            })

            logger.info(f'Messages to be edited in bridged groups: {msg_doc.get("bridge_messages")}')
            new_message = await Message.create(event.message, files=([file] if file else []))
            await message_queue.put({'action': 'edit', 'body': {'to_edit': msg_doc, 'new_message': new_message}})

    async def deleted_poller(self):
        """
        Poll the admin log to get recent deleted messages.
        The bot needs to be an admin (regardless of rights) in each group.
        """
        # Wait for other platforms to initialize
        await asyncio.sleep(30)
        while True:
            # Poll outbound telegram groups only
            for group in (await utils.get_bridge_map()):
                if not group.startswith('telegram/'):
                    continue
                chat_id = int(group.split('/', 1)[1])
                # Only check the most recent 500 messages
                messages = msg_collection.find({
                    'bridge_messages.group': group,
                }).sort({'_id': -1}).limit(500)
                messages = await messages.to_list(None)
                # logger.info(f'Poller got {len(messages)} messages for group {group}')
                msg_ids = []
                for message in messages:
                    # Extract message ids to poll (all messages from current group) from bridge_messages field
                    msg_ids.extend([d.get('message_id') for d in message.get('bridge_messages', []) if d.get('group') == group])
                if not msg_ids:
                    continue
                # logger.info(f'Poller got {len(msg_ids)} msg_ids: {msg_ids}')
                try:
                    # Return type is ChannelMessages
                    msgs = await self.bot(functions.channels.GetMessagesRequest(
                        channel=chat_id,
                        id=msg_ids,
                    ))
                    msgs = msgs.messages
                except errors.FloodWaitError as e:
                    logger.info(f'FloodWaitError in deleted_poller: sleep {e.seconds} seconds')
                    await asyncio.sleep(e.seconds)
                except errors.RPCError as e:
                    logger.warn(f'Poller error on GetMessagesRequest: {e}')
                    pass
                # logger.info(f'Poller got {len(msgs)} msgs: {msgs}')
                # Find holes in messages
                to_delete = []
                for i in range(len(msgs)):
                    if type(msgs[i]) is types.MessageEmpty or msgs[i] is None:
                        msg_doc = await db.find_bridged_messages_to_update(group, msg_ids[i])
                        if not msg_doc or msg_doc.get('deleted') or not msg_doc.get('bridge_messages'):
                            continue
                        to_delete.append(msg_doc)
                        await db.delete_message_record(msg_doc)
                if not to_delete:
                    continue
                logger.info(f'Messages to be deleted in bridged groups: {to_delete}')
                await message_queue.put({'action': 'delete', 'body': to_delete})

            # Sleep after finishing a loop of all chats
            await asyncio.sleep(3)

    def construct_files(self, files: list[File]) -> tuple[list[str], list[str], list[types.DocumentAttributeFilename]]:
        # Telethon does not provide a way to override default filename.
        # The workaround from https://github.com/LonamiWebs/Telethon/issues/1473 does not work for large files
        # since it requires reading all bytes into memory at once.
        # Telegram does not allow multiple medias in a message (except album which must be photos) either.
        # Luckily we don't have to bother renaming images since the filename won't be displayed...
        image_files, other_files, attr = [], [], []
        for file in files:
            if file.is_empty(): continue
            if file.is_image():
                image_files.append(file.path)
            else:
                other_files.append(file.path)
                if not file.metadata.get('filename'):
                    attr.append([])
                else:
                    attr.append(types.DocumentAttributeFilename(file_name=file.metadata.get('filename')))
        return image_files, other_files, attr
