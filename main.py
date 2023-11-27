import asyncio
import discord
import logging
import utils
from config import Config
from database import MongoDB
from discordbot import Discord
from irc import IRC
from message import get_relay_message, get_deleted_message, get_edited_message
from telegram import Telegram
from telethon import errors

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

# Initialize shared bridge objects
message_queue = utils.message_queue

# IRC bot initialization
irc = IRC()
irc_bot = irc.bot

# Telegram bot initialization
tg = Telegram()
tg_bot = tg.bot

# Discord bot initialization
dc = Discord()
dc_bot = dc.bot

# Initialize MongoDB client
db = MongoDB()
msg_collection = db.collection


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
                # List of msg_doc dicts
                old_messages = message.get('body', {})
                if type(old_messages) is not list: old_messages = [old_messages]
                irc_groups_notified = set()
                for old_message in old_messages:
                    for to_delete in old_message.get('bridge_messages', []):
                        group_to_delete, id_to_delete = to_delete.get('group', ''), to_delete.get('message_id')
                        platform, group_id = group_to_delete.split('/', 1)
                        if platform == 'irc':
                            # Send a message to inform users of the delete, but only once for bulk deletion
                            if group_id in irc_groups_notified: continue
                            irc_bot.send('PRIVMSG', target=group_id, message=(await get_deleted_message(old_messages)))
                            irc_groups_notified.add(group_id)
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
                old_message = message.get('body', {}).get('to_edit', {})
                for to_edit in old_message.get('bridge_messages', {}):
                    group_to_edit, id_to_edit = to_edit.get('group', ''), to_edit.get('message_id')
                    platform, group_id = group_to_edit.split('/', 1)
                    relay_message_text = await get_relay_message(new_message, platform)
                    if platform == 'irc':
                        # Send a message to inform users of the edit
                        irc_bot.send('PRIVMSG', target=group_id, message=(await get_edited_message(old_message, new_message)))
                    elif platform == 'telegram':
                        # Workaround: deal with the first message in each group only
                        # TODO: find a better solution for edge cases
                        if group_to_edit in groups_edited: continue
                        try:
                            image_files, other_files, attrs = tg.construct_files(new_message.files)
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
                            await msg.edit(content=relay_message_text, attachments=dc.construct_files(new_message.files))
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
        for group_to_send in (await utils.get_bridge_map()).get(message.from_group, []):
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
                    image_files, other_files, attrs = tg.construct_files(message.files)
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
                sent = await channel.send(relay_message_text, files=dc.construct_files(message.files))
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
        tg.deleted_poller(),
        tg_bot.run_until_disconnected(),
    )

try:
    tg_bot.loop.run_until_complete(main())
except KeyboardInterrupt:
    exit(0)
