from html import escape

from app.analytics.periods import parse_period
from app.storage.models import Post


class DashboardService:
    def __init__(self, sources, posts, comments) -> None:
        self.sources = sources
        self.posts = posts
        self.comments = comments

    async def render(self, source_id: int, period_value: str) -> str:
        source = await self.sources.get_source(source_id)
        if source is None or not source.is_active:
            raise ValueError("Source not found or inactive.")

        period = parse_period(period_value)
        posts = await self.posts.list_by_period(source_id, period.start_iso, period.end_iso)
        new_posts = await self.posts.count_created_by_period(
            source_id,
            period.start_iso,
            period.end_iso,
        )
        comments_count = await self.comments.count_by_period(
            source_id,
            period.start_iso,
            period.end_iso,
        )

        posts_count = len(posts)
        reactions_total = sum(post.reactions_total for post in posts)
        average_reactions = reactions_total / posts_count if posts_count else 0
        average_comments = comments_count / posts_count if posts_count else 0

        top_reactions = sorted(posts, key=lambda item: item.reactions_total, reverse=True)[:3]
        top_comments = sorted(posts, key=lambda item: item.comments_count, reverse=True)[:3]

        return "\n".join(
            [
                "<b>Argus dashboard</b>",
                f"Источник: {escape(source.display_name)}",
                f"Период: {escape(period.label)}",
                "",
                f"Постов: {posts_count}",
                f"Новых постов: {new_posts}",
                f"Комментариев: {comments_count}",
                f"Реакций всего: {reactions_total}",
                f"Среднее реакций на пост: {average_reactions:.2f}",
                f"Среднее комментариев на пост: {average_comments:.2f}",
                "",
                "Топ-3 поста по реакциям:",
                *self._format_top(top_reactions, metric="reactions"),
                "",
                "Топ-3 поста по комментариям:",
                *self._format_top(top_comments, metric="comments"),
            ]
        )

    def _format_top(self, posts: list[Post], *, metric: str) -> list[str]:
        if not posts:
            return ["Нет данных."]

        lines: list[str] = []
        for index, post in enumerate(posts, start=1):
            value = post.reactions_total if metric == "reactions" else post.comments_count
            snippet = self._snippet(post)
            lines.append(f"{index}. {snippet} - {value}")
        return lines

    def _snippet(self, post: Post) -> str:
        text = (post.text or "").replace("\n", " ").strip()
        if len(text) > 80:
            text = f"{text[:77]}..."
        if not text:
            text = f"message_id={post.telegram_message_id}"
        if post.post_url:
            text = f"{text} ({post.post_url})"
        return escape(text)
