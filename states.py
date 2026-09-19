from aiogram.fsm.state import State, StatesGroup


class BuyStates(StatesGroup):
    waiting_custom_amount = State()
    waiting_game_id = State()


class AdminProductStates(StatesGroup):
    name = State()
    amount = State()
    price = State()


class AdminSettingsStates(StatesGroup):
    value = State()


class AdminRateStates(StatesGroup):
    value = State()


class AdminAdminStates(StatesGroup):
    waiting_user = State()


class AdminBroadcastStates(StatesGroup):
    waiting_message = State()


class RejectOrderStates(StatesGroup):
    reason = State()
