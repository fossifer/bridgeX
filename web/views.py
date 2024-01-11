from django.core.exceptions import PermissionDenied
from django.http import HttpResponse
from django.shortcuts import render
from bot.config import Config

config = Config('bridge.yaml')
filter = Config('filter.yaml')

async def index(request):
    context = {}
    return render(request, 'index.dtl', context)

async def config_view(request):
    return HttpResponse('Hello world')

async def filter_view(request):
    context = {
        'filters': await filter.get('filters'),
    }
    return render(request, 'filter.dtl', context)

def oauth_callback(request):
    # Callback is handled by middleware
    raise PermissionDenied()
