"""Persistent presentation template and user-confirmed profile bindings."""

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from app.vk.likes import VKLikesService
from app.vk.presentation import fill_presentation, name_tokens

# Explicitly confirmed by the project owner; do not infer IDs from nickname similarity.
INITIAL_BINDINGS = {
    "Цой Владислав Витальевич": 572058492,
    "Первов Герман Алексеевич": 1076022785,
    "Петрова Ирина Викторовна": 374396012,
}


class VKLikesWorkspace:
    def __init__(self, service: VKLikesService) -> None:
        self.service = service
        self.settings = service.vk_service.runtime_settings
        self.template_path = (
            Path(service.vk_service.settings.database_path).parent / "vk_likes_template.pptx"
        )
        self._lock = asyncio.Lock()
        self.auth_helper = None

    async def seed_bindings(self) -> None:
        if await self.settings.get("vk_likes_bindings_seed_v1"):
            return
        bindings = await self.bindings()
        existing = {name_tokens(name) for name in bindings}
        for name, user_id in INITIAL_BINDINGS.items():
            if name_tokens(name) not in existing:
                bindings[name] = user_id
        await self.settings.set("vk_likes_name_bindings", json.dumps(bindings, ensure_ascii=False))
        await self.settings.set("vk_likes_bindings_seed_v1", "true")

    async def bindings(self) -> dict[str, int]:
        return json.loads(await self.settings.get("vk_likes_name_bindings") or "{}")

    async def save_template(self, data: bytes, filename: str) -> None:
        if len(data) > 20 * 1024 * 1024:
            raise ValueError("Шаблон должен быть не больше 20 МБ.")
        # Validate editable tables before replacing a working template.
        empty = {
            "posts": [],
            "rows": [],
            "posts_count": 0,
            "finished_at": datetime.now(timezone.utc).isoformat(),  # noqa: UP017 - Python 3.10
        }
        await asyncio.to_thread(fill_presentation, data, empty)
        async with self._lock:
            await asyncio.to_thread(self._write_template, data)
            await self.settings.set("vk_likes_template_name", Path(filename).name[:200])

    def _write_template(self, data: bytes) -> None:
        self.template_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.template_path.with_suffix(".tmp")
        temporary.write_bytes(data)
        temporary.replace(self.template_path)

    async def template(self) -> bytes | None:
        try:
            return await asyncio.to_thread(self.template_path.read_bytes)
        except FileNotFoundError:
            return None
