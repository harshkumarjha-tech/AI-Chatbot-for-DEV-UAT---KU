import os
import sys
import time
import requests
import json
import re
import threading
import difflib
from contextvars import ContextVar
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langchain_groq import ChatGroq
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_community.vectorstores import FAISS
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.messages import SystemMessage
from langchain_classic.agents import AgentExecutor, create_tool_calling_agent
from langchain_core.tools import tool

from data_rag_builder import (
    build_or_refresh_data_rag,
    track_query,
    DB_FAISS_DATA_PATH,
)

os.environ['CURL_CA_BUNDLE'] = ''
load_dotenv("KEY.env")

# Fix Windows terminal encoding
sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')

VECTOR_STORE      = None   # RAG Store 1 - API Endpoint Docs (Swagger)
VECTOR_STORE_DATA = None   # RAG Store 2 - Actual University DATA

DB_FAISS_PATH = "vectorstore/db_faiss"

CURRENT_KEY_INDEX = 0
key_lock          = threading.Lock()

CURRENT_GROQ_KEY_INDEX = 0
groq_key_lock          = threading.Lock()

data_store_lock   = threading.Lock()

temp_full_data_var: ContextVar[str] = ContextVar("temp_full_data", default=None)

# ==============================================================================
# PRESENTATION MODE
# Set PRESENTATION_MODE=true in KEY.env before your demo.
# When ON:
#   - STATIC queries skip LLM summarization (saves 1 API call each)
#   - Common CHAT greetings use instant template replies (saves 1 API call each)
#   - Combined with pre-warmed Redis cache = near-ZERO API calls during demo
# ==============================================================================
PRESENTATION_MODE = os.getenv("PRESENTATION_MODE", "false").lower() == "true"

if PRESENTATION_MODE:
    print("[PRESENTATION MODE ON] LLM calls reduced for demo safety.")


# ==============================================================================
# SHARED HELPER: HTML TABLE BUILDER
# Used by both the Live API path and the Static RAG path.
# ==============================================================================
def build_html_table(items: list, title: str, color: str = "green") -> str:
    """Converts a list of dicts into a styled, scrollable HTML table string."""
    if not items:
        return ""

    preferred = ["code", "description", "durationofYears", "graduation", "capacity"]

    if isinstance(items[0], dict):
        all_keys = list(items[0].keys())
        headers  = [k for k in preferred if k in all_keys] + \
                   [k for k in all_keys if k not in preferred]
                   
        # Automatically sort items by primary key if present (ascending)
        sort_keys = ["id", "code", "roleid", "holidayid", "academicyearid"]
        for sk in sort_keys:
            match = next((k for k in all_keys if k.lower() == sk), None)
            if match:
                try:
                    # Sort numerically if values are numeric strings, otherwise alphabetically
                    def _sort_val(x, key=match):
                        v = x.get(key)
                        if v is None:
                            return (1, 0, "")  # None values go to the end
                        try:
                            return (0, int(v), "")
                        except (ValueError, TypeError):
                            return (0, 0, str(v))
                    items = sorted(items, key=_sort_val)
                    break
                except Exception:
                    pass
    else:
        headers = []

    lines = [f"<div class='mb-2 text-{color}-400 font-bold mt-4'>{title}</div>"]
    lines.append("<div class='max-h-64 overflow-y-auto overflow-x-auto bg-gray-900 "
                 "rounded-lg border border-gray-700 shadow-lg'>")
    lines.append("<table class='w-full text-left text-xs text-gray-300 whitespace-nowrap'>")

    if headers:
        lines.append("<thead class='text-xs text-gray-400 bg-gray-800 uppercase sticky top-0'><tr>")
        for h in headers:
            clean_h = re.sub(r'(?<!^)(?=[A-Z])', ' ', str(h)).title()
            lines.append(f"<th class='px-4 py-3 font-semibold'>{clean_h}</th>")
        lines.append("</tr></thead>")
        lines.append("<tbody class='divide-y divide-gray-800'>")
        for item in items:
            lines.append("<tr class='hover:bg-gray-800 transition-colors'>")
            for h in headers:
                val = item.get(h, "-")
                if isinstance(val, (dict, list)):
                    val = json.dumps(val)
                lines.append(f"<td class='px-4 py-2'>{val}</td>")
            lines.append("</tr>")
        lines.append("</tbody>")
    else:
        lines.append("<tbody class='divide-y divide-gray-800'>")
        for item in items:
            lines.append(f"<tr class='hover:bg-gray-800'><td class='px-4 py-2'>{item}</td></tr>")
        lines.append("</tbody>")

    lines.append("</table></div>")
    return "".join(lines)


# ==============================================================================
# TOOL 1: Search API Documentation  (RAG Store 1 - Swagger Endpoint Docs)
# ==============================================================================
@tool
def search_api_documentation(query: str) -> str:
    """Searches the local FAISS RAG database for API endpoints."""
    if not VECTOR_STORE:
        return "Error: Database offline."
    print("[WAIT] Throttling API to protect Free Tier limits...")
    time.sleep(3)
    docs = VECTOR_STORE.similarity_search(query, k=15)
    return "\n\n".join([d.page_content for d in docs])


