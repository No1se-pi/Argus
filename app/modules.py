from dataclasses import dataclass, field
from enum import Enum

try:
    from enum import StrEnum
except ImportError:
    class StrEnum(str, Enum):
        pass

from app.config import Settings


class ModuleStatus(StrEnum):
    OK = "ok"
    DISABLED = "disabled"
    CONFIG_MISSING = "config_missing"
    AUTH_REQUIRED = "auth_required"
    ERROR = "error"


class RuntimeMode(StrEnum):
    FULL_MODE = "FULL_MODE"
    VK_ONLY = "VK_ONLY"
    TG_ONLY = "TG_ONLY"
    CONTROL_ONLY = "CONTROL_ONLY"


@dataclass(frozen=True)
class ModuleInfo:
    name: str
    enabled: bool
    status: ModuleStatus
    reason: str = ""
    last_error: str | None = None
    available_commands: list[str] = field(default_factory=list)

    @property
    def is_available(self) -> bool:
        return self.enabled and self.status == ModuleStatus.OK


class ModuleRegistry:
    def __init__(
        self,
        *,
        settings: Settings,
        runtime_settings,
        vk_service=None,
        telegram_collector=None,
    ) -> None:
        self.settings = settings
        self.runtime_settings = runtime_settings
        self.vk_service = vk_service
        self.telegram_collector = telegram_collector

    async def module_infos(self, *, check_network: bool = False) -> list[ModuleInfo]:
        return [
            await self.vk_info(check_network=check_network),
            await self.telegram_info(),
        ]

    async def vk_info(self, *, check_network: bool = False) -> ModuleInfo:
        enabled = await self._runtime_bool("enable_vk_monitor", self.settings.enable_vk_monitor)
        commands = [
            "/vk_status",
            "/vk_setup",
            "/vk_recent_posts",
            "/vk_posts",
            "/vk_recent_comments",
            "/vk_sync",
            "/vk_dashboard",
            "/vk_watch_on",
            "/vk_watch_off",
        ]
        if not enabled:
            return ModuleInfo(
                name="VK Monitor",
                enabled=False,
                status=ModuleStatus.DISABLED,
                reason="VK Monitor is disabled",
                available_commands=[],
            )

        if self.vk_service is None:
            return ModuleInfo(
                name="VK Monitor",
                enabled=True,
                status=ModuleStatus.ERROR,
                reason="VK service is not initialized",
                available_commands=[],
            )

        return await self.vk_service.module_info(commands, check_network=check_network)

    async def telegram_info(self) -> ModuleInfo:
        enabled = await self._runtime_bool(
            "enable_telegram_monitor",
            self.settings.enable_telegram_monitor,
        )
        commands = [
            "/tg_status",
            "/tg_add_source",
            "/tg_sources",
            "/tg_keywords",
            "/tg_add_keyword",
            "/tg_remove_keyword",
            "/tg_recent_posts",
            "/tg_posts",
            "/tg_dashboard",
            "/tg_sync_posts",
            "/tg_set_mode",
            "/add_source",
            "/sources",
            "/dashboard",
        ]
        if not enabled:
            return ModuleInfo(
                name="Telegram Monitor",
                enabled=False,
                status=ModuleStatus.DISABLED,
                reason="Telegram Monitor is disabled",
                available_commands=[],
            )

        if not self.settings.has_telegram_monitor_config:
            missing = []
            if self.settings.tg_api_id is None:
                missing.append("TG_API_ID")
            if self.settings.tg_api_hash is None:
                missing.append("TG_API_HASH")
            return ModuleInfo(
                name="Telegram Monitor",
                enabled=True,
                status=ModuleStatus.CONFIG_MISSING,
                reason=", ".join(missing) + " not set",
                available_commands=[],
            )

        if not self.settings.telethon_session_file.exists():
            return ModuleInfo(
                name="Telegram Monitor",
                enabled=True,
                status=ModuleStatus.AUTH_REQUIRED,
                reason="Telethon session not found",
                available_commands=[],
            )

        if self.telegram_collector is None:
            return ModuleInfo(
                name="Telegram Monitor",
                enabled=True,
                status=ModuleStatus.AUTH_REQUIRED,
                reason=(
                    "Telethon session exists but user is not authorized "
                    "or client failed to start"
                ),
                available_commands=[],
            )

        if not self.telegram_collector.is_connected():
            return ModuleInfo(
                name="Telegram Monitor",
                enabled=True,
                status=ModuleStatus.ERROR,
                reason="Telethon client is disconnected",
                available_commands=[],
            )

        return ModuleInfo(
            name="Telegram Monitor",
            enabled=True,
            status=ModuleStatus.OK,
            available_commands=commands,
        )

    async def current_mode(self) -> RuntimeMode:
        vk = await self.vk_info(check_network=False)
        tg = await self.telegram_info()
        vk_ok = vk.is_available
        tg_ok = tg.is_available
        if vk_ok and tg_ok:
            return RuntimeMode.FULL_MODE
        if vk_ok:
            return RuntimeMode.VK_ONLY
        if tg_ok:
            return RuntimeMode.TG_ONLY
        return RuntimeMode.CONTROL_ONLY

    async def available_commands(self) -> list[str]:
        commands = ["/start", "/request_access", "/menu", "/help", "/status", "/modules", "/setup"]
        for module in await self.module_infos(check_network=False):
            commands.extend(module.available_commands)
        return sorted(set(commands))

    async def disabled_commands(self) -> list[str]:
        disabled: list[str] = []
        for module in await self.module_infos(check_network=False):
            if module.status == ModuleStatus.OK:
                continue
            if module.name == "VK Monitor":
                disabled.extend(
                    [
                        "/vk_status",
                        "/vk_dashboard",
                        "/vk_recent_comments",
                        "/vk_recent_posts",
                        "/vk_posts",
                        "/vk_sync",
                    ]
                )
            if module.name == "Telegram Monitor":
                disabled.extend(
                    [
                        "/tg_add_source",
                        "/tg_sources",
                        "/tg_keywords",
                        "/tg_add_keyword",
                        "/tg_remove_keyword",
                        "/tg_recent_posts",
                        "/tg_posts",
                        "/tg_dashboard",
                        "/tg_sync_posts",
                        "/tg_set_mode",
                    ]
                )
        return sorted(set(disabled))

    async def set_module_enabled(self, key: str, enabled: bool) -> None:
        if key not in {"enable_vk_monitor", "enable_telegram_monitor"}:
            raise ValueError("Unknown module setting.")
        await self.runtime_settings.set(key, "true" if enabled else "false", is_secret=False)

    async def _runtime_bool(self, key: str, default: bool) -> bool:
        value = await self.runtime_settings.get(key)
        if value is None:
            return default
        return value.strip().lower() in {"1", "true", "yes", "on"}
