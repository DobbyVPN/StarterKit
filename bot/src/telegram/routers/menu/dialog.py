from aiogram.enums import ButtonStyle
from aiogram_dialog import Dialog, StartMode
from aiogram_dialog.widgets.input import MessageInput
from aiogram_dialog.widgets.style import Style
from aiogram_dialog.widgets.text import Format
from magic_filter import F

from src.application.common.policy import Permission
from src.core.constants import INLINE_QUERY_INVITE
from src.core.enums import BannerName
from src.telegram.keyboards import build_buttons_row
from src.telegram.routers.dashboard.handlers import on_smart_search
from src.telegram.states import Dashboard, MainMenu
from src.telegram.utils import require_permission
from src.telegram.widgets import Banner, I18nFormat, IgnoreUpdate
from src.telegram.widgets.kbd import (
    Button,
    CopyText,
    Row,
    Start,
    SwitchInlineQueryChosenChatButton,
    SwitchTo,
    Url,
    WebApp,
)
from src.telegram.window import Window

from .getters import connect_getter, invite_getter, menu_getter
from .handlers import (
    on_buy,
    on_get_trial,
    on_invite,
    on_reset_referral_code,
    on_text_button_click,
    on_withdraw_points,
)

custom_buttons = (
    build_buttons_row(1, text_on_click=on_text_button_click),
    build_buttons_row(2, text_on_click=on_text_button_click),
    build_buttons_row(3, text_on_click=on_text_button_click),
)

support_button = Url(
    text=I18nFormat("btn-menu.support"),
    id="support",
    url=Format("{support_url}"),
)

# One screen whose text and buttons follow the subscription: none, active, expired, paused.
menu = Window(
    Banner(BannerName.MENU),
    I18nFormat("msg-main-menu"),
    # No subscription
    Row(
        Button(
            text=I18nFormat("btn-menu.trial"),
            id="trial_free",
            on_click=on_get_trial,
            when=F["trial_available"] & F["trial_is_free"],
            style=Style(ButtonStyle.SUCCESS),
        ),
        Button(
            text=I18nFormat("btn-menu.trial-paid"),
            id="trial_paid",
            on_click=on_get_trial,
            when=F["trial_available"] & ~F["trial_is_free"],
            style=Style(ButtonStyle.SUCCESS),
        ),
        when=F["sub_state"] == "NONE",
    ),
    Row(
        Button(
            text=I18nFormat("btn-menu.buy"),
            id="buy",
            on_click=on_buy,
        ),
        when=F["sub_state"] == "NONE",
    ),
    Row(
        SwitchTo(
            text=I18nFormat("btn-menu.how-it-works"),
            id="how_it_works",
            state=MainMenu.HOW_IT_WORKS,
        ),
        support_button,
        when=F["sub_state"] == "NONE",
    ),
    # Active: connect once the key is issued; until then the app can already be downloaded.
    Row(
        SwitchTo(
            text=I18nFormat("btn-menu.connect"),
            id="connect",
            state=MainMenu.CONNECT,
            style=Style(ButtonStyle.SUCCESS),
            when=F["key_issued"],
        ),
        SwitchTo(
            text=I18nFormat("btn-menu.download"),
            id="download",
            state=MainMenu.DOWNLOAD,
            style=Style(ButtonStyle.SUCCESS),
            when=~F["key_issued"],
        ),
        when=F["sub_state"] == "ACTIVE",
    ),
    Row(
        Button(
            text=I18nFormat("btn-menu.renew"),
            id="renew",
            on_click=on_buy,
            when=~F["is_unlimited"],
        ),
        Button(
            text=I18nFormat("btn-menu.invite"),
            id="invite",
            on_click=on_invite,
            when=F["referral_enabled"],
        ),
        when=F["sub_state"] == "ACTIVE",
    ),
    # Expired
    Row(
        Button(
            text=I18nFormat("btn-menu.renew"),
            id="renew_expired",
            on_click=on_buy,
            style=Style(ButtonStyle.SUCCESS),
        ),
        when=F["sub_state"] == "EXPIRED",
    ),
    # Support for everyone except the "no subscription" state, which has it next to "How"
    Row(support_button, when=F["sub_state"] != "NONE"),
    Row(
        WebApp(
            text=I18nFormat("btn-menu.web-cabinet"),
            url=Format("{web_cabinet_url}"),
        ),
        when=F["web_enabled"],
    ),
    *custom_buttons,
    Row(
        Start(
            text=I18nFormat("btn-menu.dashboard"),
            id="dashboard",
            state=Dashboard.MAIN,
            mode=StartMode.RESET_STACK,
            when=require_permission(Permission.VIEW_DASHBOARD),
        ),
    ),
    MessageInput(func=on_smart_search),
    IgnoreUpdate(),
    state=MainMenu.MAIN,
    getter=menu_getter,
)

