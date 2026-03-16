from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from .config import Settings


async def ensure_authorized(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    settings: Settings = context.bot_data["settings"]
    user = update.effective_user
    if user and user.id in settings.authorized_user_ids:
        return True

    if update.effective_chat:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="Unauthorized user.",
        )
    return False
