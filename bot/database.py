import logging
import os
from .config import Config
from datetime import datetime, timedelta
from motor.motor_asyncio import AsyncIOMotorClient
from .utils import get_bridge_map

config = Config('bridge.yaml')
logger = logging.getLogger(__name__)
try:
    logger.setLevel(config.get_nowait('Logging', 'level', default='INFO'))
except ValueError:
    # Unknown level, use default INFO level
    pass

class MongoDB:
    """
    The singleton MongoDB instance.
    """

    def __new__(cls, *args, **kw):
        if not hasattr(cls, '_instance'):
            cls._instance = None
        if not cls._instance:
            orig = super(MongoDB, cls)
            # Object parent class only takes the type itself as argument
            cls._instance = orig.__new__(cls)
            cls._instance.client = None
        return cls._instance

    def __init__(self):
        if not self.client:
            # Initialize MongoDB client
            self.client = AsyncIOMotorClient(config.get_nowait('Mongo', 'uri'))
            self.db = self.client[config.get_nowait('Mongo', 'database_name')]
            self.collection = self.db[config.get_nowait('Mongo', 'collection_name')]

    async def find_bridged_messages_to_update(self, group: str, message_id: int):
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
        message = await self.collection.find_one({
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
        outbound_groups = set((await get_bridge_map()).get(group, set()))
        groups_to_update = outbound_groups & connected_groups
        message['bridge_messages'] = [m for m in message.get('bridge_messages', {}) if m.get('group') in groups_to_update]
        return message

    async def delete_message_record(self, msg_doc):
        """
        Delete all media files contained in the given message, and delete the record from db.
        Note that currently the method does not actually remove a record,
        it just mark the record as deleted for possible future references.
        """
        if not msg_doc or msg_doc.get('deleted') or not msg_doc.get('bridge_messages'):
            return

        # Delete all files contained in the message
        for file in msg_doc.get('files', []):
            if not file: continue
            if file.get('path'):
                logger.info(f'Deleting local file {file.get("path")}')
                try:
                    os.remove(file.get('path'))
                except OSError:
                    pass

        # Mark as deleted internally
        await self.collection.update_one({'_id': msg_doc.get('_id')}, {
            '$set': {
                'deleted': True,
                'deleted_at': datetime.utcnow(),
            }
        })

    async def get_active_groups_on_platform(self, user_id, platform='irc') -> list[str]:
        """
        An active user is currently defined as having sent a message in any monitored channel within 10 minutes.

        Currently only active IRC users are tracked to display a system message after they quit.

        TODO: make the duration configurable
        """
        timeout = 600  # in seconds
        deadline = datetime.utcnow() - timedelta(seconds=timeout)
        # TODO: Find the most recent channels
        ret = set()
        messages = self.collection.find({
            'from_user_id': user_id,
            # Exclude system messages like join/quit, change nick, ...
            'system': False,
            'created_at': {
                '$gte': deadline,
            },
        })
        # For each message found, add corresponding groups on given platform
        for message in await messages.to_list(length=10):
            ret |= {bm.get('group') for bm in message.get('bridge_messages', []) if bm.get('group', '').startswith(platform)}

        return list(ret)
