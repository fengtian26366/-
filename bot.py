import os
import re
import html
import sqlite3
import logging
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)


# ==================================================
# Railway 配置
# ==================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

# 默认中国时间。Railway Variables 可加：
# BOT_TIMEZONE=Asia/Shanghai
# BOT_TIMEZONE=Asia/Colombo
BOT_TIMEZONE = os.getenv("BOT_TIMEZONE", "Asia/Shanghai").strip()

try:
    LOCAL_TZ = ZoneInfo(BOT_TIMEZONE)
except Exception:
    LOCAL_TZ = ZoneInfo("Asia/Shanghai")

BASE_DIR = Path(__file__).resolve().parent

DATA_DIR = Path(
    os.getenv(
        "RAILWAY_VOLUME_MOUNT_PATH",
        str(BASE_DIR / "data")
    )
)

DATA_DIR.mkdir(parents=True, exist_ok=True)

# 每个群独立记录地址；本版每次出现都会生成一条独立审核记录
DB_PATH = DATA_DIR / "usdt_address_audit_every_time.db"


# ==================================================
# 日志
# ==================================================

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


# ==================================================
# USDT 地址识别
# ==================================================

TRC20_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])T[1-9A-HJ-NP-Za-km-z]{33}(?![A-Za-z0-9])"
)

EVM_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])0x[a-fA-F0-9]{40}(?![A-Za-z0-9])"
)


def now_text() -> str:
    return datetime.now(LOCAL_TZ).strftime("%Y/%m/%d %H:%M:%S")


def format_dt(dt: datetime | None) -> str:
    if not dt:
        return now_text()

    try:
        return dt.astimezone(LOCAL_TZ).strftime("%Y/%m/%d %H:%M:%S")
    except Exception:
        return now_text()


def normalize_address(address: str) -> str:
    address = address.strip()

    if address.lower().startswith("0x"):
        return address.lower()

    return address


def get_address_type(address: str) -> str:
    if address.startswith("T"):
        return "USDT-TRC20"

    if address.lower().startswith("0x"):
        return "USDT-ERC20/BEP20"

    return "USDT"


def detect_addresses(text: str) -> list[str]:
    addresses = []
    addresses.extend(TRC20_PATTERN.findall(text))
    addresses.extend(EVM_PATTERN.findall(text))

    result = []
    seen = set()

    for address in addresses:
        key = normalize_address(address)

        if key not in seen:
            seen.add(key)
            result.append(address)

    return result


def short_text(text: str, max_len: int = 800) -> str:
    text = text.strip()

    if len(text) <= max_len:
        return text

    return text[:max_len] + "..."


# ==================================================
# 数据库
# ==================================================

def db_conn():
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    return sqlite3.connect(
        str(DB_PATH),
        timeout=30,
        check_same_thread=False,
    )


