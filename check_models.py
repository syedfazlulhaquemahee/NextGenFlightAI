import os
from google import genai

client = genai.Client(api_key=os.getenv("GOOGLE_API_KEY", "None"))

for m in client.models.list():
    methods = getattr(m, "supported_actions", None) or getattr(m, "supported_generation_methods", [])
    if "generateContent" in methods:
        print(m.name)
