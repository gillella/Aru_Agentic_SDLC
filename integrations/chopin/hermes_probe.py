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
    from telegram.ext import Application, ApplicationHandlerStop, ExtBot
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
            cases = [('/plan catalog', -5325492504, 6431233670, 'create', 'Fixture draft confirmed'),
                     ('/plan_status doc1', -5325492504, 6431233670, 'status', 'authoritative comments/decisions unavailable'),
                     ('/plan_compile doc1', -5325492504, 6431233670, None, 'Compilation disabled'),
                     ('/plan catalog', -5325492504, 99999, None, 'context is required'),
                     ('/plan catalog', -99, 6431233670, None, 'context is required'),
                     ('/Plan_compile doc1', -5325492504, 6431233670, None, 'Compilation disabled'),
                     ('/PLAN_COMPILE@fixture_bot doc1', -5325492504, 6431233670, None, 'Compilation disabled'),
                     ('/Plan_status doc1', -5325492504, 6431233670, 'status', 'authoritative comments/decisions unavailable'),
                     ('/PLAN_STATUS@fixture_bot doc1', -5325492504, 6431233670, 'status', 'authoritative comments/decisions unavailable'),
                     ('/Plan catalog', -5325492504, 6431233670, 'create', 'Fixture draft confirmed')]

            def update(index, text, chat=-5325492504, user=6431233670):
                return Update.de_json({'update_id': index, 'message': {
                    'message_id': index + 1, 'date': 1, 'chat': {'id': chat, 'type': 'group'},
                    'from': {'id': user, 'is_bot': False, 'first_name': 'Fixture'}, 'text': text,
                    'entities': [{'type': 'bot_command', 'offset': 0, 'length': len(text.split()[0])}]}}, bot)

            for index, (text, chat, user, operation, response) in enumerate(cases):
                before_calls, before_sent = len(calls), len(sent)
                await app.process_update(update(index, text, chat, user))
                expected = [(operation, 'gillella/unum-catalog', text.partition(' ')[2])] if operation else []
                assert calls[before_calls:] == expected, (text, calls[before_calls:])
                assert len(sent) == before_sent + 1, 'Real command invocation did not produce exactly one response'
                assert response in sent[-1], (text, sent[-1])

            # Real PTB ignores unknown commands. The callback must also refuse
            # them if directly invoked or registered more broadly in the future.
            event = update(len(cases), '/unknown catalog')
            before_calls, before_sent = len(calls), len(sent)
            await app.process_update(event)
            assert len(calls) == before_calls and len(sent) == before_sent
            try:
                await app.handlers[-10][0].callback(event, None)
            except ApplicationHandlerStop:
                pass
            else:
                raise AssertionError('Unknown callback command did not stop handler dispatch')
            assert len(calls) == before_calls, 'Unknown callback command invoked a reader or writer'
            assert len(sent) == before_sent + 1 and 'unsupported command' in sent[-1]
        finally:
            await app.shutdown()

    with patch.object(module, 'create_plan', create), patch.object(module, 'status', status):
        asyncio.run(exercise())
    print('Actual Hermes discovery + PTB command dispatch: 12 cases passed; all network is fixture-only.')
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
