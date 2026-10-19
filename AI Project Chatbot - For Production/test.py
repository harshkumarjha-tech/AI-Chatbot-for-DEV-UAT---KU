import google.generativeai as genai
import os
from dotenv import load_dotenv

# Load your exact API keys
load_dotenv("KEY.env")
keys_string = os.getenv("GEMINI_API_KEYS") or os.getenv("KEY")
first_key = [k.strip() for k in keys_string.split(",") if k.strip()][0]

# Configure the API
genai.configure(api_key=first_key)

print("🔍 Asking Google for your exact available models...\n")

# Loop through and print every model your key is allowed to use
for m in genai.list_models():
    if 'generateContent' in m.supported_generation_methods:
        print(f"✅ YOU CAN USE: {m.name.replace('models/', '')}")