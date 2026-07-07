from aiogram.fsm.state import State, StatesGroup


class VKSetupStates(StatesGroup):
    waiting_token = State()
    waiting_group_id = State()
