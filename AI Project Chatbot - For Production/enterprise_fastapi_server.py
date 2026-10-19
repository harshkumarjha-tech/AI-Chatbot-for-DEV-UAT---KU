import uvicorn
import hashlib
import os
import sys
import re
import redis
import asyncio
import json
import threading
import requests
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from typing import List, Dict
from datetime import datetime as dt
from dotenv import load_dotenv
from apscheduler.schedulers.background import BackgroundScheduler

from langchain_core_free_tier_rag import (
    get_ai_response,
    initialize_system,
    initialize_data_rag,
    reload_data_rag,
    get_semantic_endpoint,
    ingest_new_documents,
)
from data_rag_builder import nightly_refresh_job, get_analytics_summary

load_dotenv("KEY.env")

# Fix Windows terminal encoding (cp1252 can't print emojis)
sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# REDIS CONNECTION
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
REDIS_URL    = os.getenv("REDIS_URL")
redis_client = None

if REDIS_URL:
    redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    try:
        redis_client.ping()
        print("âœ… SUCCESS: Connected to Cloud Redis Database!")
    except Exception as e:
        print(f"âš ï¸  Redis connection failed: {e}")
        redis_client = None
else:
    print("âš ï¸  REDIS_URL not found in KEY.env.")


# ═══════════════════════════════════════════════════════════════════════════════
# NIGHTLY REFRESH STATE & CONCURRENCY LOCK
# ═══════════════════════════════════════════════════════════════════════════════
class RefreshState:
    """Thread-safe singleton that prevents concurrent nightly refresh runs."""
    def __init__(self):
        self._lock       = threading.Lock()
        self.is_running  = False
        self.progress    = ""       # e.g. "Fetching Course... (2/4)"
        self.step        = 0
        self.total_steps = 0
        self.last_result = None     # "success" | "failed" | "no_data" | None
        self.last_run_at = None     # ISO timestamp of last completed run
        self.started_at  = None     # ISO timestamp of current/last start

    def try_start(self) -> bool:
        """Attempt to acquire the lock. Returns True if acquired (no other run active)."""
        acquired = self._lock.acquire(blocking=False)
        if acquired:
            self.is_running  = True
            self.progress    = "Starting..."
            self.step        = 0
            self.total_steps = 0
            self.last_result = None
            self.started_at  = dt.now().isoformat()
        return acquired

    def update(self, message: str, step: int, total: int):
        """Called by the data layer to report progress."""
        self.progress    = message
        self.step        = step
        self.total_steps = total

    def finish(self, result: str):
        """Mark the run as complete and release the lock."""
        self.is_running  = False
        self.last_result = result
        self.last_run_at = dt.now().isoformat()
        self.progress    = ""
        try:
            self._lock.release()
        except RuntimeError:
            pass   # lock was not held (safety)

    def status_dict(self) -> dict:
        return {
            "is_running":   self.is_running,
            "progress":     self.progress,
            "step":         self.step,
            "total_steps":  self.total_steps,
            "last_result":  self.last_result,
            "last_run_at":  self.last_run_at,
            "started_at":   self.started_at,
        }

refresh_state = RefreshState()


# â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• 
# HYBRID CACHE KEY LOGIC
# â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• 
def get_cache_key(message: str, history: List[Dict[str, str]] = None) -> str:
    """
    🌟 THE ULTIMATE HYBRID CACHING STRATEGY 🌟
    Combines Master Shared Caches for raw data dumps WITH
    Exact Match Hashes for deep AI analytics.
    """
    clean_message     = message.strip().lower()
    semantic_endpoint = get_semantic_endpoint(clean_message)

    if semantic_endpoint:
        print(f"🎯 Router Match: Generic request → {semantic_endpoint}")
        return f"chat_cache:endpoint:{semantic_endpoint}"

    # To prevent cross-contamination of pronouns ("what about them?"), 
    # we MUST include the recent history in the exact hash.
    history_str = ""
    if history:
        history_str = "|".join([h.get("content", "").strip().lower() for h in history[-2:]])
        
    hash_input = f"{clean_message}|{history_str}"
    unique_hash = hashlib.md5(hash_input.encode()).hexdigest()
    print(f"🧠 Analytics Hash → {unique_hash[:8]}...")
    return f"chat_cache:exact_hash:{unique_hash}"


