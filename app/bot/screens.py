from html import escape
from typing import Any

from app.modules import ModuleInfo, ModuleRegistry, ModuleStatus

STATUS_ICON = {
    ModuleStatus.OK: "✅",
    ModuleStatus.DISABLED: "❌",
    ModuleStatus.CONFIG_MISSING: "⚠️",
    ModuleStatus.AUTH_REQUIRED: "⚠️",
    ModuleStatus.ERROR: "❌",
}


async def main_menu_text(module_registry: ModuleRegistry) -> str:
    mode = await module_registry.current_mode()
    modules = await module_registry.module_infos(check_network=False)
    module_lines = "\n".join(_module_line(module) for module in modules)
    return "\n".join(
        [
            "<b>Argus Control Panel</b>",
            "",
            f"Режим: <b>{mode.value}</b>",
            "",
            "✅ Bot UI",
            "✅ Database",
            "✅ Scheduler",
            module_lines,
            "",
            "Выбери раздел:",
        ]
    )


async def status_text(module_registry: ModuleRegistry) -> str:
    mode = await module_registry.current_mode()
    modules = await module_registry.module_infos(check_network=False)
    lines = [
        "<b>Argus status</b>",
        "",
        "<b>Core:</b>",
        "✅ Bot UI: online",
        "✅ Database: online",
        "✅ Scheduler: online",
        "",
        "<b>Modules:</b>",
    ]
    for module in modules:
        lines.append(_module_line(module))
        if module.reason:
            lines.append(f"   Reason: {escape(module.reason)}")
    lines.extend(
        [
            "",
            "<b>Current mode:</b>",
            mode.value,
            "",
            "<b>Available commands:</b>",
        ]
    )
    lines.extend(f"✅ {escape(command)}" for command in await module_registry.available_commands())
    disabled = await module_registry.disabled_commands()
    if disabled:
        lines.extend(["", "<b>Disabled commands:</b>"])
        lines.extend(f"❌ {escape(command)}" for command in disabled)
    return "\n".join(lines)


async def modules_text(module_registry: ModuleRegistry) -> str:
    modules = await module_registry.module_infos(check_network=False)
    lines = ["<b>Modules</b>", ""]
    lines.extend(_module_line(module) for module in modules)
    lines.append("✅ Database — ok")
    return "\n".join(lines)


async def setup_text(module_registry: ModuleRegistry) -> str:
    modules = await module_registry.module_infos(check_network=False)
    lines = ["<b>Setup</b>", "", "Что требует внимания:"]
    missing = False
    for module in modules:
        if module.status == ModuleStatus.OK:
            continue
        missing = True
        lines.append(f"{_module_line(module)}")
        if module.reason:
            lines.append(f"Причина: {escape(module.reason)}")
    if not missing:
        lines.append("Все активные модули выглядят настроенными.")
    return "\n".join(lines)


def unavailable_text(module: ModuleInfo) -> str:
    return "\n".join(
        [
            f"<b>{escape(module.name)} недоступен.</b>",
            f"Причина: {escape(module.reason or module.status.value)}",
        ]
    )


def telegram_auth_cli_text(configured: bool) -> str:
    if not configured:
        return "\n".join(
            [
                "<b>Telethon auth недоступен</b>",
                "",
                "Сначала укажи <code>TG_API_ID</code> и <code>TG_API_HASH</code> в .env.",
            ]
        )

    return "\n".join(
        [
            "<b>Telethon auth</b>",
            "",
            "Telegram блокирует вход, если login-code отправить в Telegram-чат или боту.",
            "Поэтому код нужно вводить только локально в терминале на этом ПК.",
            "",
            "Останови Argus и запусти:",
            "<code>python -m app.telegram_login</code>",
            "",
            "После успешной авторизации снова запусти Argus.",
        ]
    )


def _module_line(module: ModuleInfo) -> str:
    icon = STATUS_ICON[module.status]
    return f"{icon} {escape(module.name)} — {escape(module.status.value)}"


async def reviews_menu_text(stats: dict, sched_status: dict, config: Any) -> str:
    enabled = config.enabled
    enabled_icon = "🟢 Включен" if enabled else "🔴 Выключен"
    health = sched_status.get("health_status", "UNKNOWN")
    health_icon = "✅" if health == "HEALTHY" else ("⏸️" if health == "IDLE_DISABLED" else "⚠️")

    last_cycle = sched_status.get("last_completed_cycle_at") or "еще не выполнялся"
    poll_min = max(1, config.poll_interval_seconds // 60)

    return "\n".join(
        [
            "⭐ <b>Reviews Monitor</b>",
            "",
            f"Статус: <b>{enabled_icon}</b>",
            f"Здоровье шедулера: {health_icon} <b>{escape(health)}</b>",
            f"Интервал опроса: <b>{poll_min} мин.</b>",
            f"Последний полный цикл: <code>{escape(last_cycle)}</code>",
            "",
            "<b>Статистика:</b>",
            f"• Всего филиалов: <b>{stats.get('total_sources', 12)}</b> "
            f"(активных: {stats.get('active_sources', 0)})",
            f"• Проблемных филиалов: <b>{stats.get('degraded_sources', 0)}</b>",
            f"• Всего отзывов в базе: <b>{stats.get('total_reviews', 0)}</b>",
            f"• Ожидает отправки в TG: <b>{stats.get('unsent_reviews', 0)}</b>",
            "",
            "Выбери действие:",
        ]
    )


def reviews_sources_text(sources: list) -> str:
    lines = [
        "📋 <b>Филиалы Reviews Monitor (12 источников):</b>",
        "",
    ]
    for s in sources:
        platform_name = "Яндекс" if getattr(s, "platform", "") == "yandex" else "2ГИС"
        status_icon = (
            "🟢"
            if getattr(s, "last_status", "")
            in ("SUCCESS", "SUCCESS_NO_NEW_REVIEWS", "SUCCESS_NEW_REVIEWS")
            else ("🟡" if getattr(s, "last_status", "") == "PENDING" else "🔴")
        )
        branch = getattr(s, "branch_name", "Филиал")
        last_chk = getattr(s, "last_checked_at", None) or "не проверялся"
        rating_str = (
            f" ⭐ {s.last_rating:.1f}" if getattr(s, "last_rating", None) is not None else ""
        )

        lines.append(f"{status_icon} <b>{escape(branch)}</b> ({platform_name}){rating_str}")
        lines.append(f"   Проверка: <code>{escape(last_chk)}</code>")
        if getattr(s, "last_error", None):
            lines.append(f"   Ошибка: <i>{escape(s.last_error[:80])}</i>")
        lines.append("")

    return "\n".join(lines)
