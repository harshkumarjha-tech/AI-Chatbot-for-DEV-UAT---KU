# 🏗️ Karnvati University AI Chatbot — Complete System Documentation

## Project Overview

This is a **RAG-based (Retrieval-Augmented Generation) AI chatbot** for **Karnvati University**, built with **FastAPI + LangChain + FAISS + Redis**. It answers questions about university data (courses, roles, academic years, hostels, etc.) using a combination of local vector search, live API calls, and AI-powered analysis — powered by **NVIDIA NIM** (primary) with **Groq/Llama** (fallback).

**Key URLs:**
- **Chat UI:** `http://localhost:8000`
- **University API:** `http://kuriseapi.karnavatiuniversity.edu.in`

---

## Architecture Diagram

```
                         ┌──────────────────────────────┐
                         │    web_user_interface.html    │
                         │      (Browser Chat UI)       │
                         └──────────────┬───────────────┘
                                        │ POST /api/chat
                         ┌──────────────▼───────────────┐
                         │   enterprise_fastapi_server   │
                         │         (FastAPI)             │
                         └──────────────┬───────────────┘
                                        │
                    ┌───────────────────┬┴──────────────────┐
                    ▼                   ▼                    ▼
           ┌──────────────┐   ┌──────────────────┐  ┌──────────────┐
           │ Redis Cache  │   │ AI Pipeline       │  │ Discovery    │
           │ (Upstash)    │   │ (langchain_core)  │  │ Engine       │
           └──────────────┘   └────────┬─────────┘  └──────────────┘
                                       │
                          ┌────────────┼────────────┐
                          ▼            ▼            ▼
                   ┌──────────┐ ┌──────────┐ ┌──────────────┐
                   │  CHAT    │ │  STATIC  │ │    LIVE      │
                   │  Path    │ │  Path    │ │    Path      │
                   └──────────┘ └──────────┘ └──────────────┘
                        │            │              │
                        ▼            ▼              ▼
                   ┌──────────┐ ┌──────────┐ ┌──────────────┐
                   │ LLM      │ │RAG Store │ │ RAG Store 1  │
                   │ (Direct) │ │2 (FAISS) │ │ + Live API   │
                   └──────────┘ └──────────┘ └──────────────┘
                                     │
                          ┌──────────┼──────────┐
                          ▼          ▼          ▼
                   ┌───────────┐ ┌────────┐ ┌────────┐
                   │ Keyword   │ │Status  │ │Analyt. │
                   │ Filter    │ │Filter  │ │Filter  │
                   └───────────┘ └────────┘ └────────┘

     LLM Providers:
     ┌──────────────────────┐  ┌──────────────────────┐
     │ NVIDIA NIM           │  │ Groq (Fallback)      │
     │ Nemotron 3 Ultra     │  │ Llama 3.1 8B         │
     │ 🔑 N keys (rotation) │  │ 🔑 2 keys (rotation) │
     └──────────────────────┘  └──────────────────────┘
```

---

## File Structure & Responsibilities

| File | Lines | Role |
|------|-------|------|
| `enterprise_fastapi_server.py` | ~579 | FastAPI server: all HTTP endpoints, Redis cache layer, discovery engine, nightly scheduler, synonym management |
| `langchain_core_free_tier_rag.py` | ~1025 | **Core AI brain**: intent classification, 3 execution paths (CHAT/STATIC/LIVE), keyword/status/analytical filtering, LLM calls, agent tools, deduplication |
| `data_rag_builder.py` | ~182 | RAG Store 2 builder: fetches data from university API, converts to documents, nightly refresh job, Redis analytics |
| `rag_registry.py` | ~212 | Self-updating data type registry: auto-generates keywords, regex patterns, and intent descriptions for new data types |
| `rag_registry.json` | ~50 | Persistent JSON storage for registered data types and their keywords |
| `web_user_interface.html` | ~52K | Chat UI frontend: dark theme, sidebar, quick questions, admin panel, discovery flow |
| `demo_cache_warmer.py` | ~120 | Pre-warms Redis cache using Groq (free) before demos |
| `KEY.env` | ~15 | API keys: NVIDIA NIM, Groq, Redis URL, Swagger token |

---

## Complete Request Lifecycle

### Phase 0: Server Startup

When `python -m uvicorn enterprise_fastapi_server:app --host 0.0.0.0 --port 8000` runs:

