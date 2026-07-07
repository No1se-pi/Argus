from html import escape

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


def _module_line(module: ModuleInfo) -> str:
    icon = STATUS_ICON[module.status]
    return f"{icon} {escape(module.name)} — {escape(module.status.value)}"