# ==============================================================================
# TOOL 2: Execute Live API Request
# ==============================================================================
@tool
def execute_api_request(endpoint_path: str, search_filter: str = "") -> str:
    """
    Executes a GET request to the university server to fetch live data.
    If the user wants a specific item, provide it in 'search_filter'.
    """
    print(f"[API] Executing live request to {endpoint_path}...")
    if search_filter:
        print(f"[FILTER] Tool-Level Filtering Activated for: '{search_filter}'")
    time.sleep(3)

    if not endpoint_path.startswith('/'):
        endpoint_path = '/' + endpoint_path

    mock_data = {
        "/api/ReEvaluationReTotalingDet": [{"id": 1, "student_name": "Test Student", "status": "Pending Re-evaluation", "subject": "Mathematics"}],
        "/api/ScholarshipSetupHeader": [{"id": 101, "name": "Merit Scholarship", "amount": 50000, "criteria": "GPA > 3.5"}, {"id": 102, "name": "Sports Scholarship", "amount": 25000, "criteria": "National Level Player"}],
        "/api/HostelInventoryAllocation": [{"hostel": "Block A", "room_no": "101", "bed_no": "1", "student_name": "Test Student", "status": "Allocated"}],
    }

    try:
        if endpoint_path in mock_data:
            class MockResponse:
                def __init__(self, data):
                    self.status_code = 200
                    self.text = json.dumps(data)
                    self._data = data
                def json(self):
                    return self._data
            response = MockResponse(mock_data[endpoint_path])
        else:
            base_url = "http://kuriseapi.karnavatiuniversity.edu.in"
            url = f"{base_url}{endpoint_path}"
            response = requests.get(url, timeout=15)

        if response.status_code in [401, 403]:
            return (f"SYSTEM ERROR {response.status_code}: Unauthorized. "
                    "Tell the user: 'This API is secured. You need a Bearer or JWT token.'")

        if response.status_code != 200:
            return (f"SYSTEM ERROR {response.status_code}: The endpoint '{endpoint_path}' "
                    "does not exist! DO NOT GUESS URLs. Use 'search_api_documentation' first.")

        raw_data        = response.text
        parsed_json     = None
        total_extracted = 0

        try:
            parsed_json = response.json()
        except Exception:
            pass

        if parsed_json and isinstance(parsed_json, list):
            if search_filter:
                filtered_list = []
                try:
                    pattern = re.compile(search_filter, re.IGNORECASE)
                except re.error:
                    pattern = re.compile(re.escape(search_filter), re.IGNORECASE)

                for item in parsed_json:
                    if isinstance(item, dict):
                        item_str = " ".join([str(v) for v in item.values() if v is not None])
                        if pattern.search(item_str):
                            filtered_list.append(item)
                    else:
                        if pattern.search(str(item)):
                            filtered_list.append(item)

                parsed_json = filtered_list
                raw_data    = json.dumps(parsed_json)

            total_extracted = len(parsed_json)

            if total_extracted > 0:
                color         = "blue" if search_filter else "green"
                display_title = search_filter.replace('|', ', ').upper() if search_filter else "ALL RECORDS"
                title         = f"RESULTS FOR '{display_title}' ({total_extracted} Items):"
                html_table    = build_html_table(parsed_json, title, color)
                temp_full_data_var.set(html_table)

        preferred = []
        if parsed_json and isinstance(parsed_json, list) and total_extracted > 0 \
                and isinstance(parsed_json[0], dict):
            all_keys  = list(parsed_json[0].keys())
            pref_cols = ["code", "description", "durationofYears", "graduation", "capacity"]
            preferred = [k for k in pref_cols if k in all_keys] + \
                        [k for k in all_keys if k not in pref_cols]

        metadata_prefix = (f"\n[SYSTEM METADATA: Your tool successfully extracted "
                           f"exactly {total_extracted} items matching the criteria.]\n")

        ai_context_data = raw_data
        if total_extracted > 3 and parsed_json and isinstance(parsed_json[0], dict):
            primary_keys    = preferred[:3] if preferred else list(parsed_json[0].keys())[:3]
            minimized_list  = [{k: item.get(k) for k in primary_keys} for item in parsed_json]
            ai_context_data = json.dumps(minimized_list)

        return metadata_prefix + f"Data Context (Primary Keys Only):\n{ai_context_data}"

    except Exception as e:
        return f"API Call Failed: {str(e)}"


# ==============================================================================
# 2. RAG ENGINE INITIALIZATION
# ==============================================================================
def initialize_system():
    """Initialize RAG Store 1 - API Endpoint Docs from Swagger."""
    global VECTOR_STORE
    embeddings = HuggingFaceEmbeddings(model_name="all-MiniLM-L6-v2")

    if os.path.exists(DB_FAISS_PATH):
        VECTOR_STORE = FAISS.load_local(
            DB_FAISS_PATH, embeddings, allow_dangerous_deserialization=True
        )
    else:
        url  = "http://kuriseapi.karnavatiuniversity.edu.in/swagger/v1/swagger.json"
        res  = requests.get(url)
        data = res.json()

        docs = []
        for path, methods in data.get("paths", {}).items():
            for method, details in methods.items():
                if method.lower() == 'get':
                    summary = details.get("summary", "No description")
                    docs.append(
                        f"Endpoint: {path} | Method: {method.upper()} | Summary: {summary}"
                    )

        VECTOR_STORE = FAISS.from_texts(docs, embeddings)
        os.makedirs('vectorstore', exist_ok=True)
        VECTOR_STORE.save_local(DB_FAISS_PATH)


def initialize_data_rag():
    """Initialize RAG Store 2 - Actual University DATA."""
    global VECTOR_STORE_DATA
    VECTOR_STORE_DATA = build_or_refresh_data_rag(force_refresh=False)
    if VECTOR_STORE_DATA:
        print("[OK] RAG Store 2 (Data) is online.")
    else:
        print("[WARN] RAG Store 2 (Data) failed to load.")


def reload_data_rag():
    """Hot-reload RAG Store 2 after nightly refresh (thread-safe)."""
    global VECTOR_STORE_DATA
    with data_store_lock:
        embeddings = HuggingFaceEmbeddings(model_name="all-MiniLM-L6-v2")
        VECTOR_STORE_DATA = FAISS.load_local(
            DB_FAISS_DATA_PATH, embeddings, allow_dangerous_deserialization=True
        )
        print("[OK] RAG Store 2 hot-reloaded after nightly refresh.")


