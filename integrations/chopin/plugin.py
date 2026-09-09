"""Hermes supported Telegram extension; never owns polling or lifecycle state."""
from __future__ import annotations

import asyncio
import os

from .mcp_client import COMPILE_BLOCKED, authorized, create_plan, status
from .private import load_config

NO_CONTEXT = 'Chopin refuses this invocation: authenticated Telegram platform/chat/user context is required.'


def register(ctx):
    # Generic raw-argument callbacks cannot establish caller identity. Keep them
    # inert, including in CLI/other platforms. PTB supplies real Update identity.
    for name in ('plan_status', 'plan_compile'):
        ctx.register_command(name, lambda raw_args: NO_CONTEXT, description='Catalog pilot (authorized Telegram only)')

    def telegram_factory(application, adapter):
        from telegram.ext import ApplicationHandlerStop, CommandHandler

        async def command(update, context):
            try:
                config = load_config(os.environ['CHOPIN_PILOT_CONFIG'])
                chat, user = update.effective_chat, update.effective_user
                if (chat is None or user is None or user.is_bot or update.edited_message is not None
                        or update.channel_post is not None or update.message is None
                        or update.message.sender_chat is not None
                        or ctx.profile_name != 'default'
                        or not authorized(config, 'telegram', str(chat.id), str(user.id))):
                    result = NO_CONTEXT
                else:
                    name = update.message.text.split()[0].split('@')[0].lstrip('/').lower()
                    args = update.message.text.partition(' ')[2].strip()
                    if name == 'plan_compile':
                        result = COMPILE_BLOCKED
                    elif name == 'plan_status':
                        result = await asyncio.to_thread(status, config, args)
                    elif name == 'plan':
                        result = await asyncio.to_thread(create_plan, config, args)
                    else:
                        result = 'Chopin request refused: unsupported command.'
            except Exception:
                result = ('Chopin request refused or unconfirmed; details suppressed. '
                          'If creation was attempted, retry the exact topic to reuse its durable payload/key. '
                          'No document identity is confirmed by this error.')
            if update.effective_message:
                await update.effective_message.reply_text(result, parse_mode=None, disable_web_page_preview=True)
            raise ApplicationHandlerStop

        # Existing Telegram Application, before core group 0. HandlerStop prevents
        # the same command reaching core or an LLM. No getUpdates or new bot.
        application.add_handler(CommandHandler(['plan', 'plan_status', 'plan_compile'], command), group=-10)

    ctx.register_telegram_handler(telegram_factory)
