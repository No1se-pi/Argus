"""Suggestions are evidence for a reviewer, never implicit account bindings."""

import csv
import io
from difflib import SequenceMatcher

from app.vk.presentation import fill_presentation, name_tokens

TRANSLIT = str.maketrans(
    dict(
        zip(
            "абвгдежзийклмнопрстуфхыэё",
            "abvgdehziiklmnoprstufhiee",
            strict=True,
        )
    )
)
EXTRA = {
    "ж": "zh",
    "ц": "ts",
    "ч": "ch",
    "ш": "sh",
    "щ": "shch",
    "ъ": "",
    "ь": "",
    "ю": "iu",
    "я": "ia",
}


def latin(value):
    return "".join(EXTRA.get(char, char.translate(TRANSLIT)) for char in value.casefold()).replace(
        "y", "i"
    )


def suggestions(name: str, profiles: list[dict]) -> list[dict]:
    target = name_tokens(name)
    if len(target) < 2:
        return []
    surname, first = map(latin, target[:2])
    results = []
    for profile in profiles:
        parts = list(map(latin, name_tokens(profile["full_name"])))
        if len(parts) < 2 or profile["user_id"] <= 0:
            continue
        best = None
        for index, part in enumerate(parts):
            last_score = SequenceMatcher(None, surname, part).ratio()
            if last_score < 0.85 or (len(surname) < 6 and part != surname):
                continue
            first_score = max(
                SequenceMatcher(None, first, token).ratio()
                for n, token in enumerate(parts)
                if n != index
            )
            if not profile["full_name"].isascii() and first_score < 0.55:
                continue
            score = 0.55 * last_score + 0.45 * first_score
            reason = "Совпадает фамилия; имя отличается или скрыто"
            if first_score >= 0.95:
                reason = "Совпадают фамилия и имя; отчество и личность не подтверждены"
            elif not profile["full_name"].isascii():
                reason = "Похожее написание фамилии/имени; требуется ручная сверка"
            if best is None or score > best[0]:
                best = score, reason
        if best:
            results.append({**profile, "score": best[0], "reason": best[1]})
    return sorted(results, key=lambda row: row["score"], reverse=True)[:4]


def identity_csv(template: bytes, snapshot: dict) -> bytes:
    parent = snapshot.get("identity_report") or snapshot.get("vk")
    empty = {"posts": [], "rows": [], "posts_count": 0, "finished_at": snapshot["captured_at"]}
    result = fill_presentation(template, parent or empty, bindings=snapshot["bindings"])
    output = io.StringIO(newline="")
    writer = csv.writer(output, delimiter=";")
    writer.writerow(
        ["ФИО в шаблоне", "Статус", "Возможный профиль", "Ссылка", "Основание", "Нужна проверка"]
    )
    for match in result.matches:
        if match.status not in {"not_found", "ambiguous", "conflict"}:
            continue
        candidates = suggestions(match.full_name, parent["rows"]) if parent else []
        if not candidates:
            writer.writerow(
                [match.full_name, match.status, "", "", "Подходящий кандидат не найден", "да"]
            )
        for candidate in candidates:
            profile_name = candidate["full_name"]
            if profile_name.startswith(("=", "+", "-", "@", "\t", "\r")):
                profile_name = "'" + profile_name
            writer.writerow(
                [
                    match.full_name,
                    match.status,
                    profile_name,
                    f"https://vk.ru/id{candidate['user_id']}",
                    candidate["reason"],
                    "да",
                ]
            )
    return output.getvalue().encode("utf-8-sig")