def ingest_new_documents(documents: list, source_endpoint: str = "") -> bool:
    """
    Hot-add new Document objects into RAG Store 2 (in memory + on disk).
    Called when user confirms a live API discovery result is correct.
    Deduplicates by normalizing raw_json (sorted keys) to prevent duplicates
    even if JSON key ordering differs between API calls.
    Returns True on success.
    """
    global VECTOR_STORE_DATA
    try:
        embeddings = HuggingFaceEmbeddings(model_name="all-MiniLM-L6-v2")

        def _normalize_json(raw: str) -> str:
            """Canonical JSON string with sorted keys for stable comparison."""
            try:
                return json.dumps(json.loads(raw), sort_keys=True, ensure_ascii=False)
            except (json.JSONDecodeError, TypeError):
                return raw

        with data_store_lock:
            if VECTOR_STORE_DATA is None:
                VECTOR_STORE_DATA = FAISS.from_documents(documents, embeddings)
                print(f"[OK] Ingested {len(documents)} new documents (initial load) into RAG Store 2.")
            else:
                # Build set of normalized JSON strings already in the store
                existing_jsons = set()
                for existing_doc in VECTOR_STORE_DATA.docstore._dict.values():
                    raw = existing_doc.metadata.get("raw_json", "")
                    if raw:
                        existing_jsons.add(_normalize_json(raw))

                unique_docs = []
                for doc in documents:
                    norm = _normalize_json(doc.metadata.get("raw_json", ""))
                    if norm not in existing_jsons:
                        unique_docs.append(doc)
                        existing_jsons.add(norm)  # prevent intra-batch dupes too

                if unique_docs:
                    VECTOR_STORE_DATA.add_documents(unique_docs)
                    print(f"[OK] Ingested {len(unique_docs)} NEW documents into RAG Store 2. (Skipped {len(documents) - len(unique_docs)} duplicates)")
                else:
                    print(f"[OK] Skipped {len(documents)} documents during ingest (all were duplicates already in RAG).")

            os.makedirs(os.path.dirname(DB_FAISS_DATA_PATH), exist_ok=True)
            VECTOR_STORE_DATA.save_local(DB_FAISS_DATA_PATH)
        return True
    except Exception as e:
        print(f"[ERROR] ingest_new_documents failed: {e}")
        return False


