import logging
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)


class VKAPIError(Exception):
    """Raised when VK API returns an error response."""

    def __init__(
        self, message: str, *, code: int | str | None = None, method: str | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.method = method

    @property
    def auth_reason(self) -> str | None:
        if str(self.code) != "5":
            return None
        value = str(self).lower()
        if "user is blocked" in value or "user was banned" in value:
            return "account_blocked"
        if "another ip" in value or "ip address" in value:
            return "ip_mismatch"
        if "expired" in value or "has expired" in value:
            return "expired"
        if "revoked" in value:
            return "revoked"
        if "invalid access_token" in value or "invalid access token" in value:
            return "invalid"
        return "auth_rejected"


def vk_error_text(error: VKAPIError) -> str:
    """Explain known errors without echoing arbitrary server text or credentials."""
    reasons = {
        "account_blocked": "ВК отклонил этот API-запрос с причиной «user is blocked». "
        "Это не подтверждает полную блокировку профиля. Проверьте вход и авторизацию "
        "в ВК; если они работают, ограничение нужно уточнять у ВК.",
        "ip_mismatch": "ВК сообщает: токен выдан для другого IP-адреса. "
        "Получите токен для подключения, с которого работает Argus.",
        "expired": "Срок действия пользовательского токена ВК истёк. Обновите токен.",
        "revoked": "Пользовательский токен ВК отозван. Требуется новая авторизация.",
        "invalid": "ВК отклонил пользовательский токен как недействительный. "
        "Проверьте VK_USER_ACCESS_TOKEN и источник токена в настройках.",
        "auth_rejected": "ВК отклонил авторизацию пользователя. Проверьте токен и вход "
        "в аккаунт ВК. Код 5 сам по себе не означает недостаток прав метода.",
    }
    if error.auth_reason:
        return f"VK API: код 5. {reasons[error.auth_reason]}"
    if str(error.code) in {"7", "15", "27"}:
        return "ВК отказал в доступе к методу или объекту. Проверьте тип токена и права доступа."
    if str(error.code) in {"6", "9", "29"}:
        return "ВК ограничил частоту запросов. Повторите попытку позже."
    return "ВК не позволил завершить запрос. Проверьте настройки и повторите попытку."


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
            async with session.get(
                f"{self.BASE_URL}/{method}", params=payload, timeout=30
            ) as response:
                response.raise_for_status()
                try:
                    data = await response.json(content_type=None)
                except ValueError as exc:
                    raise VKAPIError("VK вернул ответ без JSON.", method=method) from exc

        if not isinstance(data, dict):
            raise VKAPIError("VK вернул некорректный ответ.", method=method)

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

    async def get_wall_page(self, group_id: int, *, offset: int = 0) -> dict[str, Any]:
        response = await self.request(
            "wall.get",
            {"owner_id": -abs(group_id), "filter": "owner", "count": 100, "offset": offset},
        )
        return self._validate_page(response, "wall.get")

    async def get_likes_page(
        self, group_id: int, post_id: int, *, offset: int = 0
    ) -> dict[str, Any]:
        response = await self.request(
            "likes.getList",
            {
                "type": "post",
                "owner_id": -abs(group_id),
                "item_id": post_id,
                "filter": "likes",
                "friends_only": 0,
                "extended": 0,
                "skip_own": 0,
                "count": 1000,
                "offset": offset,
            },
        )
        return self._validate_page(response, "likes.getList")

    @staticmethod
    def _validate_page(response: Any, method: str) -> dict[str, Any]:
        if (
            not isinstance(response, dict)
            or not isinstance(response.get("items"), list)
            or type(response.get("count")) is not int
            or response["count"] < 0
        ):
            raise VKAPIError("VK вернул некорректную страницу данных.", method=method)
        return response

    async def get_users(self, user_ids: list[int]) -> list[dict[str, Any]]:
        response = await self.request("users.get", {"user_ids": ",".join(map(str, user_ids))})
        if not isinstance(response, list) or any(
            not isinstance(user, dict) or type(user.get("id")) is not int for user in response
        ):
            raise VKAPIError("VK вернул некорректные имена профилей.", method="users.get")
        return response

    async def get_groups(self, group_ids: list[int]) -> list[dict[str, Any]]:
        response = await self.request(
            "groups.getById", {"group_ids": ",".join(str(abs(item)) for item in group_ids)}
        )
        groups = response.get("groups") if isinstance(response, dict) else response
        if not isinstance(groups, list) or any(
            not isinstance(group, dict) or type(group.get("id")) is not int or group["id"] <= 0
            for group in groups
        ):
            raise VKAPIError("VK вернул некорректные имена сообществ.", method="groups.getById")
        return groups

    async def resolve_group_id(self, reference: str) -> int:
        response = await self.request("groups.getById", {"group_id": reference})
        groups = response.get("groups") if isinstance(response, dict) else response
        if (
            not isinstance(groups, list)
            or len(groups) != 1
            or not isinstance(groups[0], dict)
            or type(groups[0].get("id")) is not int
            or groups[0]["id"] <= 0
        ):
            raise VKAPIError("Сообщество ВК не найдено.", method="groups.getById")
        return groups[0]["id"]

    async def get_wall_post(self, group_id: int, post_id: int) -> dict[str, Any]:
        response = await self.request("wall.getById", {"posts": f"{-abs(group_id)}_{post_id}"})
        items = response.get("items") if isinstance(response, dict) else response
        if (
            not isinstance(items, list)
            or len(items) != 1
            or not isinstance(items[0], dict)
            or items[0].get("id") != post_id
            or items[0].get("owner_id") != -abs(group_id)
            or type(items[0].get("date")) is not int
        ):
            raise VKAPIError("Пост ВК недоступен.", method="wall.getById")
        return items[0]
