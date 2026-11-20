"""Clone template slides and prune the package graph so obsolete reviews are removed."""

import copy
import io
import posixpath
import re
from dataclasses import dataclass
from zipfile import ZIP_DEFLATED, BadZipFile, ZipFile

from lxml import etree

from app.reports.branding import BRANCH_LOGOS, PLATFORM_LOGOS, logo
from app.reports.render import HEIGHT, WIDTH, RenderedPage
from app.reports.review_details import is_negative
from app.vk.presentation import NS, _set_text, _text, fill_presentation

R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
REL = "http://schemas.openxmlformats.org/package/2006/relationships"
CT = "http://schemas.openxmlformats.org/package/2006/content-types"
P, A = NS["p"], NS["a"]
BRANCHES = tuple(BRANCH_LOGOS)
DGIS_RATING_ORDER = ("Датахаб", "Кибер", "Техно", "Диджитал", "АртТех", "Моссовет")
YANDEX_RATING_ORDER = ("Датахаб", "Моссовет", "Диджитал", "АртТех", "Техно", "Кибер")


def report_title(period: str) -> str:
    return "Отзывы и активность " + period.replace("–", " - ").replace("—", " - ")


def geometry(shape):
    transform = shape.find("./*/a:xfrm", NS)
    if transform is not None:
        offset, size = transform.find("a:off", NS), transform.find("a:ext", NS)
        if offset is not None and size is not None:
            return tuple(int(item.get(key)) for item, key in (
                (offset, "x"), (offset, "y"), (size, "cx"), (size, "cy")
            ))
    return 0, 0, 0, 0


def branch_name(source: dict) -> str:
    return source["branch_name"].removeprefix("УО ").strip()


def count_label(count: int, complete: bool) -> str:
    return str(count) if complete else f"≥{count}" if count else "н/д"



def xml(data: bytes):
    return etree.fromstring(data, etree.XMLParser(resolve_entities=False, no_network=True))


def serialize(root):
    return etree.tostring(root, encoding="UTF-8", xml_declaration=True, standalone=True)


def relationship_part(part: str) -> str:
    parent, basename = posixpath.split(part)
    return posixpath.join(parent, "_rels", basename + ".rels")


def target_part(part: str, target: str) -> str:
    resolved = posixpath.normpath(
        target.lstrip("/")
        if target.startswith("/")
        else posixpath.join(posixpath.dirname(part), target)
    )
    if resolved.startswith("../"):
        raise ValueError("Небезопасная структура PPTX.")
    return resolved


@dataclass
class Slide:
    prototype: str
    title: str | None = None
    image: RenderedPage | None = None
    cover: bool = False
    summary: tuple[list[dict], list[dict]] | None = None
    platform: str | None = None


