"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB
"""
import calendar
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

# --------------------------------------------------------------------------- #
# MongoDB configuration & Timezone
# --------------------------------------------------------------------------- #
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))

MIN_EPOCH = 100_000_000_000
MAX_EPOCH = 4_102_444_800_000


# --------------------------------------------------------------------------- #
# Index initialization
# --------------------------------------------------------------------------- #
def init_indexes():
    """Ensure all required indexes exist idempotently on startup."""
    try:
        # employees collection
        db.employees.create_index([("emp_code", 1)], unique=True)
        db.employees.create_index([("department", 1)])
        db.employees.create_index([("joined_on", 1)])
        db.employees.create_index([("department", 1), ("joined_on", 1)])

        # attendance_logs collection
        db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
        db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
        db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])
        db.attendance_logs.create_index([("date", 1), ("late_minutes", 1)])
    except PyMongoError:
        pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_indexes()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Helpers: Rounding, Dates & Business Rules
# --------------------------------------------------------------------------- #
def round_half_up(value: Optional[float | Decimal], decimals: int) -> Optional[float]:
    """R8: Rounding of reported numbers to specified decimals using half-up."""
    if value is None:
        return None
    d = Decimal(str(value))
    target = Decimal("10") ** -decimals
    return float(d.quantize(target, rounding=ROUND_HALF_UP))


def epoch_to_datetime(ms: int) -> datetime:
    """
    R1: Convert epoch milliseconds to UTC datetime, truncated to whole seconds.
    09:40:00.900 counts as 09:40:00.
    """
    truncated_sec = ms // 1000
    return datetime.fromtimestamp(truncated_sec, tz=timezone.utc)


def dt_to_epoch(dt: Optional[datetime]) -> Optional[int]:
    """Convert UTC BSON datetime to epoch milliseconds integer."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def compute_attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: A record's date is the IST calendar date of its punch_in instant.
    Exception - overnight shifts (shift_end <= shift_start): a punch-in whose IST
    time is strictly earlier than shift_end belongs to the shift that started the previous day.
    """
    punch_in_ist = punch_in_utc.astimezone(IST)
    is_overnight = shift_end <= shift_start
    if is_overnight:
        end_h, end_m = map(int, shift_end.split(":"))
        if punch_in_ist.time() < time(end_h, end_m):
            return (punch_in_ist.date() - timedelta(days=1)).isoformat()
    return punch_in_ist.date().isoformat()


def compute_late_minutes(punch_in_utc: datetime, shift_start: str, attendance_date: str) -> int:
    """
    R2: Grace period is 10 minutes. A punch-in is late only if strictly more than 10:00
    after shift_start. When late, late_minutes = whole minutes (floor) elapsed since shift_start.
    Otherwise 0.
    """
    start_h, start_m = map(int, shift_start.split(":"))
    att_d = date.fromisoformat(attendance_date)
    shift_start_ist = datetime.combine(att_d, time(start_h, start_m), tzinfo=IST)
    punch_in_ist = punch_in_utc.astimezone(IST)
    delta_seconds = (punch_in_ist - shift_start_ist).total_seconds()
    if delta_seconds > 600:
        return int(delta_seconds // 60)
    return 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """
    R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up.
    """
    delta_seconds = (punch_out_utc - punch_in_utc).total_seconds()
    return round_half_up(delta_seconds / 3600.0, 2)


def compute_overtime(punch_out_utc: datetime, shift_end: str, shift_start: str, attendance_date: str) -> int:
    """
    R3: overtime_minutes = whole minutes (floor) between shift_end and punch_out,
    counted only when that is >= 30; otherwise 0.
    Overnight shifts end on the next calendar day.
    """
    end_h, end_m = map(int, shift_end.split(":"))
    att_d = date.fromisoformat(attendance_date)
    is_overnight = shift_end <= shift_start
    end_d = att_d + timedelta(days=1) if is_overnight else att_d
    shift_end_ist = datetime.combine(end_d, time(end_h, end_m), tzinfo=IST)
    punch_out_ist = punch_out_utc.astimezone(IST)
    delta_seconds = (punch_out_ist - shift_end_ist).total_seconds()
    diff_minutes = int(delta_seconds // 60)
    return diff_minutes if diff_minutes >= 30 else 0


def compute_half_day(work_hours: Optional[float]) -> bool:
    """
    R5: After punch-out, half_day = true when the rounded work_hours < 4.50.
    """
    if work_hours is None:
        return False
    return work_hours < 4.50


# --------------------------------------------------------------------------- #
# Document serialization
# --------------------------------------------------------------------------- #
def serialize_employee(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_epoch(doc.get("created_at")),
    }


def serialize_history_entry(entry: dict) -> dict:
    changes = {}
    for field, change in entry.get("changes", {}).items():
        from_val = change.get("from")
        to_val = change.get("to")
        if field in ("punch_in", "punch_out"):
            from_val = dt_to_epoch(from_val) if isinstance(from_val, datetime) else from_val
            to_val = dt_to_epoch(to_val) if isinstance(to_val, datetime) else to_val
        changes[field] = {"from": from_val, "to": to_val}
    return {
        "at": dt_to_epoch(entry["at"]) if isinstance(entry.get("at"), datetime) else entry.get("at"),
        "by": entry["by"],
        "reason": entry["reason"],
        "changes": changes,
    }


def serialize_attendance(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_epoch(doc.get("punch_in")),
        "punch_out": dt_to_epoch(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": [serialize_history_entry(h) for h in doc.get("history", [])],
    }


# --------------------------------------------------------------------------- #
# Pydantic Models & Input Validation
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, v: str) -> str:
        try:
            date.fromisoformat(v)
        except ValueError:
            raise ValueError("Invalid calendar date for joined_on")
        return v

    @model_validator(mode="after")
    def validate_shifts(self) -> "EmployeeCreate":
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=MIN_EPOCH, le=MAX_EPOCH)
    status: str = Field("PRESENT", pattern=r"^(PRESENT|WFH|ON_DUTY)$")


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=MIN_EPOCH, le=MAX_EPOCH)


class RegularizeRequest(BaseModel):
    status: Optional[str] = Field(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$")
    punch_in: Optional[int] = Field(None, ge=MIN_EPOCH, le=MAX_EPOCH)
    punch_out: Optional[int] = Field(None, ge=MIN_EPOCH, le=MAX_EPOCH)
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Endpoints: System & Employees
# --------------------------------------------------------------------------- #
@app.get("/health", tags=["system"])
def health():
    """Readiness probe. Returns 200 only if MongoDB answers a ping, otherwise 503."""
    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(
            status_code=503,
            detail="Database unavailable",
        )


@app.post("/employees", status_code=201, tags=["employees"])
def create_employee(body: EmployeeCreate):
    """
    Create an employee. Unique emp_code is enforced by unique DB index.
    created_at is set by the server.
    """
    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    doc = body.model_dump()
    doc["created_at"] = now_utc
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="emp_code already exists",
        )
    return serialize_employee(doc)


@app.get("/employees", tags=["employees"])
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """List employees sorted by emp_code ascending."""
    query = {}
    if department:
        query["department"] = department
    total = db.employees.count_documents(query)
    skip = (page - 1) * page_size
    cursor = db.employees.find(query).sort("emp_code", 1).skip(skip).limit(page_size)
    items = [serialize_employee(e) for e in cursor]
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# --------------------------------------------------------------------------- #
# Endpoints: Attendance
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201, tags=["attendance"])
def punch_in(body: PunchInRequest):
    """
    Record a punch-in for an employee.
    Concurrency: guaranteed unique by compound index on (emp_code, date).
    """
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(
            status_code=404,
            detail="Employee not found",
        )

    if body.punched_at is not None:
        punch_in_utc = epoch_to_datetime(body.punched_at)
    else:
        punch_in_utc = datetime.now(timezone.utc).replace(microsecond=0)

    attendance_date = compute_attendance_date(punch_in_utc, emp["shift_start"], emp["shift_end"])
    late_min = compute_late_minutes(punch_in_utc, emp["shift_start"], attendance_date)

    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status,
        "punch_in": punch_in_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_min,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="already punched in for this date",
        )

    return serialize_attendance(doc)


@app.post("/attendance/punch-out", status_code=200, tags=["attendance"])
def punch_out(body: PunchOutRequest):
    """
    Closes employee's most recent record whose punch_in is at or before punched_at.
    """
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(
            status_code=404,
            detail="Employee not found",
        )

    if body.punched_at is not None:
        punch_out_utc = epoch_to_datetime(body.punched_at)
    else:
        punch_out_utc = datetime.now(timezone.utc).replace(microsecond=0)

    # Find the most recent record whose punch_in is at or before punch_out_utc
    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$lte": punch_out_utc},
        },
        sort=[("punch_in", -1)],
    )

    if not record or not record.get("punch_in"):
        raise HTTPException(
            status_code=404,
            detail="No punch-in found",
        )

    punch_in_utc = record["punch_in"]
    if punch_in_utc.tzinfo is None:
        punch_in_utc = punch_in_utc.replace(tzinfo=timezone.utc)

    # Validation: punch_out must be after punch_in and <= 24 hours after it
    if punch_out_utc <= punch_in_utc:
        raise HTTPException(
            status_code=422,
            detail="punch_out must be after punch_in",
        )

    if (punch_out_utc - punch_in_utc).total_seconds() > 24 * 3600:
        raise HTTPException(
            status_code=422,
            detail="punch_out cannot be more than 24 hours after punch_in",
        )

    # If already punched out, return 409
    if record.get("punch_out") is not None:
        raise HTTPException(
            status_code=409,
            detail="that record is already punched out",
        )

    work_hours = compute_work_hours(punch_in_utc, punch_out_utc)
    overtime_min = compute_overtime(punch_out_utc, emp["shift_end"], emp["shift_start"], record["date"])
    half_day = compute_half_day(work_hours)

    # Atomic update ensuring two simultaneous punch-outs yield exactly one 200 and one 409
    updated_doc = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": punch_out_utc,
                "work_hours": work_hours,
                "overtime_minutes": overtime_min,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated_doc:
        raise HTTPException(
            status_code=409,
            detail="that record is already punched out",
        )

    return serialize_attendance(updated_doc)


@app.get("/attendance", tags=["attendance"])
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = Query(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """
    List attendance records sorted by date descending, then emp_code ascending.
    """
    if date_from and date_to:
        if date_from > date_to:
            raise HTTPException(
                status_code=422,
                detail="date_from must be <= date_to",
            )

    query = {}
    if emp_code:
        query["emp_code"] = emp_code
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from
        if date_to:
            query["date"]["$lte"] = date_to
    if status:
        query["status"] = status

    total = db.attendance_logs.count_documents(query)
    skip = (page - 1) * page_size
    cursor = db.attendance_logs.find(query).sort([("date", -1), ("emp_code", 1)]).skip(skip).limit(page_size)
    items = [serialize_attendance(d) for d in cursor]
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}", tags=["attendance"])
def regularize_attendance(
    emp_code: str,
    date: str = Path(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    body: RegularizeRequest = ...,
):
    """
    Regularize (manually correct) one day's attendance record with full audit trail.
    """
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(
            status_code=404,
            detail="Employee not found",
        )

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(
            status_code=404,
            detail="Attendance record not found",
        )

    # Validate that at least one change field was supplied
    if body.status is None and body.punch_in is None and body.punch_out is None:
        raise HTTPException(
            status_code=422,
            detail="No changes requested",
        )

    # Determine proposed values
    if body.status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                status_code=422,
                detail="Setting status ABSENT or LEAVE clears both punch times; punch_in/punch_out cannot be supplied",
            )
        new_status = body.status
        new_punch_in = None
        new_punch_out = None
    else:
        new_status = body.status if body.status is not None else record["status"]
        new_punch_in = epoch_to_datetime(body.punch_in) if body.punch_in is not None else record.get("punch_in")
        new_punch_out = epoch_to_datetime(body.punch_out) if body.punch_out is not None else record.get("punch_out")

    # Presence status requires punch_in
    if new_status in ("PRESENT", "WFH", "ON_DUTY"):
        if new_punch_in is None:
            raise HTTPException(
                status_code=422,
                detail="Presence status requires a punch_in",
            )

    # Ensure punch_in stays on record's attendance date
    if new_punch_in is not None:
        calc_date = compute_attendance_date(new_punch_in, emp["shift_start"], emp["shift_end"])
        if calc_date != date:
            raise HTTPException(
                status_code=422,
                detail=f"punch_in falls on attendance date {calc_date}, does not match record date {date}",
            )

    # Validate punch_out order and duration
    if new_punch_out is not None:
        if new_punch_in is None:
            raise HTTPException(
                status_code=422,
                detail="punch_out requires a punch_in",
            )
        if new_punch_out <= new_punch_in:
            raise HTTPException(
                status_code=422,
                detail="punch_out must be after punch_in",
            )
        if (new_punch_out - new_punch_in).total_seconds() > 24 * 3600:
            raise HTTPException(
                status_code=422,
                detail="punch_out cannot be more than 24 hours after punch_in",
            )

    # Compute new derived values
    if new_status in ("ABSENT", "LEAVE"):
        new_late_minutes = 0
        new_work_hours = None
        new_overtime_minutes = 0
        new_half_day = False
    else:
        new_late_minutes = compute_late_minutes(new_punch_in, emp["shift_start"], date)
        if new_punch_out is not None:
            new_work_hours = compute_work_hours(new_punch_in, new_punch_out)
            new_overtime_minutes = compute_overtime(new_punch_out, emp["shift_end"], emp["shift_start"], date)
            new_half_day = compute_half_day(new_work_hours)
        else:
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False

    # Build changes dictionary (only fields that actually changed)
    changes = {}

    def check_diff(field_name: str, old_val: Any, new_val: Any):
        if old_val != new_val:
            changes[field_name] = {"from": old_val, "to": new_val}

    check_diff("status", record.get("status"), new_status)
    check_diff("punch_in", record.get("punch_in"), new_punch_in)
    check_diff("punch_out", record.get("punch_out"), new_punch_out)
    check_diff("work_hours", record.get("work_hours"), new_work_hours)
    check_diff("late_minutes", record.get("late_minutes", 0), new_late_minutes)
    check_diff("overtime_minutes", record.get("overtime_minutes", 0), new_overtime_minutes)
    check_diff("half_day", record.get("half_day", False), new_half_day)

    if not changes:
        raise HTTPException(
            status_code=422,
            detail="Request changes nothing",
        )

    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    history_entry = {
        "at": now_utc,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Optimistic concurrency control using history array length
    current_history = record.get("history", [])
    updated_doc = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "history": {"$size": len(current_history)},
        },
        {
            "$set": {
                "status": new_status,
                "punch_in": new_punch_in,
                "punch_out": new_punch_out,
                "work_hours": new_work_hours,
                "late_minutes": new_late_minutes,
                "overtime_minutes": new_overtime_minutes,
                "half_day": new_half_day,
            },
            "$push": {
                "history": history_entry,
            },
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated_doc:
        raise HTTPException(
            status_code=409,
            detail="Concurrent modification conflict",
        )

    return serialize_attendance(updated_doc)


# --------------------------------------------------------------------------- #
# Endpoints: Analytics
# --------------------------------------------------------------------------- #
def get_month_range(month: str) -> tuple[str, str, int, int, int]:
    try:
        year, m = map(int, month.split("-"))
        last_day = calendar.monthrange(year, m)[1]
    except Exception:
        raise HTTPException(status_code=422, detail="Invalid month format")
    return f"{month}-01", f"{month}-{last_day:02d}", year, m, last_day


@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"])
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    """Monthly attendance summary for one employee."""
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(
            status_code=404,
            detail="Employee not found",
        )

    start_date, end_date, year, m, last_day = get_month_range(month)

    # Count working days (Mon-Fri) from max(joined_on, first_of_month) to end_of_month
    emp_joined = date.fromisoformat(emp["joined_on"])
    eff_start = max(date(year, m, 1), emp_joined)
    month_end = date(year, m, last_day)

    working_days = 0
    if eff_start <= month_end:
        cur = eff_start
        while cur <= month_end:
            if cur.weekday() < 5:  # Mon=0, Fri=4
                working_days += 1
            cur += timedelta(days=1)

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": start_date, "$lte": end_date},
            }
        },
        {
            "$project": {
                "status": 1,
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                "half_day": {"$ifNull": ["$half_day", False]},
                "day_of_week": {
                    "$dayOfWeek": {
                        "$dateFromString": {
                            "dateString": "$date",
                            "format": "%Y-%m-%d",
                        }
                    }
                },
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$gte": ["$day_of_week", 2]},
                                    {"$lte": ["$day_of_week", 6]},
                                ]
                            },
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }
        },
    ]

    res = list(db.attendance_logs.aggregate(pipeline))
    if res:
        data = res[0]
        raw_present = data["present_days"]
        leave_days = data["leave_days"]
        late_count = data["late_count"]
        total_late = data["total_late_minutes"]
        total_ot = data["total_overtime_minutes"]
    else:
        raw_present = 0.0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0

    present_days = round_half_up(raw_present, 2)
    attendance_pct = (
        round_half_up((present_days / working_days) * 100, 2)
        if working_days > 0
        else None
    )

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": attendance_pct,
    }


def build_department_summary_pipeline(start_date: str, end_date: str, department: Optional[str] = None) -> list:
    match_stage = {"joined_on": {"$lte": end_date}}
    if department:
        match_stage["department"] = department

    return [
        {"$match": match_stage},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$gte": ["$date", start_date]},
                                    {"$lte": ["$date", end_date]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {
            "$project": {
                "department": 1,
                "emp_code": 1,
                "present_days": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {
                                        "$and": [
                                            {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                            {
                                                "$gte": [
                                                    {
                                                        "$dayOfWeek": {
                                                            "$dateFromString": {
                                                                "dateString": "$$l.date",
                                                                "format": "%Y-%m-%d",
                                                            }
                                                        }
                                                    },
                                                    2,
                                                ]
                                            },
                                            {
                                                "$lte": [
                                                    {
                                                        "$dayOfWeek": {
                                                            "$dateFromString": {
                                                                "dateString": "$$l.date",
                                                                "format": "%Y-%m-%d",
                                                            }
                                                        }
                                                    },
                                                    6,
                                                ]
                                            },
                                        ]
                                    },
                                    {"$cond": [{"$ifNull": ["$$l.half_day", False]}, 0.5, 1.0]},
                                    0.0,
                                ]
                            }
                        }
                    }
                },
                "late_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {"$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]},
                                    1,
                                    0,
                                ]
                            }
                        }
                    }
                },
                "total_late_minutes": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {"$ifNull": ["$$l.late_minutes", 0]}
                        }
                    }
                },
                "leave_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {"$cond": [{"$eq": ["$$l.status", "LEAVE"]}, 1, 0]}
                        }
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {"$cond": [{"$eq": ["$$l.status", "ON_DUTY"]}, 1, 0]}
                        }
                    }
                },
                "work_hours_sum": {
                    "$sum": {
                        "$map": {
                            "input": {
                                "$filter": {
                                    "input": "$logs",
                                    "as": "l",
                                    "cond": {
                                        "$and": [
                                            {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                            {"$ne": ["$$l.work_hours", None]}
                                        ]
                                    }
                                }
                            },
                            "as": "l",
                            "in": "$$l.work_hours"
                        }
                    }
                },
                "work_hours_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {
                                "$and": [
                                    {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$$l.work_hours", None]}
                                ]
                            }
                        }
                    }
                }
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$present_days"},
                "late_count": {"$sum": "$late_count"},
                "total_late_minutes": {"$sum": "$total_late_minutes"},
                "leave_count": {"$sum": "$leave_count"},
                "on_duty_count": {"$sum": "$on_duty_count"},
                "total_wh_sum": {"$sum": "$work_hours_sum"},
                "total_wh_count": {"$sum": "$work_hours_count"}
            }
        },
        {"$sort": {"_id": 1}}
    ]


@app.get("/analytics/departments/summary", tags=["analytics"])
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    """Department summary for a month."""
    start_date, end_date, _, _, _ = get_month_range(month)
    pipeline = build_department_summary_pipeline(start_date, end_date, department)
    results = list(db.employees.aggregate(pipeline))

    items = []
    for r in results:
        wh_count = r["total_wh_count"]
        avg_wh = (r["total_wh_sum"] / wh_count) if wh_count > 0 else None
        items.append({
            "department": r["_id"],
            "headcount": r["headcount"],
            "present_days": round_half_up(r["present_days"], 2),
            "avg_work_hours": round_half_up(avg_wh, 2),
            "late_count": r["late_count"],
            "total_late_minutes": r["total_late_minutes"],
            "leave_count": r["leave_count"],
            "on_duty_count": r["on_duty_count"],
        })

    return {"month": month, "items": items}


def build_late_leaderboard_pipeline(start_date: str, end_date: str, limit: int, department: Optional[str] = None) -> list:
    pipeline = [
        {
            "$match": {
                "date": {"$gte": start_date, "$lte": end_date},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                }
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        }
    ])
    return pipeline


@app.get("/analytics/leaderboard/late", tags=["analytics"])
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    """
    Ranks employees by total late_minutes in the month using standard competition ranking.
    """
    start_date, end_date, _, _, _ = get_month_range(month)
    pipeline = build_late_leaderboard_pipeline(start_date, end_date, limit, department)
    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


def build_department_trend_pipeline(department: str, from_date: str, to_date: str, dept_emp_codes: list) -> list:
    from_dt = datetime.strptime(from_date, "%Y-%m-%d")
    to_dt = datetime.strptime(to_date, "%Y-%m-%d")
    to_dt_plus_1 = to_dt + timedelta(days=1)

    return [
        {
            "$match": {
                "emp_code": {"$in": dept_emp_codes},
                "date": {"$gte": from_date, "$lte": to_date},
            }
        },
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": "$_id",
                "date_dt": {
                    "$dateFromString": {
                        "dateString": "$_id",
                        "format": "%Y-%m-%d",
                    }
                },
                "present_count": 1,
                "late_count": 1,
            }
        },
        {
            "$densify": {
                "field": "date_dt",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, to_dt_plus_1],
                },
            }
        },
        {
            "$project": {
                "date": {
                    "$dateToString": {
                        "format": "%Y-%m-%d",
                        "date": "$date_dt",
                    }
                },
                "date_dt": 1,
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
                "day_of_week": {"$dayOfWeek": "$date_dt"},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"d": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$d"]},
                                ]
                            }
                        }
                    },
                    {"$count": "hc"},
                ],
                "as": "hc_docs",
            }
        },
        {
            "$project": {
                "date": 1,
                "present_count": 1,
                "late_count": 1,
                "is_working_day": {
                    "$and": [
                        {"$gte": ["$day_of_week", 2]},
                        {"$lte": ["$day_of_week", 6]},
                    ]
                },
                "headcount": {"$ifNull": [{"$arrayElemAt": ["$hc_docs.hc", 0]}, 0]},
            }
        },
        {
            "$project": {
                "date": 1,
                "present_count": 1,
                "late_count": 1,
                "is_working_day": 1,
                "headcount": 1,
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                },
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
        {"$sort": {"date": 1}},
    ]


@app.get("/analytics/departments/{department}/trend", tags=["analytics"])
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    """
    Daily attendance trend for a department.
    Returns exactly one row per calendar day from from_date to to_date inclusive.
    """
    try:
        f_d = date.fromisoformat(from_date)
        t_d = date.fromisoformat(to_date)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="Invalid date format",
        )

    if t_d < f_d:
        raise HTTPException(
            status_code=422,
            detail="'to' date must be on or after 'from' date",
        )

    if (t_d - f_d).days > 92:
        raise HTTPException(
            status_code=422,
            detail="Date range cannot exceed 92 days",
        )

    # 404 if department has no employees
    dept_employees = list(db.employees.find({"department": department}, {"_id": 0, "emp_code": 1}))
    if not dept_employees:
        raise HTTPException(
            status_code=404,
            detail=f"Department '{department}' not found",
        )

    dept_emp_codes = [e["emp_code"] for e in dept_employees]
    pipeline = build_department_trend_pipeline(department, from_date, to_date, dept_emp_codes)
    rows = list(db.attendance_logs.aggregate(pipeline))

    items = []
    for r in rows:
        items.append({
            "date": r["date"],
            "is_working_day": r["is_working_day"],
            "headcount": r["headcount"],
            "present_count": round_half_up(r["present_count"], 2),
            "late_count": r["late_count"],
            "attendance_rate": round_half_up(r["attendance_rate"], 4),
            "moving_avg_7d": round_half_up(r["moving_avg_7d"], 4),
        })

    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Endpoints: Admin & Explain
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}", tags=["admin"])
def explain_endpoint(
    endpoint: str = Path(..., pattern=r"^(attendance_list|employee_monthly|department_summary|late_leaderboard|department_trend)$"),
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_param: Optional[str] = Query(None, alias="from"),
    to_param: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """
    MongoDB explain with executionStats verbosity for the main query or aggregation.
    """
    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "date_from must be <= date_to")
        query = {}
        if emp_code:
            query["emp_code"] = emp_code
        if date_from or date_to:
            query["date"] = {}
            if date_from:
                query["date"]["$gte"] = date_from
            if date_to:
                query["date"]["$lte"] = date_to
        if status:
            query["status"] = status
        skip = (page - 1) * page_size
        explain_res = (
            db.attendance_logs.find(query)
            .sort([("date", -1), ("emp_code", 1)])
            .skip(skip)
            .limit(page_size)
            .explain()
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain_res,
        }

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month are required for employee_monthly")
        start_date, end_date, _, _, _ = get_month_range(month)
        pipeline = [
            {
                "$match": {
                    "emp_code": emp_code,
                    "date": {"$gte": start_date, "$lte": end_date},
                }
            }
        ]
        explain_res = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain_res,
        }

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required for department_summary")
        start_date, end_date, _, _, _ = get_month_range(month)
        pipeline = build_department_summary_pipeline(start_date, end_date, department)
        explain_res = db.command(
            "explain",
            {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": explain_res,
        }

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required for late_leaderboard")
        start_date, end_date, _, _, _ = get_month_range(month)
        pipeline = build_late_leaderboard_pipeline(start_date, end_date, limit, department)
        explain_res = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain_res,
        }

    elif endpoint == "department_trend":
        if not department or not from_param or not to_param:
            raise HTTPException(422, "department, from, and to are required for department_trend")
        dept_employees = list(db.employees.find({"department": department}, {"_id": 0, "emp_code": 1}))
        dept_emp_codes = [e["emp_code"] for e in dept_employees]
        pipeline = build_department_trend_pipeline(department, from_param, to_param, dept_emp_codes)
        explain_res = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain_res,
        }
