import asyncio
import bottom
import discord
import logging
import os
from config import Config
from datetime import datetime
from io import BytesIO
from message import File, Message, get_relay_message
from motor.motor_asyncio import AsyncIOMotorClient
from telethon import TelegramClient, events, errors, functions, types
from typing import Optional

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

# IRC bot initialization
irc_bot = bottom.Client(
    host=config.get_nowait('IRC', 'host'),
    port=config.get_nowait('IRC', 'port'),
    ssl=config.get_nowait('IRC', 'ssl')
)

# Telegram bot initialization
tg_bot = TelegramClient(
    config.get_nowait('Telegram', 'session', default='bridge'),
    config.get_nowait('Telegram', 'api_id'),
    config.get_nowait('Telegram', 'api_hash')
)
tg_bot.start(bot_token=config.get_nowait('Telegram', 'bot_token'))

# Discord bot initialization
intents = discord.Intents.default()
intents.message_content = True

dc_bot = discord.Client(intents=intents)

@dc_bot.event
async def on_ready():
    logging.info(f'Discord: we have logged in as {dc_bot.user}')

# Initialize MongoDB client
mongo_client = AsyncIOMotorClient(config.get_nowait('Mongo', 'uri'))
db = mongo_client[config.get_nowait('Mongo', 'database_name')]
msg_collection = db[config.get_nowait('Mongo', 'collection_name')]

# Initialize the asyncio.Queue
message_queue: asyncio.Queue[Message] = asyncio.Queue()


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
    # IRC does not send us the time when a message is received
    # so just use the current UTC time
    received_at = datetime.utcnow()
    mynick = await config.get('IRC', 'nick')
    # Don't echo self
    if nick == mynick:
        return
    # Must be in bridge map
    if 'irc/' + target not in bridge_map:
        return

    await message_queue.put(await Message.create({
        'group': target,
        'host': kwargs.get('host', ''),
        'nick': nick,
        'text': message,
        'created_at': received_at,
    }))

async def telegram_download_media(message: types.Message) -> Optional[File]:
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

@tg_bot.on(events.NewMessage(incoming=True))
async def telegram_listener(event):
    """
    Telegram listener serves as a producer to add new messages to queue.
    """
    # Telegram bots cannot see self messages so we are fine
    if ('telegram/' + str(event.chat_id)) not in bridge_map:
        return
    # Albums are handled otherwise
    if event.grouped_id:
        return
    logger.info(f'Telegram {event.chat_id} incoming message: ' + str(event.message))
    file = await telegram_download_media(event.message)
    logger.info(f'files arg={([file] if file else [])}')
    await message_queue.put(await Message.create(event.message, files=([file] if file else [])))

@tg_bot.on(events.Album)
async def telegram_album_listener(event):
    """
    Since albums in Telegram are sent to client as consecutive new message updates,
    we need a standalone handler for this instead of inventing wheels to deal with that
    """
    if ('telegram/' + str(event.chat_id)) not in bridge_map:
        return

    # Counting how many photos or videos the album has
    logger.info(f'Telegram {event.chat_id} incoming album with {len(event)} items: {event.text}')

    files = []
    for i in range(len(event)):
        # Download media for each album message
        file = await telegram_download_media(event.messages[i])
        if file:
            if event.messages[i].message:
                file.metadata['description'] = event.messages[i].message
            files.append(file)

    # Note: only caption of the first message is retained. Others are discarded.
    # TODO: record all message ids of the album for delete/edit
    await message_queue.put(await Message.create(event.messages[0], files=files))

