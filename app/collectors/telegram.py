import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlparse

from telethon import TelegramClient, errors, types, utils
from telethon.tl.functions.channels import JoinChannelRequest, LeaveChannelRequest
from telethon.tl.types import InputPeerChannel, InputPeerChat

from app.config import Settings
from app.scheduler.rate_limit import TelegramRateLimiter
from app.storage.models import Post, Source, TelegramGroupMessage
from app.storage.repositories import RepositoryBundle

logger = logging.getLogger(__name__)


class TelegramSourceError(Exception):
    """Raised when a Telegram source cannot be resolved or used."""


class LargeFloodWait(Exception):
    def __init__(self, seconds: int, operation: str) -> None:
        super().__init__(f"FloodWait {seconds}s during {operation}")
        self.seconds = seconds
        self.operation = operation


@dataclass(frozen=True)
class ResolvedTelegramSource:
    link: str
    username: str | None
    title: str
    entity_id: int
    access_hash: int | None
    entity_type: str


@dataclass(frozen=True)
class PostsSyncResult:
    source_id: int
    initialized: bool
    fetched_count: int
    saved_count: int
    new_posts: list[Post]
    last_message_id: int | None


@dataclass(frozen=True)
class DiscussionSyncResult:
    source_id: int
    initialized: bool
    fetched_count: int
    saved_count: int
    new_messages: list[TelegramGroupMessage]
    last_message_id: int | None


@dataclass(frozen=True)
class MetricsSyncResult:
    source_id: int
    processed_posts: int
    saved_items: int


