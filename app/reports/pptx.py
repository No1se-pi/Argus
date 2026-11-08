"""Clone template slides and prune the package graph so obsolete reviews are removed."""

import copy
import io
import posixpath
from dataclasses import dataclass
from zipfile import ZIP_DEFLATED, BadZipFile, ZipFile

from lxml import etree

from app.reports.render import HEIGHT, WIDTH, RenderedPage
from app.vk.presentation import NS, _set_text, _text, fill_presentation

R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
REL = "http://schemas.openxmlformats.org/package/2006/relationships"
CT = "http://schemas.openxmlformats.org/package/2006/content-types"
P, A = NS["p"], NS["a"]


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

    def card_prototypes(self) -> tuple[str, str]:
        candidates = [
            part
            for part in self.order
            if any(
                _text(shape).strip().casefold() == "скриншоты"
                for shape in self.roots[part].xpath("./p:cSld/p:spTree/p:sp", namespaces=NS)
            )
        ]
        if not candidates:
            raise ValueError("В шаблоне не найден слайд карточек отзывов.")
        # The supplied template has both sections; sharing its first card layout
        # is also valid because the footer is generated for the current platform.
        return candidates[0], candidates[-1]

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
                for shape in root.xpath("./p:cSld/p:spTree/p:sp", namespaces=NS):
                    text = _text(shape).strip()
                    if text and text[0].isdigit():
                        first, last = self.period.split("–", 1)
                        label = first[:5] + "-" + last[:5]
                        years = (
                            first[-4:] if first[-4:] == last[-4:] else first[-4:] + "–" + last[-4:]
                        )
                        _set_text(shape, label if len(text) > 4 else years)
                        if len(text) > 4:
                            body = shape.find("p:txBody/a:bodyPr", NS)
                            if body is not None:
                                body.set("wrap", "none")
                            for run in shape.xpath(".//a:rPr", namespaces=NS):
                                run.set("sz", "1400")
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
        return int(self.width * 0.02), int(self.height * 0.22), width, int(width * HEIGHT / WIDTH)

    @staticmethod
    def picture(rid, x, y, width, height):
        picture = etree.Element(f"{{{P}}}pic")
        nonvisual = etree.SubElement(picture, f"{{{P}}}nvPicPr")
        etree.SubElement(nonvisual, f"{{{P}}}cNvPr", id="9000", name="Argus content")
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
