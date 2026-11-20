"""Project artwork used on the activity report slides."""

import base64
import io
from functools import lru_cache
from pathlib import Path

from PIL import Image

BRANCH_LOGOS = {
    "Датахаб": "datahab",
    "Кибер": "cyber",
    "Диджитал": "digital",
    "АртТех": "arttech",
    "Техно": "techno",
    "Моссовет": "mossoviet",
}
PLATFORM_LOGOS = {"yandex": "yandex_map", "2gis": "2gis"}


@lru_cache(maxsize=8)
def logo(name: str) -> tuple[bytes, int, int]:
    path = Path(__file__).with_name("logos") / f"{name}.png"
    with Image.open(path) as source:
        image = source.convert("RGBA")
    bounds = image.getbbox()
    if bounds:
        image = image.crop(bounds)
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue(), image.width, image.height


@lru_cache(maxsize=8)
def logo_data_url(name: str) -> str:
    with Image.open(io.BytesIO(logo(name)[0])) as image:
        image.thumbnail((420, 136), Image.Resampling.LANCZOS)
        output = io.BytesIO()
        image.save(output, format="PNG", optimize=True)
    return "data:image/png;base64," + base64.b64encode(output.getvalue()).decode("ascii")
