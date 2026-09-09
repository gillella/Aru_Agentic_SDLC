"""Offline compatibility probe, invoked ONLY with a temporary Hermes home.

Uses actual PluginManager discovery and PTB Application.process_update. Fake
Telegram HTTP responses and MCP calls are fixtures, not live integration proof.
"""
import asyncio
import importlib
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch


def main():  # noqa: C901 -- bounded offline compatibility probe
    home = Path(os.environ['HERMES_HOME'])
    if home != Path(os.environ['HOME']) or not (home / 'OFFLINE_FIXTURE').is_file():
        raise RuntimeError('Probe requires an explicitly marked temporary home.')
    from hermes_cli.plugins import PluginManager
    from telegram import Update
    from telegram.ext import Application, ExtBot
    from telegram.request import BaseRequest

    sent = []

    class FixtureHTTP(BaseRequest):
        @property
        def read_timeout(self):
            return 2

        async def initialize(self):
            pass

        async def shutdown(self):
            pass

        async def do_request(self, url, method, request_data=None, **kwargs):
            if url.endswith('/getMe'):
                result = {'id': 999, 'is_bot': True, 'first_name': 'Fixture', 'username': 'fixture_bot'}
            elif url.endswith('/sendMessage'):
                fields = request_data.parameters
                sent.append(fields['text'])
                result = {'message_id': len(sent), 'date': 1, 'chat': {'id': int(fields['chat_id']), 'type': 'group'}, 'text': fields['text']}
            else:
                raise AssertionError('Unexpected Telegram operation')
            return 200, json.dumps({'ok': True, 'result': result}).encode()

    manager = PluginManager()
    manager.discover_and_load()
    factories = manager.get_telegram_handler_factories()
    factory = next(factory for factory, name in factories if name == 'chopin-pilot')
    module = importlib.import_module(factory.__module__)
    # Real loaded fallback callbacks receive only raw args and must stay inert.
    for name in ('plan_status', 'plan_compile'):
        assert 'context is required' in manager._plugin_commands[name]['handler']('arbitrary')
    bot = ExtBot('123456:OFFLINE_FIXTURE_ONLY', request=FixtureHTTP(), get_updates_request=FixtureHTTP())
    app = Application.builder().bot(bot).build()
    factory(app, None)
    calls = []

    def create(config, topic):
        calls.append(('create', config['repository'], topic))
        return 'Fixture draft confirmed'

    def status(config, args):
        calls.append(('status', config['repository'], args))
        return 'Fixture source revision; authoritative comments/decisions unavailable'

    async def exercise():
        await app.initialize()
        try:
            cases = [('/plan catalog', -5325492504, 6431233670, True),
                     ('/plan_status doc1', -5325492504, 6431233670, True),
                     ('/plan_compile doc1', -5325492504, 6431233670, True),
                     ('/plan catalog', -5325492504, 99999, False),
                     ('/plan catalog', -99, 6431233670, False)]
            for index, (text, chat, user, allowed) in enumerate(cases):
                event = Update.de_json({'update_id': index, 'message': {
                    'message_id': index + 1, 'date': 1, 'chat': {'id': chat, 'type': 'group'},
                    'from': {'id': user, 'is_bot': False, 'first_name': 'Fixture'}, 'text': text,
                    'entities': [{'type': 'bot_command', 'offset': 0, 'length': len(text.split()[0])}]}}, bot)
                await app.process_update(event)
                assert sent, 'Real command invocation did not produce a response'
                if not allowed:
                    assert 'context is required' in sent[-1]
            assert len(calls) == 2, calls
            assert calls[0] == ('create', 'gillella/unum-catalog', 'catalog')
            assert 'Compilation disabled' in sent[2]
            assert len(sent) == 5
        finally:
            await app.shutdown()

    with patch.object(module, 'create_plan', create), patch.object(module, 'status', status):
        asyncio.run(exercise())
    print('Actual Hermes discovery + PTB command dispatch: 5 cases passed; all network is fixture-only.')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # This probe has exclusively dummy fixtures, no live secrets.
        print(type(exc).__name__ + ': ' + str(exc), file=sys.stderr)
        raise
