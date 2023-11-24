import asyncio
import bottom
import discord
import logging
from collections import defaultdict
from config import Config
from datetime import datetime
from message import Message, get_relay_message
from motor.motor_asyncio import AsyncIOMotorClient
from telethon import TelegramClient, events, errors, functions, types

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

@tg_bot.on(events.NewMessage(incoming=True))
async def telegram_listener(event):
    """
    Telegram listener serves as a producer to add new messages to queue.
    """
    # Telegram bots cannot see self messages so we are fine
    if ('telegram/' + str(event.chat_id)) not in bridge_map:
        return
    logger.info(f'Telegram {event.chat_id} incoming message: ' + str(event.message))
    await message_queue.put(await Message.create(event.message))

@tg_bot.on(events.MessageDeleted)
async def telegram_deleted_listener(event):
    """
    Telegram listener that detects when messages are deleted.
    From telethon doc it isn't 100% reliable. Actually it works like only 1% of the time.
    So we have to implement the polling method as well ¯\_(ツ)_/¯
    """
    if ('telegram/' + str(event.chat_id)) not in bridge_map:
        return
    logger.info(f'Telegram message {event.deleted_ids} were deleted in {event.chat_id}')
    to_delete = defaultdict(list)
    for msg_id in event.deleted_ids:
        old_msg, next_to_delete = await find_bridged_messages_and_update('telegram', event.chat_id, msg_id, None, {
            'deleted': True,
            'deleted_at': datetime.utcnow(),
        })
        # Do not re-delete a message
        if old_msg.get('deleted'):
            continue
        # Merge two default dicts
        for key in next_to_delete:
            to_delete[key].extend(next_to_delete[key])
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
                '$or': [
                    {'to_groups': group},
                    {'from_platform': 'telegram', 'from_group': str(chat_id)},
                ]
            }).sort({'_id': -1}).limit(500)
            messages = await messages.to_list(None)
            # logger.info(f'Poller got {len(messages)} messages for group {group}')
            msg_ids = []
            for message in messages:
                if message.get('from_group') == str(chat_id) and message.get('from_platform') == 'telegram':
                    # Outbound message
                    msg_ids.append(message.get('from_message_id'))
                else:
                    try:
                        # Inbound message, pull the message id from to_message list
                        idx = message.get('to_groups', []).index(group)
                        msg_ids.append(message.get('to_message_ids', [])[idx])
                    except ValueError:
                        continue
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
            to_delete = defaultdict(list)
            for i in range(len(msgs)):
                if type(msgs[i]) is types.MessageEmpty or msgs[i] is None:
                    old_msg, next_to_delete = await find_bridged_messages_and_update('telegram', chat_id, msg_ids[i], None, {
                        'deleted': True,
                        'deleted_at': datetime.utcnow(),
                    })
                    if old_msg.get('deleted'):
                        continue
                    for key in next_to_delete:
                        to_delete[key].extend(next_to_delete[key])
            if not to_delete:
                continue
            logger.info(f'Messages to be deleted in bridged groups: {to_delete}')
            await message_queue.put({'action': 'delete', 'body': to_delete})

        # Sleep after finishing a loop of all chats
        await asyncio.sleep(3)

@tg_bot.on(events.MessageEdited)
async def telegram_edited_listener(event):
    """
    Telegram listener that detects when messages are edited.
    """
    if ('telegram/' + str(event.chat_id)) not in bridge_map:
        return
    logger.info(f'Telegram message {event.message.id} were edited in {event.chat_id}')
    _, to_edit = await find_bridged_messages_and_update('telegram', event.chat_id, event.message.id, False, {
        'edited_at': event.message.edit_date,
        'text': event.message.text,
        # TODO: embeds
    })
    if not to_edit:
        return
    logger.info(f'Messages to be edited in bridged groups: {to_edit}')
    new_message = await Message.create(event.message)
    await message_queue.put({'action': 'edit', 'body': {'to_edit': to_edit, 'new_message': new_message}})

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
    await message_queue.put(await Message.create(message))

