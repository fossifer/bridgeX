import re
import asyncio
import aiohttp
from .config import Config
from .message import Message

class Filter:
    def __init__(self):
        self.config = Config('filter.yaml')
        self.bridge_config = Config('bridge.yaml')

    def get_properties(self) -> dict:
        return {
            # Keys: keys used in filter.yaml
            # Values: fields used in message objects and mongodb
            'text': 'text',
            'nick': 'from_nick',
            'fwd_from': 'fwd_from',
        }

    async def check_spam_api(self, message: Message) -> bool:
        """
        Check if a Telegram message is spam using the external API.
        
        Args:
            message: Message object to check
            
        Returns:
            bool: True if message is spam, False otherwise
        """
        # Only check Telegram messages
        if not message.from_group or not message.from_group.startswith('telegram/'):
            return False
            
        # Extract chat_id from from_group (format: "telegram/chat_id")
        try:
            chat_id = int(message.from_group.split('/', 1)[1])
        except (ValueError, IndexError):
            return False
            
        # Get required IDs
        user_id = message.from_user_id
        message_id = message.from_message_id
        
        if not all([user_id, message_id]):
            return False
            
        # Get API configuration
        try:
            api_key = await self.bridge_config.get('SpamCheck', 'api_key')
            base_url = await self.bridge_config.get('SpamCheck', 'base_url', default='https://tg-cleaner.toolforge.org')
            delay_ms = await self.bridge_config.get('SpamCheck', 'delay_ms', default=1000)
        except Exception as e:
            # If config is missing, don't filter
            return False
            
        # Add delay to wait for remote antispam module
        await asyncio.sleep(delay_ms / 1000.0)
        
        # Prepare API request
        url = f"{base_url}/api/spam-check"
        headers = {
            "X-API-Key": api_key,
            "Content-Type": "application/json"
        }
        data = {
            "message_id": message_id,
            "chat_id": chat_id,
            "user_id": user_id
        }
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=data, headers=headers) as response:
                    if response.status == 200:
                        result = await response.json()
                        return result.get('is_spam', False)
                    else:
                        # If API call fails, don't filter the message
                        return False
        except Exception as e:
            # If API call fails, don't filter the message
            return False

    async def test(self, message: Message, to_group: str) -> bool:
        """
        Test if the input message matches any filter.
        """
        # First check spam API for Telegram messages
        if await self.check_spam_api(message):
            return True
            
        # Then check regular filters
        filters = await self.config.get('filters')
        for filter in filters:
            event = filter.get('event')
            # Default event is 'send'
            if not event or event == 'send':
                if not re.search(filter.get('group', ''), message.from_group):
                    continue
            elif event == 'receive':
                if not re.search(filter.get('group', ''), to_group):
                    continue
            else:
                # Invalid event
                continue

            # Make sure all properties in the filter are matching
            for key, field in self.get_properties().items():
                if not filter.get(key):
                    continue
                try:
                    if not re.search(filter.get(key), getattr(message, field)):
                        # This filter does not match
                        break
                except AttributeError:
                    break
            else:
                return True

            # If filter reply as well (default is true), check if the replied message matches filter
            if filter.get('filter_reply') == False or not message.reply_to:
                continue
            for key, field in self.get_properties().items():
                if not filter.get(key):
                    continue
                if not re.search(filter.get(key), message.reply_to.get(field)):
                    break
            else:
                return True

        return False
