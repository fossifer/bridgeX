import yaml
import asyncio
from functools import reduce

class Config:
    """
    This class factory will return the same config instance for each path

    path: filename of the yaml config file
    """

    def __new__(cls, *args, **kw):
        path = kw.get('path') or (args[0] if len(args) else None)
        if not path:
            raise TypeError('The creation of Config missing path argument, which can be either passed as positional or keyword')
        if not hasattr(cls, '_instances'):
            cls._instances = {}
        if not cls._instances.get(path):
            orig = super(Config, cls)
            # Object parent class only takes the type itself as argument
            cls._instances[path] = orig.__new__(cls)
            # Initialize (the normal __init__() but only called once)
            cls._instances[path]._path = path
            cls._instances[path]._data = None
            cls._instances[path]._lock = asyncio.Lock()
        return cls._instances.get(path)

    def __init__(self, path: str=''):
        # Just for argument hint when doing Config(...)
        pass

    async def load(self):
        async with self._lock:
            with open(self._path, 'r') as yaml_file:
                self._data = yaml.safe_load(yaml_file) or {}

    async def get(self, *keys, default=None):
        async with self._lock:
            if self._data is None:
                # Load config from file if data is empty
                self._lock.release()
                await self.load()
                await self._lock.acquire()
            current_dict = self._data
            for key in keys:
                if key not in current_dict:
                    return default
                current_dict = current_dict[key]
            return current_dict

    def get_nowait(self, *keys, default=None):
        """
        This method is NOT coroutine safe.
        """
        if self._data is None:
            with open(self._path, 'r') as yaml_file:
                self._data = yaml.safe_load(yaml_file) or {}
        current_dict = self._data
        for key in keys:
            if key not in current_dict:
                return default
            current_dict = current_dict[key]
        return current_dict

    async def set(self, value, *keys):
        async with self._lock:
            if self._data is None:
                self._lock.release()
                await self.load()
                await self._lock.acquire()
            current_dict = self._data
            for key in keys[:-1]:
                current_dict = current_dict.setdefault(key, {})
            current_dict[keys[-1]] = value
            with open(self._path, 'w') as yaml_file:
                yaml.dump(self._data, yaml_file)

    async def delete(self, *keys):
        async with self._lock:
            if self._data is None:
                self._lock.release()
                await self.load()
                await self._lock.acquire()
            current_dict = self._data
            for key in keys[:-1]:
                current_dict = current_dict.get(key, {})
            if keys[-1] in current_dict:
                del current_dict[keys[-1]]
                with open(self._path, 'w') as yaml_file:
                    yaml.dump(self._data, yaml_file)

async def main():
    # Run some tests here
    c = Config(path='tests/bridge-bak.yaml')
    d = Config(path='tests/bridge-bak.yaml')
    irc_cfg = await c.get('IRC')
    print(irc_cfg)
    import time  # So the new password will change in each run
    new_password = 'helloWorld123!' + str(time.time())
    irc_cfg['password'] = new_password
    await d.set(irc_cfg, 'IRC')
    # Should be the same instance
    assert((await c.get('IRC', 'password')) == new_password)

    e = Config(path='tests/test.yaml')
    await e.set([1, 2, 3, 4], 'Test')
    # Not the same instance as bridge-bak
    assert((await e.get('IRC')) is None)

if __name__ == '__main__':
    asyncio.get_event_loop().run_until_complete(main())