@dc_bot.event
async def on_message_delete(message):
    """
    Discord listener that detects when a message is deleted.
    I am glad that it is much more reliable than the telegram listener.
    """
    if ('discord/' + str(message.channel.id)) not in bridge_map:
        return
    logger.info(f'Discord message {message.id} were deleted in {message.channel.id}')
    old_msg, to_delete = await find_bridged_messages_and_update('discord', message.channel.id, message.id, message.author == dc_bot.user, {
        'deleted': True,
        'deleted_at': datetime.utcnow(),
    })
    # Do not re-delete a message
    if old_msg.get('deleted') or not to_delete:
        return
    logger.info(f'Messages to be deleted in bridged groups: {to_delete}')
    await message_queue.put({'action': 'delete', 'body': to_delete})

@dc_bot.event
async def on_message_edit(_, message):
    """
    Discord listener that detects when a message is edited.

    It takes two arguments: before and after. We do not need the before one.
    """
    if ('discord/' + str(message.channel.id)) not in bridge_map:
        return
    if message.author == dc_bot.user:
        return
    logger.info(f'Discord message {message.id} were edited in {message.channel.id}')
    _, to_edit = await find_bridged_messages_and_update('discord', message.channel.id, message.id, False, {
        'edited_at': message.edited_at,
        'text': message.content,
        # TODO: embeds
    })
    if not to_edit:
        return
    logger.info(f'Messages to be edited in bridged groups: {to_edit}')
    new_message = await Message.create(message)
    await message_queue.put({'action': 'edit', 'body': {'to_edit': to_edit, 'new_message': new_message}})