# ==============================================================================
# 3. STATIC PATH HANDLER
# ==============================================================================
def get_static_response(user_input: str, llm) -> str:
    """
    Handles STATIC intent: searches RAG Store 2, builds HTML table.
    In PRESENTATION_MODE: skips the LLM summarization call entirely to save API quota.
    """
    used_or_fallback = False
    if not VECTOR_STORE_DATA:
        return "<div class='text-red-400 text-sm'>Data store is initializing. Please try again.</div>"

    with data_store_lock:
        docs = VECTOR_STORE_DATA.similarity_search(user_input, k=200)

    if not docs:
        return "<div class='text-gray-400 text-sm'>No matching data found in the local database.</div>"

    # Identify the primary target type:
    # 1. Use dedicated regex matching to beat FAISS typo confusion (ignoring adjectives)
    target_type = ""
    query_lower = user_input.lower()
    
    from rag_registry import build_target_type_regex_map, _load_registry
    for label, pattern in build_target_type_regex_map().items():
        if re.search(pattern, query_lower):
            target_type = label
            break
            
    # 1.5. If strict regex fails, try fuzzy matching against all registry keywords
    if not target_type:
        query_words = set(query_lower.split())
        registry = _load_registry()
        for label, entry in registry.items():
            kw_words = set()
            for kw in entry.get("keywords", []):
                kw_words.update(kw.split())
            
            # Check if any query word is a close match to any registered keyword word
            for qw in query_words:
                if difflib.get_close_matches(qw, kw_words, n=1, cutoff=0.8):
                    target_type = label
                    break
            if target_type:
                break
        
    # 2. If regex and fuzzy fail, fallback to the top semantic FAISS match
    if not target_type:
        target_type = docs[0].metadata.get("data_type", "")

    # Reconstruct items from stored metadata, keeping ONLY the target_type
    items = []
    filtered_docs = []
    for doc in docs:
        if doc.metadata.get("data_type") == target_type:
            filtered_docs.append(doc)
            try:
                item = json.loads(doc.metadata.get("raw_json", "{}"))
                if item:
                    items.append(item)
            except Exception:
                pass
    
    docs = filtered_docs

    # Remove the target_type label words from the extracted keywords to avoid double-filtering.
    # target_type filtering above already narrowed the docs to the correct category,
    # so running the keyword filter against the same category word (e.g. "commerce") on raw
    # JSON record values will always yield 0 matches and trigger a false NO_DATA_FOUND.
    _type_words = set(target_type.lower().split()) if target_type else set()
    # Also include ALL registered keywords for this target_type (e.g. "hostel", "hostels",
    # "accommodation", "role", "roles", "academic", "year", "years" etc.)
    # These are intent/category words that won't appear as JSON values — stripping them
    # prevents the keyword filter from falsely returning 0 results.
    if target_type:
        from rag_registry import _load_registry as _reg_load
        _reg = _reg_load()
        if target_type in _reg:
            for _kw in _reg[target_type].get("keywords", []):
                _type_words.update(_kw.lower().split())

    # 2.5 Exact Column Match Filter
    #     Detects queries like "Department Codefor Enrollment = 01" or "code is 1135"
    if items and isinstance(items[0], dict):
        col_keys = sorted(list(items[0].keys()), key=len, reverse=True)
        for k in col_keys:
            if len(k) < 3 and k.lower() not in ["id"]:
                continue
            col_match = re.search(r'\b' + re.escape(k.lower()) + r'\b', query_lower)
            if col_match:
                substr_after_col = query_lower[col_match.end():].strip()
                match = re.match(r'^(?:=|:|\bequals\b|\bis\b)\s*([a-z0-9\.\-]+)', substr_after_col)
                if match:
                    target_val = match.group(1).strip()
                    if target_val:
                        matched_items = [it for it in items if str(it.get(k, "")).strip().lower() == target_val]
                        items = matched_items
                        print(f"[STATIC] Exact Column Filter: {k} == '{target_val}' -> {len(items)} items")
                        user_input = re.sub(re.escape(target_val), "", user_input, flags=re.IGNORECASE)

    # 3. AI Keyword Extraction
    # Use LLM to extract core subjects, ignoring conversational filler.
    sample_json = "{}"
    headers = []
    if items and len(items) > 0 and isinstance(items[0], dict):
        headers = list(items[0].keys())
        
        # Aggregate unique values per column to give the AI a complete "dictionary" of available data
        column_values = {k: set() for k in headers}
        for item in items:
            for k, v in item.items():
                if v is not None and str(v).strip():
                    val_str = str(v).strip()
                    # Keep values under 100 chars to avoid massive description bloat
                    if len(val_str) < 100:
                        column_values[k].add(val_str)
        
        # Convert sets to lists, limit to top 20 unique values per column to save tokens
        schema_summary = {}
        for k, vals in column_values.items():
            schema_summary[k] = list(vals)[:20]
            
        sample_json = json.dumps(schema_summary)
    extraction_prompt = (
        "You are an intelligent data filter extractor for a university database.\n"
        "Your job is to extract exact search keywords from the user's query that will be used to filter the database.\n"
        "To help you map the user's request to the actual data, here is the schema and a sample row of the data we are searching:\n"
        f"HEADERS: {', '.join(headers)}\n"
        f"SAMPLE ROW: {sample_json}\n\n"
        "RULES:\n"
        "1. Extract ONLY the specific values, nouns, or criteria the user is looking for.\n"
        "2. IMPORTANT: If the user's term is a synonym for a specific value in the sample data, output the EXACT keyword from the data instead (e.g., if user says 'bachelor' and data says 'UG', output 'UG').\n"
        "3. Ignore conversational filler (e.g., 'suggest', 'find', 'which', 'student').\n"
        "4. Ignore category names if they are generic (e.g., 'course', 'program', 'university').\n"
        "5. Ignore analytical dimension words (e.g., 'shortest', 'longest', 'duration', 'cost', 'capacity', 'size') — they are handled elsewhere.\n"
        "6. Ignore status/temporal words (e.g., 'current', 'active', 'latest', 'oldest', 'now') — they are handled elsewhere.\n\n"
        "OUTPUT FORMAT:\n"
        "Output the result as a strict JSON object with a single key 'keywords' containing a list of strings.\n"
        "Example output: {\"keywords\": [\"hons\", \"2y\"]}\n"
        "If no keywords remain, output: {\"keywords\": []}\n"
        "DO NOT output any conversational text, explanations, or markdown code blocks. ONLY output valid JSON."
    )
    try:
        from langchain_core.messages import SystemMessage
        ai_reply = llm.invoke([SystemMessage(content=extraction_prompt), ("human", user_input)])
        extracted_text = ai_reply.content.strip()
        print(f"[STATIC] AI Extracted Keywords Raw: {extracted_text}")
        
        # Robust JSON parsing
        import json as _json
        import re as _re
        try:
            clean_text = _re.sub(r'```(?:json)?\n?(.*?)\n?```', r'\1', extracted_text, flags=_re.DOTALL).strip()
            # Find the first { and last } to strip conversational garbage if any remains
            start_idx = clean_text.find('{')
            end_idx = clean_text.rfind('}')
            if start_idx != -1 and end_idx != -1:
                clean_text = clean_text[start_idx:end_idx+1]
            parsed = _json.loads(clean_text)
            raw_kws = [str(k).lower().strip().replace(".", "").replace("-", "") for k in parsed.get("keywords", [])]
        except _json.JSONDecodeError:
            print(f"[STATIC] JSON parse failed, falling back to comma split.")
            raw_kws = [kw.strip().replace(".", "").replace("-", "") for kw in extracted_text.lower().split(",") if kw.strip()]

        # Backup filter: Smaller LLMs (like Groq fallbacks) sometimes still extract generic nouns despite the prompt.
        ignored_nouns = {"student", "students", "university", "college", "course", "courses", "program", "programs", "degree", "degrees", "detail", "details", "information", "technology", "technologies", "skill", "skills", "knowledge", "expert", "expertise", "field"}
        keywords = [kw for kw in raw_kws if kw not in ignored_nouns]
        # Strip category-label words already handled by target_type filtering above.
        # Also strip multi-word phrases where EVERY word is a type word
        # (e.g. "academic years" → both "academic" and "years" are in _type_words).
        keywords = [
            kw for kw in keywords
            if kw not in _type_words
            and not all(w in _type_words for w in kw.split())
        ]
    except Exception as e:
        print(f"[STATIC] AI Keyword extraction failed: {e}")
        raise e
    
    # Detect negation: "not", "except", "excluding", "without", "other than", "don't", "dont"
    negation_words = {"not", "except", "excluding", "without", "dont", "other"}
    is_negated = bool(set(keywords) & negation_words)
    if is_negated:
        # Remove negation words themselves from the keyword list (they're not data filters)
        keywords = [kw for kw in keywords if kw not in negation_words]
        # Also remove filler words that often accompany negation (e.g., "other than")
        negation_filler = {"than", "related", "belong", "student", "students", "type", "category"}
        keywords = [kw for kw in keywords if kw not in negation_filler]
        if keywords:
            print(f"[STATIC] Negation detected! Will EXCLUDE items matching: {keywords}")

    if keywords:
        # ---- Synonyms Dictionary Expansion ----
        expanded_keyword_groups = []
        try:
            import redis
            import os
            from dotenv import load_dotenv
            load_dotenv('KEY.env')
            # Simple direct Redis connection for the stateless function
            redis_url = os.getenv('REDIS_URL')
            if redis_url:
                r = redis.Redis.from_url(redis_url, decode_responses=True)
                synonyms_data = r.get("university_bot:synonyms")
                synonyms_dict = json.loads(synonyms_data) if synonyms_data else {}
            else:
                print("[STATIC] REDIS_URL not found in KEY.env")
                synonyms_dict = {}
        except Exception as e:
            print(f"[STATIC] Failed to load synonyms from Redis: {e}")
            synonyms_dict = {}

        for kw in keywords:
            # If the keyword exists in the dictionary, use its synonyms. Otherwise, just the keyword itself.
            # We must copy the list so we don't mutate the dictionary cache in memory
            syns = list(synonyms_dict.get(kw, [kw]))
            # Ensure the base keyword is always in the list just in case
            if kw not in syns:
                syns.append(kw)
            
            # Built-in abbreviations logic merged into expansion phase
            BUILTIN_ABBREV_SYNONYMS = {
                "pharmacy":     ["pharm"],
                "engineering":  ["engg", "eng"],
                "science":      ["sci"],
                "commerce":     ["bcom", "mcom"],
                "arts":         ["ba"],
                "management":   ["mba", "bba", "pgpm"],
                "architecture": ["arch"],
                "law":          ["llb", "llm"],
                "nursing":      ["gnm"],
                "agriculture":  ["agri"],
                "design":       ["bdes", "mdes"],
                "computer":     ["bca", "mca", "mcs"],
                "education":    ["bed", "med"],
                "aviation":     ["aviation"],
            }
            if kw in BUILTIN_ABBREV_SYNONYMS:
                for abbrev in BUILTIN_ABBREV_SYNONYMS[kw]:
                    if abbrev not in syns:
                        syns.append(abbrev)
                
            # Normalize each synonym: remove . and - to squash acronyms, replace rest with spaces
            def normalize_text(text):
                t = text.replace(".", "").replace("-", "")
                t = re.sub(r'[^a-z0-9\s]', ' ', t)
                return ' '.join(t.split())
                
            norm_syns = [normalize_text(s) for s in syns]
            expanded_keyword_groups.append(norm_syns)
        
        print(f"[STATIC] Expanded keyword groups for filtering: {expanded_keyword_groups}")

        kw_matched_items = []
        kw_unmatched_items = []
        items_before_kw_filter = list(items)   # snapshot for abbreviation retry
        for item in items:
            # Normalize the JSON string to space-separated words
            item_raw = json.dumps(item).lower()
            item_clean = item_raw.replace(".", "").replace("-", "")
            item_clean = re.sub(r'[^a-z0-9\s]', ' ', item_clean)
            item_str = f" {' '.join(item_clean.split())} "
            
            # For an item to match, it must satisfy EVERY keyword group
            # It satisfies a keyword group if it contains ANY of the synonyms as a discrete word
            is_match = True
            for syn_group in expanded_keyword_groups:
                # match with spaces to ensure word boundary
                if not any(f" {syn} " in item_str for syn in syn_group if syn):
                    is_match = False
                    break
            
            if is_match:
                kw_matched_items.append(item)
            else:
                kw_unmatched_items.append(item)
        
        if is_negated:
            # NEGATION MODE: return items that DON'T match the keywords
            if kw_unmatched_items:
                items = kw_unmatched_items
                print(f"[STATIC] Negation filter: {len(kw_unmatched_items)} items excluded {len(kw_matched_items)} matches.")
            else:
                print(f"[STATIC] Negation filter: ALL items matched — nothing to exclude.")
        else:
            # NORMAL MODE: return items that DO match the keywords
            if kw_matched_items:
                items = kw_matched_items
            else:
                # Keyword matched 0 items using AND logic. Try OR logic.
                kw_matched_items_or = []
                for item in items_before_kw_filter:
                    item_raw = json.dumps(item).lower()
                    item_clean = item_raw.replace(".", "").replace("-", "")
                    item_clean = re.sub(r'[^a-z0-9\s]', ' ', item_clean)
                    item_str = f" {' '.join(item_clean.split())} "
                    
                    is_match_or = False
                    for syn_group in expanded_keyword_groups:
                        if any(f" {syn} " in item_str for syn in syn_group if syn):
                            is_match_or = True
                            break
                    if is_match_or:
                        kw_matched_items_or.append(item)
                
                if kw_matched_items_or:
                    items = kw_matched_items_or
                    used_or_fallback = True
                    print(f"[STATIC] AND filter yielded 0 results. OR fallback matched {len(items)} items for {keywords}.")
                else:
                    print(f"[STATIC] Strict keyword filter yielded 0 results for {keywords}.")
                    items = []

    # 3.5a Status Analytical Filter (current, active, running, latest)
    #     Fixes queries like "which academic year is current?" by checking runingYear==1
    status_current_words = {"current", "active", "running", "runing", "now", "today", "ongoing", "latest", "present"}
    status_oldest_words  = {"oldest", "first", "earliest", "beginning"}
    query_words_set_status = set(re.findall(r'\b[a-z]+\b', query_lower))

    if items and isinstance(items[0], dict):
        if query_words_set_status & status_current_words:
            for flag_col in ["runingYear", "yearforAdmission"]:
                matched = [it for it in items if str(it.get(flag_col, "0")) == "1"]
                if matched:
                    items = matched
                    print(f"[STATIC] Status filter: 'current' → {flag_col}==1 → {len(items)} items")
                    break
            else:
                try:
                    items = [max(items, key=lambda x: int(x.get("sequence", 0)))]
                    print(f"[STATIC] Status filter: 'current' → max(sequence) → {len(items)} items")
                except Exception:
                    pass
        elif query_words_set_status & status_oldest_words:
            try:
                items = [min(items, key=lambda x: int(x.get("sequence", 0)))]
                print(f"[STATIC] Status filter: 'oldest' → min(sequence) → {len(items)} items")
            except Exception:
                pass

    # 3.5 Smart Analytical Filtering (shortest, longest, highest, lowest, etc.)
    #     Detects comparative/superlative queries and filters items to the min/max group.
    #     Runs 100% locally — ZERO API calls.
    if items and isinstance(items[0], dict):
        min_words = {"shortest", "minimum", "least", "smallest", "lowest", "cheapest",
                     "fewest", "min", "fewer", "shorter", "lesser", "less"}
        max_words = {"longest", "maximum", "most", "largest", "highest", "expensive",
                     "top", "greatest", "max", "bigger", "longer", "greater", "more"}

        query_words_set = set(re.findall(r'\b[a-z]+\b', query_lower))
        is_min_query = bool(query_words_set & min_words)
        is_max_query = bool(query_words_set & max_words)

        if is_min_query or is_max_query:
            # Map query context words to actual data column names (case-insensitive substring match)
            column_hints = {
                "duration":  ["duration"],
                "capacity":  ["capacity", "seats", "intake"],
                "cost":      ["cost", "fee", "price", "amount"],
                "year":      ["year"],
            }

            target_col = None
            for hint_keyword, col_patterns in column_hints.items():
                if hint_keyword in query_lower:
                    for k in items[0].keys():
                        if any(p in k.lower() for p in col_patterns):
                            target_col = k
                            break
                    if target_col:
                        break

            # If no column matched from query context, auto-detect the first numeric column
            # (skip id/code fields since those aren't analytically useful)
            if not target_col:
                skip_cols = {"id", "code", "roleid", "holidayid", "academicyearid", "active"}
                for k in items[0].keys():
                    if k.lower() in skip_cols:
                        continue
                    sample_val = items[0].get(k)
                    if sample_val is not None:
                        cleaned = str(sample_val).lower().replace("y", "").replace("yr", "").strip()
                        try:
                            float(cleaned)
                            target_col = k
                            break
                        except (ValueError, TypeError):
                            continue

            if target_col:
                def _extract_num(item, col=target_col):
                    val = item.get(col)
                    if val is None:
                        return None
                    cleaned = str(val).lower().replace("y", "").replace("yr", "").strip()
                    try:
                        return float(cleaned)
                    except (ValueError, TypeError):
                        return None

                valid_pairs = [(item, _extract_num(item)) for item in items]
                valid_pairs = [(item, num) for item, num in valid_pairs if num is not None]

                if valid_pairs:
                    if is_min_query:
                        target_val = min(num for _, num in valid_pairs)
                        direction = "MIN"
                    else:
                        target_val = max(num for _, num in valid_pairs)
                        direction = "MAX"

                    items = [item for item, num in valid_pairs if num == target_val]
                    print(f"[STATIC] Analytical filter: {direction} of '{target_col}' = {target_val} → {len(items)} items")
                    
        # 3.6 Exact Value Analytical Filtering (e.g., "2 years duration", "50 capacity")
        nums = re.findall(r'\b\d+(?:\.\d+)?\b', query_lower)
        if nums and not (is_min_query or is_max_query):
            column_hints = {
                "duration":  ["duration", "year"],
                "capacity":  ["capacity", "seats", "intake"],
                "cost":      ["cost", "fee", "price", "amount"],
            }
            target_col = None
            for hint_keyword, col_patterns in column_hints.items():
                if hint_keyword in query_lower:
                    for k in items[0].keys():
                        if any(p in k.lower() for p in col_patterns):
                            target_col = k
                            break
                    if target_col:
                        break
            
            if target_col:
                try:
                    target_num = float(nums[0])
                    def _extract_num(item, col=target_col):
                        val = item.get(col)
                        if val is None:
                            return None
                        cleaned = str(val).lower().replace("y", "").replace("yr", "").replace("year", "").replace("years", "").strip()
                        try:
                            return float(cleaned)
                        except (ValueError, TypeError):
                            return None
                            
                    matched_items = [it for it in items if _extract_num(it) == target_num]
                    items = matched_items
                    print(f"[STATIC] Exact Numeric Filter: '{target_col}' == {target_num} → {len(items)} items")
                except ValueError:
                    pass

    # 4. Sort the items alphabetically (FAISS similarity order looks random to users)
    if items and isinstance(items[0], dict):
        # Try to sort by Description, Name, or fallback to the very first column
        sort_key = next((k for k in items[0].keys() if 'desc' in k.lower() or 'name' in k.lower()), list(items[0].keys())[0])
        try:
            def _static_sort_val(x, key=sort_key):
                v = x.get(key)
                if v is None:
                    return (1, 0, "")
                try:
                    return (0, int(v), "")
                except (ValueError, TypeError):
                    return (0, 0, str(v).lower())
            items.sort(key=_static_sort_val)
        except Exception:
            pass

    # Build HTML table (purple = local RAG source)
    html_table = ""
    if items:
        data_type  = docs[0].metadata.get("data_type", "Record") if docs else "Record"
        title      = f"LOCAL DATABASE RESULTS ({len(items)} {data_type} Records):"
        html_table = build_html_table(items, title, color="purple")

    # ------------------------------------------------------------------
    # PRESENTATION MODE: skip LLM call, use smart auto-generated summary
    # ------------------------------------------------------------------
    if not items:
        # Return a special marker so the frontend can show the "Search University APIs" button.
        import json as _json
        data_type = docs[0].metadata.get("data_type", "records").lower() if docs else "records"
        marker_payload = _json.dumps({
            "__type__": "NO_DATA_FOUND",
            "data_type": data_type,
            "query": user_input
        })
        print(f"[STATIC] Zero items matched — returning NO_DATA_FOUND marker to frontend.")
        return f"__NO_DATA_FOUND__{marker_payload}"
    elif PRESENTATION_MODE:
        data_type = docs[0].metadata.get("data_type", "records").lower() if docs else "records"
        ai_text   = (f"Found {len(items)} matching {data_type} records in the university database. "
                     f"The complete list is displayed below.")
        print(f"[PRESENTATION] STATIC path: LLM skipped. Template used. ({len(items)} items)")
    else:
        import json as _json
        context = "\n\n".join([_json.dumps(it) for it in items[:8]])
        
        or_fallback_notice = ""
        if used_or_fallback:
            or_fallback_notice = (
                "CRITICAL INSTRUCTION: The system could NOT find any records matching ALL the user's keywords together. "
                "Instead, it used 'OR' logic to find records matching ANY of the keywords. "
                "You MUST explicitly tell the user: 'I couldn't find records matching all your criteria exactly, but here are records matching some of them: <NUMBER> items.'\n"
            )
            
        system_prompt = (
            "You are a University ChatBot assistant for Karnvati University.\n"
            "Answer the user's question in EXACTLY 1-2 sentences using the data below.\n"
            "The system will automatically display a table - do NOT create tables or lists yourself.\n"
            f"IMPORTANT: The system found exactly {len(items)} matching records total. You MUST use this exact number in your response.\n"
            f"{or_fallback_notice}"
            "State only the analytical conclusion (e.g., 'Found <NUMBER> matching records. Full list displayed below.').\n\n"
            f"DATA FROM LOCAL UNIVERSITY DATABASE (showing sample of {min(len(items), 8)} out of {len(items)} records):\n{context}"
        )
        try:
            ai_reply = llm.invoke([SystemMessage(content=system_prompt), ("human", user_input)])
            ai_text  = ai_reply.content
        except Exception as e:
            print(f"[STATIC] LLM summary generation failed: {e}")
            raise e

    full_response = f"{ai_text}"
    if html_table:
        full_response += f" {html_table}"

    return full_response


