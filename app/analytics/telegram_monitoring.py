from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402


def generate_monitoring_charts(rows: list[dict], output_dir: Path) -> list[Path]:
    """Generate four compact PNG charts from pre-aggregated daily statistics."""
    output_dir.mkdir(parents=True, exist_ok=True)
    charts = [
        ("activity", "Сообщения по дням", _sum_by(rows, "date", "messages_count")),
        ("channels", "Сообщения по источникам", _sum_by(rows, "title", "messages_count")),
        ("sentiment", "Настроение", {
            "positive": sum(int(r.get("positive_count", 0)) for r in rows),
            "neutral": sum(int(r.get("neutral_count", 0)) for r in rows),
            "negative": sum(int(r.get("negative_count", 0)) for r in rows),
        }),
        ("risk", "Уровни риска", {
            "severity 1": sum(int(r.get("severity_1_count", 0)) for r in rows),
            "severity 2": sum(int(r.get("severity_2_count", 0)) for r in rows),
            "severity 3": sum(int(r.get("severity_3_count", 0)) for r in rows),
        }),
    ]
    paths = []
    for name, title, values in charts:
        labels = list(values) or ["нет данных"]
        counts = list(values.values()) or [0]
        figure, axis = plt.subplots(figsize=(8, 4.5))
        axis.bar(labels, counts, color="#4776E6")
        axis.set_title(title)
        axis.tick_params(axis="x", rotation=30)
        figure.tight_layout()
        path = output_dir / f"telegram_{name}.png"
        figure.savefig(path, dpi=140)
        plt.close(figure)
        paths.append(path)
    return paths


def _sum_by(rows: list[dict], key: str, value: str) -> dict[str, int]:
    result: dict[str, int] = {}
    for row in rows:
        label = str(row.get(key) or "unknown")
        result[label] = result.get(label, 0) + int(row.get(value, 0) or 0)
    return result