| Step | What Happens | Cost |
|------|-------------|------|
| 1 | Connects to **Cloud Redis** (Upstash) for caching + analytics | Free |
| 2 | **RAG Store 1** — Loads FAISS index of Swagger API docs (GET endpoints from `/swagger/v1/swagger.json`) | Free (local HuggingFace `all-MiniLM-L6-v2` embeddings) |
| 3 | **RAG Store 2** — Loads FAISS index of university data (courses, roles, academic years, hostels) | Free (local embeddings) |
| 4 | Arms **APScheduler** nightly refresh (midnight IST) | Free |
| 5 | Seeds `rag_registry.json` if first run (Course, Role, AcademicYear, HostelMaster) | Free |

**Static Data Endpoints (Nightly Refresh):**
- `/api/Course` → 140 records
- `/api/RoleMaster` → 14 records
- `/api/AcademicYear` → 11 records
- `/api/HostelMaster` → 16 records

---

### Phase 1: User Sends a Message

User types in the chat UI → `POST /api/chat` with `{message, history, is_warmer}`

### Phase 2: Hybrid Cache Check

The server uses a **2-tier caching strategy**:

| Cache Type | Key Format | When Used | Example |
|------------|-----------|-----------|---------|
| **Master Shared Cache** | `chat_cache:endpoint:/api/Course` | Generic queries without adjectives/filters | "show all courses", "list roles" |
| **Exact Hash Cache** | `chat_cache:exact_hash:a1b2c3...` | Specific/analytical queries | "which BBA course has shortest duration?" |

**Smart routing logic** (`get_semantic_endpoint`):
1. If the query contains adjectives (e.g., "pharmacy courses", "BBA courses") → bypasses Master Cache → gets its own unique hash
2. If the query is generic (e.g., "all courses") → uses Master Cache (shared across all users)
3. If cache **HIT** → instant response, **ZERO LLM calls**
4. If cache **MISS** → runs the full AI pipeline below

**Cache TTL:** 86400 seconds (24 hours)

**Never cached:** Error responses, empty results, `__NO_DATA_FOUND__` markers, `__DO_NOT_CACHE__` markers

---

### Phase 3: AI Pipeline (on Cache Miss)

#### Step 3.0 — LLM Selection & Key Rotation

| Scenario | LLM Used | Details |
|----------|----------|--------|
| Live user request | NVIDIA NIM (Nemotron 3 Ultra 550B) | N keys, round-robin rotation |
| Cache warmer (`is_warmer=true`) | Groq (Llama 3.1 8B) | 2 keys, round-robin |
| NVIDIA fails (500/429) | Auto-failover → Groq | Same 2 keys |

#### Step 3.1 — Contextual Query Rewriting (Conditional)

**Triggers only** when the user uses follow-up words like `"these"`, `"those"`, `"them"`, `"above"`, `"fetched"`, `"show me"`, `"tell me"`, `"what about"` AND there is chat history (≥2 messages).

```
Input:  "show me more about these"
Output: "show me more about B.Tech engineering courses"
```

Uses last 2 history items (HTML-stripped, truncated to 150 chars each) as context.

**Cost:** 0 tokens if no follow-up detected; ~100-200 tokens if triggered.

#### Step 3.2 — 3-Way Intent Classification

Uses the LLM to classify the query as exactly one of:

| Intent | Meaning | Examples |
|--------|---------|---------|
| **CHAT** | Greetings, chit-chat, personal | "hi", "how are you", "thanks" |
| **STATIC** | Rarely-changing data, analytical queries | "show all courses", "shortest duration", "current academic year" |
| **LIVE** | Real-time, personal, transactional | "my attendance", "exam schedule", "fee payment status" |

The STATIC intent description is **dynamically built** from the `rag_registry.json` — so any newly-ingested data type automatically becomes part of the classifier's knowledge.

**Cost:** ~50-100 tokens (small prompt + 1-word response)

---

### Phase 4: Three Execution Paths

#### Path A: CHAT (Conversational Reply)

**PRESENTATION_MODE optimization:** Common greetings return a **hardcoded template** → 0 LLM calls.

Normal mode: Sends a system prompt ("You are a University ChatBot...") + user message to LLM.

**Cost:** 0 tokens (template) or ~100-300 tokens (LLM reply)

---

#### Path B: STATIC (Local RAG Search — No Live API Call)

This is the most complex path with multiple filtering stages:

##### Stage 1: FAISS Similarity Search
- Queries `VECTOR_STORE_DATA` for top-200 similar documents
- Each document has metadata: `{source_endpoint, data_type, raw_json}`