class TemplateDeck:
    def __init__(self, template: bytes, snapshot: dict, timezone_name: str):
        report = snapshot.get("vk")
        if report is None:
            report = {
                "posts": [],
                "rows": [],
                "posts_count": 0,
                "finished_at": snapshot["captured_at"],
            }
        filled = fill_presentation(
            template,
            report,
            bindings=snapshot["bindings"] if snapshot.get("vk") else {},
            timezone_name=timezone_name,
        )
        self.matches_csv = filled.audit_csv()
        try:
            with ZipFile(io.BytesIO(filled.pptx)) as package:
                self.parts = {entry.filename: package.read(entry) for entry in package.infolist()}
        except BadZipFile as exc:
            raise ValueError("Повреждённый PPTX.") from exc
        self.presentation = xml(self.parts["ppt/presentation.xml"])
        self.rels = xml(self.parts["ppt/_rels/presentation.xml.rels"])
        by_id = {
            rel.get("Id"): target_part("ppt/presentation.xml", rel.get("Target"))
            for rel in self.rels
        }
        self.order = [
            by_id[item.get(f"{{{R}}}id")] for item in self.presentation.find(f"{{{P}}}sldIdLst")
        ]
        self.roots = {part: xml(self.parts[part]) for part in self.order}
        self.size = self.presentation.find(f"{{{P}}}sldSz")
        self.width, self.height = int(self.size.get("cx")), int(self.size.get("cy"))
        self.period = snapshot["period"]
        self.have_vk = snapshot.get("vk") is not None

    def find(self, title: str, *, after: int = 0) -> str:
        for part in self.order[after:]:
            if title.casefold() in _text(self.roots[part]).casefold():
                return part
        raise ValueError(f"В шаблоне не найден раздел «{title}».")

    def card_prototype(self, platform_start: str) -> str:
        for part in self.order[self.order.index(platform_start) + 1:]:
            tree = self.roots[part].find("p:cSld/p:spTree", NS)
            if any(_text(shape).strip().casefold() == "скриншоты" for shape in tree):
                return part
        raise ValueError("В шаблоне не найден слайд карточек отзывов.")

    def summary_logos(self, prototype: str):
        tree = self.roots[prototype].find("p:cSld/p:spTree", NS)
        pictures = [
            picture for picture in tree.findall("p:pic", NS)
            if self.height * .45 <= geometry(picture)[1] <= self.height * .58
        ]
        if len(pictures) != len(BRANCHES):
            return {}
        return dict(zip(BRANCHES, sorted(pictures, key=lambda shape: geometry(shape)[0]),
                        strict=True))

    def rating_branches(self, sources: list[dict], *, dgis: bool):
        by_name = {branch_name(source).casefold(): source for source in sources}
        order = DGIS_RATING_ORDER if dgis else YANDEX_RATING_ORDER
        result = [(branch, by_name.pop(branch.casefold(), {})) for branch in order]
        result.extend((source["branch_name"], source) for source in by_name.values())
        return result

    def update_summary(self, tree, spec: Slide):
        sources, reviews = spec.summary
        logos = self.summary_logos(spec.prototype)
        if not logos:
            raise ValueError("В информационной справке должны быть шесть логотипов отделений.")
        centers = {branch: geometry(logo)[0] + geometry(logo)[2] // 2
                   for branch, logo in logos.items()}
        by_name = {branch_name(source).casefold(): source for source in sources}
        reviews_by_source = {
            source["id"]: [item for item in reviews if item["source_id"] == source["id"]]
            for source in sources
        }
        total = sum(map(len, reviews_by_source.values()))
        complete = all(
            by_name.get(branch.casefold(), {}).get("coverage_complete") for branch in BRANCHES
        ) and all(source.get("coverage_complete") for source in sources)
        for shape in list(tree):
            text = _text(shape).strip()
            if text.startswith("Неделя") or text.startswith("Период"):
                parts = shape.xpath(".//a:p[a:r/a:t]", namespaces=NS)
                if len(parts) >= 2:
                    _set_text(parts[0], "Период")
                    _set_text(parts[1], self.period)
                    for part in parts[2:]:
                        _set_text(part, "")
                else:
                    _set_text(shape, self.period)
            elif text.startswith("Всего отзывов"):
                parts = shape.xpath(".//a:p[a:r/a:t]", namespaces=NS)
                label = count_label(total, complete)
                if len(parts) >= 2:
                    _set_text(parts[1], label)
                    for part in parts[2:]:
                        _set_text(part, "")
                else:
                    _set_text(shape, f"Всего отзывов: {label}")
            elif text.isdecimal():
                # The template has a mix of grouped and standalone numeral shapes.
                x, y, width, _ = geometry(shape)
                if self.height * .51 <= y <= self.height * .74:
                    branch = min(centers, key=lambda key: abs(centers[key] - x - width / 2))
                    source = by_name.get(branch.casefold(), {})
                    count = len(reviews_by_source.get(source.get("id"), ()))
                    _set_text(shape, count_label(count, source.get("coverage_complete", False)))
                    for run in shape.xpath(".//a:r/a:rPr", namespaces=NS):
                        for fill in run.xpath("./a:solidFill | ./a:gradFill | ./a:noFill", namespaces=NS):
                            run.remove(fill)
                        fill = etree.Element(f"{{{A}}}solidFill")
                        etree.SubElement(fill, f"{{{A}}}srgbClr", val="000000")
                        after_lines = int(run.find("a:ln", NS) is not None)
                        run.insert(after_lines, fill)
            elif re.search(r"негатив|с ответом|без ответа|модерац", text, re.IGNORECASE):
                tree.remove(shape)
        for index, (branch, center) in enumerate(centers.items()):
            source = by_name.get(branch.casefold(), {})
            negatives = [
                item for item in reviews_by_source.get(source.get("id"), ()) if is_negative(item)
            ]
            if not negatives:
                continue
            answered = sum(bool(item.get("details", {}).get("reply")) for item in negatives)
            count = len(negatives)
            adjective = "негативный" if count % 10 == 1 and count % 100 != 11 else "негативных"
            lines = [f"+{count} {adjective}"]
            if answered:
                lines.append("с ответом" if answered == count else f"с ответом: {answered}")
            width = int(self.width * .135)
            tree.append(self.textbox(
                9100 + index, center - width // 2, int(self.height * .757),
                width, int(self.height * .096), lines,
            ))

    @staticmethod
    def textbox(identity, x, y, width, height, lines, *, name="Негативные отзывы",
                color="FF0000", font_size="1050", align="ctr"):
        shape = etree.Element(f"{{{P}}}sp")
        nv = etree.SubElement(shape, f"{{{P}}}nvSpPr")
        etree.SubElement(nv, f"{{{P}}}cNvPr", id=str(identity), name=name)
        etree.SubElement(nv, f"{{{P}}}cNvSpPr", txBox="1")
        etree.SubElement(nv, f"{{{P}}}nvPr")
        props = etree.SubElement(shape, f"{{{P}}}spPr")
        transform = etree.SubElement(props, f"{{{A}}}xfrm")
        etree.SubElement(transform, f"{{{A}}}off", x=str(x), y=str(y))
        etree.SubElement(transform, f"{{{A}}}ext", cx=str(width), cy=str(height))
        body = etree.SubElement(shape, f"{{{P}}}txBody")
        etree.SubElement(body, f"{{{A}}}bodyPr", lIns="0", rIns="0", tIns="0", bIns="0", wrap="none")
        etree.SubElement(body, f"{{{A}}}lstStyle")
        for line in lines:
            para = etree.SubElement(body, f"{{{A}}}p")
            etree.SubElement(para, f"{{{A}}}pPr", algn=align)
            run = etree.SubElement(para, f"{{{A}}}r")
            font = etree.SubElement(run, f"{{{A}}}rPr", lang="ru-RU", sz=font_size, b="1")
            fill = etree.SubElement(font, f"{{{A}}}solidFill")
            etree.SubElement(fill, f"{{{A}}}srgbClr", val=color)
            etree.SubElement(run, f"{{{A}}}t").text = line
        return shape

    def vk_slides(self) -> list[str]:
        return [part for part in self.order if "СТАТИСТИКА ЛАЙКОВ" in _text(self.roots[part])]

    def build(self, slides: list[Slide]) -> bytes:
        if len(slides) > 250:
            raise ValueError("Презентация превышает 250 слайдов. Сократите период.")
        generated = dict(self.parts)
        slide_list = self.presentation.find(f"{{{P}}}sldIdLst")
        slide_list.clear()
        for rel in list(self.rels):
            if rel.get("Type", "").endswith("/slide"):
                self.rels.remove(rel)
        for tag in ("custShowLst", "extLst"):
            for node in self.presentation.findall(f"{{{P}}}{tag}"):
                self.presentation.remove(node)
        content_types = xml(self.parts["[Content_Types].xml"])
        for item in list(content_types):
            if item.get("ContentType", "").endswith("presentationml.slide+xml"):
                content_types.remove(item)
        if not any(item.get("Extension") == "png" for item in content_types):
            etree.SubElement(
                content_types, f"{{{CT}}}Default", Extension="png", ContentType="image/png"
            )
        for index, spec in enumerate(slides, 1):
            part = f"ppt/slides/argus{index}.xml"
            root = copy.deepcopy(self.roots[spec.prototype])
            rel_part = relationship_part(spec.prototype)
            rels = (
                xml(self.parts[rel_part])
                if rel_part in self.parts
                else etree.Element(f"{{{REL}}}Relationships")
            )
            if spec.cover:
                tree = root.find("p:cSld/p:spTree", NS)
                for shape in tree.findall("p:sp", NS):
                    text = _text(shape).strip()
                    placeholder = shape.find("p:nvSpPr/p:nvPr/p:ph", NS)
                    if (text.casefold() == "slide text"
                            or (placeholder is not None and placeholder.get("type") == "body")
                            or re.fullmatch(r"\d{4}", text)
                            or re.match(r"\d{1,2}\.\d{2}", text)):
                        tree.remove(shape)
                    elif text.casefold().startswith("отзывы"):
                        _set_text(shape, "Отзывы и активность")
                        for run in shape.xpath(".//a:rPr", namespaces=NS):
                            run.set("sz", "3000")

                start, end = self.period.split("–", 1)
                date = self.textbox(
                    9500, int(self.width * .872), int(self.height * .857),
                    int(self.width * .097), int(self.height * .108),
                    [f"{start[:5]} -", end[:5]],
                    name="Период отчёта", color="FFFFFF", font_size="1700",
                )
                tree.append(date)
            elif spec.summary is not None:
                tree = root.find("p:cSld/p:spTree", NS)
                self.update_summary(tree, spec)
                for slot, (branch, picture) in enumerate(self.summary_logos(spec.prototype).items()):
                    x, _, width, _ = geometry(picture)
                    identity = picture.find("p:nvPicPr/p:cNvPr", NS).get("id")
                    (cloned,) = tree.xpath("./p:pic[p:nvPicPr/p:cNvPr/@id=$id]",
                                           namespaces=NS, id=identity)
                    tree.remove(cloned)
                    center = x + width // 2
                    slot_width = int(self.width * .125)
                    # One visible logo height for all six departments. Size it to
                    # fit the widest wordmark, then keep its original proportions.
                    self.add_logo(generated, rels, tree, BRANCH_LOGOS[branch], 9300 + slot,
                                  center - slot_width // 2, int(self.height * .485),
                                  slot_width, int(self.height * .0185))
                if spec.platform:
                    self.add_logo(generated, rels, tree, PLATFORM_LOGOS[spec.platform], 9310,
                                  int(self.width * .81), int(self.height * .90),
                                  int(self.width * .14), int(self.height * .042))
            elif spec.image is not None:
                tree = root.find("p:cSld/p:spTree", NS)
                title = None
                for child in list(tree)[2:]:
                    if title is None and child.tag == f"{{{P}}}sp" and _text(child).strip():
                        title = child
                    else:
                        tree.remove(child)
                if title is not None and spec.title:
                    _set_text(title, spec.title)
                image_part = f"ppt/media/argus{index}.png"
                generated[image_part] = spec.image.image
                image_id = f"rIdArgusImage{index}"
                etree.SubElement(
                    rels,
                    f"{{{REL}}}Relationship",
                    Id=image_id,
                    Type=R + "/image",
                    Target=f"../media/argus{index}.png",
                )
                x, y, width, height = self.body_rect()
                tree.append(self.picture(image_id, x, y, width, height))
                if spec.platform:
                    self.add_logo(
                        generated, rels, tree, PLATFORM_LOGOS[spec.platform], 9310,
                        int(self.width * .81), int(self.height * .912),
                        int(self.width * .14), int(self.height * .034),
                    )
                for link_index, (lx, ly, lw, lh, url) in enumerate(spec.image.links):
                    if not isinstance(url, str) or not url.startswith(("https://", "http://")):
                        continue
                    link_id = f"rIdArgusLink{link_index}"
                    etree.SubElement(
                        rels,
                        f"{{{REL}}}Relationship",
                        Id=link_id,
                        Type=R + "/hyperlink",
                        Target=url,
                        TargetMode="External",
                    )
                    tree.append(
                        self.link_shape(
                            link_id,
                            int(x + lx * width),
                            int(y + ly * height),
                            int(lw * width),
                            int(lh * height),
                            link_index + 10,
                        )
                    )
            elif not self.have_vk:
                for shape in root.xpath(".//p:sp", namespaces=NS):
                    text = _text(shape).strip()
                    if text.startswith(("Собрано ", "Актуально на ", "Счет за ", "За неделю ")):
                        _set_text(shape, "Нет данных ВК за выбранный период")
            # Keep the layout and relationships actually referenced by current content.
            referenced = {
                value
                for node in root.iter()
                for key, value in node.attrib.items()
                if key.startswith("{" + R + "}")
            }
            for rel in list(rels):
                if (
                    not rel.get("Type", "").endswith("/slideLayout")
                    and rel.get("Id") not in referenced
                ):
                    rels.remove(rel)
            generated[part] = serialize(root)
            generated[relationship_part(part)] = serialize(rels)
            rid = f"rIdArgusSlide{index}"
            etree.SubElement(
                slide_list, f"{{{P}}}sldId", id=str(255 + index), **{f"{{{R}}}id": rid}
            )
            etree.SubElement(
                self.rels,
                f"{{{REL}}}Relationship",
                Id=rid,
                Type=R + "/slide",
                Target=f"slides/argus{index}.xml",
            )
            etree.SubElement(
                content_types,
                f"{{{CT}}}Override",
                PartName="/" + part,
                ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml",
            )
        generated["ppt/presentation.xml"] = serialize(self.presentation)
        generated["ppt/_rels/presentation.xml.rels"] = serialize(self.rels)
        if "docProps/core.xml" in generated:
            props = xml(generated["docProps/core.xml"])
            tag = "{http://purl.org/dc/elements/1.1/}title"
            title = props.find(tag)
            if title is None:
                title = etree.SubElement(props, tag)
            title.text = report_title(self.period)
            generated["docProps/core.xml"] = serialize(props)
        if "docProps/app.xml" in generated:
            props = xml(generated["docProps/app.xml"])
            for node in props.iter():
                if etree.QName(node).localname == "Slides":
                    node.text = str(len(slides))
            generated["docProps/app.xml"] = serialize(props)
        # Traverse OPC relationships. Old review images, notes and slides have no
        # incoming edges and are physically excluded, rather than merely hidden.
        retained = {"[Content_Types].xml", "_rels/.rels"}
        queue = [""]
        visited = set()
        while queue:
            part = queue.pop()
            if part in visited:
                continue
            visited.add(part)
            relations = "_rels/.rels" if not part else relationship_part(part)
            if relations not in generated:
                continue
            retained.add(relations)
            for rel in xml(generated[relations]):
                if rel.get("TargetMode") == "External":
                    continue
                target = target_part(part, rel.get("Target"))
                if target not in generated:
                    raise ValueError(f"В PPTX отсутствует связанная часть: {target}")
                retained.add(target)
                queue.append(target)
        for item in list(content_types):
            if item.get("PartName") and item.get("PartName").lstrip("/") not in retained:
                content_types.remove(item)
        generated["[Content_Types].xml"] = serialize(content_types)
        out = io.BytesIO()
        with ZipFile(out, "w", compression=ZIP_DEFLATED) as archive:
            for part in sorted(retained):
                archive.writestr(part, generated[part])
        if out.tell() > 40 * 1024 * 1024:
            raise ValueError("Файл больше 40 МБ. Выберите более короткий период.")
        return out.getvalue()

    def body_rect(self):
        width = int(self.width * 0.96)
        return int(self.width * 0.02), int(self.height * 0.18), width, int(width * HEIGHT / WIDTH)

    def add_logo(self, generated, rels, tree, name, identity, x, y, width, height):
        data, image_width, image_height = logo(name)
        if width * image_height < height * image_width:
            draw_width, draw_height = width, round(width * image_height / image_width)
        else:
            draw_width, draw_height = round(height * image_width / image_height), height
        asset = f"argus-logo-{name}.png"
        generated[f"ppt/media/{asset}"] = data
        rid = f"rIdArgusLogo{identity}"
        etree.SubElement(
            rels, f"{{{REL}}}Relationship", Id=rid, Type=R + "/image", Target=f"../media/{asset}"
        )
        tree.append(self.picture(
            rid, x + (width - draw_width) // 2, y + (height - draw_height) // 2,
            draw_width, draw_height, identity=identity, name=f"Логотип {name}",
        ))

    @staticmethod
    def picture(rid, x, y, width, height, *, identity=9000, name="Argus content"):
        picture = etree.Element(f"{{{P}}}pic")
        nonvisual = etree.SubElement(picture, f"{{{P}}}nvPicPr")
        etree.SubElement(nonvisual, f"{{{P}}}cNvPr", id=str(identity), name=name)
        etree.SubElement(nonvisual, f"{{{P}}}cNvPicPr")
        etree.SubElement(nonvisual, f"{{{P}}}nvPr")
        fill = etree.SubElement(picture, f"{{{P}}}blipFill")
        etree.SubElement(fill, f"{{{A}}}blip", **{f"{{{R}}}embed": rid})
        etree.SubElement(etree.SubElement(fill, f"{{{A}}}stretch"), f"{{{A}}}fillRect")
        props = etree.SubElement(picture, f"{{{P}}}spPr")
        transform = etree.SubElement(props, f"{{{A}}}xfrm")
        etree.SubElement(transform, f"{{{A}}}off", x=str(x), y=str(y))
        etree.SubElement(transform, f"{{{A}}}ext", cx=str(width), cy=str(height))
        geometry = etree.SubElement(props, f"{{{A}}}prstGeom", prst="rect")
        etree.SubElement(geometry, f"{{{A}}}avLst")
        etree.SubElement(etree.SubElement(props, f"{{{A}}}ln"), f"{{{A}}}noFill")
        return picture

    @staticmethod
    def link_shape(rid, x, y, width, height, identity):
        shape = etree.Element(f"{{{P}}}sp")
        nv = etree.SubElement(shape, f"{{{P}}}nvSpPr")
        props = etree.SubElement(
            nv, f"{{{P}}}cNvPr", id=str(identity), name=f"Review link {identity}"
        )
        etree.SubElement(props, f"{{{A}}}hlinkClick", **{f"{{{R}}}id": rid})
        etree.SubElement(nv, f"{{{P}}}cNvSpPr")
        etree.SubElement(nv, f"{{{P}}}nvPr")
        sp = etree.SubElement(shape, f"{{{P}}}spPr")
        transform = etree.SubElement(sp, f"{{{A}}}xfrm")
        etree.SubElement(transform, f"{{{A}}}off", x=str(x), y=str(y))
        etree.SubElement(transform, f"{{{A}}}ext", cx=str(width), cy=str(height))
        geometry = etree.SubElement(sp, f"{{{A}}}prstGeom", prst="rect")
        etree.SubElement(geometry, f"{{{A}}}avLst")
        fill = etree.SubElement(
            etree.SubElement(sp, f"{{{A}}}solidFill"), f"{{{A}}}srgbClr", val="FFFFFF"
        )
        etree.SubElement(fill, f"{{{A}}}alpha", val="0")
        etree.SubElement(etree.SubElement(sp, f"{{{A}}}ln"), f"{{{A}}}noFill")
        return shape