# ==============================================================================
# 4. INTENT GATEWAY - 3-WAY CLASSIFIER
# ==============================================================================
def classify_intent_gateway(user_input: str, llm) -> str:
    """
    3-Way Intent Classifier using the provided LLM.
    Returns: 'CHAT', 'STATIC', or 'LIVE'
    """
    from rag_registry import build_static_intent_description
    prompt = f"""Classify the user's university chatbot message into EXACTLY ONE of three categories:

CHAT   - General chit-chat, greetings, personal or emotional messages (e.g., "hi", "how are you", "thanks")
STATIC - Questions about data that rarely changes (e.g. {build_static_intent_description()}). Also includes ALL analytical or comparative questions (shortest, longest, highest, lowest, min, max, duration, cost, best, suggest).
LIVE   - Questions about real-time, personal, or transactional data: attendance, timetable, exam schedule, exam results, fee payments, individual student grades, admission application status

Reply with ONE WORD ONLY: CHAT, STATIC, or LIVE."""

    response = llm.invoke([SystemMessage(content=prompt), ("human", user_input)])
    intent   = response.content.strip().upper()
    print(f"[INTENT] '{user_input[:50]}' -> {intent}")

    if "CHAT"   in intent: return "CHAT"
    if "STATIC" in intent: return "STATIC"
    if "LIVE"   in intent: return "LIVE"
    return "LIVE"   # Safe fallback