@tg_bot.on(events.MessageDeleted)
async def telegram_deleted_listener(event):
    """
    Telegram listener that detects when messages are deleted.
    From telethon doc it isn't 100% reliable. Actually it works like only 1% of the time.
    So we have to implement the polling method as well ¯\_(ツ)_/¯
    """
    group = 'telegram/' + str(event.chat_id)
    if group not in bridge_map:
        return
    logger.info(f'Telegram message {event.deleted_ids} were deleted in {event.chat_id}')
    to_delete = []
    for msg_id in event.deleted_ids:
        msg_doc = await find_bridged_messages_to_update(group, msg_id)
        if not msg_doc or msg_doc.get('deleted') or not msg_doc.get('bridge_messages'):
            continue
        to_delete.extend(msg_doc.get("bridge_messages"))
        await delete_message_record(msg_doc)

    if not to_delete:
        return
    logger.info(f'Messages to be deleted in bridged groups: {to_delete}')
    await message_queue.put({'action': 'delete', 'body': to_delete})

async def telegram_deleted_poller():
    """
    Poll the admin log to get recent deleted messages.
    The bot needs to be an admin (regardless of rights) in each group.
    """
    # Wait for other platforms to initialize
    await asyncio.sleep(30)
    while True:
        # Poll outbound telegram groups only
        for group in bridge_map:
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
                msgs = await tg_bot(functions.channels.GetMessagesRequest(
                    channel=chat_id,
                    id=msg_ids,
                ))
                msgs = msgs.messages
            except errors.FloodWaitError as e:
                logger.info(f'FloodWaitError in telegram_deleted_poller: sleep {e.seconds} seconds')
                await asyncio.sleep(e.seconds)
            except errors.RPCError as e:
                logger.warn(f'Poller error on GetMessagesRequest: {e}')
                pass
            # logger.info(f'Poller got {len(msgs)} msgs: {msgs}')
            # Find holes in messages
            to_delete = []
            for i in range(len(msgs)):
                if type(msgs[i]) is types.MessageEmpty or msgs[i] is None:
                    msg_doc = await find_bridged_messages_to_update(group, msg_ids[i])
                    if not msg_doc or msg_doc.get('deleted') or not msg_doc.get('bridge_messages'):
                        continue
                    to_delete.extend(msg_doc.get("bridge_messages"))
                    await delete_message_record(msg_doc)
            if not to_delete:
                continue
            logger.info(f'Messages to be deleted in bridged groups: {to_delete}')
            await message_queue.put({'action': 'delete', 'body': to_delete})

        # Sleep after finishing a loop of all chats
        await asyncio.sleep(3)

# TODO: compare old/new messages, do not re-download and re-upload same files
@tg_bot.on(events.MessageEdited)
async def telegram_edited_listener(event):
    """
    Telegram listener that detects when messages are edited.
    """
    group = 'telegram/' + str(event.chat_id)
    if group not in bridge_map:
        return
    logger.info(f'Telegram message {event.message.id} were edited in {event.chat_id}')
    msg_doc = await find_bridged_messages_to_update(group, event.message.id)
    if not msg_doc or not msg_doc.get('bridge_messages'):
        return
    
    file = await telegram_download_media(event.message)
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
    await message_queue.put({'action': 'edit', 'body': {'to_edit': msg_doc.get("bridge_messages"), 'new_message': new_message}})

async def discord_download_media(message: discord.Message) -> list[File]:
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
    logger.info(f'Downloaded Discord files: {ret}')
    return ret

@dc_bot.event
async def on_message(message):
    """
    Discord listener serves as a producer to add new messages to queue.
    """
    # Don't echo self
    if message.author == dc_bot.user:
        return
    if ('discord/' + str(message.channel.id)) not in bridge_map:
        return
    logger.info(f'Discord {message.channel.id} incoming message: ' + str(message))
    files = await discord_download_media(message)
    await message_queue.put(await Message.create(message, files=files))

@dc_bot.event
async def on_message_delete(message):
    """
    Discord listener that detects when a message is deleted.
    I am glad that it is much more reliable than the telegram listener.
    """
    group = 'discord/' + str(message.channel.id)
    if group not in bridge_map:
        return
    logger.info(f'Discord message {message.id} were deleted in {message.channel.id}')
    msg_doc = await find_bridged_messages_to_update(group, message.id)
    if not msg_doc or msg_doc.get('deleted') or not msg_doc.get('bridge_messages'):
        return
    logger.info(f'Messages to be deleted in bridged groups: {msg_doc.get("bridge_messages")}')
    # Put the request into queue for workers to actually delete messages
    await message_queue.put({'action': 'delete', 'body': msg_doc.get("bridge_messages")})
    await delete_message_record(msg_doc)