# â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• 
# NIGHTLY SCHEDULER WRAPPER
# â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• 
def scheduled_nightly_refresh():
    """APScheduler calls this every night at midnight IST."""
    if not refresh_state.try_start():
        print("\u26a0\ufe0f APScheduler: Nightly refresh SKIPPED -- another run is already in progress.")
        return
    try:
        print("\n\u23f0 APScheduler: Triggering nightly refresh...")
        new_store = nightly_refresh_job(redis_client, progress_callback=refresh_state.update)
        if new_store:
            reload_data_rag()
            refresh_state.finish("success")
        else:
            refresh_state.finish("no_data")
    except Exception as e:
        print(f"\u274c Nightly refresh crashed: {e}")
        refresh_state.finish("failed")


# â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• 
# APP LIFESPAN (startup / shutdown)
# â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• â• 
@asynccontextmanager
async def lifespan(app: FastAPI):
    print("ðŸš€ Booting Antigravity Enterprise Backend...")

    # 1. RAG Store 1 â€” API Endpoint Docs (Swagger)
    initialize_system()

    # 2. RAG Store 2 â€” Actual University DATA
    initialize_data_rag()

    # 3. Nightly refresh scheduler (midnight IST)
    scheduler = BackgroundScheduler(timezone="Asia/Kolkata")
    scheduler.add_job(scheduled_nightly_refresh, "cron", hour=0, minute=0)
    scheduler.start()
    print("âœ… Nightly refresh scheduler armed (daily @ 00:00 IST).")

    print("âœ… All systems online. Antigravity is ready.")
    yield

    scheduler.shutdown(wait=False)
    print("ðŸ”´ Antigravity Backend shut down cleanly.")


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# FASTAPI APP
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
app = FastAPI(
    title="Antigravity University AI â€” Production Backend",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # Replace "*" with your UI's URL in real production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ChatRequest(BaseModel):
    message: str
    history: List[Dict[str, str]] = []
    is_warmer: bool = False
    mode: str = "auto"


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# ENDPOINTS
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

@app.post("/api/chat")
async def chat_endpoint(request: ChatRequest):
    """Main chat endpoint. Checks Redis cache first, then runs AI pipeline."""
    try:
        cache_key = get_cache_key(request.message, request.history)

        # 1. Check Redis cache
        if redis_client:
            cached_reply = redis_client.get(cache_key)
            if cached_reply:
                return {"reply": cached_reply, "cached": True}

        # 2. Run AI pipeline (non-blocking â€” passes redis_client for analytics)
        response_text = await asyncio.to_thread(
            get_ai_response, request.message, request.history, redis_client, request.is_warmer, request.mode
        )

        # 3. Cache result (skip on errors, empty states, or no-data markers)
        is_safe_to_cache = (
            "SYSTEM ERROR" not in response_text and
            "Found 0 matching" not in response_text and
            "__NO_DATA_FOUND__" not in response_text and
            "__DO_NOT_CACHE__" not in response_text
        )
        if redis_client and is_safe_to_cache:
            redis_client.set(cache_key, response_text, ex=86400)

        return {"reply": response_text, "cached": False}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/analytics")
async def analytics_endpoint():
    """
    Returns top searched queries and most-hit API endpoints from Redis.
    Use this to understand what users search for most.
    """
    if not redis_client:
        raise HTTPException(status_code=503, detail="Redis is not connected.")
    return get_analytics_summary(redis_client)


@app.post("/api/trigger-nightly-refresh")
async def trigger_nightly_refresh():
    """
    Manually trigger the nightly data refresh job.
    Returns immediately and runs the refresh in a background thread.
    Returns 409-like message if a refresh is already running.
    Auto-deletes old RAG data, rebuilds from scratch, then hot-reloads.
    """
    if refresh_state.is_running:
        return {
            "message": "⏳ A refresh is already in progress. Please wait for it to complete.",
            "already_running": True,
            "status": refresh_state.status_dict(),
        }

    if not refresh_state.try_start():
        return {
            "message": "⏳ A refresh is already in progress. Please wait for it to complete.",
            "already_running": True,
            "status": refresh_state.status_dict(),
        }

    # Run the heavy work in a background thread so the HTTP response returns immediately
    def _run_refresh():
        try:
            print("🔧 Manual nightly refresh triggered via /api/trigger-nightly-refresh")
            new_store = nightly_refresh_job(redis_client, progress_callback=refresh_state.update)
            if new_store:
                reload_data_rag()
                refresh_state.finish("success")
            else:
                refresh_state.finish("no_data")
        except Exception as e:
            print(f"❌ Manual refresh crashed: {e}")
            refresh_state.finish("failed")

    threading.Thread(target=_run_refresh, daemon=True).start()

    return {
        "message": "🔄 Nightly refresh started! Old RAG data will be wiped and rebuilt from scratch.",
        "already_running": False,
        "status": refresh_state.status_dict(),
    }


@app.get("/api/refresh-status")
async def refresh_status_endpoint():
    """Returns the current state of the nightly refresh job for frontend polling."""
    return refresh_state.status_dict()


@app.post("/api/clear-cache")
async def clear_cache_endpoint():
    """Wipe the entire Redis cache (admin use only)."""
    try:
        if redis_client:
            keys_to_delete = redis_client.keys("chat_cache:*")
            if keys_to_delete:
                redis_client.delete(*keys_to_delete)
            print(f"🧹 Cloud Redis Cache cleared ({len(keys_to_delete)} items). Synonyms & Analytics preserved.")
            return {"message": "✅ Cloud cache cleared successfully!"}
        return {"message": "Redis is not connected."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/wipe-rag")
async def wipe_rag_endpoint():
    """Wipe the local FAISS RAG directories."""
    try:
        from langchain_core_free_tier_rag import DB_FAISS_PATH, DB_FAISS_DATA_PATH
        import shutil
        import os
        
        deleted = []
        if os.path.exists(DB_FAISS_PATH):
            shutil.rmtree(DB_FAISS_PATH)
            deleted.append("Swagger Docs")
            
        if os.path.exists(DB_FAISS_DATA_PATH):
            shutil.rmtree(DB_FAISS_DATA_PATH)
            deleted.append("University Data")
            
        if deleted:
            return {"message": f"RAG Store ({', '.join(deleted)}) wiped! Run nightly refresh to rebuild."}
        
        return {"message": "RAG Stores are already empty."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ══════════════════════════════════════════════════════════════════════════════
# DISCOVERY CATALOGUE — all 87 university API GET endpoints, grouped by topic
# ══════════════════════════════════════════════════════════════════════════════
DISCOVERY_CATALOGUE = [
    {"label": "AcademicSetup",                  "endpoint": "/api/AcademicSetup",                  "keywords": ["academic", "setup", "academic setup"]},
    {"label": "AcademicYear",                   "endpoint": "/api/AcademicYear",                   "keywords": ["academic year", "year", "session"]},
    {"label": "AllowStudentReRegistration",     "endpoint": "/api/AllowStudentReRegistration",     "keywords": ["re registration", "re-registration", "reregistration"]},
    {"label": "AlumniRegistration",             "endpoint": "/api/AlumniRegistration",             "keywords": ["alumni", "graduate", "ex student", "alumni registration"]},
    {"label": "Announcement",                   "endpoint": "/api/Announcement",                   "keywords": ["announcement", "notice", "news", "update"]},
    {"label": "ApplPrequalMarkCollege",         "endpoint": "/api/ApplPrequalMarkCollege",         "keywords": ["prequalification", "prequal", "mark", "college mark"]},
    {"label": "ApplicationRegistration",        "endpoint": "/api/ApplicationRegistration",        "keywords": ["application", "application registration", "admission form"]},
    {"label": "ApplyScholarship",               "endpoint": "/api/ApplyScholarship",               "keywords": ["scholarship application", "apply scholarship"]},
    {"label": "AttendanceCriteria",             "endpoint": "/api/AttendanceCriteria",             "keywords": ["attendance criteria", "attendance rules", "attendance policy"]},
    {"label": "Category",                       "endpoint": "/api/Category",                       "keywords": ["category", "categories"]},
    {"label": "Chapter",                        "endpoint": "/api/Chapter",                        "keywords": ["chapter", "chapters", "syllabus chapter"]},
    {"label": "CompanyInformation",             "endpoint": "/api/CompanyInformation",             "keywords": ["company", "company information", "organisation"]},
    {"label": "Course",                         "endpoint": "/api/Course",                         "keywords": ["course", "courses", "program", "degree"]},
    {"label": "CourseFeeHeader",                "endpoint": "/api/CourseFeeHeader",                "keywords": ["course fee", "fee header", "fees", "tuition fee"]},
    {"label": "CourseFeeLine",                  "endpoint": "/api/CourseFeeLine",                  "keywords": ["fee line", "course fee details", "fee breakdown"]},
    {"label": "CourseSubjExGroupHead",          "endpoint": "/api/CourseSubjExGroupHead",          "keywords": ["exam group", "subject group", "examination group header"]},
    {"label": "CourseSubjExGroupLine",          "endpoint": "/api/CourseSubjExGroupLine",          "keywords": ["exam group line", "subject group line"]},
    {"label": "CourseSubjectHeader",            "endpoint": "/api/CourseSubjectHeader",            "keywords": ["course subject", "subject header", "course subjects"]},
    {"label": "CourseSyllabusHeader",           "endpoint": "/api/CourseSyllabusHeader",           "keywords": ["syllabus", "course syllabus", "syllabus header"]},
    {"label": "CourseSyllabusLine",             "endpoint": "/api/CourseSyllabusLine",             "keywords": ["syllabus line", "syllabus details"]},
    {"label": "Customer",                       "endpoint": "/api/Customer",                       "keywords": ["customer", "customers"]},
    {"label": "Dimension",                      "endpoint": "/api/Dimension",                      "keywords": ["dimension", "dimensions"]},
    {"label": "DimensionValue",                 "endpoint": "/api/DimensionValue",                 "keywords": ["dimension value", "dimension values"]},
    {"label": "DocumentAttachment",             "endpoint": "/api/DocumentAttachment",             "keywords": ["document attachment", "document", "attachment"]},
    {"label": "Documents",                      "endpoint": "/api/Documents",                      "keywords": ["documents", "files", "forms"]},
    {"label": "EduCalendar",                    "endpoint": "/api/EduCalendar",                    "keywords": ["calendar", "edu calendar", "academic calendar"]},
    {"label": "EduCalendarEntry",               "endpoint": "/api/EduCalendarEntry",               "keywords": ["calendar entry", "schedule entry"]},
    {"label": "EduCalendarHolidaySetup",        "endpoint": "/api/EduCalendarHolidaySetup",        "keywords": ["holiday setup", "holidays", "holiday list"]},
    {"label": "EduCalendarMultiEventEntry",     "endpoint": "/api/EduCalendarMultiEventEntry",     "keywords": ["multi event", "events", "calendar events"]},
    {"label": "EduCalendarOffDaySetup",         "endpoint": "/api/EduCalendarOffDaySetup",         "keywords": ["off day", "offday", "holiday off day"]},
    {"label": "EducationSetup",                 "endpoint": "/api/EducationSetup",                 "keywords": ["education setup", "education config"]},
    {"label": "ElectiveApplication",            "endpoint": "/api/ElectiveApplication",            "keywords": ["elective application", "elective apply"]},
    {"label": "ElectiveCourseMapping",          "endpoint": "/api/ElectiveCourseMapping",          "keywords": ["elective mapping", "elective course", "elective"]},
    {"label": "ElectiveSection",                "endpoint": "/api/ElectiveSection",                "keywords": ["elective section"]},
    {"label": "Employee",                       "endpoint": "/api/Employee",                       "keywords": ["employee", "staff", "faculty", "teacher", "professors"]},
    {"label": "EventMaster",                    "endpoint": "/api/EventMaster",                    "keywords": ["event", "events", "event master", "university events"]},
    {"label": "ExaminationSetup",               "endpoint": "/api/ExaminationSetup",               "keywords": ["examination", "exam setup", "exam configuration"]},
    {"label": "ExternalDateSheetLine",          "endpoint": "/api/ExternalDateSheetLine",          "keywords": ["external date sheet", "external exam schedule", "date sheet"]},
    {"label": "FeedbackDetail",                 "endpoint": "/api/FeedbackDetail",                 "keywords": ["feedback details", "feedback"]},
    {"label": "FeedbackQuestions",              "endpoint": "/api/FeedbackQuestions",              "keywords": ["feedback questions"]},
    {"label": "Graduation",                     "endpoint": "/api/Graduation",                     "keywords": ["graduation", "convocation", "graduate"]},
    {"label": "HostelApplication",              "endpoint": "/api/HostelApplication",              "keywords": ["hostel application", "hostel apply", "hostel form"]},
    {"label": "HostelInventoryAllocation",      "endpoint": "/api/HostelInventoryAllocation",      "keywords": ["hostel inventory", "hostel allocation", "hostel items"]},
    {"label": "HostelMaster",                   "endpoint": "/api/HostelMaster",                   "keywords": ["hostel", "hostels", "hostel master", "hostel list", "accommodation", "dorm", "dormitory", "residential"]},
    {"label": "InternalDateSheetLine",          "endpoint": "/api/InternalDateSheetLine",          "keywords": ["internal date sheet", "internal exam", "internal schedule"]},
    {"label": "Issue",                          "endpoint": "/api/Issue",                          "keywords": ["issue", "issues", "complaint", "grievance"]},
    {"label": "IssueTracker",                   "endpoint": "/api/IssueTracker",                   "keywords": ["issue tracker", "track issue", "complaint tracker"]},
    {"label": "KeyMaster",                      "endpoint": "/api/KeyMaster",                      "keywords": ["key master", "keys", "key list"]},
    {"label": "LeaveApplication",               "endpoint": "/api/LeaveApplication",               "keywords": ["leave application", "leave apply", "leave request"]},
    {"label": "PlacementProgram",               "endpoint": "/api/PlacementProgram",               "keywords": ["placement", "campus placement", "placement program", "job", "recruitment"]},
    {"label": "PortalUsers",                    "endpoint": "/api/PortalUsers",                    "keywords": ["portal users", "portal accounts", "users"]},
    {"label": "PostExternalAwardLine",          "endpoint": "/api/PostExternalAwardLine",          "keywords": ["external award", "award", "prize"]},
    {"label": "ProgramOutcomeDescription",      "endpoint": "/api/ProgramOutcomeDescription",      "keywords": ["program outcome", "learning outcome", "po", "course outcome"]},
    {"label": "ReAppearStudentDetails",         "endpoint": "/api/ReAppearStudentDetails",         "keywords": ["reappear", "re appear", "supplementary", "back", "atkt"]},
    {"label": "ReEvaluationReTotalingDet",      "endpoint": "/api/ReEvaluationReTotalingDet",      "keywords": ["re-evaluation", "retotaling", "re evaluation", "revaluation"]},
    {"label": "RegisterEmployeeRoomWise",       "endpoint": "/api/RegisterEmployeeRoomWise",       "keywords": ["employee room", "room wise employee"]},
    {"label": "RegistrationDocumentMaster",     "endpoint": "/api/RegistrationDocumentMaster",     "keywords": ["registration document", "required documents", "admission documents"]},
    {"label": "RoleMaster",                     "endpoint": "/api/RoleMaster",                     "keywords": ["role", "roles", "user roles", "role master"]},
    {"label": "RoomAllocationCollege",          "endpoint": "/api/RoomAllocationCollege",          "keywords": ["room allocation", "room", "allotment", "room allotment"]},
    {"label": "RoomMaster",                     "endpoint": "/api/RoomMaster",                     "keywords": ["room", "rooms", "room master", "classroom", "room list"]},
    {"label": "RoomType",                       "endpoint": "/api/RoomType",                       "keywords": ["room type", "room types", "hostel room type"]},
    {"label": "RoomWiseBed",                    "endpoint": "/api/RoomWiseBed",                    "keywords": ["bed", "beds", "room bed", "hostel bed"]},
    {"label": "ScholarshipSetupHeader",         "endpoint": "/api/ScholarshipSetupHeader",         "keywords": ["scholarship", "scholarships", "scholarship setup", "scholarship list"]},
    {"label": "Section",                        "endpoint": "/api/Section",                        "keywords": ["section", "sections", "batch section"]},
    {"label": "Semester",                       "endpoint": "/api/Semester",                       "keywords": ["semester", "semesters", "term"]},
    {"label": "Student",                        "endpoint": "/api/Student",                        "keywords": ["student", "students", "student list", "enrolled"]},
    {"label": "StudentAttendanceHeader",        "endpoint": "/api/StudentAttendanceHeader",        "keywords": ["attendance", "student attendance"]},
    {"label": "StudentAttendanceLine",          "endpoint": "/api/StudentAttendanceLine",          "keywords": ["attendance line", "attendance details"]},
    {"label": "StudentBacklogLedger",           "endpoint": "/api/StudentBacklogLedger",           "keywords": ["backlog", "backlog ledger", "pending subjects", "fail subjects"]},
    {"label": "StudentCertificate",             "endpoint": "/api/StudentCertificate",             "keywords": ["certificate", "certificates", "bonafide", "student certificate"]},
    {"label": "StudentDetailsMentorship",       "endpoint": "/api/StudentDetailsMentorship",       "keywords": ["mentorship", "mentor", "mentoring"]},
    {"label": "StudentDocumentAttachment",      "endpoint": "/api/StudentDocumentAttachment",      "keywords": ["student document", "student attachment", "uploaded documents"]},
    {"label": "StudentEventRegistration",       "endpoint": "/api/StudentEventRegistration",       "keywords": ["event registration", "student event", "event signup"]},
    {"label": "StudentExternalHeader",          "endpoint": "/api/StudentExternalHeader",          "keywords": ["external exam", "external marks", "external header"]},
    {"label": "StudentExternalLine",            "endpoint": "/api/StudentExternalLine",            "keywords": ["external marks line", "external result line"]},
    {"label": "StudentInternalHeader",          "endpoint": "/api/StudentInternalHeader",          "keywords": ["internal marks", "internal exam", "internal header"]},
    {"label": "StudentInternalLine",            "endpoint": "/api/StudentInternalLine",            "keywords": ["internal marks line", "internal result"]},
    {"label": "StudentOptionalSubject",         "endpoint": "/api/StudentOptionalSubject",         "keywords": ["optional subject", "elective subject"]},
    {"label": "StudentReRegistration",          "endpoint": "/api/StudentReRegistration",          "keywords": ["re registration", "student re-registration"]},
    {"label": "StudentRegistration",            "endpoint": "/api/StudentRegistration",            "keywords": ["student registration", "enrollment", "enrolment"]},
    {"label": "StudentSubject",                 "endpoint": "/api/StudentSubject",                 "keywords": ["student subject", "enrolled subjects", "subject list"]},
    {"label": "StudentWiseHold",                "endpoint": "/api/StudentWiseHold",                "keywords": ["hold", "student hold", "fee hold", "noc"]},
    {"label": "SubCategory",                    "endpoint": "/api/SubCategory",                    "keywords": ["subcategory", "sub category"]},
    {"label": "Subject",                        "endpoint": "/api/Subject",                        "keywords": ["subject", "subjects", "subject list", "paper"]},
    {"label": "SubjectClassification",          "endpoint": "/api/SubjectClassification",          "keywords": ["subject classification", "subject type"]},
    {"label": "TimeTableGeneration",            "endpoint": "/api/TimeTableGeneration",            "keywords": ["timetable", "time table", "schedule", "class schedule"]},
    {"label": "TimeTableSlot",                  "endpoint": "/api/TimeTableSlot",                  "keywords": ["timetable slot", "time slot", "class slot"]},
    {"label": "TransportApplication",           "endpoint": "/api/TransportApplication",           "keywords": ["transport", "bus", "transportation", "commute", "shuttle"]},
    {"label": "Unit",                           "endpoint": "/api/Unit",                           "keywords": ["unit", "units"]},
    {"label": "UserRoleMatrix",                 "endpoint": "/api/UserRoleMatrix",                 "keywords": ["user role matrix", "role matrix", "permission", "permissions"]},
    {"label": "WireTransfer",                   "endpoint": "/api/WireTransfer",                   "keywords": ["wire transfer", "bank transfer", "payment transfer"]},
    {"label": "Year",                           "endpoint": "/api/Year",                           "keywords": ["year", "years"]},
]

UNIVERSITY_BASE_URL = "http://kuriseapi.karnavatiuniversity.edu.in"


class DiscoverRequest(BaseModel):
    query: str
    excluded_endpoints: List[str] = []


DISCOVER_STOP_WORDS = {
    "give", "me", "all", "the", "a", "an", "is", "are", "was", "were",
    "show", "list", "fetch", "get", "find", "tell", "what", "which",
    "how", "many", "in", "of", "and", "to", "for", "at", "this", "that",
    "these", "those", "here", "there", "available", "university",
    "please", "can", "you", "i", "want", "need", "my", "our", "we", "do",
    "does", "did", "have", "has", "about", "with", "from", "by", "on",
    "any", "some", "your",
}


@app.post("/api/discover")
async def discover_endpoint(request: DiscoverRequest):
    """
    Fuzzy-matches the user query against all known university API endpoints,
    fetches live data from the best match, and returns results.
    - Strips stop words so 'give me all the hostels in university' → ['hostels']
    - Skips excluded_endpoints (for retry flow when user says result is wrong)
    - Auto-skips 401/403 unauthorized endpoints and tries the next best match
    Does NOT cache anything — user must confirm accuracy before saving.
    """
    import difflib

    query_lower = request.query.lower()
    # Strip stop words so generic query words don't dilute the score
    all_words = set(re.findall(r'[a-z]+', query_lower))
    query_words = all_words - DISCOVER_STOP_WORDS
    if not query_words:
        query_words = all_words  # fallback if everything was a stop word

    excluded = set(request.excluded_endpoints)

    def score_entry(entry):
        if entry["endpoint"] in excluded:
            return -1.0
        best = 0.0
        for kw in entry["keywords"]:
            kw_words = set(kw.split())
            # Fuzzy overlap: count words that match exactly or are very close
            fuzzy_overlap = 0
            for qw in query_words:
                if qw in kw_words:
                    fuzzy_overlap += 1
                else:
                    # Check for close matches with a high cutoff (e.g. 0.8)
                    if difflib.get_close_matches(qw, kw_words, n=1, cutoff=0.8):
                        fuzzy_overlap += 1
                        
            if fuzzy_overlap > 0:
                # To prioritize multi-word matches like "hostel inventory", 
                # we weight the score heavily by how much of the keyword is matched.
                coverage = fuzzy_overlap / len(kw_words)
                precision = fuzzy_overlap / len(query_words)
                
                # Combine coverage and precision (F1-like score)
                score = (2 * coverage * precision) / (coverage + precision)
                
                ratio = difflib.SequenceMatcher(None, " ".join(sorted(query_words)), kw).ratio()
                best = max(best, score * 0.85 + ratio * 0.15)
        return best

    scored = [(e, score_entry(e)) for e in DISCOVERY_CATALOGUE]
    scored.sort(key=lambda x: x[1], reverse=True)
    # Filter out excluded and zero-score entries
    candidates = [(e, s) for e, s in scored if s > 0.0]

    if not candidates:
        return {"found": False, "message": "No matching university API found for this query. All relevant APIs may have been tried."}

    attempted = list(excluded)
    
    from data_rag_builder import STATIC_ENDPOINTS

    # Try candidates in order — skip 401/403 endpoints automatically
    for best_entry, best_score in candidates[:5]:  # try up to 5 best matches
        endpoint = best_entry["endpoint"]
        
        # Prevent re-fetching and duplicating data that is already in RAG Store 2
        if endpoint in STATIC_ENDPOINTS:
            print(f"[DISCOVER] Skipping '{endpoint}' because it is already ingested in RAG Store 2.")
            continue
            
        attempted.append(endpoint)
        print(f"[DISCOVER] Trying '{endpoint}' (score={best_score:.2f}) for query: '{request.query}'")

        try:
            mock_data = {
                "/api/ReEvaluationReTotalingDet": [{"id": 1, "student_name": "Test Student", "status": "Pending Re-evaluation", "subject": "Mathematics"}],
                "/api/ScholarshipSetupHeader": [{"id": 101, "name": "Merit Scholarship", "amount": 50000, "criteria": "GPA > 3.5"}, {"id": 102, "name": "Sports Scholarship", "amount": 25000, "criteria": "National Level Player"}],
                "/api/HostelInventoryAllocation": [{"hostel": "Block A", "room_no": "101", "bed_no": "1", "student_name": "Test Student", "status": "Allocated"}],
            }
            if endpoint in mock_data:
                data = mock_data[endpoint]
                return {
                    "found": True,
                    "endpoint": endpoint,
                    "label": best_entry["label"],
                    "count": len(data),
                    "records": data,
                    "attempted": attempted,
                }
                
            url = f"{UNIVERSITY_BASE_URL}{endpoint}"
            resp = await asyncio.to_thread(requests.get, url, timeout=20)

            if resp.status_code in (401, 403):
                print(f"[DISCOVER] {endpoint} → {resp.status_code} Unauthorized. Skipping...")
                excluded.add(endpoint)
                continue

            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, dict):
                for v in data.values():
                    if isinstance(v, list):
                        data = v
                        break
            if not isinstance(data, list) or len(data) == 0:
                print(f"[DISCOVER] {endpoint} returned empty data. Skipping...")
                excluded.add(endpoint)
                continue

            return {
                "found": True,
                "endpoint": endpoint,
                "label": best_entry["label"],
                "count": len(data),
                "records": data,
                "attempted": attempted,
            }

        except Exception as e:
            print(f"[DISCOVER] {endpoint} → Error: {e}. Skipping...")
            excluded.add(endpoint)
            continue

    return {
        "found": False,
        "message": f"Searched {len(attempted)} APIs but none returned valid data for your query. The data may require authentication or may not be available publicly.",
        "attempted": attempted,
    }


class IngestRequest(BaseModel):
    endpoint: str
    label: str
    records: List[Dict]


@app.post("/api/ingest-to-rag")
async def ingest_to_rag_endpoint(request: IngestRequest):
    """
    Permanently saves user-confirmed API discovery data into RAG Store 2 (FAISS on disk).
    Also registers the endpoint in the nightly refresh list so it stays up-to-date.
    """
    from langchain_core.documents import Document
    from data_rag_builder import item_to_text, STATIC_ENDPOINTS

    label = request.label
    docs = [
        Document(
            page_content=item_to_text(item, label),
            metadata={
                "source_endpoint": request.endpoint,
                "data_type": label,
                "raw_json": json.dumps(item, sort_keys=True, ensure_ascii=False),
            },
        )
        for item in request.records
    ]

    success = await asyncio.to_thread(ingest_new_documents, docs)

    if success:
        # Register for future nightly refreshes so the data stays current
        STATIC_ENDPOINTS[request.endpoint] = label
        from rag_registry import register_data_type
        register_data_type(label, request.endpoint)
        print(f"[INGEST] {request.endpoint} registered for nightly refresh.")
        return {"message": f"✅ {len(docs)} {label} records saved to RAG permanently!"}
    else:
        raise HTTPException(status_code=500, detail="Failed to ingest data into RAG Store.")


@app.get("/api/ReEvaluationReTotalingDet")
async def mock_re_evaluation():
    return [{"id": 1, "student_name": "Test Student", "status": "Pending Re-evaluation", "subject": "Mathematics"}]

@app.get("/api/ScholarshipSetupHeader")
async def mock_scholarship():
    return [{"id": 101, "name": "Merit Scholarship", "amount": 50000, "criteria": "GPA > 3.5"}, {"id": 102, "name": "Sports Scholarship", "amount": 25000, "criteria": "National Level Player"}]

@app.get("/api/HostelInventoryAllocation")
async def mock_hostel_inventory():
    return [{"hostel": "Block A", "room_no": "101", "bed_no": "1", "student_name": "Test Student", "status": "Allocated"}]


class SynonymsRequest(BaseModel):
    synonyms: Dict[str, List[str]]

@app.get("/api/synonyms")
async def get_synonyms_endpoint():
    """Fetch the synonyms dictionary from Redis."""
    try:
        if redis_client:
            data = redis_client.get("university_bot:synonyms")
            if data:
                return json.loads(data)
        return {}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/synonyms")
async def save_synonyms_endpoint(request: SynonymsRequest):
    """Save the synonyms dictionary to Redis."""
    try:
        if redis_client:
            redis_client.set("university_bot:synonyms", json.dumps(request.synonyms))
            return {"message": "✅ Synonyms saved successfully!"}
        return {"message": "Redis is not connected."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/")
async def serve_frontend():
    """Serve the web UI."""
    with open("web_user_interface.html", "r", encoding="utf-8") as f:
        html_content = f.read()
    return HTMLResponse(content=html_content, status_code=200)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
