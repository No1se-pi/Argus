from aiogram import Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from app.bot.admin import AdminOnlyMiddleware
from app.bot.activity import router as activity_router
from app.bot.callbacks import router as callbacks_router
from app.bot.handlers import router
from app.bot.vk_likes import router as vk_likes_router
from app.bot.vk_likes_ui import router as vk_likes_ui_router
from app.config import Settings


def create_dispatcher(settings: Settings, **dependencies) -> Dispatcher:
    dispatcher = Dispatcher(storage=MemoryStorage())
    admin_middleware = AdminOnlyMiddleware(settings)
    activity_router.message.middleware(admin_middleware)
    activity_router.callback_query.middleware(admin_middleware)
    dispatcher.include_router(activity_router)
    router.message.middleware(admin_middleware)
    callbacks_router.callback_query.middleware(admin_middleware)
    callbacks_router.message.middleware(admin_middleware)
    vk_likes_router.message.middleware(admin_middleware)
    vk_likes_router.callback_query.middleware(admin_middleware)
    vk_likes_ui_router.message.middleware(admin_middleware)
    vk_likes_ui_router.callback_query.middleware(admin_middleware)
    dispatcher.include_router(vk_likes_ui_router)
    dispatcher.include_router(vk_likes_router)
    dispatcher.include_router(router)
    dispatcher.include_router(callbacks_router)
    dispatcher.workflow_data.update(settings=settings, **dependencies)
    return dispatcher
