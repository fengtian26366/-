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

# TRC20 USDT 地址：T 开头，34 位
TRC20_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])T[1-9A-HJ-NP-Za-km-z]{33}(?![A-Za-z0-9])"
)

# ERC20 / BEP20 / Polygon 等 EVM 地址：0x + 40 位十六进制
EVM_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])0x[a-fA-F0-9]{40}(?![A-Za-z0-9])"
)


def now_text() -> str:
    return datetime.now().strftime("%Y/%m/%d %H:%M:%S")


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


def add_column_if_missing(
    conn,
    table_name: str,
    column_name: str,
    column_sql: str,
):
    cursor = conn.execute(f"PRAGMA table_info({table_name})")
    columns = [row[1] for row in cursor.fetchall()]

    if column_name not in columns:
        conn.execute(
            f"ALTER TABLE {table_name} ADD COLUMN {column_sql}"
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
                sender_id INTEGER,
                sender_name TEXT,
                sender_mention TEXT,
                message_text TEXT,
                occurrence_count INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'pending',
                operator_id INTEGER,
                operator_name TEXT,
                created_at TEXT NOT NULL,
                last_seen_at TEXT,
                decided_at TEXT
            )
            """
        )

        # 兼容旧版数据库，自动补字段
        add_column_if_missing(conn, "reviews", "sender_id", "sender_id INTEGER")
        add_column_if_missing(conn, "reviews", "sender_mention", "sender_mention TEXT")
        add_column_if_missing(conn, "reviews", "message_text", "message_text TEXT")
        add_column_if_missing(conn, "reviews", "occurrence_count", "occurrence_count INTEGER NOT NULL DEFAULT 1")
        add_column_if_missing(conn, "reviews", "last_seen_at", "last_seen_at TEXT")

        conn.commit()


def create_or_update_review(
    address: str,
    chat_id: int,
    original_message_id: int,
    sender_id: int | None,
    sender_name: str,
    sender_mention: str,
    message_text: str,
):
    """
    返回：
    review, is_new

    is_new=True 代表第一次出现，需要 @ 管理员。
    is_new=False 代表重复出现，不 @ 管理员，只记录出现次数。
    """

    normalized = normalize_address(address)

    with db_conn() as conn:
        conn.row_factory = sqlite3.Row

        old = conn.execute(
            """
            SELECT *
            FROM reviews
            WHERE address_normalized = ?
            """,
            (normalized,),
        ).fetchone()

        if old:
            conn.execute(
                """
                UPDATE reviews
                SET
                    occurrence_count = occurrence_count + 1,
                    last_seen_at = ?,
                    message_text = ?
                WHERE address_normalized = ?
                """,
                (
                    now_text(),
                    message_text,
                    normalized,
                ),
            )

            conn.commit()

            review = conn.execute(
                """
                SELECT *
                FROM reviews
                WHERE address_normalized = ?
                """,
                (normalized,),
            ).fetchone()

            return review, False

        cursor = conn.execute(
            """
            INSERT INTO reviews (
                address,
                address_normalized,
                address_type,
                chat_id,
                original_message_id,
                sender_id,
                sender_name,
                sender_mention,
                message_text,
                occurrence_count,
                status,
                created_at,
                last_seen_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'pending', ?, ?)
            """,
            (
                address,
                normalized,
                get_address_type(address),
                chat_id,
                original_message_id,
                sender_id,
                sender_name,
                sender_mention,
                message_text,
                now_text(),
                now_text(),
            ),
        )

        review_id = cursor.lastrowid

        conn.commit()

        review = conn.execute(
            """
            SELECT *
            FROM reviews
            WHERE id = ?
            """,
            (review_id,),
        ).fetchone()

        return review, True


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
                now_text(),
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

def build_status_text(review) -> str:
    status = review["status"]

    if status == "pending":
        return "⏳ 待确认"

    if status == "out":
        return "✅ 已确认出"

    if status == "no":
        return "❌ 已确认不出"

    return status


def build_record_text(
    review,
    show_admin_mentions: str = "",
) -> str:
    address = html.escape(review["address"])
    address_type = html.escape(review["address_type"])
    occurrence_count = review["occurrence_count"]
    status_text = html.escape(build_status_text(review))

    sender = (
        review["sender_mention"]
        or html.escape(review["sender_name"] or "未知用户")
    )

    message_text = html.escape(
        short_text(review["message_text"] or "")
    )

    text = "🚨 <b>USDT 地址记录</b>\n\n"

    # 只有第一次出现新地址时才会传 show_admin_mentions
    if show_admin_mentions:
        text += f"管理员：{show_admin_mentions}\n"

    text += (
        f"发送人：{sender}\n\n"
        f"USDT 地址：\n<code>{address}</code>\n\n"
        f"类型：{address_type}\n"
        f"出现次数：{occurrence_count}\n"
        f"是否确认：{status_text}\n"
    )

    if review["operator_name"]:
        text += f"确认人：{html.escape(review['operator_name'])}\n"

    if review["decided_at"]:
        text += f"确认时间：{html.escape(review['decided_at'])}\n"

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
        "新地址会 @ 管理员，重复地址只记录出现次数。"
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
        f"全部命中地址：{total}\n"
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

    for address in addresses:
        review, is_new = create_or_update_review(
            address=address,
            chat_id=chat.id,
            original_message_id=message.message_id,
            sender_id=sender_id,
            sender_name=sender_name,
            sender_mention=sender_mention,
            message_text=text,
        )

        keyboard = None
        admin_mentions = ""

        # 只有第一次出现新地址时 @ 管理员
        if is_new:
            admin_mentions = await get_admin_mentions(
                context=context,
                chat_id=chat.id,
            )

        # 只要这个地址还没确认，就显示按钮。
        # 重复出现时也会显示按钮，但不会 @ 管理员。
        if review["status"] == "pending":
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "✅ 出",
                            callback_data=f"review:{review['id']}:out",
                        ),
                        InlineKeyboardButton(
                            "❌ 不出",
                            callback_data=f"review:{review['id']}:no",
                        ),
                    ]
                ]
            )

        alert_text = build_record_text(
            review=review,
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

    updated_review = get_review(review_id)

    # 如果已经被其他管理员点过，则刷新当前按钮消息为最终状态
    if not success:
        old_operator = updated_review["operator_name"] or "其他管理员"

        try:
            await query.edit_message_text(
                text=build_record_text(updated_review),
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

    # 成功处理后，只编辑当前机器人警报消息，不额外再发第二条
    try:
        await query.edit_message_text(
            text=build_record_text(updated_review),
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
