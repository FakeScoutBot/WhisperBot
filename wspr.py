import os
import asyncio
import logging
import random
import string
import re
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Union, Any, Tuple

from motor.motor_asyncio import AsyncIOMotorClient
from pyrogram import Client, filters, types
from pyrogram.enums import ParseMode
from pyrogram.errors import BadRequest
from pyrogram.handlers import InlineQueryHandler, CallbackQueryHandler, MessageHandler

# Configure logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Constants
USERNAME_CACHE_EXPIRY = 60 * 60  # 1 hour in seconds
MESSAGE_EXPIRY_DAYS = 7  # Messages will be deleted after 7 days

# Load environment variables
BOT_TOKEN = os.environ.get("BOT_TOKEN")
API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH")
MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
DB_NAME = os.environ.get("DB_NAME", "whisper_bot")

# Check essential environment variables
if not BOT_TOKEN or not API_ID or not API_HASH:
    logger.error("Missing essential environment variables: BOT_TOKEN, API_ID, or API_HASH")
    exit(1)

# Initialize MongoDB client
mongo_client = AsyncIOMotorClient(MONGO_URI)
db = mongo_client[DB_NAME]
whispers_collection = db["whispers"]
users_collection = db["users"]

# Create indexes
async def setup_db_indexes():
    await whispers_collection.create_index("created_at", expireAfterSeconds=MESSAGE_EXPIRY_DAYS * 24 * 60 * 60)
    await whispers_collection.create_index("message_id", unique=True)
    await users_collection.create_index("user_id", unique=True)
    await users_collection.create_index("username", sparse=True)
    logger.info("Database indexes created")

# Initialize Pyrogram client
app = Client(
    "whisper_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
)

# Global storage for whisper messages (in-memory)
# Format: {message_id: [target_user_id, sender_id, message_content]}
whisper_messages = {}

# In-memory cache for username resolution
username_cache: Dict[int, Dict[str, Union[str, datetime]]] = {}
username_to_id_cache: Dict[str, Dict[str, Union[int, datetime]]] = {}

# Helper functions
def generate_message_id() -> int:
    """Generate a random message ID."""
    return random.randint(10000000, 999999999)

async def get_user_info(username_or_id):
    """Get user information from username or ID."""
    try:
        # Try to interpret as an ID first
        if isinstance(username_or_id, str) and username_or_id.isdigit():
            username_or_id = int(username_or_id)
        
        if isinstance(username_or_id, int):
            # Search by user ID
            user_doc = await users_collection.find_one({"user_id": username_or_id})
            if user_doc:
                return user_doc
        else:
            # Remove @ if present
            if username_or_id.startswith('@'):
                username_or_id = username_or_id[1:]
            
            # Search by username
            user_doc = await users_collection.find_one({"username": username_or_id})
            if user_doc:
                return user_doc
    except Exception as e:
        logger.error(f"Error getting user info: {e}")
    
    return None

async def store_user_info(user: types.User):
    """Store user information in database."""
    user_data = {
        "user_id": user.id,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "last_seen": datetime.now()
    }
    
    # Add username if available
    if user.username:
        user_data["username"] = user.username
    
    # Update user info in database
    await users_collection.update_one(
        {"user_id": user.id},
        {"$set": user_data},
        upsert=True
    )
    
    # Update cache
    if user.username:
        username_cache[user.id] = {
            "username": user.username,
            "timestamp": datetime.now()
        }
        username_to_id_cache[user.username] = {
            "user_id": user.id,
            "timestamp": datetime.now()
        }

