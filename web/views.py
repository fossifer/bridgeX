from django.http import HttpResponse
from bot.config import Config

config = Config('bridge.yaml')

async def index(request):
    return HttpResponse(f"Hello, world. IRC host: {await config.get('IRC', 'host')}.")
