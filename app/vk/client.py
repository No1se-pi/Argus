import logging
from typing import Any

import aiohttp


logger = logging.getLogger(__name__)


class VKAPIError(Exception):
    """Raised when VK API returns an error response."""

    def __init__(self, message: str, *, code: int | str | None = None, method: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.method = method


class VKClient:
    BASE_URL = "https://api.vk.com/method"

    def __init__(self, *, access_token: str, api_version: str) -> None:
        self.access_token = access_token
        self.api_version = api_version

    async def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        payload = dict(params or {})
        payload["access_token"] = self.access_token
        payload["v"] = self.api_version

        async with aiohttp.ClientSession() as session:
            async with session.get(f"{self.BASE_URL}/{method}", params=payload, timeout=30) as response:
                data = await response.json(content_type=None)

        if "error" in data:
            error = data["error"]
            code = error.get("error_code", "unknown")
            message = error.get("error_msg", "Unknown VK API error")
            raise VKAPIError(f"VK API error {code}: {message}", code=code, method=method)

        return data.get("response")

    async def get_group(self, group_id: int) -> dict[str, Any]:
        response = await self.request(
            "groups.getById",
            {"group_id": abs(group_id), "fields": "screen_name,name"},
        )
        if isinstance(response, list) and response:
            return response[0]
        if isinstance(response, dict) and "groups" in response and response["groups"]:
            return response["groups"][0]
        raise VKAPIError("VK group not found")

    async def get_wall_posts(self, group_id: int, *, count: int) -> list[dict[str, Any]]:
        response = await self.request(
            "wall.get",
            {"owner_id": -abs(group_id), "count": count},
        )
        return list(response.get("items", [])) if isinstance(response, dict) else []

    async def get_wall_comments(
        self,
        group_id: int,
        post_id: int,
        *,
        count: int = 100,
    ) -> list[dict[str, Any]]:
        response = await self.request(
            "wall.getComments",
            {
                "owner_id": -abs(group_id),
                "post_id": post_id,
                "count": min(count, 100),
                "sort": "desc",
                "thread_items_count": 0,
            },
        )
        return list(response.get("items", [])) if isinstance(response, dict) else []

    async def get_long_poll_server(self, group_id: int) -> dict[str, Any]:
        response = await self.request("groups.getLongPollServer", {"group_id": abs(group_id)})
        if not isinstance(response, dict):
            raise VKAPIError("VK Long Poll server response is invalid")
        return response
