import os
from dotenv import load_dotenv
import langchain_core_free_tier_rag

load_dotenv('KEY.env')
history = []

queries = [
    "fetch me all the cources",
    "which of theses is for arts and commerce student"
]

for q in queries:
    print(f"\n========== QUERY: {q} ==========\n")
    try:
        response = langchain_core_free_tier_rag.get_ai_response(q, history)
        print("RESPONSE:")
        print(response[:800] + "..." if len(response) > 800 else response)
        
        # mock history append
        history.append({"role": "user", "content": q})
        history.append({"role": "assistant", "content": response})
    except Exception as e:
        print("ERROR:", e)
