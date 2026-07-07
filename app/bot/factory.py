from aiogram import Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from app.bot.admin import AdminOnlyMiddleware
from app.bot.callbacks import router as callbacks_router
from app.bot.handlers import router
from app.config import Settings


def create_dispatcher(settings: Settings, **dependencies) -> Dispatcher:
    dispatcher = Dispatcher(storage=MemoryStorage())
    router.message.middleware(AdminOnlyMiddleware(settings.admin_ids))
    callbacks_router.callback_query.middleware(AdminOnlyMiddleware(settings.admin_ids))
    callbacks_router.message.middleware(AdminOnlyMiddleware(settings.admin_ids))
    dispatcher.include_router(router)
    dispatcher.include_router(callbacks_router)
    dispatcher.workflow_data.update(settings=settings, **dependencies)
    return dispatcher