def init_db():
    with db_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS addresses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                address TEXT NOT NULL,
                address_normalized TEXT NOT NULL,
                address_type TEXT NOT NULL,
                occurrence_count INTEGER NOT NULL DEFAULT 1,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                UNIQUE(chat_id, address_normalized)
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                address_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                address TEXT NOT NULL,
                address_normalized TEXT NOT NULL,
                address_type TEXT NOT NULL,
                original_message_id INTEGER NOT NULL,
                sender_id INTEGER,
                sender_name TEXT,
                sender_mention TEXT,
                message_text TEXT,
                message_sent_at TEXT,
                occurrence_no INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                operator_id INTEGER,
                operator_name TEXT,
                created_at TEXT NOT NULL,
                decided_at TEXT,
                FOREIGN KEY(address_id) REFERENCES addresses(id)
            )
            """
        )

        conn.commit()


def create_event_for_address(
    address: str,
    chat_id: int,
    original_message_id: int,
    sender_id: int | None,
    sender_name: str,
    sender_mention: str,
    message_text: str,
    message_sent_at: str,
):
    """
    每个群独立记录地址。
    每次出现地址，都会生成一条独立 event，所以每次都有按钮。

    返回：
    event, is_new_address

    is_new_address=True：当前群第一次出现该地址，@ 当前群管理员。
    is_new_address=False：当前群重复出现该地址，不 @ 管理员，但仍然有按钮。
    """

    normalized = normalize_address(address)
    address_type = get_address_type(address)

    with db_conn() as conn:
        conn.row_factory = sqlite3.Row

        old = conn.execute(
            """
            SELECT *
            FROM addresses
            WHERE chat_id = ?
              AND address_normalized = ?
            """,
            (
                chat_id,
                normalized,
            ),
        ).fetchone()

        if old:
            occurrence_no = int(old["occurrence_count"]) + 1

            conn.execute(
                """
                UPDATE addresses
                SET
                    occurrence_count = ?,
                    last_seen_at = ?
                WHERE id = ?
                """,
                (
                    occurrence_no,
                    now_text(),
                    old["id"],
                ),
            )

            address_id = old["id"]
            is_new_address = False

        else:
            occurrence_no = 1

            cursor = conn.execute(
                """
                INSERT INTO addresses (
                    chat_id,
                    address,
                    address_normalized,
                    address_type,
                    occurrence_count,
                    first_seen_at,
                    last_seen_at
                )
                VALUES (?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    chat_id,
                    address,
                    normalized,
                    address_type,
                    now_text(),
                    now_text(),
                ),
            )

            address_id = cursor.lastrowid
            is_new_address = True

        cursor = conn.execute(
            """
            INSERT INTO events (
                address_id,
                chat_id,
                address,
                address_normalized,
                address_type,
                original_message_id,
                sender_id,
                sender_name,
                sender_mention,
                message_text,
                message_sent_at,
                occurrence_no,
                status,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            (
                address_id,
                chat_id,
                address,
                normalized,
                address_type,
                original_message_id,
                sender_id,
                sender_name,
                sender_mention,
                message_text,
                message_sent_at,
                occurrence_no,
                now_text(),
            ),
        )

        event_id = cursor.lastrowid

        conn.commit()

        event = conn.execute(
            """
            SELECT *
            FROM events
            WHERE id = ?
            """,
            (event_id,),
        ).fetchone()

        return event, is_new_address


def get_event(event_id: int):
    with db_conn() as conn:
        conn.row_factory = sqlite3.Row

        cursor = conn.execute(
            """
            SELECT *
            FROM events
            WHERE id = ?
            """,
            (event_id,),
        )

        return cursor.fetchone()


def finish_event(
    event_id: int,
    status: str,
    operator_id: int,
    operator_name: str,
) -> bool:
    """
    管理员点击“出 / 不出”后，记录本次确认：
    - 出 / 不出
    - 确认人
    - 确认时间
    """

    with db_conn() as conn:
        cursor = conn.execute(
            """
            UPDATE events
            SET
                status = ?,
                operator_id = ?,
                operator_name = ?,
                decided_at = ?
            WHERE
                id = ?
                AND status = 'pending'
            """,
            (
                status,
                operator_id,
                operator_name,
                now_text(),
                event_id,
            ),
        )

        conn.commit()
        return cursor.rowcount == 1


def get_stats(chat_id: int | None = None):
    with db_conn() as conn:
        if chat_id is None:
            total_address = conn.execute(
                "SELECT COUNT(*) FROM addresses"
            ).fetchone()[0]

            total_event = conn.execute(
                "SELECT COUNT(*) FROM events"
            ).fetchone()[0]

            pending = conn.execute(
                "SELECT COUNT(*) FROM events WHERE status = 'pending'"
            ).fetchone()[0]

            out_count = conn.execute(
                "SELECT COUNT(*) FROM events WHERE status = 'out'"
            ).fetchone()[0]

            no_count = conn.execute(
                "SELECT COUNT(*) FROM events WHERE status = 'no'"
            ).fetchone()[0]

            return total_address, total_event, pending, out_count, no_count

        total_address = conn.execute(
            "SELECT COUNT(*) FROM addresses WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()[0]

        total_event = conn.execute(
            "SELECT COUNT(*) FROM events WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()[0]

        pending = conn.execute(
            "SELECT COUNT(*) FROM events WHERE chat_id = ? AND status = 'pending'",
            (chat_id,),
        ).fetchone()[0]

        out_count = conn.execute(
            "SELECT COUNT(*) FROM events WHERE chat_id = ? AND status = 'out'",
            (chat_id,),
        ).fetchone()[0]

        no_count = conn.execute(
            "SELECT COUNT(*) FROM events WHERE chat_id = ? AND status = 'no'",
            (chat_id,),
        ).fetchone()[0]

    return total_address, total_event, pending, out_count, no_count


# ==================================================
# 用户与权限
# ==================================================

def get_user_name(user) -> str:
    if not user:
        return "未知用户"

    name = user.full_name or str(user.id)

    if user.username:
        return f"{name} (@{user.username})"

    return name


def get_user_mention(user) -> str:
    if not user:
        return "未知用户"

    if user.username:
        return f"@{html.escape(user.username)}"

    name = html.escape(user.full_name or str(user.id))

    return f'<a href="tg://user?id={user.id}">{name}</a>'


async def is_admin(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    user_id: int,
) -> bool:
    try:
        member = await context.bot.get_chat_member(
            chat_id=chat_id,
            user_id=user_id,
        )

        return member.status in (
            "creator",
            "administrator",
        )

    except Exception as e:
        logger.error("管理员权限检查失败: %s", e)
        return False


async def get_admin_mentions(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
) -> str:
    mentions = []

    try:
        admins = await context.bot.get_chat_administrators(
            chat_id=chat_id
        )

        for admin in admins:
            user = admin.user

            if user.is_bot:
                continue

            if user.username:
                mentions.append(
                    f"@{html.escape(user.username)}"
                )
            else:
                name = html.escape(
                    user.full_name or str(user.id)
                )

                mentions.append(
                    f'<a href="tg://user?id={user.id}">{name}</a>'
                )

    except Exception as e:
        logger.error("获取管理员列表失败: %s", e)

    if not mentions:
        return ""

    return " ".join(mentions)


# ==================================================
# 消息内容
# ==================================================

def build_status_text(event) -> str:
    status = event["status"]

    if status == "pending":
        return "⏳ 待确认"

    if status == "out":
        return "✅ 已确认出"

    if status == "no":
        return "❌ 已确认不出"

    return status


def safe_get(row, key: str, default=""):
    try:
        value = row[key]
        if value is None:
            return default
        return value
    except Exception:
        return default


def build_event_text(
    event,
    show_admin_mentions: str = "",
) -> str:
    address = html.escape(event["address"])
    address_type = html.escape(event["address_type"])
    occurrence_no = event["occurrence_no"]
    status_text = html.escape(build_status_text(event))

    sender = (
        safe_get(event, "sender_mention")
        or html.escape(safe_get(event, "sender_name", "未知用户"))
    )

    message_text = html.escape(
        short_text(safe_get(event, "message_text", ""))
    )

    message_sent_at = safe_get(event, "message_sent_at")
    if not message_sent_at:
        message_sent_at = safe_get(event, "created_at", "")

    text = "🚨 <b>USDT 地址记录</b>\n\n"

    # 只有当前群第一次出现新地址才 @ 管理员
    if show_admin_mentions:
        text += f"管理员：{show_admin_mentions}\n"

    text += (
        f"发送人：{sender}\n"
        f"发送时间：{html.escape(message_sent_at)}\n\n"
        f"USDT 地址：\n<code>{address}</code>\n\n"
        f"类型：{address_type}\n"
        f"出现次数：{occurrence_no}\n"
        f"是否确认：{status_text}\n"
    )

    if safe_get(event, "operator_name"):
        text += f"确认人：{html.escape(safe_get(event, 'operator_name'))}\n"

    if safe_get(event, "decided_at"):
        text += f"确认时间：{html.escape(safe_get(event, 'decided_at'))}\n"

    text += (
        f"\n发送内容：\n"
        f"<blockquote>{message_text}</blockquote>"
    )

    return text


# ==================================================
# 命令
# ==================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.message:
        return

    await update.message.reply_text(
        "USDT 地址审核机器人已启动。\n\n"
        "每个群独立记录地址。新地址会 @ 当前群管理员；重复地址不 @，但每次都会有按钮。"
    )


async def stats(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.message:
        return

    chat = update.effective_chat

    if chat and chat.type in ("group", "supergroup"):
        total_address, total_event, pending, out_count, no_count = get_stats(chat.id)
        title = "📊 当前群地址审核统计"
    else:
        total_address, total_event, pending, out_count, no_count = get_stats(None)
        title = "📊 全部群地址审核统计"

    await update.message.reply_text(
        f"{title}\n\n"
        f"去重地址数：{total_address}\n"
        f"总出现次数：{total_event}\n"
        f"待处理：{pending}\n"
        f"已确认出：{out_count}\n"
        f"已确认不出：{no_count}"
    )


# ==================================================
# 监听群消息
# ==================================================

async def handle_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if not message or not chat:
        return

    if chat.type not in ("group", "supergroup"):
        return

    text = message.text or message.caption or ""

    if not text:
        return

    addresses = detect_addresses(text)

    if not addresses:
        return

    sender_name = get_user_name(user)
    sender_mention = get_user_mention(user)
    sender_id = user.id if user else None
    message_sent_at = format_dt(message.date)

    for address in addresses:
        event, is_new_address = create_event_for_address(
            address=address,
            chat_id=chat.id,
            original_message_id=message.message_id,
            sender_id=sender_id,
            sender_name=sender_name,
            sender_mention=sender_mention,
            message_text=text,
            message_sent_at=message_sent_at,
        )

        admin_mentions = ""

        # 当前群第一次出现该地址：@ 当前群管理员
        # 当前群第 2 次及以上：不 @，但仍然给按钮
        if is_new_address:
            admin_mentions = await get_admin_mentions(
                context=context,
                chat_id=chat.id,
            )

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "✅ 出",
                        callback_data=f"event:{event['id']}:out",
                    ),
                    InlineKeyboardButton(
                        "❌ 不出",
                        callback_data=f"event:{event['id']}:no",
                    ),
                ]
            ]
        )

        alert_text = build_event_text(
            event=event,
            show_admin_mentions=admin_mentions,
        )

        await message.reply_text(
            text=alert_text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )


# ==================================================
# 按钮处理
# ==================================================

async def handle_button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if not query:
        return

    match = re.fullmatch(
        r"event:(\d+):(out|no)",
        query.data or "",
    )

    if not match:
        await query.answer()
        return

    event_id = int(match.group(1))
    decision = match.group(2)

    event = get_event(event_id)

    if not event:
        await query.answer(
            "记录不存在",
            show_alert=True,
        )
        return

    admin = await is_admin(
        context=context,
        chat_id=event["chat_id"],
        user_id=query.from_user.id,
    )

    if not admin:
        await query.answer(
            "只有群管理员可以操作",
            show_alert=True,
        )
        return

    operator_name = get_user_name(query.from_user)

    success = finish_event(
        event_id=event_id,
        status=decision,
        operator_id=query.from_user.id,
        operator_name=operator_name,
    )

    updated_event = get_event(event_id)

    if not success:
        old_operator = safe_get(updated_event, "operator_name", "其他管理员") or "其他管理员"

        try:
            await query.edit_message_text(
                text=build_event_text(updated_event),
                parse_mode="HTML",
                reply_markup=None,
            )

        except Exception as e:
            logger.warning("编辑已处理消息失败: %s", e)

        await query.answer(
            f"已经处理过了，操作人：{old_operator}",
            show_alert=True,
        )

        return

    # 点击“出 / 不出”后，只编辑当前机器人消息，并显示确认人、确认时间
    try:
        await query.edit_message_text(
            text=build_event_text(updated_event),
            parse_mode="HTML",
            reply_markup=None,
        )

    except Exception as e:
        logger.error("编辑确认结果失败: %s", e)

    await query.answer("操作成功")


# ==================================================
# 错误处理
# ==================================================

async def error_handler(
    update,
    context,
):
    logger.error(
        "机器人运行错误",
        exc_info=context.error,
    )


# ==================================================
# 启动
# ==================================================

def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "请在 Railway Variables 里添加 BOT_TOKEN"
        )

    init_db()

    print(f"数据库位置：{DB_PATH}")
    print(f"当前时间配置：{BOT_TIMEZONE}")
    print("USDT 地址审核机器人正在启动...")

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stats", stats))

    app.add_handler(
        CallbackQueryHandler(
            handle_button,
            pattern=r"^event:",
        )
    )

    app.add_handler(
        MessageHandler(
            filters.ALL & ~filters.COMMAND,
            handle_message,
        )
    )

    app.add_error_handler(error_handler)

    print("机器人已启动，等待群消息...")

    app.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()
