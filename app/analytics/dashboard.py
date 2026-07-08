from html import escape
from io import BytesIO
import os
from pathlib import Path

from app.analytics.periods import parse_period
from app.storage.models import Post, Source


class DashboardService:
    def __init__(self, sources, posts, comments, group_messages=None, vk=None) -> None:
        self.sources = sources
        self.posts = posts
        self.comments = comments
        self.group_messages = group_messages
        self.vk = vk

    async def render(self, source_id: int, period_value: str) -> str:
        source = await self.sources.get_source(source_id)
        if source is None or not source.is_active:
            raise ValueError("Source not found or inactive.")

        period = parse_period(period_value)
        if source.telegram_monitor_mode == "discussion" and self.group_messages is not None:
            return await self._render_discussion(source, period)

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

    async def render_chart_png(self, source_id: int, period_value: str) -> bytes | None:
        source = await self.sources.get_source(source_id)
        if source is None or not source.is_active:
            raise ValueError("Source not found or inactive.")

        period = parse_period(period_value)
        buckets = _bucket_labels(period.start_iso, period.end_iso, period_value)
        if not buckets:
            return None

        if source.telegram_monitor_mode == "discussion" and self.group_messages is not None:
            messages = await self.group_messages.list_by_period(
                source_id,
                period.start_iso,
                period.end_iso,
            )
            series = {
                "Сообщения": _counts_by_bucket([item.date for item in messages], buckets),
                "Комментарии": [0 for _label, _start, _end in buckets],
                "Реакции": [0 for _label, _start, _end in buckets],
            }
        else:
            posts = await self.posts.list_by_period(source_id, period.start_iso, period.end_iso)
            comments = await self.comments.list_by_period(
                source_id,
                period.start_iso,
                period.end_iso,
            )
            series = {
                "Посты": _counts_by_bucket([post.date for post in posts], buckets),
                "Комментарии": _counts_by_bucket([comment.date for comment in comments], buckets),
                "Реакции": _sum_by_bucket(
                    [(post.date, post.reactions_total) for post in posts],
                    buckets,
                ),
            }

        return _render_chart(
            title=f"{source.display_name} · {period.label}",
            labels=[label for label, _start, _end in buckets],
            series=series,
        )

    async def _render_discussion(self, source: Source, period) -> str:
        if self.group_messages is None:
            messages_count = 0
        else:
            messages_count = await self.group_messages.count_by_period(
                source.id,
                period.start_iso,
                period.end_iso,
            )
        return "\n".join(
            [
                "<b>Argus dashboard</b>",
                f"Источник: {escape(source.display_name)}",
                f"Период: {escape(period.label)}",
                "",
                f"Сообщений/comments: {messages_count}",
                "Реакций всего: 0",
                "Среднее реакций на сообщение: 0.00",
                "",
                "Discussion-источник считается как поток новых сообщений группы.",
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


def _bucket_labels(start_iso: str, end_iso: str, period_value: str):
    from datetime import datetime, timedelta

    start = datetime.fromisoformat(start_iso)
    end = datetime.fromisoformat(end_iso)
    by_hour = period_value.strip().lower() in {"24h", "1d"}
    step = timedelta(hours=1) if by_hour else timedelta(days=1)
    fmt = "%H:%M" if by_hour else "%d.%m"
    buckets = []
    current = start.replace(minute=0, second=0, microsecond=0) if by_hour else start.replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    while current <= end:
        next_at = current + step
        buckets.append((current.strftime(fmt), current, next_at))
        current = next_at
    return buckets


def _counts_by_bucket(dates: list[str], buckets) -> list[int]:
    return _sum_by_bucket([(date, 1) for date in dates], buckets)


def _sum_by_bucket(items: list[tuple[str, int]], buckets) -> list[int]:
    from datetime import datetime

    values = [0 for _label, _start, _end in buckets]
    for raw_date, value in items:
        item_date = datetime.fromisoformat(raw_date)
        for index, (_label, start, end) in enumerate(buckets):
            if start <= item_date < end:
                values[index] += value
                break
    return values


def _render_chart(title: str, labels: list[str], series: dict[str, list[int]]) -> bytes | None:
    if not labels:
        return None

    try:
        mpl_config_dir = Path("data/matplotlib")
        mpl_config_dir.mkdir(parents=True, exist_ok=True)
        os.environ.setdefault("MPLCONFIGDIR", str(mpl_config_dir.resolve()))
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return None

    colors = ["#1d4ed8", "#facc15", "#dc2626"]
    x_values = list(range(len(labels)))
    max_value = 0

    figure, axis = plt.subplots(figsize=(10, 4.8), facecolor="white")
    for index, (name, values) in enumerate(series.items()):
        aligned_values = list(values[: len(labels)])
        if len(aligned_values) < len(labels):
            aligned_values.extend([0] * (len(labels) - len(aligned_values)))
        max_value = max(max_value, *(aligned_values or [0]))
        axis.plot(
            x_values,
            aligned_values,
            marker="o",
            linewidth=2.4,
            markersize=4.5,
            color=colors[index % len(colors)],
            label=name,
        )

    if max_value <= 0:
        axis.text(
            0.5,
            0.52,
            "Нет данных за период",
            ha="center",
            va="center",
            transform=axis.transAxes,
            color="#6b7280",
            fontsize=13,
        )
        axis.set_ylim(0, 1)
    else:
        axis.set_ylim(0, max(1, max_value * 1.15))

    axis.set_title(title)
    axis.set_xticks(x_values)
    axis.set_xticklabels(labels)
    axis.grid(True, alpha=0.22)
    axis.legend()
    axis.tick_params(axis="x", rotation=45)
    axis.margins(x=0.02)
    figure.tight_layout()

    buffer = BytesIO()
    figure.savefig(buffer, format="png", dpi=140)
    plt.close(figure)
    return buffer.getvalue()