# Bot handlers for Ultroid-style implementation
@app.on_inline_query(filters.regex(r"^wspr"))
async def handle_wspr_query(client, inline_query):
    """Handle initial 'wspr' inline query."""
    query_parts = inline_query.query.split(maxsplit=1)
    
    # Store sender info
    await store_user_info(inline_query.from_user)
    
    # Check if user specified a target
    if len(query_parts) < 2:
        # No target specified
        results = [
            types.InlineQueryResultArticle(
                title="Give Username",
                description="You didn't type a username or ID.",
                input_message_content=types.InputTextMessageContent(
                    "You didn't specify a username or ID."
                ),
            )
        ]
        await inline_query.answer(results=results)
        return
    
    # Try to get username or ID from query
    target = query_parts[1].split(maxsplit=1)[0]
    
    # Try to find the user
    user_info = await get_user_info(target)
    
    if not user_info:
        # User not found
        results = [
            types.InlineQueryResultArticle(
                title="User Not Found",
                description="Make sure username or ID is correct.",
                input_message_content=types.InputTextMessageContent(
                    "Make sure username or ID is correct."
                ),
            )
        ]
        await inline_query.answer(results=results)
        return
    
    # Check if message text was provided
    if len(query_parts[1].split(maxsplit=1)) < 2:
        # No message text
        results = [
            types.InlineQueryResultArticle(
                title="Type your message",
                description="You didn't type your message.",
                input_message_content=types.InputTextMessageContent(
                    "You didn't type your message."
                ),
            )
        ]
        await inline_query.answer(results=results)
        return
    
    # Get message text
    message_text = query_parts[1].split(maxsplit=1)[1]
    
    # Generate a message ID for this whisper
    message_id = generate_message_id()
    
    # Store the whisper data
    user_display = user_info.get("username", f"user{user_info['user_id']}")
    if user_info.get("username"):
        user_display = f"@{user_display}"
    
    # Store in memory
    whisper_messages[message_id] = [user_info["user_id"], inline_query.from_user.id, message_text]
    
    # Create inline result
    results = [
        types.InlineQueryResultArticle(
            title=user_info.get("first_name", "User"),
            description=message_text,
            input_message_content=types.InputTextMessageContent(
                f"🔒 **Secret Message** for {user_display}"
            ),
            reply_markup=types.InlineKeyboardMarkup([
                [
                    types.InlineKeyboardButton("📖 Show Message", callback_data=f"show_{message_id}"),
                    types.InlineKeyboardButton("🗑 Delete", callback_data=f"del_{message_id}")
                ],
                [
                    types.InlineKeyboardButton("📝 New Message", switch_inline_query_current_chat=f"wspr {target} ")
                ]
            ])
        )
    ]
    
    await inline_query.answer(results=results, cache_time=1)

@app.on_inline_query(filters.regex(r"^msg"))
async def handle_msg_query(client, inline_query):
    """Handle 'msg' inline query for user info."""
    query_parts = inline_query.query.split(maxsplit=1)
    
    # Store sender info
    await store_user_info(inline_query.from_user)
    
    # Check if user specified a target
    if len(query_parts) < 2:
        # No target specified
        results = [
            types.InlineQueryResultArticle(
                title="Give Username",
                description="You didn't type a username or ID.",
                input_message_content=types.InputTextMessageContent(
                    "You didn't specify a username or ID."
                ),
            )
        ]
        await inline_query.answer(results=results)
        return
    
    # Get target username or ID
    target = query_parts[1]
    
    # Try to find the user
    user_info = await get_user_info(target)
    
    if not user_info:
        # User not found
        name = f"Can't find user: {target}"
        results = [
            types.InlineQueryResultArticle(
                title=name,
                description="User not found",
                input_message_content=types.InputTextMessageContent(name),
            )
        ]
        await inline_query.answer(results=results)
        return
    
    # Create user info text
    user_id = user_info["user_id"]
    first_name = user_info.get("first_name", "Unknown")
    username = user_info.get("username")
    
    text = f"**Name:** `{first_name}`\n"
    text += f"**ID:** `{user_id}`\n"
    
    if username:
        text += f"**Username:** `{username}`\n"
        url = f"https://t.me/{username}"
    else:
        mention = f"[{first_name}](tg://user?id={user_id})"
        text += f"**Mention:** {mention}\n"
        url = f"tg://user?id={user_id}"
    
    # Create inline result
    buttons = [
        [
            types.InlineKeyboardButton("Private", url=url),
            types.InlineKeyboardButton("Secret Message", switch_inline_query_current_chat=f"wspr {username or user_id} Hello 👋")
        ]
    ]
    
    results = [
        types.InlineQueryResultArticle(
            title=first_name,
            description="Touch me",
            input_message_content=types.InputTextMessageContent(text),
            reply_markup=types.InlineKeyboardMarkup(buttons)
        )
    ]
    
    await inline_query.answer(results=results, cache_time=60)