class TelegramCollector:
    def __init__(
        self,
        *,
        client: TelegramClient,
        settings: Settings,
        repositories: RepositoryBundle,
        rate_limiter: TelegramRateLimiter,
    ) -> None:
        self.client = client
        self.settings = settings
        self.repositories = repositories
        self.rate_limiter = rate_limiter

    async def resolve_source(self, link_or_username: str) -> ResolvedTelegramSource:
        reference = self._normalize_reference(link_or_username)
        try:
            entity = await self._call_telegram(
                "resolve source",
                lambda: self.client.get_entity(reference),
            )
        except ValueError as exc:
            raise TelegramSourceError(
                "Источник недоступен. Проверьте username/link или доступ user-аккаунта."
            ) from exc

        if isinstance(entity, types.Channel):
            return self._resolved_from_channel(link_or_username.strip(), entity)

        if isinstance(entity, types.Chat):
            return self._resolved_from_chat(link_or_username.strip(), entity)

        raise TelegramSourceError("Поддерживаются только Telegram-каналы и группы.")

    async def resolve_source_by_chat_id(
        self,
        chat_id: int,
        *,
        fallback_title: str | None = None,
    ) -> ResolvedTelegramSource:
        entity_id, peer_type = utils.resolve_id(chat_id)
        peer = peer_type(entity_id)
        try:
            entity = await self._call_telegram(
                "resolve forwarded source",
                lambda: self.client.get_entity(peer),
            )
        except ValueError as exc:
            raise TelegramSourceError(
                "Не получилось получить entity из forwarded message. "
                "User-аккаунт должен иметь доступ к этому каналу/группе."
            ) from exc

        link = str(chat_id)
        if isinstance(entity, types.Channel):
            return self._resolved_from_channel(link, entity, fallback_title=fallback_title)
        if isinstance(entity, types.Chat):
            return self._resolved_from_chat(link, entity, fallback_title=fallback_title)
        raise TelegramSourceError("Поддерживаются только Telegram-каналы и группы.")

    async def sync_posts(self, source: Source, limit: int | None = None) -> PostsSyncResult:
        entity = self._entity_from_source(source)
        fetch_limit = limit or self.settings.sync_limit
        messages = await self._call_telegram(
            f"fetch posts for source {source.id}",
            lambda: self.client.get_messages(entity, limit=fetch_limit),
        )
        regular_messages = [message for message in messages if getattr(message, "id", None)]
        if not regular_messages:
            return PostsSyncResult(source.id, False, 0, 0, [], source.last_message_id)

        max_message_id = max(message.id for message in regular_messages)
        if source.last_message_id is None:
            await self.repositories.sources.set_last_message_id(source.id, max_message_id)
            return PostsSyncResult(
                source_id=source.id,
                initialized=True,
                fetched_count=len(regular_messages),
                saved_count=0,
                new_posts=[],
                last_message_id=max_message_id,
            )

        new_messages = sorted(
            [message for message in regular_messages if message.id > source.last_message_id],
            key=lambda message: message.id,
        )
        saved_posts: list[Post] = []
        for message in new_messages:
            post = await self._save_message_as_post(source, message)
            saved_posts.append(post)

        if max_message_id > source.last_message_id:
            await self.repositories.sources.set_last_message_id(source.id, max_message_id)

        return PostsSyncResult(
            source_id=source.id,
            initialized=False,
            fetched_count=len(regular_messages),
            saved_count=len(saved_posts),
            new_posts=saved_posts,
            last_message_id=max(max_message_id, source.last_message_id),
        )

    async def sync_discussion(
        self,
        source: Source,
        limit: int | None = None,
    ) -> DiscussionSyncResult:
        entity = self._entity_from_source(source)
        fetch_limit = limit or self.settings.tg_discussion_fetch_limit
        messages = await self._call_telegram(
            f"fetch discussion messages for source {source.id}",
            lambda: self.client.get_messages(entity, limit=fetch_limit),
        )
        regular_messages = [message for message in messages if getattr(message, "id", None)]
        if not regular_messages:
            return DiscussionSyncResult(source.id, False, 0, 0, [], source.last_message_id)

        max_message_id = max(message.id for message in regular_messages)
        if source.last_message_id is None:
            await self.repositories.sources.set_last_message_id(source.id, max_message_id)
            return DiscussionSyncResult(
                source_id=source.id,
                initialized=True,
                fetched_count=len(regular_messages),
                saved_count=0,
                new_messages=[],
                last_message_id=max_message_id,
            )

        new_messages = sorted(
            [message for message in regular_messages if message.id > source.last_message_id],
            key=lambda message: message.id,
        )
        saved_messages: list[TelegramGroupMessage] = []
        for message in new_messages:
            saved, _created = await self.repositories.group_messages.upsert_message(
                source_id=source.id,
                telegram_message_id=message.id,
                from_id=getattr(message, "sender_id", None),
                date=self._message_date_iso(message),
                text=getattr(message, "message", None),
                message_url=self._post_url(source, message.id),
            )
            saved_messages.append(saved)

        if max_message_id > source.last_message_id:
            await self.repositories.sources.set_last_message_id(source.id, max_message_id)

        return DiscussionSyncResult(
            source_id=source.id,
            initialized=False,
            fetched_count=len(regular_messages),
            saved_count=len(saved_messages),
            new_messages=saved_messages,
            last_message_id=max(max_message_id, source.last_message_id),
        )

    async def sync_reactions(
        self,
        source: Source,
        start_iso: str,
        end_iso: str,
    ) -> MetricsSyncResult:
        posts = await self.repositories.posts.list_by_period(source.id, start_iso, end_iso)
        tracked_limit = source.tracked_posts_limit or self.settings.tg_tracked_posts_limit
        posts = posts[-tracked_limit:]
        if not posts:
            return MetricsSyncResult(source.id, processed_posts=0, saved_items=0)

        entity = self._entity_from_source(source)
        message_ids = [post.telegram_message_id for post in posts]
        messages = await self._call_telegram(
            f"fetch reactions for source {source.id}",
            lambda: self.client.get_messages(entity, ids=message_ids),
        )
        if not isinstance(messages, list):
            messages = [messages]

        post_by_message_id = {post.telegram_message_id: post for post in posts}
        saved = 0
        for message in messages:
            if message is None:
                continue
            post = post_by_message_id.get(message.id)
            if post is None:
                continue
            reactions_total, by_type = self._extract_reactions(message)
            comments_count = self._extract_comments_count(message)
            await self.repositories.posts.update_metrics(post.id, reactions_total, comments_count)
            await self.repositories.snapshots.create_snapshot(
                source_id=source.id,
                post_id=post.id,
                snapshot_type="reactions",
                period_start=start_iso,
                period_end=end_iso,
                reactions_total=reactions_total,
                comments_count=comments_count,
                payload_json=json.dumps({"reactions": by_type}, ensure_ascii=False),
            )
            saved += 1

        return MetricsSyncResult(source.id, processed_posts=len(posts), saved_items=saved)

    async def sync_comments(
        self,
        source: Source,
        start_iso: str,
        end_iso: str,
    ) -> MetricsSyncResult:
        posts = await self.repositories.posts.list_by_period(source.id, start_iso, end_iso)
        if not posts:
            return MetricsSyncResult(source.id, processed_posts=0, saved_items=0)

        entity = self._entity_from_source(source)
        saved_comments = 0
        for post in posts[-self.settings.sync_limit :]:
            try:
                saved_for_post = await self._sync_comments_for_post(source, post, entity)
                saved_comments += saved_for_post
                if saved_for_post:
                    await self.repositories.posts.update_metrics(
                        post.id,
                        post.reactions_total,
                        max(post.comments_count, saved_for_post),
                    )
            except LargeFloodWait:
                raise
            except Exception as exc:
                logger.info(
                    "Comments unavailable for source %s post %s: %s",
                    source.id,
                    post.telegram_message_id,
                    exc,
                )

        return MetricsSyncResult(
            source_id=source.id,
            processed_posts=min(len(posts), self.settings.sync_limit),
            saved_items=saved_comments,
        )

    async def join_source(self, source: Source) -> None:
        entity = self._entity_from_source(source)
        if source.telegram_entity_type != "channel":
            raise TelegramSourceError(
                "Join поддерживается только для публичных каналов/супергрупп."
            )
        await self._call_telegram(
            f"join source {source.id}",
            lambda: self.client(JoinChannelRequest(entity)),
        )

    async def leave_source(self, source: Source) -> None:
        entity = self._entity_from_source(source)
        if source.telegram_entity_type != "channel":
            raise TelegramSourceError("Leave поддерживается только для каналов/супергрупп.")
        await self._call_telegram(
            f"leave source {source.id}",
            lambda: self.client(LeaveChannelRequest(entity)),
        )

    def is_connected(self) -> bool:
        return self.client.is_connected()

    async def _sync_comments_for_post(
        self,
        source: Source,
        post: Post,
        entity: object,
        *,
        retry_after_small_flood: bool = True,
    ) -> int:
        await self.rate_limiter.wait()
        saved = 0
        try:
            async for comment in self.client.iter_messages(
                entity,
                reply_to=post.telegram_message_id,
                limit=self.settings.comments_limit_per_post,
            ):
                if comment is None or getattr(comment, "id", None) is None:
                    continue
                await self.repositories.comments.upsert_comment(
                    source_id=source.id,
                    post_id=post.id,
                    telegram_message_id=comment.id,
                    from_id=getattr(comment, "sender_id", None),
                    date=self._message_date_iso(comment),
                    text=getattr(comment, "message", None),
                )
                saved += 1
        except errors.FloodWaitError as exc:
            seconds = self._flood_seconds(exc)
            if seconds <= self.settings.flood_wait_small_seconds and retry_after_small_flood:
                logger.warning("Small FloodWait %ss during comments sync; sleeping", seconds)
                await asyncio.sleep(seconds + 1)
                return await self._sync_comments_for_post(
                    source,
                    post,
                    entity,
                    retry_after_small_flood=False,
                )
            raise LargeFloodWait(seconds, "sync comments") from exc
        return saved

    async def _save_message_as_post(self, source: Source, message: object) -> Post:
        reactions_total, _by_type = self._extract_reactions(message)
        comments_count = self._extract_comments_count(message)
        return await self.repositories.posts.upsert_post(
            source_id=source.id,
            telegram_message_id=message.id,
            date=self._message_date_iso(message),
            text=getattr(message, "message", None),
            views=getattr(message, "views", None),
            reactions_total=reactions_total,
            comments_count=comments_count,
            post_url=self._post_url(source, message.id),
        )

    async def _call_telegram(self, operation: str, factory):
        await self.rate_limiter.wait()
        try:
            return await factory()
        except errors.FloodWaitError as exc:
            seconds = self._flood_seconds(exc)
            if seconds <= self.settings.flood_wait_small_seconds:
                logger.warning("Small FloodWait %ss during %s; sleeping", seconds, operation)
                await asyncio.sleep(seconds + 1)
                await self.rate_limiter.wait()
                try:
                    return await factory()
                except errors.FloodWaitError as retry_exc:
                    retry_seconds = self._flood_seconds(retry_exc)
                    raise LargeFloodWait(retry_seconds, operation) from retry_exc
            raise LargeFloodWait(seconds, operation) from exc

    def _entity_from_source(self, source: Source) -> object:
        if source.telegram_entity_id is not None:
            if source.telegram_entity_type == "channel" and source.telegram_access_hash is not None:
                return InputPeerChannel(source.telegram_entity_id, source.telegram_access_hash)
            if source.telegram_entity_type == "chat":
                return InputPeerChat(source.telegram_entity_id)

        if source.username:
            return source.username
        return source.link

    def _resolved_from_channel(
        self,
        link: str,
        entity: types.Channel,
        *,
        fallback_title: str | None = None,
    ) -> ResolvedTelegramSource:
        return ResolvedTelegramSource(
            link=link,
            username=entity.username,
            title=entity.title or fallback_title or entity.username or str(entity.id),
            entity_id=entity.id,
            access_hash=entity.access_hash,
            entity_type="channel",
        )

    def _resolved_from_chat(
        self,
        link: str,
        entity: types.Chat,
        *,
        fallback_title: str | None = None,
    ) -> ResolvedTelegramSource:
        return ResolvedTelegramSource(
            link=link,
            username=None,
            title=entity.title or fallback_title or str(entity.id),
            entity_id=entity.id,
            access_hash=None,
            entity_type="chat",
        )

    def _normalize_reference(self, value: str) -> str:
        reference = value.strip()
        if not reference:
            raise TelegramSourceError("Передайте username или ссылку на источник.")

        if reference.startswith("@"):
            return reference[1:]

        parsed = urlparse(reference)
        if parsed.netloc in {"t.me", "telegram.me"}:
            path = parsed.path.strip("/")
            if not path:
                raise TelegramSourceError("Ссылка не содержит username источника.")
            return path

        return reference

    def _extract_reactions(self, message: object) -> tuple[int, dict[str, int]]:
        reactions = getattr(message, "reactions", None)
        results = getattr(reactions, "results", None) or []
        by_type: dict[str, int] = {}
        total = 0
        for item in results:
            count = int(getattr(item, "count", 0) or 0)
            key = self._reaction_key(getattr(item, "reaction", None))
            by_type[key] = by_type.get(key, 0) + count
            total += count
        return total, by_type

    def _reaction_key(self, reaction: object) -> str:
        if isinstance(reaction, types.ReactionEmoji):
            return reaction.emoticon
        if isinstance(reaction, types.ReactionCustomEmoji):
            return f"custom:{reaction.document_id}"
        if reaction is None:
            return "unknown"
        return reaction.__class__.__name__

    def _extract_comments_count(self, message: object) -> int:
        replies = getattr(message, "replies", None)
        return int(getattr(replies, "replies", 0) or 0)

    def _message_date_iso(self, message: object) -> str:
        raw_date = getattr(message, "date", None)
        if raw_date is None:
            return datetime.now(UTC).isoformat()
        if raw_date.tzinfo is None:
            raw_date = raw_date.replace(tzinfo=UTC)
        return raw_date.astimezone(UTC).isoformat()

    def _post_url(self, source: Source, message_id: int) -> str | None:
        if not source.username:
            return None
        return f"https://t.me/{source.username}/{message_id}"

    def _flood_seconds(self, exc: errors.FloodWaitError) -> int:
        return int(getattr(exc, "seconds", None) or getattr(exc, "value", 0) or 0)
