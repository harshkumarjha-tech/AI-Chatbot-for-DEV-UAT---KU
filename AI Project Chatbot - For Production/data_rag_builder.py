"""
ANTIGRAVITY DATA RAG BUILDER
RAG Store 2: Fetches actual university DATA from static API
endpoints, converts to semantic text chunks, and builds a
FAISS vector store for fast, offline semantic search.
Also provides Redis analytics helpers and the nightly refresh job.
"""

import os
import shutil
import json
import time
import requests
from datetime import datetime
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_community.vectorstores import FAISS
from langchain_core.documents import Document

os.environ["CURL_CA_BUNDLE"] = ""

BASE_URL           = "http://kuriseapi.karnavatiuniversity.edu.in"
DB_FAISS_DATA_PATH = "vectorstore/db_faiss_data"

# â”€â”€ These are the REAL API endpoint paths (verified working) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Add or remove endpoints here as needed. All data is stored in RAG Store 2
# and refreshed nightly. Users can query any of this data without a live API call.
STATIC_ENDPOINTS = {
    "/api/Course":        "Course",
    "/api/RoleMaster":    "Role",
    "/api/AcademicYear":  "AcademicYear",
    "/api/HostelMaster":  "HostelMaster",
}

def _get_embeddings():
    return HuggingFaceEmbeddings(model_name="all-MiniLM-L6-v2")

def fetch_endpoint_data(endpoint_path):
    url = f"{BASE_URL}{endpoint_path}"
    try:
        response = requests.get(url, timeout=15)
        if response.status_code == 200:
            data = response.json()
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                for v in data.values():
                    if isinstance(v, list):
                        return v
        print(f"   WARNING HTTP {response.status_code} from {endpoint_path}")
        return []
    except Exception as e:
        print(f"   WARNING Request failed for {endpoint_path}: {e}")
        return []

def _camel_to_title(key):
    result = ""
    for i, c in enumerate(str(key)):
        result += (" " + c) if c.isupper() and i > 0 else c
    return result.strip().title()

def item_to_text(item, data_type):
    if not isinstance(item, dict):
        return f"[{data_type.upper()}] {item}"
    parts = [f"[{data_type.upper()}]"]
    for key, value in item.items():
        if value is None or value == "":
            continue
        parts.append(f"{_camel_to_title(key)}: {value}")
    return "\n".join(parts)

def build_documents(endpoint, data_type, items):
    docs = []
    for item in items:
        docs.append(Document(
            page_content=item_to_text(item, data_type),
            metadata={
                "source_endpoint": endpoint,
                "data_type":       data_type,
                "raw_json":        json.dumps(item, sort_keys=True, ensure_ascii=False),
            }
        ))
    return docs

def build_or_refresh_data_rag(force_refresh=False, progress_callback=None):
    embeddings = _get_embeddings()
    if os.path.exists(DB_FAISS_DATA_PATH) and not force_refresh:
        print("OK RAG Store 2: Loading from disk (db_faiss_data)...")
        return FAISS.load_local(DB_FAISS_DATA_PATH, embeddings, allow_dangerous_deserialization=True)

    print("BUILDING RAG Store 2: Fetching from university API...")
    all_docs = []
    total_endpoints = len(STATIC_ENDPOINTS)
    for idx, (endpoint, data_type) in enumerate(STATIC_ENDPOINTS.items(), 1):
        if progress_callback:
            progress_callback(f"Fetching {data_type}... ({idx}/{total_endpoints})", idx, total_endpoints)
        print(f"   Fetching {endpoint}...")
        items = fetch_endpoint_data(endpoint)
        if items:
            docs = build_documents(endpoint, data_type, items)
            all_docs.extend(docs)
            print(f"   OK {len(docs)} {data_type} records loaded.")
        else:
            print(f"   WARNING No data from {endpoint}")
        time.sleep(1)

    if not all_docs:
        print("WARNING RAG Store 2: No documents fetched.")
        return None

    if progress_callback:
        progress_callback("Building FAISS vector store...", total_endpoints, total_endpoints)

    store = FAISS.from_documents(all_docs, embeddings)
    os.makedirs(DB_FAISS_DATA_PATH, exist_ok=True)
    store.save_local(DB_FAISS_DATA_PATH)
    print(f"OK RAG Store 2: {len(all_docs)} records saved to disk.")
    return store

def track_query(redis_client, query):
    if not redis_client:
        return
    try:
        redis_client.zincrby("search_analytics:queries", 1, query.strip().lower())
    except Exception as e:
        print(f"WARNING Analytics track_query failed: {e}")

def track_endpoint(redis_client, endpoint):
    if not redis_client:
        return
    try:
        redis_client.zincrby("search_analytics:endpoints", 1, endpoint)
    except Exception as e:
        print(f"WARNING Analytics track_endpoint failed: {e}")

def get_top_queries(redis_client, n=20):
    if not redis_client:
        return []
    try:
        results = redis_client.zrevrange("search_analytics:queries", 0, n - 1, withscores=True)
        return [(q, int(s)) for q, s in results]
    except Exception as e:
        print(f"WARNING get_top_queries failed: {e}")
        return []

def get_top_endpoints(redis_client, n=10):
    if not redis_client:
        return []
    try:
        results = redis_client.zrevrange("search_analytics:endpoints", 0, n - 1, withscores=True)
        return [(ep, int(s)) for ep, s in results]
    except Exception as e:
        print(f"WARNING get_top_endpoints failed: {e}")
        return []

def get_analytics_summary(redis_client):
    return {
        "top_queries":   [{"query": q, "searches": s} for q, s in get_top_queries(redis_client, 20)],
        "top_endpoints": [{"endpoint": ep, "hits": h} for ep, h in get_top_endpoints(redis_client, 10)],
        "generated_at":  datetime.now().isoformat(),
    }

def nightly_refresh_job(redis_client=None, progress_callback=None):
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\nNIGHTLY REFRESH JOB @ {stamp}")

    if redis_client:
        top_queries = get_top_queries(redis_client, 20)
        top_eps     = get_top_endpoints(redis_client, 10)
        print("\nTOP 20 MOST SEARCHED QUERIES:")
        for i, (q, s) in enumerate(top_queries, 1):
            print(f"   {i:2}. [{s:4}x]  {q}")
        print("\nTOP 10 HIT ENDPOINTS:")
        for ep, h in top_eps:
            print(f"   [{h:4}x]  {ep}")
    else:
        print("WARNING Redis not connected - analytics skipped.")

    # ── AUTO-DELETE old RAG Store before rebuild ──
    if os.path.exists(DB_FAISS_DATA_PATH):
        if progress_callback:
            progress_callback("Wiping old RAG data...", 0, 0)
        shutil.rmtree(DB_FAISS_DATA_PATH)
        print("🗑️  Old RAG Store 2 (db_faiss_data) wiped before rebuild.")
    else:
        print("ℹ️  No existing RAG Store 2 to wipe — building fresh.")

    print("\nRebuilding RAG Store 2 with fresh university data...")
    new_store = build_or_refresh_data_rag(force_refresh=True, progress_callback=progress_callback)

    if new_store:
        print("OK Nightly refresh complete - RAG Store 2 is up to date.")
    else:
        print("WARNING Nightly refresh failed - no data fetched.")

    print("NIGHTLY REFRESH DONE\n")
    return new_store