@app.on_callback_query(filters.regex(r"^show_(\d+)$"))
async def show_message_callback(client, callback_query):
    """Handle 'show message' button press."""
    # Extract message ID
    message_id = int(callback_query.matches[0].group(1))
    
    # Check if message exists
    if message_id not in whisper_messages:
        await callback_query.answer("This message has expired or does not exist.", show_alert=True)
        return
    
    # Get message data
    target_id, sender_id, message_text = whisper_messages[message_id]
    
    # Check if the user is the intended recipient
    if callback_query.from_user.id == target_id:
        # Show the message
        await callback_query.answer(message_text, show_alert=True)
    else:
        # Not for this user
        await callback_query.answer("This message is not for you.", show_alert=True)

@app.on_callback_query(filters.regex(r"^del_(\d+)$"))
async def delete_message_callback(client, callback_query):
    """Handle 'delete message' button press."""
    # Extract message ID
    message_id = int(callback_query.matches[0].group(1))
    
    # Check if message exists
    if message_id not in whisper_messages:
        await callback_query.answer("This message has already been deleted.", show_alert=True)
        return
    
    # Get message data
    target_id, sender_id, message_text = whisper_messages[message_id]
    
    # Check if the user is the sender
    if callback_query.from_user.id == sender_id:
        # Delete the message
        del whisper_messages[message_id]
        await callback_query.answer("Message deleted.", show_alert=True)
        
        # Update the message
        try:
            await callback_query.edit_message_text(
                "🗑 **This message has been deleted.**",
                reply_markup=None
            )
        except BadRequest:
            # Message too old to edit
            pass
    else:
        # Not the sender
        await callback_query.answer("Only the sender can delete this message.", show_alert=True)

@app.on_message(filters.command("start"))
async def start_command(client, message):
    """Handle /start command."""
    # Store user information
    await store_user_info(message.from_user)
    
    welcome_text = (
        f"👋 Hello, {message.from_user.first_name}!\n\n"
        f"I'm **Whisper Bot** - I help you send secret messages that can only be viewed by the intended recipient.\n\n"
        f"**How to use me:**\n"
        f"1. Type `@{(await client.get_me()).username} wspr @username your_message` in any chat\n"
        f"2. The recipient will see a button to view your message\n"
        f"3. Only they can read the message\n\n"
        f"You can also use: `@{(await client.get_me()).username} msg @username` to get user info.\n\n"
        f"Try it now!"
    )
    
    await message.reply(
        welcome_text,
        reply_markup=types.InlineKeyboardMarkup([
            [types.InlineKeyboardButton("Try Whisper", switch_inline_query_current_chat="wspr ")],
            [types.InlineKeyboardButton("User Info", switch_inline_query_current_chat="msg ")]
        ]),
        parse_mode=ParseMode.MARKDOWN
    )

@app.on_message(filters.command("help"))
async def help_command(client, message):
    """Handle /help command."""
    help_text = (
        "🔒 **Whisper Bot Help**\n\n"
        "Send private messages that can only be viewed by the intended recipient.\n\n"
        "**Commands:**\n"
        "/start - Start the bot\n"
        "/help - Show this help message\n\n"
        "**How to use:**\n"
        f"1. Whisper format: `@{(await client.get_me()).username} wspr @username message`\n"
        f"2. User info format: `@{(await client.get_me()).username} msg @username`\n\n"
        "Only the intended recipient can view the message.\n"
        "The sender can delete the message at any time."
    )
    
    await message.reply(
        help_text,
        parse_mode=ParseMode.MARKDOWN
    )

# Main functions for bot operation
async def startup():
    """Run startup tasks."""
    try:
        # Setup database indexes
        await setup_db_indexes()
        
        # Start the bot
        await app.start()
        bot_info = await app.get_me()
        logger.info(f"Bot started as @{bot_info.username}")
        
        # Keep the bot running
        await asyncio.Event().wait()
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error(f"Error during startup: {e}")
    finally:
        # Cleanup
        await shutdown()

async def shutdown():
    """Run shutdown tasks."""
    try:
        await app.stop()
        mongo_client.close()
        logger.info("Bot and database connections closed")
    except Exception as e:
        logger.error(f"Error during shutdown: {e}")

# Main entry point
if __name__ == "__main__":
    try:
        asyncio.run(startup())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")