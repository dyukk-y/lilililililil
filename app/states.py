"""FSM-состояния бота."""
from aiogram.fsm.state import StatesGroup, State

# ================== STATES ==================
class PostState(StatesGroup):
    wait_photo = State()
    wait_text_after_photo = State()
    wait_text_only = State()

class RejectState(StatesGroup):
    wait_reason = State()

class BroadcastState(StatesGroup):
    wait_broadcast_text = State()
    wait_broadcast_photo = State()
    wait_broadcast_text_with_photo = State()
    wait_broadcast_confirm = State()

class BlacklistState(StatesGroup):
    wait_keyword = State()

class SubscriptionState(StatesGroup):
    wait_subscription_add = State()

class AdminPostState(StatesGroup):
    wait_post_id_for_publish = State()
    wait_post_id_for_reject = State()
    wait_reject_reason = State()
    wait_reject_confirm = State()

class DeletePostState(StatesGroup):
    wait_post_link = State()
    wait_reason = State()

class AuthorLookupState(StatesGroup):
    wait_post_link = State()

class SettingsState(StatesGroup):
    wait_new_value = State()

class AutoPhraseState(StatesGroup):
    wait_phrase = State()

