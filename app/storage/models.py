from dataclasses import dataclass

from aiosqlite import Row


@dataclass(frozen=True)
class Source:
    id: int
    kind: str
    link: str
    username: str | None
    title: str
    telegram_entity_id: int | None
    telegram_access_hash: int | None
    telegram_entity_type: str | None
    telegram_monitor_mode: str
    tracked_posts_limit: int | None
    last_message_id: int | None
    is_active: bool
    last_error: str | None
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: Row) -> "Source":
        return cls(
            id=row["id"],
            kind=row["kind"],
            link=row["link"],
            username=row["username"],
            title=row["title"],
            telegram_entity_id=row["telegram_entity_id"],
            telegram_access_hash=row["telegram_access_hash"],
            telegram_entity_type=row["telegram_entity_type"],
            telegram_monitor_mode=row["telegram_monitor_mode"],
            tracked_posts_limit=row["tracked_posts_limit"],
            last_message_id=row["last_message_id"],
            is_active=bool(row["is_active"]),
            last_error=row["last_error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @property
    def display_name(self) -> str:
        if self.title:
            return self.title
        if self.username:
            return f"@{self.username}"
        return self.link

    @property
    def telegram_reference_id(self) -> int | None:
        if self.telegram_entity_id is None:
            return None
        if self.telegram_entity_type == "channel":
            return -(1_000_000_000_000 + abs(self.telegram_entity_id))
        return self.telegram_entity_id


@dataclass(frozen=True)
class Post:
    id: int
    source_id: int
    telegram_message_id: int
    date: str
    text: str | None
    views: int | None
    reactions_total: int
    comments_count: int
    post_url: str | None
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: Row) -> "Post":
        return cls(
            id=row["id"],
            source_id=row["source_id"],
            telegram_message_id=row["telegram_message_id"],
            date=row["date"],
            text=row["text"],
            views=row["views"],
            reactions_total=row["reactions_total"],
            comments_count=row["comments_count"],
            post_url=row["post_url"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


@dataclass(frozen=True)
class Comment:
    id: int
    source_id: int
    post_id: int
    telegram_message_id: int
    from_id: int | None
    date: str
    text: str | None
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: Row) -> "Comment":
        return cls(
            id=row["id"],
            source_id=row["source_id"],
            post_id=row["post_id"],
            telegram_message_id=row["telegram_message_id"],
            from_id=row["from_id"],
            date=row["date"],
            text=row["text"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


@dataclass(frozen=True)
class TelegramGroupMessage:
    id: int
    source_id: int
    telegram_message_id: int
    from_id: int | None
    date: str
    text: str | None
    message_url: str | None
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: Row) -> "TelegramGroupMessage":
        return cls(
            id=row["id"],
            source_id=row["source_id"],
            telegram_message_id=row["telegram_message_id"],
            from_id=row["from_id"],
            date=row["date"],
            text=row["text"],
            message_url=row["message_url"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


@dataclass(frozen=True)
class TelegramKeyword:
    id: int
    keyword: str
    is_active: bool
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: Row) -> "TelegramKeyword":
        return cls(
            id=row["id"],
            keyword=row["keyword"],
            is_active=bool(row["is_active"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


@dataclass(frozen=True)
class VkSource:
    id: int
    group_id: int
    group_name: str | None
    screen_name: str | None
    is_active: bool
    monitor_mode: str
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: Row) -> "VkSource":
        return cls(
            id=row["id"],
            group_id=row["group_id"],
            group_name=row["group_name"],
            screen_name=row["screen_name"],
            is_active=bool(row["is_active"]),
            monitor_mode=row["monitor_mode"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @property
    def display_name(self) -> str:
        return self.group_name or self.screen_name or str(self.group_id)


@dataclass(frozen=True)
class VkPost:
    id: int
    group_id: int
    post_id: int
    owner_id: int
    text: str | None
    date: str
    likes_count: int
    comments_count: int
    reposts_count: int
    views_count: int
    url: str | None
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: Row) -> "VkPost":
        return cls(
            id=row["id"],
            group_id=row["group_id"],
            post_id=row["post_id"],
            owner_id=row["owner_id"],
            text=row["text"],
            date=row["date"],
            likes_count=row["likes_count"],
            comments_count=row["comments_count"],
            reposts_count=row["reposts_count"],
            views_count=row["views_count"],
            url=row["url"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


@dataclass(frozen=True)
class VkComment:
    id: int
    group_id: int
    post_id: int
    comment_id: int
    from_id: int | None
    text: str | None
    date: str
    parent_comment_id: int | None
    is_deleted: bool
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: Row) -> "VkComment":
        return cls(
            id=row["id"],
            group_id=row["group_id"],
            post_id=row["post_id"],
            comment_id=row["comment_id"],
            from_id=row["from_id"],
            text=row["text"],
            date=row["date"],
            parent_comment_id=row["parent_comment_id"],
            is_deleted=bool(row["is_deleted"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