@dc_bot.event
async def on_message_edit(_, message):
    """
    Discord listener that detects when a message is edited.

    It takes two arguments: before and after. We do not need the before one.
    """
    group = 'discord/' + str(message.channel.id)
    if group not in bridge_map:
        return
    if message.author == dc_bot.user:
        return
    logger.info(f'Discord message {message.id} were edited in {message.channel.id}')
    msg_doc = await find_bridged_messages_to_update(group, message.id)
    if not msg_doc or not msg_doc.get('bridge_messages'):
        return

    files = await discord_download_media(message)
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
    await message_queue.put({'action': 'edit', 'body': {'to_edit': msg_doc.get("bridge_messages"), 'new_message': new_message}})

async def find_bridged_messages_to_update(group: str, message_id: int):
    """
    Find all relayed messages connected with given group id and message id from MongoDB.

    Returns the message document, but 'bridge_messages' field is filtered so
    it only includes outbound connected groups so calling method can update edit/delete events.
    Example: groups A, B, C, D has relationship
    A --
        |--> C --> D
    B --
    Then for messages originated from A, its bridge_messages field will have A and C.
    If its relayed message in C is deleted, this method will find this message in MongoDB,
    but will update the bridge_messages to {A, C} & {D} == {}, where {D} comes from Bridge config,
    since C's updates should only propagate to group D.
    """
    message = await msg_collection.find_one({
        'bridge_messages': {
            '$elemMatch': {
                'group': group,
                'message_id': message_id,
            }
        }
    })
    if not message:
        return
    connected_groups = {m.get('group') for m in message.get('bridge_messages', {})}
    # Only update outbound connected groups
    outbound_groups = set(bridge_map.get(group, set()))
    groups_to_update = outbound_groups & connected_groups
    message['bridge_messages'] = [m for m in message.get('bridge_messages', {}) if m.get('group') in groups_to_update]
    return message

async def delete_message_record(msg_doc):
    """
    Delete all media files contained in the given message, and delete the record from db.
    Note that currently the method does not actually remove a record,
    it just mark the record as deleted for possible future references.
    """
    if not msg_doc or msg_doc.get('deleted') or not msg_doc.get('bridge_messages'):
        return

    # Delete all files contained in the message
    for file in msg_doc.get('files', []):
        if file.get('path'):
            logger.info(f'Deleting local file {file.get("path")}')
            try:
                os.remove(file.get('path'))
            except OSError:
                pass

    # Mark as deleted internally
    await msg_collection.update_one({'_id': msg_doc.get('_id')}, {
        '$set': {
            'deleted': True,
            'deleted_at': datetime.utcnow(),
        }
    })

def construct_telegram_files(files: list[File]) -> tuple[list[str], list[str], list[types.DocumentAttributeFilename]]:
    # Telethon does provide a way to override default filename.
    # The method from https://github.com/LonamiWebs/Telethon/issues/1473 does not work for large files
    # since it requires reading all bytes into memory at once
    # Telegram also does not allow multiple medias in a message (except album which must be photos)
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

def construct_discord_files(files: list[File]) -> list[discord.File]:
    ret = []
    for file in files:
        if file.is_empty(): continue
        ret.append(discord.File(
            file.path,
            spoiler=file.metadata.get('is_spoiler', False),
            description=file.metadata.get('description', '')
        ))
    return ret

