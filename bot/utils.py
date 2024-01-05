import asyncio
import logging
from .config import Config

config = Config('bridge.yaml')
logger = logging.getLogger(__name__)
try:
    logger.setLevel(config.get_nowait('Logging', 'level', default='INFO'))
except ValueError:
    # Unknown level, use default INFO level
    pass

# The global message queue used by listeners and workers
message_queue = asyncio.Queue()

async def get_bridge_map():
    bridge_cfg: 'list[list[str]]' = await config.get('Bridge', default=[])
    bridge_map: 'dict[str, list[str]]' = dict()
    for groups in bridge_cfg:
        for group in groups:
            if group in bridge_map:
                logger.warning(f'duplicate mapping in config: {group} - previous mapping will be overwritten')
            # Map each group with other connected groups
            bridge_map[group] = [g for g in groups if g != group]
    return bridge_map

async def get_groups(platform: str):
    """
    Generate all group ids of the given platform, without platform prefix
    """
    for group in (await get_bridge_map()).keys():
        if group.startswith(platform.lower()):
            yield group.split('/', 1)[1]

def normurl(url: str) -> str:
    """
    Add a slash in case the url in config does not.
    """
    if not url: return url
    return url + '/' if url[-1] != '/' else url
