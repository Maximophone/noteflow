"""
Discord I/O Core Module - Handles Discord bot I/O operations without business logic.
"""

import asyncio
import logging
from typing import List, Dict, Any, Callable, Optional, Union
import discord
from discord import Message, User, DMChannel, TextChannel, Member
from datetime import datetime

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("discord_io")

class DiscordIOCore:
    """A decoupled I/O interface for a Discord bot."""

    def __init__(self, token: str):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True

        self.client = discord.Client(intents=intents)
        self.token = token
        self.event_callback = None

        self.client.event(self.on_ready)
        self.client.event(self.on_message)

    def set_event_callback(self, callback: Callable[[Dict[str, Any]], Any]):
        self.event_callback = callback

    async def on_ready(self):
        logger.info(f'Logged in as {self.client.user.name} ({self.client.user.id})')

    async def on_message(self, message: Message):
        if message.author.id == self.client.user.id:
            return

        if isinstance(message.channel, DMChannel):
            event = await self._on_dm(message)
            if self.event_callback:
                await self.event_callback(event)
            return

        if self.client.user in message.mentions:
            event = await self._on_mention(message)
            if self.event_callback:
                await self.event_callback(event)
            return

    async def _on_dm(self, message: Message) -> Dict[str, Any]:
        return {
            'type': 'dm',
            'user_id': str(message.author.id),
            'text': message.content,
            'timestamp': message.created_at.isoformat(),
            'author_name': message.author.name,
            'message_id': str(message.id)
        }

    async def _on_mention(self, message: Message) -> Dict[str, Any]:
        return {
            'type': 'mention',
            'user_id': str(message.author.id),
            'channel_id': str(message.channel.id),
            'guild_id': str(message.guild.id),
            'text': message.content,
            'timestamp': message.created_at.isoformat(),
            'author_name': message.author.name,
            'message_id': str(message.id)
        }

    async def send_dm(self, user_id: int, text: str) -> bool:
        try:
            if not isinstance(user_id, int):
                user_id = int(user_id)
                
            for _ in range(3):
                try:
                    user = await self.client.fetch_user(user_id)
                    await user.send(text)
                    return True
                except discord.HTTPException as e:
                    if e.status == 429:
                        retry_after = e.retry_after
                        logger.warning(f"Rate limited, retrying after {retry_after} seconds")
                        await asyncio.sleep(retry_after)
                    else:
                        raise
            
            return False
        except Exception as e:
            logger.error(f"Error sending DM to user {user_id}: {str(e)}")
            return False

    async def post_message(self, channel_id: int, text: str) -> bool:
        try:
            if not isinstance(channel_id, int):
                channel_id = int(channel_id)
                
            for _ in range(3):
                try:
                    channel = await self.client.fetch_channel(channel_id)
                    if not isinstance(channel, (TextChannel, DMChannel)):
                        logger.error(f"Channel {channel_id} is not a text channel")
                        return False
                    
                    await channel.send(text)
                    return True
                except discord.HTTPException as e:
                    if e.status == 429:
                        retry_after = e.retry_after
                        logger.warning(f"Rate limited, retrying after {retry_after} seconds")
                        await asyncio.sleep(retry_after)
                    else:
                        raise
            
            return False
        except Exception as e:
            logger.error(f"Error posting message to channel {channel_id}: {str(e)}")
            return False

    async def read_recent_messages(self, channel_id: int, limit: int = 100) -> List[Dict[str, Any]]:
        try:
            if not isinstance(channel_id, int):
                channel_id = int(channel_id)
                
            if limit > 100:
                limit = 100
                
            for _ in range(3):
                try:
                    channel = await self.client.fetch_channel(channel_id)
                    if not isinstance(channel, (TextChannel, DMChannel)):
                        logger.error(f"Channel {channel_id} is not a text channel")
                        return []
                    
                    messages = []
                    async for msg in channel.history(limit=limit):
                        messages.append({
                            'content': msg.content,
                            'author_id': str(msg.author.id),
                            'author_name': msg.author.name,
                            'timestamp': msg.created_at.isoformat(),
                            'message_id': str(msg.id)
                        })
                    
                    return messages
                except discord.HTTPException as e:
                    if e.status == 429:
                        retry_after = e.retry_after
                        logger.warning(f"Rate limited, retrying after {retry_after} seconds")
                        await asyncio.sleep(retry_after)
                    else:
                        raise
            
            return []
        except Exception as e:
            logger.error(f"Error reading messages from channel {channel_id}: {str(e)}")
            return []

    async def read_user_dm_history(self, user_id: int, limit: int = 100) -> List[Dict[str, Any]]:
        try:
            if not isinstance(user_id, int):
                user_id = int(user_id)
                
            if limit > 100:
                limit = 100
                
            for _ in range(3):
                try:
                    user = await self.client.fetch_user(user_id)
                    dm_channel = user.dm_channel
                    
                    if dm_channel is None:
                        dm_channel = await user.create_dm()
                    
                    messages = []
                    async for msg in dm_channel.history(limit=limit):
                        messages.append({
                            'content': msg.content,
                            'author_id': str(msg.author.id),
                            'author_name': msg.author.name,
                            'timestamp': msg.created_at.isoformat(),
                            'message_id': str(msg.id)
                        })
                    
                    return messages
                except discord.HTTPException as e:
                    if e.status == 429:
                        retry_after = e.retry_after
                        logger.warning(f"Rate limited, retrying after {retry_after} seconds")
                        await asyncio.sleep(retry_after)
                    else:
                        raise
            
            return []
        except Exception as e:
            logger.error(f"Error reading DM history with user {user_id}: {str(e)}")
            return []

    async def start_bot(self):
        await self.client.start(self.token)

    def run(self):
        self.client.run(self.token)

    async def close(self):
        await self.client.close()
        
    async def reconnect(self):
        try:
            logger.info("Attempting to reconnect Discord bot...")
            await self.client.close()
            
            intents = discord.Intents.default()
            intents.message_content = True
            intents.members = True
            
            self.client = discord.Client(intents=intents)
            
            self.client.event(self.on_ready)
            self.client.event(self.on_message)
            
            await self.client.login(self.token)
            
            if self.event_callback:
                ready_event = {
                    'type': 'ready',
                    'timestamp': datetime.now().isoformat(),
                    'bot_name': self.client.user.name,
                    'bot_id': str(self.client.user.id)
                }
                await self.event_callback(ready_event)
                
            return True
        except Exception as e:
            logger.error(f"Error during reconnection: {str(e)}")
            return False

