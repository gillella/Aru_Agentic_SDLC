"""Offline compatibility probe, invoked ONLY with a temporary Hermes home.

Uses actual PluginManager discovery and PTB Application.process_update. Fake
Telegram HTTP responses and MCP calls are fixtures, not live integration proof.
"""
import argparse
import asyncio
import importlib
import importlib.util
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch


def discover_plugin():
    home = Path(os.environ['HERMES_HOME'])
    if home != Path(os.environ['HOME']) or not (home / 'OFFLINE_FIXTURE').is_file():
        raise RuntimeError('Probe requires an explicitly marked temporary home.')
    from hermes_cli.plugins import PluginManager

    manager = PluginManager()
    manager.discover_and_load()
    factories = [factory for factory, name in manager.get_telegram_handler_factories()
                 if name == 'chopin-pilot']
    assert len(factories) == 1, 'Actual Telegram factory registration missing or duplicated'
    for name in ('plan_status', 'plan_compile'):
        assert 'context is required' in manager._plugin_commands[name]['handler']('arbitrary')
    print('Actual Hermes PluginManager registration: passed', flush=True)
    return factories[0]


def probe_telegram(factory):  # noqa: C901 -- bounded offline command fixture
    # Only the absence of the top-level optional extra permits an unavailable
    # result. A broken installed module or incompatible API is a hard failure.
    if importlib.util.find_spec('telegram') is None:
        print('TELEGRAM_EXTRA_UNAVAILABLE: selected Hermes installation lacks telegram')
        return 77
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

    module = importlib.import_module(factory.__module__)
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
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--telegram', action='store_true', help='Also exercise real PTB command dispatch')
    args = parser.parse_args()
    factory = discover_plugin()
    return probe_telegram(factory) if args.telegram else 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        # This probe has exclusively dummy fixtures, no live secrets.
        print(type(exc).__name__ + ': ' + str(exc), file=sys.stderr)
        raise