download_links = (
    Row(
        Url(text=I18nFormat("btn-connect.ios"), id="dl_ios", url=Format("{url_ios}")),
        Url(text=I18nFormat("btn-connect.android"), id="dl_android", url=Format("{url_android}")),
    ),
    Row(
        Url(text=I18nFormat("btn-connect.windows"), id="dl_windows", url=Format("{url_windows}")),
        Url(text=I18nFormat("btn-connect.macos"), id="dl_macos", url=Format("{url_macos}")),
    ),
)

# Download only: works before buying and while the key is on its way.
download = Window(
    Banner(BannerName.MENU),
    I18nFormat("msg-menu-download"),
    *download_links,
    Row(
        SwitchTo(text=I18nFormat("btn-back.general"), id="back", state=MainMenu.MAIN),
    ),
    IgnoreUpdate(),
    state=MainMenu.DOWNLOAD,
    getter=connect_getter,
)

# Connect: needs the key. Downloading is one tap away for those without the app.
connect = Window(
    Banner(BannerName.MENU),
    I18nFormat("msg-menu-connect"),
    Row(
        CopyText(
            text=I18nFormat("btn-connect.copy-key"),
            copy_text=Format("{sub_key}"),
        ),
        when=F["key_issued"],
    ),
    Row(
        Url(text=I18nFormat("btn-connect.guide"), id="guide", url=Format("{guide_url}")),
        when=F["guide_url"],
    ),
    Row(
        SwitchTo(
            text=I18nFormat("btn-menu.download"),
            id="download",
            state=MainMenu.DOWNLOAD,
        ),
    ),
    Row(
        SwitchTo(text=I18nFormat("btn-back.general"), id="back", state=MainMenu.MAIN),
    ),
    IgnoreUpdate(),
    state=MainMenu.CONNECT,
    getter=connect_getter,
)

how_it_works = Window(
    Banner(BannerName.MENU),
    I18nFormat("msg-menu-how-it-works"),
    Row(
        Button(
            text=I18nFormat("btn-menu.trial"),
            id="trial_free",
            on_click=on_get_trial,
            when=F["trial_available"] & F["trial_is_free"],
            style=Style(ButtonStyle.SUCCESS),
        ),
    ),
    Row(
        Button(text=I18nFormat("btn-menu.buy"), id="buy", on_click=on_buy),
    ),
    Row(
        SwitchTo(text=I18nFormat("btn-menu.download"), id="download", state=MainMenu.DOWNLOAD),
    ),
    Row(
        SwitchTo(text=I18nFormat("btn-back.general"), id="back", state=MainMenu.MAIN),
    ),
    IgnoreUpdate(),
    state=MainMenu.HOW_IT_WORKS,
    getter=menu_getter,
)

invite = Window(
    Banner(BannerName.REFERRAL),
    I18nFormat("msg-menu-invite"),
    Row(
        CopyText(
            text=I18nFormat("btn-invite.copy"),
            copy_text=Format("{referral_url}"),
        ),
        SwitchInlineQueryChosenChatButton(
            text=I18nFormat("btn-invite.send"),
            query=Format(INLINE_QUERY_INVITE),
            allow_user_chats=True,
            allow_group_chats=True,
            allow_channel_chats=True,
            id="send",
        ),
    ),
    Row(
        Button(
            text=I18nFormat("btn-invite.withdraw-points"),
            id="withdraw_points",
            on_click=on_withdraw_points,
            when=~F["has_points"],
        ),
        Url(
            text=I18nFormat("btn-invite.withdraw-points"),
            id="withdraw_points",
            url=Format("{withdraw}"),
            when=F["has_points"],
        ),
        when=F["is_points_reward"],
    ),
    Row(
        Button(
            text=I18nFormat("btn-invite.reset-referral"),
            id="reset_referral",
            on_click=on_reset_referral_code,
            when=F["referral_reset_enabled"],
        ),
    ),
    Row(
        SwitchTo(text=I18nFormat("btn-back.general"), id="back", state=MainMenu.MAIN),
    ),
    IgnoreUpdate(),
    state=MainMenu.INVITE,
    getter=invite_getter,
)

router = Dialog(menu, connect, download, how_it_works, invite)