##### Stage 2: Target Type Detection (3-tier)
1. **Regex matching** — uses `build_target_type_regex_map()` from `rag_registry.py` to match registered keywords (e.g., `\b(course|courses|cource|cources)\b`)
2. **Fuzzy matching** — if regex fails, uses `difflib.get_close_matches` against all registry keywords (cutoff=0.8)
3. **FAISS fallback** — if both fail, uses the `data_type` from the top semantic match

##### Stage 3: Type-Based Filtering
- Keeps only documents matching the detected `target_type` (e.g., only "Course" docs)
- Extracts raw JSON items from document metadata

##### Stage 4: AI Keyword Extraction
- LLM extracts core subject nouns from the query (ignoring verbs, adjectives, filler, analytical words, status words)
- Example: "show me all pharmacy courses with shortest duration" → keywords: `["pharmacy"]`
- Strips category-label words already handled by target_type (prevents double-filtering)

##### Stage 5: Synonym Expansion
- Loads user-defined synonyms from Redis (`university_bot:synonyms`)
- Each keyword is expanded to include all registered synonyms
- Example: if "bca" has synonyms ["bachelor of computer applications"], both are searched

##### Stage 6: Keyword Filtering
- For each item, normalizes the JSON to space-separated lowercase words
- Checks if ALL expanded keyword groups match within the item text
- Supports **negation** ("courses NOT related to commerce" → excludes matches)

##### Stage 6b: Abbreviation Retry
- If keyword filter returns 0 results, retries with built-in abbreviation synonyms:
  - `pharmacy → pharm`
  - `engineering → engg, eng`
  - `commerce → bcom, mcom`
  - `computer → bca, mca, mcs`
  - (14 abbreviation groups total)

##### Stage 7: Status Analytical Filter
- Detects `current`, `active`, `running`, `latest`, `oldest`, `first`, `earliest` in query
- For "current" → filters by `runingYear==1` or `yearforAdmission==1`, or fallback to `max(sequence)`
- For "oldest" → filters by `min(sequence)`

##### Stage 8: Smart Analytical Filter (min/max)
- Detects comparative/superlative words: `shortest`, `longest`, `highest`, `lowest`, `most`, `least`, etc.
- Maps query context to data columns: `duration → durationofYears`, `capacity → capacity`, `cost → fee`
- Auto-detects numeric columns if no hint word matched
- Filters items to the min/max group

##### Stage 9: Sort & Build Response
- Sorts items alphabetically (by description/name)
- Builds styled HTML table with scrollable container
- **PRESENTATION_MODE:** Returns template summary (0 LLM calls)
- **Normal mode:** LLM generates 1-2 sentence summary from sample data
- **Zero results:** Returns `__NO_DATA_FOUND__` marker → frontend shows "Search University APIs" button

**Cost:** ~250-600 tokens (1 for keyword extraction + 1 for summary, if not in presentation mode)

---

#### Path C: LIVE (Agentic Tool-Calling)

Uses a **LangChain AgentExecutor** with two tools:

| Tool | What It Does | Cost |
|------|-------------|------|
| `search_api_documentation` | Searches RAG Store 1 (FAISS Swagger docs) for the right endpoint | Free (local) |
| `execute_api_request` | Makes live HTTP GET to university API, filters results, builds HTML table | Free (HTTP) |

The Agent iterates: Think → Call Tool → Observe → Think → Call Tool → Final Answer

Both tools have a **3-second throttle delay** to protect free-tier API rate limits.

`execute_api_request` includes:
- Mock data endpoints (for re-evaluation, scholarship, hostel inventory)
- Live API calls to university server
- Search filter support (regex pipe-separated strings)
- Smart data minimization: sends only 3 primary columns to LLM (saves tokens)
- Full HTML table stored in `temp_full_data_var` (ContextVar) for the response

**Cost:** ~800-2000+ tokens (multiple agent iterations)

---

## Discovery Engine

When the STATIC path returns "No data found", the frontend shows a **"🔍 Search University APIs"** button. Clicking it triggers the **Discovery Engine**:

### Flow:
1. Frontend calls `POST /api/discover` with `{query, excluded_endpoints}`
2. Server scores all 87 endpoints in `DISCOVERY_CATALOGUE` using fuzzy word matching (F1-score + SequenceMatcher)
3. Tries up to 5 best-matching endpoints:
   - Skips 401/403 unauthorized endpoints
   - Skips endpoints returning empty data
   - Returns first successful result with data
