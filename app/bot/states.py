from aiogram.fsm.state import State, StatesGroup


class VKSetupStates(StatesGroup):
    waiting_group_token = State()
    waiting_user_token = State()
    waiting_group_id = State()


class TelegramAuthStates(StatesGroup):
    waiting_phone = State()
    waiting_code = State()
    waiting_password = State()


class TelegramSourceSetupStates(StatesGroup):
    waiting_forward = State()


class VKLikesStates(StatesGroup):
    waiting_period = State()
    waiting_template = State()
    waiting_binding = State()
