# Load from environment variables or .env file
import os
from dotenv import load_dotenv

load_dotenv()

ASSEMBLY_AI_KEY = os.getenv("ASSEMBLY_AI_KEY")
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
CODA_API_KEY = os.getenv("CODA_API_KEY")