4. Frontend shows the API response + "✅ Correct!" / "❌ Wrong" buttons
5. If user clicks "✅ Correct!":
   - Frontend calls `POST /api/ingest-to-rag` with the API data
   - Server creates Document objects with `sort_keys=True` JSON
   - **Deduplication:** normalizes JSON with sorted keys, compares against all existing documents in FAISS docstore — only ingests truly new records
   - Registers the endpoint in `rag_registry.json` for future intent classification
   - Adds endpoint to `STATIC_ENDPOINTS` for nightly refresh
6. If user clicks "❌ Wrong" → tries the next best endpoint

### Discovery Catalogue
- 87 university API GET endpoints
- Each entry: `{label, endpoint, keywords}`
- Covers: academics, admissions, attendance, examinations, finance, hostels, library, placements, research, student services, transport, and more

---

## Admin Panel Features

The sidebar includes an **Admin Panel** section with:

| Feature | Endpoint | What It Does |
|---------|----------|-------------|
| **Trigger Nightly Refresh** | `POST /api/trigger-nightly-refresh` | Manually wipes and rebuilds RAG Store 2 from all `STATIC_ENDPOINTS`, prints analytics summary |
| **View Search Analytics** | `GET /api/analytics` | Shows top 20 searched queries and top 10 hit endpoints from Redis |
| **Manage Synonyms** | `GET/POST /api/synonyms` | Read/write custom synonym dictionary in Redis (e.g., "mtech" → ["m.tech", "master of technology"]) |
| **Wipe Redis Cache** | `POST /api/clear-cache` | Clears all `chat_cache:*` keys (preserves synonyms and analytics) |
| **Wipe RAG Database** | `POST /api/wipe-rag` | Deletes both FAISS directories (`db_faiss` and `db_faiss_data`) |

---

## RAG Registry System

The `rag_registry.py` module is a **self-updating configuration system** that keeps the bot's intent classifier, keyword detector, and stop words synchronized with the data types stored in RAG Store 2.

### How It Works:
1. **Seed data:** On first run, seeds with Course, Role, AcademicYear, HostelMaster
2. **Auto-registration:** When user confirms discovery data (via "✅ Correct!"), `register_data_type()` is called
3. **Smart keyword generation:** CamelCase label → split → pluralize → filter noise suffixes
   - `"HostelMaster"` → `["hostel", "hostels", "hostelmaster"]`
   - `"FeeStructure"` → `["fee", "fees", "structure", "structures", "feestructure"]`
4. **Dynamic outputs:**
   - `build_target_type_regex_map()` → regex patterns for target_type detection
   - `build_stop_words_set()` → combined stop words to prevent keyword filter confusion
   - `build_static_intent_description()` → human-readable description for LLM intent classifier

### Persistence:
- Stored in `rag_registry.json` on disk
- Thread-safe read/write with `threading.Lock`

---

## Nightly Refresh Job

Runs automatically at **midnight IST** via APScheduler, or manually via admin panel.

### Steps:
1. Prints top 20 searched queries and top 10 hit endpoints (from Redis analytics)
2. Calls `build_or_refresh_data_rag(force_refresh=True)` which:
   - Fetches fresh data from all `STATIC_ENDPOINTS` via HTTP GET
   - Converts each record to a `Document` with text + metadata (including `sort_keys=True` JSON)
   - Builds a **brand new** FAISS index (complete wipe + rebuild)
   - Saves to disk at `vectorstore/db_faiss_data`
3. Calls `reload_data_rag()` to hot-swap the in-memory FAISS store

**Important:** The nightly refresh does a **clean rebuild** (not append), so it naturally eliminates any duplicates that accumulated during the day.

---

## Deduplication System

The `ingest_new_documents()` function prevents duplicate data when users save discovery results:

### How It Works:
1. Every document stores its raw API data as `raw_json` in metadata
2. Both the nightly refresh builder and the discovery ingest use `json.dumps(item, sort_keys=True, ensure_ascii=False)` — ensuring identical JSON regardless of key ordering
3. Before adding new documents, the function:
   - Builds a set of normalized JSON strings from all existing documents in `VECTOR_STORE_DATA.docstore._dict`
   - Checks each incoming document against the set
   - Only adds documents whose JSON doesn't already exist
   - Also prevents intra-batch duplicates

