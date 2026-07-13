import os
import re
import html
import sqlite3
import logging
from pathlib import Path
from datetime import datetime

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyParameters,
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
# 配置
# ==================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

BASE_DIR = Path(__file__).resolve().parent

DATA_DIR = Path(
    os.getenv(
        "RAILWAY_VOLUME_MOUNT_PATH",
        str(BASE_DIR / "data")
    )
)

DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "usdt_address_audit.db"


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

    addresses.extend(
        TRC20_PATTERN.findall(text)
    )

    addresses.extend(
        EVM_PATTERN.findall(text)
    )

    result = []
    seen = set()

    for address in addresses:
        key = normalize_address(address)

        if key not in seen:
            seen.add(key)
            result.append(address)

    return result


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
            CREATE TABLE IF NOT EXISTS reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                address TEXT NOT NULL,
                address_normalized TEXT NOT NULL UNIQUE,
                address_type TEXT NOT NULL,
                chat_id INTEGER NOT NULL,
                original_message_id INTEGER NOT NULL,
                sender_name TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                operator_id INTEGER,
                operator_name TEXT,
                created_at TEXT NOT NULL,
                decided_at TEXT
            )
            """
        )

        conn.commit()


def create_review(
    address: str,
    chat_id: int,
    original_message_id: int,
    sender_name: str,
):
    normalized = normalize_address(address)

    try:
        with db_conn() as conn:
            cursor = conn.execute(
                """
                INSERT INTO reviews (
                    address,
                    address_normalized,
                    address_type,
                    chat_id,
                    original_message_id,
                    sender_name,
                    status,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    address,
                    normalized,
                    get_address_type(address),
                    chat_id,
                    original_message_id,
                    sender_name,
                    datetime.now().isoformat(timespec="seconds"),
                ),
            )

            conn.commit()
            return cursor.lastrowid

    except sqlite3.IntegrityError:
        return None


def get_review(review_id: int):
    with db_conn() as conn:
        conn.row_factory = sqlite3.Row

        cursor = conn.execute(
            """
            SELECT *
            FROM reviews
            WHERE id = ?
            """,
            (review_id,),
        )

        return cursor.fetchone()


def finish_review(
    review_id: int,
    status: str,
    operator_id: int,
    operator_name: str,
) -> bool:
    with db_conn() as conn:
        cursor = conn.execute(
            """
            UPDATE reviews
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
                datetime.now().isoformat(timespec="seconds"),
                review_id,
            ),
        )

        conn.commit()
        return cursor.rowcount == 1


def get_stats():
    with db_conn() as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM reviews"
        ).fetchone()[0]

        pending = conn.execute(
            "SELECT COUNT(*) FROM reviews WHERE status = 'pending'"
        ).fetchone()[0]

        out_count = conn.execute(
            "SELECT COUNT(*) FROM reviews WHERE status = 'out'"
        ).fetchone()[0]

        no_count = conn.execute(
            "SELECT COUNT(*) FROM reviews WHERE status = 'no'"
        ).fetchone()[0]

    return total, pending, out_count, no_count


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
        "群里出现新的 USDT 地址后，我会自动提醒管理员选择：出 / 不出。"
    )


async def stats(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.message:
        return

    total, pending, out_count, no_count = get_stats()

    await update.message.reply_text(
        "📊 地址审核统计\n\n"
        f"全部地址：{total}\n"
        f"待处理：{pending}\n"
        f"已选择出：{out_count}\n"
        f"已选择不出：{no_count}"
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

    admin_mentions = await get_admin_mentions(
        context=context,
        chat_id=chat.id,
    )

    for address in addresses:
        review_id = create_review(
            address=address,
            chat_id=chat.id,
            original_message_id=message.message_id,
            sender_name=sender_name,
        )

        if review_id is None:
            continue

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "✅ 出",
                        callback_data=f"review:{review_id}:out",
                    ),
                    InlineKeyboardButton(
                        "❌ 不出",
                        callback_data=f"review:{review_id}:no",
                    ),
                ]
            ]
        )

        safe_address = html.escape(address)
        safe_address_type = html.escape(
            get_address_type(address)
        )

        if admin_mentions:
            mention_line = f"{admin_mentions}\n\n"
        else:
            mention_line = ""

        alert_text = (
            "🚨 <b>警报：发现新地址</b>\n\n"
            f"{mention_line}"
            f"类型：{safe_address_type}\n"
            f"地址：<code>{safe_address}</code>\n\n"
            "是否出款？"
        )

        await context.bot.send_message(
            chat_id=chat.id,
            text=alert_text,
            parse_mode="HTML",
            reply_markup=keyboard,
            reply_parameters=ReplyParameters(
                message_id=message.message_id,
                allow_sending_without_reply=True,
            ),
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
        r"review:(\d+):(out|no)",
        query.data or "",
    )

    if not match:
        await query.answer()
        return

    review_id = int(match.group(1))
    decision = match.group(2)

    review = get_review(review_id)

    if not review:
        await query.answer(
            "记录不存在",
            show_alert=True,
        )
        return

    admin = await is_admin(
        context=context,
        chat_id=review["chat_id"],
        user_id=query.from_user.id,
    )

    if not admin:
        await query.answer(
            "只有群管理员可以操作",
            show_alert=True,
        )
        return

    operator_name = get_user_name(query.from_user)

    success = finish_review(
        review_id=review_id,
        status=decision,
        operator_id=query.from_user.id,
        operator_name=operator_name,
    )

    if not success:
        latest = get_review(review_id)
        old_operator = latest["operator_name"] or "其他管理员"

        await query.answer(
            f"已经处理过了，操作人：{old_operator}",
            show_alert=True,
        )
        return

    if decision == "out":
        result = "✅ 出"
    else:
        result = "❌ 不出"

    safe_result = html.escape(result)
    safe_address = html.escape(review["address"])
    safe_operator = html.escape(operator_name)
    safe_type = html.escape(review["address_type"])

    try:
        await query.edit_message_reply_markup(
            reply_markup=None
        )
    except Exception as e:
        logger.warning("删除按钮失败: %s", e)

    try:
        await context.bot.send_message(
            chat_id=review["chat_id"],
            text=(
                f"<b>{safe_result}</b>\n\n"
                f"类型：{safe_type}\n"
                f"地址：<code>{safe_address}</code>\n"
                f"操作人：{safe_operator}"
            ),
            parse_mode="HTML",
            reply_parameters=ReplyParameters(
                message_id=review["original_message_id"],
                allow_sending_without_reply=True,
            ),
        )

    except Exception as e:
        logger.error("发送结果失败: %s", e)

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
    print("USDT 地址审核机器人正在启动...")

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .build()
    )

    app.add_handler(
        CommandHandler("start", start)
    )

    app.add_handler(
        CommandHandler("stats", stats)
    )

    app.add_handler(
        CallbackQueryHandler(
            handle_button,
            pattern=r"^review:",
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