# ==============================================================================
# 5. MAIN AI RESPONSE FUNCTION
# ==============================================================================
def get_ai_response(user_input: str, history_dict: list, redis_client=None, is_warmer=False, mode="auto") -> str:
    global CURRENT_KEY_INDEX, CURRENT_GROQ_KEY_INDEX

    def execute_pipeline(current_llm) -> str:
        nonlocal user_input
        
        # --- Contextual Query Rewriting (Token Efficient) ---
        # Only trigger rewrite if we detect a reference to previous chat context
        follow_up_pattern = r'\b(these|those|them|last|earlier|fetched|above|show me|tell me|what about|theses|this)\b'
        if history_dict and len(history_dict) >= 2 and re.search(follow_up_pattern, user_input.lower()):
            print(f"[MEMORY] Follow-up keyword detected. Rewriting query using last 2 history items...")
            recent_history = history_dict[-2:]

            def _strip_html(text: str) -> str:
                """Remove HTML tags and collapse whitespace for clean history context."""
                import re as _re
                text = _re.sub(r'<[^>]+>', ' ', text)
                text = _re.sub(r'\s+', ' ', text).strip()
                return text

            history_str = "\n".join([
                f"{msg['role'].capitalize()}: {_strip_html(msg['content'])[:150]}"
                for msg in recent_history
            ])
            
            rewrite_prompt = (
                "You are an internal system tool. The user sent a follow-up query with a pronoun like 'these' or 'those' referring to the results of their previous query.\n"
                "Rewrite the follow-up query to be completely self-contained by including ALL the specific constraints, filters, and subjects from the user's previous queries in the history.\n"
                "For example, if history was 'show me MTech courses' and follow up is 'which of these are hons', rewrite to 'which MTech courses are hons'.\n"
                "Do NOT just use generic categories like 'the courses'. You MUST include the specific adjectives/filters (e.g. 'MTech').\n\n"
                f"HISTORY:\n{history_str}\n\n"
                f"NEW FOLLOW-UP QUERY: {user_input}\n"
                "OUTPUT ONLY THE REWRITTEN QUERY:"
            )
            try:
                rewrite_response = current_llm.invoke([SystemMessage(content=rewrite_prompt), ("human", user_input)])
                user_input = rewrite_response.content.strip()
                print(f"[MEMORY] Rewritten query: '{user_input}'")
            except Exception as e:
                print(f"[MEMORY] Rewrite failed, proceeding with original query. Error: {e}")

        # Step 1: Intent Classification or Forced Mode
        if mode == "bot":
            intent = "CHAT"
        elif mode == "data":
            intent = "STATIC"
        else:
            intent = classify_intent_gateway(user_input, current_llm)

        # Path A: CHAT
        if intent == "CHAT":
            # PRESENTATION MODE: use instant template for common greetings (0 LLM calls)
            if PRESENTATION_MODE:
                greetings = ["hi", "hello", "hey", "good", "thanks", "thank", "how are", "what can", "who are"]
                if any(g in user_input.lower() for g in greetings):
                    print("[PRESENTATION] CHAT path: Template reply used (no LLM call).")
                    return ("Hello! I'm your University ChatBot for Karnvati University, powered by AI. \n\n"
                            "I can help you explore courses, roles, academic years, and all university data. "
                            "What would you like to know?")

            print("[CHAT] PATH: Direct conversational reply.")
            chat_prompt = ("You are a University ChatBot for Karnvati University. "
                           "Respond conversationally and politely. Do NOT try to fetch API data.")
            response = current_llm.invoke([SystemMessage(content=chat_prompt), ("human", user_input)])
            return response.content

        # Log analytics for all DATA queries (STATIC + LIVE)
        track_query(redis_client, user_input)

        # Path B: STATIC - RAG Store 2 (no live API call)
        if intent == "STATIC":
            print("[RAG] STATIC PATH: Querying RAG Store 2 (no API call needed).")
            return get_static_response(user_input, current_llm)

        # Path C: LIVE - Agentic Tool Calling
        print("[API] LIVE PATH: Waking up University ChatBot LangChain Agent.")
        tools = [search_api_documentation, execute_api_request]

        system_instructions = """You are a University ChatBot for Karnvati University - an elite Data Architect and Analytical Engine.
Your core directive is to process user queries, route them to the correct data sources, and provide hyper-concise analytical summaries.
You operate under the following STRICT protocols:

PROTOCOL 1: TOOL EXECUTION (NO HALLUCINATIONS)
1. NEVER guess or invent API endpoints.
2. Step 1: ALWAYS use 'search_api_documentation' first with a 1-2 word keyword to find the exact endpoint.
3. Step 2: Use 'execute_api_request' to fetch the data.
4. SMART FILTERING: Pass highly specific Regex pipe-separated strings to 'search_filter' to minimize payload size.

PROTOCOL 2: ANALYTICAL CACHE AWARENESS
1. Read the [SYSTEM METADATA] block carefully.
2. Analyze ONLY the compressed data context to answer the user's specific analytical question.

PROTOCOL 3: OUTPUT FORMATTING (CRITICAL)
1. The backend system automatically attaches a beautiful UI data table.
2. YOUR FINAL ANSWER MUST BE EXACTLY 1 TO 2 SENTENCES LONG.
3. NEVER generate markdown tables, lists, or raw JSON in your final response.
4. Just state the analytical conclusion and STOP."""

        prompt = ChatPromptTemplate.from_messages([
            SystemMessage(content=system_instructions),
            ("human", "{input}"),
            MessagesPlaceholder(variable_name="agent_scratchpad"),
        ])

        agent    = create_tool_calling_agent(current_llm, tools, prompt)
        executor = AgentExecutor(agent=agent, tools=tools, verbose=True)
        response = executor.invoke({"input": user_input})

        ai_response = response["output"]
        if isinstance(ai_response, list):
            ai_response = "".join(
                chunk.get("text", "") if isinstance(chunk, dict) else str(chunk)
                for chunk in ai_response
            )

        html_box = temp_full_data_var.get()
        temp_full_data_var.set(None)

        if html_box:
            ai_response = f"{ai_response}\n\n{html_box}"
        else:
            # We didn't get a data table back (tool failed or 0 records found).
            # Inject a hidden marker so the server knows NOT to cache this empty response.
            ai_response += "<!-- __DO_NOT_CACHE__ -->"

        return ai_response

    # ==========================================
    # LLM Initialization & Fallback Controller
    # ==========================================
    def get_groq_llm():
        global CURRENT_GROQ_KEY_INDEX
        keys_string = os.getenv("GROQ_API_KEYS")
        if not keys_string: return None
        api_keys = [k.strip() for k in keys_string.split(",") if k.strip()]
        with groq_key_lock:
            selected_key = api_keys[CURRENT_GROQ_KEY_INDEX % len(api_keys)]
            CURRENT_GROQ_KEY_INDEX = (CURRENT_GROQ_KEY_INDEX + 1) % len(api_keys)
        key_num = ((CURRENT_GROQ_KEY_INDEX - 1) % len(api_keys)) + 1
        print(f"[API] Using Groq Key #{key_num} for Fallback/Warmer")
        return ChatGroq(model="llama-3.1-8b-instant", temperature=0, groq_api_key=selected_key)

    if is_warmer:
        # --- GROQ BACKUP FOR CACHE WARMER ---
        try:
            llm = get_groq_llm()
            if not llm: return "CRITICAL ERROR: GROQ_API_KEYS not found in KEY.env for cache warmer."
            return execute_pipeline(llm)
        except Exception as e:
            return f"SYSTEM ERROR: Cache warmer pipeline failed: {str(e)}"
    else:
        # --- NVIDIA NIM FOR LIVE USERS (WITH GROQ FALLBACK) ---
        keys_string = os.getenv("NVIDIA_API_KEY") or os.getenv("KEY")
        if not keys_string: return "CRITICAL ERROR: NVIDIA_API_KEY not found in KEY.env."
        
        api_keys = [k.strip() for k in keys_string.split(",") if k.strip()]
        with key_lock:
            selected_key      = api_keys[CURRENT_KEY_INDEX % len(api_keys)]
            CURRENT_KEY_INDEX = (CURRENT_KEY_INDEX + 1) % len(api_keys)
            
        key_num = ((CURRENT_KEY_INDEX - 1) % len(api_keys)) + 1
        print(f"[KEY] Live User -> Using NVIDIA NIM Key #{key_num}")
        nvidia_llm = ChatOpenAI(
            model="nvidia/nemotron-3-ultra-550b-a55b", 
            temperature=0, 
            api_key=selected_key, 
            base_url="https://integrate.api.nvidia.com/v1",
            timeout=15.0,
            max_retries=0
        )
        
        try:
            # 1. Attempt the entire pipeline with NVIDIA NIM
            return execute_pipeline(nvidia_llm)
        except Exception as e:
            print(f"\n⚠️ [FAILOVER] NVIDIA API failed with error: {str(e)}")
            print(f"🔄 [FAILOVER] Falling back to Groq API for Live User...\n")
            try:
                # 2. If NVIDIA throws a 500/429/Exception, fallback to Groq and retry
                groq_llm = get_groq_llm()
                if not groq_llm: 
                    return f"CRITICAL ERROR: NVIDIA Failed and GROQ_API_KEYS not found for fallback. (NVIDIA Error: {str(e)})"
                return execute_pipeline(groq_llm)
            except Exception as e2:
                return f"SYSTEM ERROR: Both NVIDIA and Groq failover failed. (NVIDIA: {str(e)} | Groq: {str(e2)})"


