"""Fill likes columns in an existing PPTX without rewriting names or other slides."""

import csv
import io
import json
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zipfile import BadZipFile, ZipFile

from lxml import etree

from app.time import resolve_timezone

NS = {
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
}


def name_tokens(value: str) -> tuple[str, ...]:
    value = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")
    return tuple(re.findall(r"[^\W\d_]+", value))


def _one_typo(left: str, right: str, minimum: int) -> bool:
    """One insertion, removal, substitution, or adjacent transposition; no nickname guessing."""
    if left == right or min(len(left), len(right)) < minimum:
        return False
    if abs(len(left) - len(right)) > 1:
        return False
    if len(left) == len(right):
        differences = [i for i, (a, b) in enumerate(zip(left, right, strict=True)) if a != b]
        if len(differences) == 1:
            return True
        return (
            len(differences) == 2
            and differences[1] == differences[0] + 1
            and left[differences[0]] == right[differences[1]]
            and left[differences[1]] == right[differences[0]]
        )
    short, long = sorted((left, right), key=len)
    return any(long[:i] + long[i + 1 :] == short for i in range(len(long)))


def _match_kind(full_name: str, profile_name: str) -> str | None:
    target = name_tokens(full_name)
    candidate = name_tokens(profile_name)
    if len(target) < 2 or len(candidate) < 2:
        return None
    surname, first = target[:2]
    # VK's displayed order can be either first/last or last/first. A supplied
    # patronymic must agree as well; missing patronymics cannot disambiguate people.
    for last_index, first_index in ((1, 0), (0, 1)):
        last, given = candidate[last_index], candidate[first_index]
        if len(candidate) > 2 and len(target) > 2 and candidate[2:] != target[2:]:
            continue
        if last == surname and given == first:
            return "exact"
    for last_index, first_index in ((1, 0), (0, 1)):
        last, given = candidate[last_index], candidate[first_index]
        if len(candidate) > 2 and len(target) > 2 and candidate[2:] != target[2:]:
            continue
        if (last == surname and _one_typo(given, first, 4)) or (
            given == first and _one_typo(last, surname, 6)
        ):
            return "typo"
    return None


@dataclass
class Match:
    slide: int
    full_name: str
    previous: str
    value: str
    status: str
    user_id: int | None = None
    profile_name: str = ""
    candidates: str = ""


@dataclass
class PresentationExport:
    pptx: bytes
    matches: list[Match]
    slides_updated: list[int]

    def audit_csv(self) -> bytes:
        stream = io.StringIO(newline="")
        writer = csv.writer(stream, delimiter=";")
        writer.writerow(
            [
                "Слайд",
                "ФИО из шаблона",
                "Было",
                "Сейчас",
                "Сопоставление",
                "VK ID",
                "Имя в ВК",
                "Кандидаты",
                "Сверка с исходником",
                "Пояснение",
            ]
        )
        for item in self.matches:
            values = list(asdict(item).values())
            statuses = {
                "exact": "Совпадение имени и фамилии",
                "typo": "Возможная опечатка: проверить",
                "bound": "Явная привязка VK ID",
                "bound_zero": "Явная привязка VK ID: лайков нет",
                "not_found": "Профиль не найден",
                "ambiguous": "Несколько подходящих профилей",
                "conflict": "Один профиль подходит разным ФИО",
                "no_profile": "В шаблоне указано: нет профиля ВК",
            }
            values[4] = statuses[item.status]
            comparable = item.value.isdecimal() and item.previous.strip().isdecimal()
            comparison = (
                ("Совпадает" if item.value == item.previous.strip() else "Отличается")
                if comparable
                else "Не подтверждено"
            )
            if item.status in {"ambiguous", "conflict"}:
                note = "Требуется явная привязка к VK ID; значение в PPTX — н/д."
            elif item.status == "not_found":
                note = "Отсутствие совпадения по имени не доказывает ноль лайков."
            elif item.status == "typo":
                note = "Исправлен только поиск по имени; ФИО в PPTX сохранено без изменений."
            elif comparable and comparison == "Отличается":
                note = "Текущие лайки отличаются от исторического числа в шаблоне."
            else:
                note = ""
            values.extend([comparison, note])
            writer.writerow(
                [
                    "'" + value
                    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@"))
                    else value
                    for value in values
                ]
            )
        return stream.getvalue().encode("utf-8-sig")


def _text(node) -> str:
    return "".join(node.xpath(".//a:t/text()", namespaces=NS))