### Example Log Output:
```
[OK] Skipped 140 documents during ingest (all were duplicates already in RAG).
```
or
```
[OK] Ingested 5 NEW documents into RAG Store 2. (Skipped 135 duplicates)
```

---

## Token-Saving Mechanisms

| Mechanism | Location | How It Saves |
|-----------|----------|-------------|
| **Redis Caching** | `enterprise_fastapi_server.py` | Repeat queries served from cache — 0 LLM calls |
| **Master Shared Cache** | `get_semantic_endpoint()` | Generic queries share one cached response |
| **PRESENTATION_MODE** | `langchain_core_free_tier_rag.py` | Skips LLM for STATIC summaries + CHAT greetings |
| **Demo Cache Warmer** | `demo_cache_warmer.py` | Pre-warms Redis using Groq (free) instead of NVIDIA |
| **Key Round-Robin** | `get_ai_response()` | Distributes load across multiple NVIDIA/Groq keys |
| **NVIDIA → Groq Failover** | `get_ai_response()` | Auto-switches to free Groq on NVIDIA rate limit |
| **Conditional Query Rewrite** | `execute_pipeline()` | Only rewrites on follow-up queries |
| **Data Minimization** | `execute_api_request()` | Sends only 3 primary columns to LLM context |
| **3s Throttle Delays** | Tool functions | Protects free-tier rate limits |
| **Nightly Refresh** | `data_rag_builder.py` | Keeps STATIC data current without live API calls |

---

## Frontend Features (web_user_interface.html)

### Chat Interface:
- Dark theme with glassmorphism effects
- Message bubbles with timestamps
- Typing indicator animation
- Response time badges (e.g., "📦 RAG • 17.85s")
- Scrollable data tables with sticky headers
- Support for follow-up questions

### Sidebar:
- **System Status:** RAG Store 1/2 status, Redis Cache, Nightly Scheduler
- **Quick Questions:** Pre-configured common queries (MTech courses, University roles, Academic years, etc.)
- **Admin Panel:** Trigger refresh, view analytics, manage synonyms, wipe cache/RAG

### Discovery Flow (Frontend):
1. When STATIC returns "no data found" → parses `__NO_DATA_FOUND__` marker
2. Shows "🔍 Search University APIs" button
3. Calls `/api/discover` endpoint iteratively
4. Shows results with "✅ Correct! Save to AI Knowledge Base" / "❌ Wrong, try another API" buttons
5. On save → calls `/api/ingest-to-rag` → shows success message
6. Auto-retries the original question with newly ingested data

---

## Mock Data Endpoints

Three endpoints return hardcoded mock data for demo/testing:

| Endpoint | Data |
|----------|------|
| `GET /api/ReEvaluationReTotalingDet` | 1 test student re-evaluation record |
| `GET /api/ScholarshipSetupHeader` | 2 scholarship records (Merit + Sports) |
| `GET /api/HostelInventoryAllocation` | 1 hostel allocation record |

These are also available as mock data in the `execute_api_request` tool and the discovery engine.

---

## Environment Variables (KEY.env)

| Variable | Purpose |
|----------|---------|
| `NVIDIA_API_KEY` or `KEY` | Comma-separated NVIDIA NIM API keys |
| `GROQ_API_KEYS` | Comma-separated Groq API keys |
| `REDIS_URL` | Upstash Redis connection URL |
| `PRESENTATION_MODE` | `true`/`false` — enables token-saving demo mode |

---

## How to Run

```bash
# Start the server
python -m uvicorn enterprise_fastapi_server:app --host 0.0.0.0 --port 8000

# Or with kill-existing-process safety:
netstat -ano | findstr ":8000" | Select-String "LISTENING" | ForEach-Object { ($_ -split '\s+')[-1] } | ForEach-Object { Stop-Process -Id $_ -Force }; python -m uvicorn enterprise_fastapi_server:app --host 0.0.0.0 --port 8000
```

Open `http://localhost:8000` in your browser.

---

## Known Limitations

1. **RAG for structured data** — Storing tabular API data in FAISS (vector search) is not optimal for large datasets. Industry standard is Text-to-SQL. See `implementation_plan.txt` for the upgrade path.
2. **No authentication** — The bot doesn't authenticate with the university API for protected endpoints (401/403 are skipped).
3. **Single-tenant** — Currently hardcoded for Karnvati University. Multi-tenant architecture planned for future versions.
4. **No persistent chat history** — Chat history exists only in the browser session (not saved to DB).