# ==============================================================================
# 6. HYBRID SEMANTIC ROUTER (Cache key logic)
# ==============================================================================
def get_semantic_endpoint(query: str) -> str:
    query_lower = query.lower()

    if "?" in query_lower:
        return None

    generic_words = {
        'give', 'me', 'all', 'the', 'list', 'show', 'get', 'fetch', 
        'university', 'of', 'for', 'any', 'some', 'is', 'are', 'there',
        'what', 'which', 'detail', 'details', 'record', 'records', 'info',
        'data', 'about', 'i', 'want', 'to', 'know', 'please', 'can', 'you',
        'display', 'search', 'find', 'bring', 'provide', 'a', 'an'
    }

    trigger_words = {
        "/api/Course": {"course", "courses", "cource", "cources", "class", "classes"},
        "/api/Holiday": {"holiday", "holidays", "vacation", "calendar"},
        "/api/RoleMaster": {"role", "roles", "rolemaster"},
        "/api/AcademicYear": {"academicyear", "academic", "year", "years"},
        "/api/HostelMaster": {"hostel", "hostels", "hostelmaster"}
    }

    query_words = set(re.findall(r'\b[a-z0-9\-]+\b', query_lower))

    matched_endpoint = None
    all_allowed_words = set(generic_words)

    for endpoint, t_words in trigger_words.items():
        if query_words & t_words:
            if not matched_endpoint:
                matched_endpoint = endpoint
            all_allowed_words.update(t_words)

    if not matched_endpoint:
        return None

    extra_words = query_words - all_allowed_words
    if extra_words:
        print(f"[SMART] Extra specific words detected {extra_words} -> bypassing Master Cache!")
        return None

    return matched_endpoint