def _set_text(node, value: str) -> None:
    """Reuse the first run's formatting, retaining the cell and paragraph properties."""
    runs = node.xpath(".//a:t", namespaces=NS)
    if not runs:
        raise ValueError("В ячейке шаблона отсутствует редактируемый текст.")
    runs[0].text = value
    for run in runs[1:]:
        run.text = ""


def _validate_report(report: dict[str, Any]) -> None:
    posts = report.get("posts", [])
    rows = report.get("rows", [])
    if len(posts) != report.get("posts_count"):
        raise ValueError("Выгрузка не содержит полный список постов.")
    if len({post["id"] for post in posts}) != len(posts):
        raise ValueError("В выгрузке есть дубли постов.")
    if len({row["user_id"] for row in rows}) != len(rows):
        raise ValueError("В выгрузке есть дубли профилей.")
    actual = Counter(user_id for post in posts for user_id in set(post["user_ids"]))
    if dict(actual) != {row["user_id"]: row["likes"] for row in rows}:
        raise ValueError("Числа лайков расходятся с детализацией постов.")


def fill_presentation(
    template: bytes,
    report: dict[str, Any],
    *,
    bindings: dict[str, int] | None = None,
    timezone_name: str = "Europe/Moscow",
) -> PresentationExport:
    """Unresolved identities become н/д; only a confirmed VK ID can establish zero."""
    _validate_report(report)
    mapped = {}
    for name, user_id in (bindings or {}).items():
        if type(user_id) is not int or user_id <= 0:
            raise ValueError("Привязка должна содержать положительный VK ID пользователя.")
        key = name_tokens(name)
        if key in mapped and mapped[key] != user_id:
            raise ValueError("Противоречивые привязки одного ФИО.")
        mapped[key] = user_id
    profiles = [row for row in report["rows"] if row["user_id"] > 0]
    by_id = {row["user_id"]: row for row in profiles}
    try:
        source = ZipFile(io.BytesIO(template))
    except BadZipFile as exc:
        raise ValueError("Ожидался корректный файл PPTX.") from exc
    with source:
        entries = source.infolist()
        if len({item.filename for item in entries}) != len(entries):
            raise ValueError("В PPTX есть дубли частей архива.")
        if sum(item.file_size for item in entries) > 100 * 1024 * 1024:
            raise ValueError("Распакованный PPTX превышает 100 МБ.")
        if "ppt/presentation.xml" not in source.namelist():
            raise ValueError("В архиве не найдена презентация PPTX.")
        # Resolve slide order through the presentation relationships, not file numbering.
        parser = etree.XMLParser(resolve_entities=False, no_network=True)

        def read_xml(part):
            try:
                return etree.fromstring(source.read(part), parser)
            except (KeyError, etree.XMLSyntaxError) as exc:
                raise ValueError("Повреждена структура PPTX.") from exc

        presentation = read_xml("ppt/presentation.xml")
        rels = read_xml("ppt/_rels/presentation.xml.rels")
        targets = {rel.get("Id"): rel.get("Target") for rel in rels}
        relationship_ns = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
        parts = []
        for slide in presentation.xpath("./p:sldIdLst/p:sldId", namespaces=NS):
            target = targets.get(slide.get(f"{{{relationship_ns}}}id"), "")
            part = target.lstrip("/") if target.startswith("/") else f"ppt/{target}"
            parts.append(part)
        documents = {}
        matches: list[Match] = []
        cells = []
        for number, part in enumerate(parts, 1):
            root = read_xml(part)
            for table in root.xpath(".//a:tbl", namespaces=NS):
                rows = table.findall("a:tr", NS)
                if not rows:
                    continue
                header = rows[0].findall("a:tc", NS)
                titles = [_text(cell).strip().casefold() for cell in header]
                if "фио" not in titles or "лайки" not in titles:
                    continue
                name_index, count_index = titles.index("фио"), titles.index("лайки")
                for row in rows[1:]:
                    columns = row.findall("a:tc", NS)
                    if max(name_index, count_index) >= len(columns):
                        raise ValueError(f"Неполная строка таблицы на слайде {number}.")
                    name, value_cell = _text(columns[name_index]), columns[count_index]
                    previous = _text(value_cell)
                    if not name.strip():
                        continue
                    match = Match(number, name, previous, "н/д", "not_found")
                    key = name_tokens(name)
                    if key in mapped:
                        match.user_id = mapped[key]
                        profile = by_id.get(match.user_id)
                        match.value = str(profile["likes"] if profile else 0)
                        match.profile_name = profile["full_name"] if profile else ""
                        match.status = "bound" if profile else "bound_zero"
                    elif "нет профиля" in previous.casefold():
                        match.value, match.status = previous, "no_profile"
                    else:
                        candidates = [
                            (profile, _match_kind(name, profile["full_name"]))
                            for profile in profiles
                        ]
                        exact = [(profile, kind) for profile, kind in candidates if kind == "exact"]
                        candidates = exact or [(p, k) for p, k in candidates if k == "typo"]
                        match.candidates = " | ".join(
                            f"{p['full_name']} https://vk.com/id{p['user_id']} ({p['likes']})"
                            for p, _kind in candidates
                        )
                        if len(candidates) == 1:
                            profile, kind = candidates[0]
                            match.value, match.status = str(profile["likes"]), kind
                            match.user_id = profile["user_id"]
                            match.profile_name = profile["full_name"]
                        elif candidates:
                            match.status = "ambiguous"
                    matches.append(match)
                    cells.append(value_cell)
                    documents[part] = (number, root)
        if not matches:
            raise ValueError("В PPTX нет таблиц с колонками «ФИО» и «Лайки».")
        # Do not silently assign a fuzzy match to two different people in the roster.
        owners = defaultdict(set)
        for match in matches:
            if match.user_id is not None:
                owners[match.user_id].add(name_tokens(match.full_name))
        for match, cell in zip(matches, cells, strict=True):
            if match.user_id is not None and len(owners[match.user_id]) > 1:
                match.status, match.value, match.user_id = "conflict", "н/д", None
            if _text(cell) != match.value:
                _set_text(cell, match.value)
        captured = datetime.fromisoformat(report["finished_at"]).astimezone(
            resolve_timezone(timezone_name)
        )
        for _number, root in documents.values():
            for shape in root.xpath(".//p:sp", namespaces=NS):
                text = _text(shape)
                if re.fullmatch(r"Актуально на .+|Собрано .+", text.strip()):
                    _set_text(shape, f"Собрано {captured:%d.%m.%Y} в {captured:%H:%M}")
                elif re.fullmatch(r"Счет за последние \d+ постов", text.strip()):
                    _set_text(shape, f"Счет за последние {report['posts_count']} постов")
                elif re.fullmatch(r"За неделю с .+ вышло \d+ постов", text.strip()):
                    start, end = report.get("period_start"), report.get("period_end_exclusive")
                    if start and end:
                        zone = resolve_timezone(timezone_name)
                        first = datetime.fromisoformat(start).astimezone(zone)
                        last = (datetime.fromisoformat(end) - timedelta(microseconds=1)).astimezone(
                            zone
                        )
                        _set_text(
                            shape,
                            f"Период {first:%d.%m}–{last:%d.%m.%Y}: {report['posts_count']} постов",
                        )
                    else:
                        _set_text(shape, f"Последние {report['posts_count']} постов")
        output = io.BytesIO()
        with ZipFile(output, "w") as result:
            result.comment = source.comment
            for entry in entries:
                data = source.read(entry.filename)
                if entry.filename in documents:
                    root = documents[entry.filename][1]
                    data = etree.tostring(
                        root, encoding="UTF-8", xml_declaration=True, standalone=True
                    )
                result.writestr(entry, data)
        return PresentationExport(
            output.getvalue(), matches, sorted(number for number, _root in documents.values())
        )


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Заполнить лайки в PPTX по снимку Argus.")
    parser.add_argument("template", type=Path)
    parser.add_argument("report", type=Path, help="JSON-снимок из VKLikesService")
    parser.add_argument("output", type=Path)
    parser.add_argument("--bindings", type=Path, help='JSON: {"ФИО из шаблона": VK_ID}')
    args = parser.parse_args()
    if args.output.resolve() in {args.template.resolve(), args.report.resolve()}:
        parser.error("Выходной файл должен отличаться от исходного.")
    bindings = json.loads(args.bindings.read_text(encoding="utf-8")) if args.bindings else None
    report = json.loads(args.report.read_text(encoding="utf-8"))
    result = fill_presentation(args.template.read_bytes(), report, bindings=bindings)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(result.pptx)
    args.output.with_suffix(".matches.csv").write_bytes(result.audit_csv())
    print(
        json.dumps(
            {
                "slides": result.slides_updated,
                "matches": dict(Counter(item.status for item in result.matches)),
            }
        )
    )


if __name__ == "__main__":
    main()