async def find_bridged_messages_and_update(platform: str, group_id, message_id: int, self_authored, update={}):
    """
    Find all relayed messages connected with given group id and message id from MongoDB.

    platform: discord, telegram, etc. All in lower case.
    self_authored: True if the given message is authored by self (the bridge bot), False otherwise, None if unknown (will try both)
    update: a dict with fields to be updated. Do not include MongoDB keywords like $set.
    Returns the original message and a defaultdict, key is groups in the form platform/groupid, value is list of message ids
    """
    ret = defaultdict(list)
    if self_authored:
        # It's a relayed message originated from other platforms
        message = await msg_collection.find_one_and_update({
            'to_groups': f'{platform}/{group_id}',
            'to_message_ids': message_id,
        }, {'$set': update})
        if message:
            # Return messages in to_groups, plus from_group, minus current group
            to_groups = message.get('to_groups', [])
            to_message_ids = message.get('to_message_ids', [])
            for i in range(len(to_groups)):
                if to_message_ids[i] and to_message_ids[i] != message_id:
                    ret[to_groups[i]].append(to_message_ids[i])
            ret[f'{message.get("from_platform")}/{message.get("from_group")}'].append(message.get('from_message_id'))
    else:
        # It's originated from the current group
        message = await msg_collection.find_one_and_update({
            'from_platform': platform,
            'from_group': str(group_id),
            'from_message_id': message_id,
        }, {'$set': update})
        if message:
            # Pack the messages to delete and send it to queue
            to_groups = message.get('to_groups', [])
            to_message_ids = message.get('to_message_ids', [])
            for i in range(len(to_groups)):
                if to_message_ids[i]:
                    ret[to_groups[i]].append(to_message_ids[i])
        if not message and self_authored is None:
            # Try again assuming it is authored by bridge bot
            return await find_bridged_messages_and_update(platform, group_id, message_id, True, update)
    return message, ret

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
                for group_to_delete, ids_to_delete in message.get('body', {}).items():
                    platform, group_id = group_to_delete.split('/', 1)
                    if platform == 'irc':
                        # Cannot delete IRC messages
                        continue
                    elif platform == 'telegram':
                        # Delete all messages at the same time
                        try:
                            await tg_bot.delete_messages(int(group_id), ids_to_delete)
                        except (errors.ChannelInvalidError, errors.ChannelPrivateError, errors.MessageDeleteForbiddenError) as e:
                            logger.warning(f'Telegram error occured on deleting messages {ids_to_delete} in {group_id}: {e}')
                        except Exception as e:
                            # TODO: probably catch errors.FloodWaitError
                            logger.warning(f'Unknown error occured on deleting messages {ids_to_delete} in {group_id}: {e}')
                    elif platform == 'discord':
                        # Have to delete messages one by one
                        channel = dc_bot.get_channel(int(group_id))
                        if not channel:
                            # TODO: log error
                            continue
                        for msg_id in ids_to_delete:
                            msg = await channel.fetch_message(msg_id)
                            try:
                                await msg.delete()
                            except discord.errors as e:
                                logger.warning(f'Discord error occured on deleting message {msg_id} in {group_id}: {e}')
                            except Exception as e:
                                logger.warning(f'Unknown error occured on deleting message {msg_id} in {group_id}: {e}')
                    else:
                        logger.warning(f'Unknown platform: {platform} (from {message}), please report this bug')
            elif action == 'edit':
                new_message = message.get('body', {}).get('new_message')
                for group_to_edit, ids_to_edit in message.get('body', {}).get('to_edit', {}).items():
                    platform, group_id = group_to_edit.split('/', 1)
                    relay_message_text = await get_relay_message(new_message, platform)
                    if platform == 'irc':
                        # Cannot edit IRC messages
                        continue
                    elif platform == 'telegram':
                        for msg_id in ids_to_edit:
                            try:
                                # TODO: embeds
                                await tg_bot.edit_message(int(group_id), msg_id, relay_message_text)
                            except errors.RPCError as e:
                                logger.warning(f'Telegram error occured on deleting messages {ids_to_edit} in {group_id}: {e}')
                            except Exception as e:
                                # TODO: probably catch errors.FloodWaitError
                                logger.warning(f'Unknown error occured on deleting messages {ids_to_edit} in {group_id}: {e}')
                    elif platform == 'discord':
                        channel = dc_bot.get_channel(int(group_id))
                        if not channel:
                            # TODO: log error
                            continue
                        for msg_id in ids_to_edit:
                            msg = await channel.fetch_message(msg_id)
                            try:
                                await msg.edit(content=relay_message_text)
                            except discord.errors.DiscordException as e:
                                logger.warning(f'Discord error occured on deleting message {msg_id} in {group_id}: {e}')
                            except Exception as e:
                                logger.warning(f'Unknown error occured on deleting message {msg_id} in {group_id}: {e}')
                    else:
                        logger.warning(f'Unknown platform: {platform} (from {message}), please report this bug')
            else:
                logger.warning(f'Unknown action {action} from message of a listener: {message}')
            continue
        logger.info(f'outgoing message: ' + str(message))
        from_group = message.from_platform + '/' + message.from_group
        to_msg_ids = []
        # TODO: this should be dynamically loaded from config
        for group_to_send in bridge_map.get(from_group, []):
            # TODO: make this a helper function
            platform, group_id = group_to_send.split('/', 1)
            relay_message_text = await get_relay_message(message, platform)
            if platform == 'irc':
                # TODO: if message is too long, send in multiple times
                irc_bot.send('PRIVMSG', target=group_id, message=relay_message_text)
                to_msg_ids.append(None)  # Unable to track IRC messages
                logger.info(f'sent message to {group_to_send}')
            elif platform == 'telegram':
                # Telethon only accept int group ids
                sent = await tg_bot.send_message(int(group_id), relay_message_text, parse_mode='md')
                to_msg_ids.append(sent.id)
                logger.info(f'sent message to {group_to_send}, msg id = {sent.id}')
            elif platform == 'discord':
                # Discord only accept int group ids
                channel = dc_bot.get_channel(int(group_id))
                if not channel:
                    # TODO: log error
                    continue
                sent = await channel.send(relay_message_text)
                to_msg_ids.append(sent.id)
                logger.info(f'sent message to {group_to_send}, msg id = {sent.id}')
            else:
                # Unknown platform
                to_msg_ids.append(None)
                logger.warning(f'Unknown platform: {platform} (from {group_to_send}), check your Bridge config')

        await msg_collection.insert_one({
            'deleted': False,
            'created_at': message.created_at,
            'edited_at': None,
            'deleted_at': None,
            'from_platform': message.from_platform,
            'from_group': message.from_group,
            'from_user_id': message.from_user_id,
            'from_nick': message.from_nick,
            'from_message_id': message.from_message_id,
            'to_groups': bridge_map.get(from_group, []),
            'to_message_ids': to_msg_ids,
            'text': message.text,
            'embeds': [],
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
