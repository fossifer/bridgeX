import re
from .config import Config
from .message import Message

class Filter:
    def __init__(self):
        self.config = Config('filter.yaml')

    def get_properties(self) -> dict:
        return {
            # Keys: keys used in filter.yaml
            # Values: fields used in message objects and mongodb
            'text': 'text',
            'nick': 'from_nick',
            'fwd_from': 'fwd_from',
        }

    async def test(self, message: Message, to_group: str) -> bool:
        """
        Test if the input message matches any filter.
        """
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
