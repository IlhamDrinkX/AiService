"""
Read-only Discord connector.

- Backfills history on startup for: regular text channels, voice-channel text
  chat, threads (regular + forum posts), including archived threads.
- Listens live for new messages, edits, and deletes across all of the above,
  and stores them as they happen.
- Joins newly created threads/forum posts on the fly so their messages are
  captured without a restart.
- Never sends messages, never modifies anything on the server.

Config comes from environment variables (see .env.example).
"""

import asyncio
import logging
import os
from datetime import datetime, timezone

import aiohttp
import discord
from dotenv import load_dotenv

from storage import DiscordStorage

load_dotenv()

TOKEN = os.environ["DISCORD_BOT_TOKEN"]
DB_PATH = os.environ.get("DB_PATH", "./data/discord.db")
BACKFILL_LIMIT = int(os.environ.get("BACKFILL_LIMIT", "200"))
ARCHIVED_THREAD_LIMIT = int(os.environ.get("ARCHIVED_THREAD_LIMIT", "100"))
INCLUDE_THREADS = os.environ.get("INCLUDE_THREADS", "true").lower() == "true"
INCLUDE_ARCHIVED_THREADS = os.environ.get("INCLUDE_ARCHIVED_THREADS", "true").lower() == "true"
INCLUDE_FORUM = os.environ.get("INCLUDE_FORUM", "true").lower() == "true"
INCLUDE_VOICE_TEXT = os.environ.get("INCLUDE_VOICE_TEXT", "true").lower() == "true"
# Comma-separated channel IDs to restrict to. Empty = every channel the bot
# can see. For threads/forum posts, matching the PARENT channel's id is
# enough to pull in all its threads/posts.
CHANNEL_IDS = {
    c.strip() for c in os.environ.get("CHANNEL_IDS", "").split(",") if c.strip()
}
# Transient network hiccups (seen in practice: flaky VPN truncating HTTP
# responses mid-stream) shouldn't permanently skip a channel's backfill.
BACKFILL_MAX_RETRIES = int(os.environ.get("BACKFILL_MAX_RETRIES", "3"))
BACKFILL_RETRY_BASE_DELAY = float(os.environ.get("BACKFILL_RETRY_BASE_DELAY", "2"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("discord_connector")

intents = discord.Intents.default()
intents.message_content = True   # privileged intent, must be enabled in Developer Portal
intents.guilds = True
intents.messages = True

client = discord.Client(intents=intents)
storage = DiscordStorage(DB_PATH)


def _channel_type(channel) -> str:
    if isinstance(channel, discord.Thread):
        parent = channel.parent
        return "forum_post" if isinstance(parent, discord.ForumChannel) else "thread"
    if isinstance(channel, discord.VoiceChannel):
        return "voice"
    return "text"


def _parent_channel_id(channel) -> str | None:
    if isinstance(channel, discord.Thread) and channel.parent_id:
        return str(channel.parent_id)
    return None


def _to_record(message: discord.Message) -> dict:
    reply_to = None
    if message.reference and message.reference.message_id:
        reply_to = str(message.reference.message_id)
    return {
        "message_id": str(message.id),
        "guild_id": str(message.guild.id) if message.guild else None,
        "guild_name": message.guild.name if message.guild else None,
        "channel_id": str(message.channel.id),
        "channel_name": getattr(message.channel, "name", None),
        "channel_type": _channel_type(message.channel),
        "parent_channel_id": _parent_channel_id(message.channel),
        "author_id": str(message.author.id),
        # display_name — ник на сервере (обычно настоящее имя человека, как в
        # UI Discord), а не технический юзернейм str(message.author) вроде
        # "vanche5102". Discord присылает объект участника вместе с самим
        # сообщением даже без привилегированного Members-интента, так что
        # доступно всегда, отдельный интент не нужен. По просьбе пользователя
        # (2026-07-19) — раньше в упоминаниях и списке сообщений было видно
        # либо голый ID, либо нечитаемый юзернейм.
        "author_name": getattr(message.author, "display_name", None) or str(message.author),
        "content": message.content,
        "attachments": [a.url for a in message.attachments],
        "reply_to_message_id": reply_to,
        "created_at": message.created_at.astimezone(timezone.utc).isoformat(),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def _wanted(channel) -> bool:
    """Whether we should read/store messages from this channel/thread."""
    if isinstance(channel, discord.Thread):
        if not INCLUDE_THREADS:
            return False
        if isinstance(channel.parent, discord.ForumChannel) and not INCLUDE_FORUM:
            return False
        if not CHANNEL_IDS:
            return True
        parent_id = str(channel.parent_id) if channel.parent_id else None
        return str(channel.id) in CHANNEL_IDS or (parent_id is not None and parent_id in CHANNEL_IDS)
    if isinstance(channel, discord.VoiceChannel):
        if not INCLUDE_VOICE_TEXT:
            return False
        if not CHANNEL_IDS:
            return True
        return str(channel.id) in CHANNEL_IDS
    if isinstance(channel, discord.TextChannel):
        if not CHANNEL_IDS:
            return True
        return str(channel.id) in CHANNEL_IDS
    return False


async def _backfill_channel(channel) -> int:
    """Backfill one channel's history, retrying on transient network errors.

    A retry restarts the channel from scratch rather than resuming — cheap
    at BACKFILL_LIMIT sizes, and safe because save_message() is idempotent
    (INSERT OR IGNORE), so re-fetched messages don't duplicate.
    """
    name = getattr(channel, "name", channel.id)
    for attempt in range(1, BACKFILL_MAX_RETRIES + 1):
        count = 0
        try:
            async for message in channel.history(limit=BACKFILL_LIMIT):
                storage.save_message(_to_record(message))
                count += 1
            return count
        except discord.Forbidden:
            log.warning("  #%s: no read permission, skipped", name)
            return count
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            if attempt == BACKFILL_MAX_RETRIES:
                log.warning(
                    "  #%s: network error after %d attempt(s) (%s), skipping for this run — "
                    "will retry on next startup",
                    name, attempt, e,
                )
                return count
            delay = BACKFILL_RETRY_BASE_DELAY * attempt
            log.warning(
                "  #%s: transient network error (%s), retry %d/%d in %.0fs",
                name, e, attempt, BACKFILL_MAX_RETRIES, delay,
            )
            await asyncio.sleep(delay)
    return 0


async def _backfill_threads_of(parent_channel) -> None:
    """Backfill active + archived threads/forum-posts under a parent channel.

    Only TextChannel and ForumChannel support threads — VoiceChannel does
    not, so it's excluded here rather than relying on callers to filter.
    """
    if not isinstance(parent_channel, (discord.TextChannel, discord.ForumChannel)):
        return
    if not INCLUDE_THREADS:
        return
    if isinstance(parent_channel, discord.ForumChannel) and not INCLUDE_FORUM:
        return

    for thread in list(getattr(parent_channel, "threads", [])):
        if not _wanted(thread):
            continue
        count = await _backfill_channel(thread)
        log.info("    thread #%s (under #%s): backfilled %d messages", thread.name, parent_channel.name, count)

    if not INCLUDE_ARCHIVED_THREADS:
        return
    try:
        async for thread in parent_channel.archived_threads(limit=ARCHIVED_THREAD_LIMIT):
            if not _wanted(thread):
                continue
            count = await _backfill_channel(thread)
            log.info(
                "    archived thread #%s (under #%s): backfilled %d messages",
                thread.name, parent_channel.name, count,
            )
    except discord.Forbidden:
        log.warning("    #%s: no permission to list archived threads, skipped", parent_channel.name)
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        log.warning(
            "    #%s: network error listing archived threads (%s), will retry on next startup",
            parent_channel.name, e,
        )


@client.event
async def on_ready():
    log.info("Logged in as %s", client.user)
    for guild in client.guilds:
        log.info("Backfilling guild: %s", guild.name)
        for channel in guild.channels:
            try:
                if isinstance(channel, (discord.TextChannel, discord.VoiceChannel)):
                    if _wanted(channel):
                        count = await _backfill_channel(channel)
                        log.info("  #%s: backfilled %d messages", channel.name, count)
                    await _backfill_threads_of(channel)
                elif isinstance(channel, discord.ForumChannel):
                    await _backfill_threads_of(channel)
            except Exception:
                # One bad channel shouldn't abort backfill for the rest of
                # the guild — log and move on.
                log.exception("  #%s: backfill failed, skipping", getattr(channel, "name", channel.id))
    log.info("Backfill done. Total stored: %d", storage.count())


@client.event
async def on_thread_create(thread: discord.Thread):
    """New thread/forum post created after startup — join it and pull any
    messages it already has (forum posts start with the OP message)."""
    if not _wanted(thread):
        return
    try:
        await thread.join()
    except (discord.Forbidden, discord.HTTPException):
        pass
    count = await _backfill_channel(thread)
    log.info("New thread #%s: backfilled %d messages, now listening live", thread.name, count)


@client.event
async def on_message(message: discord.Message):
    if not _wanted(message.channel):
        return
    storage.save_message(_to_record(message))


@client.event
async def on_raw_message_edit(payload: discord.RawMessageUpdateEvent):
    channel = client.get_channel(payload.channel_id)
    # If the channel isn't cached (e.g. an old archived thread) we can't
    # confirm it's in scope; err on the side of recording rather than
    # silently dropping an edit we might care about.
    if channel is not None and not _wanted(channel):
        return

    new_content = payload.data.get("content")
    if new_content is None and channel is not None:
        # Edit event without content in the payload (e.g. embed-only edit) —
        # fetch the current state instead of skipping it.
        try:
            msg = await channel.fetch_message(payload.message_id)
            new_content = msg.content
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return
    if new_content is None:
        return

    old_content = payload.cached_message.content if payload.cached_message else None
    storage.record_edit(
        str(payload.message_id),
        new_content,
        old_content,
        datetime.now(timezone.utc).isoformat(),
    )


@client.event
async def on_raw_message_delete(payload: discord.RawMessageDeleteEvent):
    storage.record_delete(str(payload.message_id), datetime.now(timezone.utc).isoformat())


@client.event
async def on_raw_bulk_message_delete(payload: discord.RawBulkMessageDeleteEvent):
    now = datetime.now(timezone.utc).isoformat()
    for message_id in payload.message_ids:
        storage.record_delete(str(message_id), now)


if __name__ == "__main__":
    asyncio.run(client.start(TOKEN))