async def worker():
    """
    The consumer will fetch messages from queue, insert into database and
    relay the message to other platform.
    """
    while True:
        message = await message_queue.get()
        if type(message) is dict:
            # internal message, indicates to delete or update existing messages
            logger.info(f'internal message: {message}')
            action = message.get('action')
            if action == 'delete':
                # TODO: delete local files included in the message
                for to_delete in message.get('body', {}):
                    group_to_delete, id_to_delete = to_delete.get('group', ''), to_delete.get('message_id')
                    platform, group_id = group_to_delete.split('/', 1)
                    if platform == 'irc':
                        # Cannot delete IRC messages
                        continue
                    elif platform == 'telegram':
                        # Delete all messages at the same time
                        try:
                            await tg_bot.delete_messages(int(group_id), [id_to_delete])
                        except (errors.ChannelInvalidError, errors.ChannelPrivateError, errors.MessageDeleteForbiddenError) as e:
                            logger.warning(f'Telegram error occured on deleting message {id_to_delete} in {group_id}: {e}')
                        except Exception as e:
                            # TODO: probably catch errors.FloodWaitError
                            logger.warning(f'Unknown error occured on deleting message {id_to_delete} in {group_id}: {e}')
                    elif platform == 'discord':
                        # Have to delete messages one by one
                        try:
                            channel = dc_bot.get_channel(int(group_id))
                            if not channel:
                                logger.warning(f'Discord error occured on deleting message {id_to_delete} in {group_id}: channel not found')
                                continue
                            msg = await channel.fetch_message(id_to_delete)
                            await msg.delete()
                        except discord.errors.DiscordException as e:
                            logger.warning(f'Discord error occured on deleting message {id_to_delete} in {group_id}: {e}')
                        except Exception as e:
                            logger.warning(f'Unknown error occured on deleting message {id_to_delete} in {group_id}: {e}')
                    else:
                        logger.warning(f'Unknown platform: {platform} (from {message}), please report this bug')
            elif action == 'edit':
                new_message = message.get('body', {}).get('new_message')
                groups_edited = set()
                for to_edit in message.get('body', {}).get('to_edit', {}):
                    group_to_edit, id_to_edit = to_edit.get('group', ''), to_edit.get('message_id')
                    platform, group_id = group_to_edit.split('/', 1)
                    relay_message_text = await get_relay_message(new_message, platform)
                    if platform == 'irc':
                        # Cannot edit IRC messages
                        continue
                    elif platform == 'telegram':
                        # Workaround: deal with the first message in each group only
                        # TODO: find a better solution for edge cases
                        if group_to_edit in groups_edited: continue
                        try:
                            image_files, other_files, attrs = construct_telegram_files(new_message.files)
                            # Workaround: first media only
                            image_files, other_files, attrs = image_files[:1], other_files[:1], attrs[:1]
                            await tg_bot.edit_message(int(group_id), id_to_edit, relay_message_text,
                                                      file=(image_files or other_files), attributes=attrs,
                                                      force_document=(new_message.files and new_message.files[0].type == 'document'))
                        except errors.RPCError as e:
                            logger.warning(f'Telegram error occured on editing messages {id_to_edit} in {group_id}: {e}')
                        except Exception as e:
                            # TODO: probably catch errors.FloodWaitError
                            logger.warning(f'Unknown error occured on editing messages {id_to_edit} in {group_id}: {e}')
                    elif platform == 'discord':
                        try:
                            channel = dc_bot.get_channel(int(group_id))
                            if not channel:
                                logger.warning(f'Discord error occured on editing message {id_to_delete} in {group_id}: channel not found')
                                continue
                            msg = await channel.fetch_message(id_to_edit)
                            await msg.edit(content=relay_message_text, attachments=construct_discord_files(new_message.files))
                        except discord.errors.DiscordException as e:
                            logger.warning(f'Discord error occured on editing message {id_to_edit} in {group_id}: {e}')
                        except Exception as e:
                            logger.warning(f'Unknown error occured on editing message {id_to_edit} in {group_id}: {e}')
                    else:
                        logger.warning(f'Unknown platform: {platform} (from {message}), please report this bug')
                    groups_edited.add(group_to_edit)
            else:
                logger.warning(f'Unknown action {action} from message of a listener: {message}')
            continue
        logger.info(f'outgoing message: ' + str(message))
        # The first item of the list in MongoDB is always the original message ('from'), others are 'to'
        bridge_messages = [{
            'group': message.from_group,
            'message_id': message.from_message_id,
        }]
        # TODO: this should be dynamically loaded from config
        for group_to_send in bridge_map.get(message.from_group, []):
            # TODO: make this a helper function
            platform, group_id = group_to_send.split('/', 1)
            relay_message_text = await get_relay_message(message, platform)
            if platform == 'irc':
                # TODO: if message is too long, send in multiple times
                irc_bot.send('PRIVMSG', target=group_id, message=relay_message_text)
                bridge_messages.append({
                    'group': group_to_send,
                    # IRC messages have no IDs
                    'message_id': None,
                })
                logger.info(f'sent message to {group_to_send}')
            elif platform == 'telegram':
                # Telethon only accept int group ids
                if message.files:
                    # TODO: how to deal with captions of each photo in album?
                    image_files, other_files, attrs = construct_telegram_files(message.files)
                    sent = []
                    # Send album or single photo
                    if image_files:
                        sent = await tg_bot.send_file(int(group_id), image_files, caption=relay_message_text,
                                                      # If the message was downloaded as a document, then upload as document as well
                                                      force_document=(message.files[0].type == 'document'))
                    # For messages after the first: reply to the first, and shall not include texts
                    first_msg = sent or None
                    if type(first_msg) is list: first_msg = first_msg[0]
                    if other_files:
                        # Can only send one message per time, with filename overridden
                        for i, file in enumerate(other_files):
                            sent.append(await tg_bot.send_file(int(group_id), file, caption=('' if first_msg else relay_message_text),
                                                               attributes=[attrs[i]], reply_to=first_msg,
                                                               # If the message was downloaded as a document, then upload as document as well
                                                               force_document=(message.files[0].type == 'document')))
                            if not first_msg and sent:
                                first_msg = sent[-1]
                else:
                    sent = await tg_bot.send_message(int(group_id), relay_message_text, parse_mode='md')
                # For albums, return will be a list, so just convert all cases to list for convenience
                if type(sent) is not list:
                    sent = [sent]
                bridge_messages.extend([{
                    'group': group_to_send,
                    'message_id': sent_msg.id,
                } for sent_msg in sent])
                logger.info(f'sent message to {group_to_send}, msg ids = {[sent_msg.id for sent_msg in sent]}')
            elif platform == 'discord':
                # Discord only accept int group ids
                channel = dc_bot.get_channel(int(group_id))
                if not channel:
                    logger.warning(f'Discord error occured on sending message to {group_id}: channel not found')
                    continue
                sent = await channel.send(relay_message_text, files=construct_discord_files(message.files))
                bridge_messages.append({
                    'group': group_to_send,
                    'message_id': sent.id,
                })
                logger.info(f'sent message to {group_to_send}, msg id = {sent.id}')
            else:
                # Unknown platform
                bridge_messages.append({
                    'group': None,
                    'message_id': None,
                })
                logger.warning(f'Unknown platform: {platform} (from {group_to_send}), check your Bridge config')

        await msg_collection.insert_one({
            'deleted': False,
            'created_at': message.created_at,
            'edited_at': None,
            'deleted_at': None,
            'bridge_messages': bridge_messages,
            'from_user_id': message.from_user_id,
            'from_nick': message.from_nick,
            'text': message.text,
            # Convert File object to dict
            'files': [vars(file) for file in message.files],
            # TODO: reply_to, fwd_from
        })

async def main():
    await asyncio.gather(
        worker(),
        irc_bot.connect(),
        dc_bot.start(config.get_nowait('Discord', 'token')),
        telegram_deleted_poller(),
        tg_bot.run_until_disconnected(),
    )

try:
    tg_bot.loop.run_until_complete(main())
except KeyboardInterrupt:
    exit(0)
