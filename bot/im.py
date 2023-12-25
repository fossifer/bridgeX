from abc import ABC, abstractmethod
from .message import File

class MessagingPlatform(ABC):
    """
    Abstract base class for all singleton IM listener.
    """

    def __new__(cls, *args, **kwargs):
        if not hasattr(cls, '_instance'):
            cls._instance = None
        if not cls._instance:
            orig = super(MessagingPlatform, cls)
            cls._instance = orig.__new__(cls, *args, **kwargs)
        return cls._instance

    def __init__(self):
        self.bot = None

    @abstractmethod
    async def download_media(self, message) -> list[File]:
        pass

    @abstractmethod
    def register_listeners(self):
        pass

    @abstractmethod
    async def construct_files(self, files: list[File]):
        pass
