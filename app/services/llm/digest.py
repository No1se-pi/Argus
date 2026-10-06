import json
from collections import Counter


def aggregate_messages(rows: list[dict]) -> dict:
    sentiments = Counter(row.get("sentiment", "neutral") for row in rows)
    topics = Counter(row.get("topic", "other") for row in rows)
    severities = Counter(int(row.get("severity", 0)) for row in rows)
    return {
        "messages_count": len(rows),
        "sentiments": dict(sentiments),
        "topics": dict(topics.most_common(10)),
        "severities": {str(key): value for key, value in severities.items()},
        "representative_messages": [row.get("text", "")[:300] for row in rows[:20]],
    }


def digest_prompt(aggregates: dict) -> str:
    return "Составь краткую сводку на русском по агрегатам, без домыслов:\n" + json.dumps(
        aggregates, ensure_ascii=False
    )
