# REVIEW.md

List every defect you found in the starter `app/main.py` (helpers and endpoints).

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `health` (lines 72-74) | Never pings MongoDB; returns HTTP 200 `{"status": "ok"}` even if database is offline or unreachable. | Stop MongoDB service; `/health` still returns 200 instead of 503. | Added `db.command("ping")` within try/except block; raises `HTTPException(503, "Database unavailable")` on failure. |
| 2 | `create_employee` (lines 79-80) | Uses check-then-insert (`find_one` followed by `insert_one`), creating a critical race condition under concurrent requests. | Run concurrent POST `/employees` requests with identical `emp_code`; multiple duplicates get inserted. | Created unique index on `employees.emp_code` at startup; catch `DuplicateKeyError` and raise HTTP 409. |
| 3 | `create_employee` (line 82) | Generates `created_at` as naive local datetime via `datetime.now()` without UTC timezone awareness. | Inspect MongoDB document: stored timestamp has no timezone, causing UTC offset drift. | Use `datetime.now(timezone.utc).replace(microsecond=0)`. |
| 4 | `create_employee` (lines 83-85) | Returns raw BSON datetime / string instead of integer epoch milliseconds in response. | Response validation against `openapi.yaml` fails on `created_at` format. | Converted BSON datetime to integer epoch milliseconds using `dt_to_epoch()`. |
| 5 | `EmployeeIn` (lines 53-61) | Missing input validation for `emp_code` regex (`^EMP\d{4,6}$`), `email`, name/department length limits. | Submitting invalid codes like `"abc"` or invalid email addresses succeeds with 201 instead of 422. | Added strict Pydantic `Field` patterns, constraints, and validators matching OpenAPI contract. |
| 6 | `EmployeeIn` (lines 58-59) | No validation that `shift_start` differs from `shift_end`. | Submitting `shift_start="09:30"` and `shift_end="09:30"` returns 201 instead of 422. | Added Pydantic model validator ensuring `shift_start != shift_end`. |
| 7 | `EmployeeIn` (line 60) | No calendar date validation on `joined_on`. | Submitting invalid date string like `"2026-02-31"` is accepted. | Added validator using `date.fromisoformat(v)`. |
| 8 | `list_employees` (line 93) | Skip calculation is incorrect: `skip = page * page_size`. For page 1, skips 20 items and misses the entire first page. | Querying `GET /employees?page=1` omits the first 20 employees. | Changed calculation to `skip = (page - 1) * page_size`. |
| 9 | `list_employees` (line 94) | `total` counts entire collection via `count_documents({})`, ignoring active `department` query filter. | Filter by department; `total` returns total employees in company rather than department count. | Passed active query filter dict `count_documents(query)` so `total` reflects filtered count. |
| 10 | `list_employees` (line 95) | Query does not sort by `emp_code` ascending. | Returned employee order is non-deterministic / insertion order. | Added `.sort("emp_code", 1)` to the MongoDB query cursor. |
| 11 | `list_employees` (line 89) | Query parameters lack bounds validation (`page >= 1`, `1 <= page_size <= 100`). | Passing `page_size=500` or `page=0` is accepted instead of 422. | Used FastAPI `Query(..., ge=1, le=100)` for strict OpenAPI compliance. |
| 12 | `list_employees` (line 95) | Returns raw BSON datetime for `created_at` in employee items. | Serialization error or non-epoch integer in response JSON. | Mapped each employee document through `serialize_employee` converting `created_at` to epoch ms. |
| 13 | `punch_in` (line 101) | Does not verify employee exists; if `emp` is None, accessing `emp["shift_start"]` crashes with 500 `TypeError`. | POST `/attendance/punch-in` with unknown `emp_code="UNKNOWN"` returns 500 instead of 404. | Check if `emp` is None and raise `HTTPException(404, "Employee not found")`. |
| 14 | `punch_in` (lines 104-105) | Check-then-insert race condition allows simultaneous requests for the same date to insert duplicates. | Concurrent punch-in requests at the exact same instant both succeed with 201. | Created compound unique index on `attendance_logs.(emp_code, date)` at startup; catch `DuplicateKeyError` and return 409. |
| 15 | `punch_in` (line 102) | `punched_at` converted via `datetime.fromtimestamp()` without timezone produces naive local time. | Instant stored in DB reflects server local timezone instead of UTC BSON date. | Used `datetime.fromtimestamp(ms / 1000, tz=timezone.utc)` with sub-second truncation. |
| 16 | `punch_in` (line 102) | Sub-second milliseconds are not truncated to whole seconds (Rule R1). | `punched_at=1783312500900` stores millisecond remainder `.900` instead of `.000`. | Truncated epoch milliseconds via `(ms // 1000) * 1000` before conversion and storage. |
| 17 | `PunchInIn` (lines 65-66) | No validation on `punched_at` bounds (accepts second-looking values below 100000000000). | Passing seconds like `1783312500` accepted instead of 422. | Added `ge=100000000000, le=4102444800000` to Pydantic model. |
| 18 | `punch_in` (line 103) | Attendance date calculated from naive `ts.date().isoformat()` without IST conversion or R1 overnight shift logic. | Overnight shift punch-in at 01:30 IST assigns the next day rather than shift start date. | Implemented `compute_attendance_date` with IST conversion and overnight shift rollover. |
| 19 | `PunchInIn` (line 66) | Accepts invalid status values (such as `ABSENT` or `LEAVE`) for punch-in. | Submitting `status="ABSENT"` on punch-in creates an invalid absent record with punch times. | Restricted `status` in model regex to `^(PRESENT\|WFH\|ON_DUTY)$`. |
| 20 | `punch_in` (line 119) | Injects `"id": str(res.inserted_id)` into response object, violating OpenAPI contract. | Response schema validation fails due to unexpected `"id"` property. | Removed `id` field injection; serialized document strictly according to `AttendanceRecord` schema. |
| 21 | `punch_in` (lines 110, 120-121) | Returns raw BSON datetime instead of epoch milliseconds. | Client receives ISO string or date dict instead of integer epoch ms. | Serialized `punch_in` via `dt_to_epoch()` returning integer milliseconds. |
| 22 | `compute_late_minutes` (lines 33-37) | Uses `punch_in.replace(hour=h, minute=m)`, which breaks for overnight shifts past midnight. | Overnight shift (22:00-06:00) punched at 00:30 calculates delta against 22:00 of the same day (future), giving negative minutes. | Pinned scheduled start time to IST calendar date of attendance record, handling midnight crossover. |
| 23 | `compute_late_minutes` (lines 36-37) | Uses `minutes > 10` on floored minutes rather than checking if elapsed time was strictly greater than 10:00 (600 seconds). | Boundary timing at 09:40:01 IST produces incorrect grace window evaluation. | Checked `delta_seconds > 600` before computing floored elapsed minutes since `shift_start`. |
| 24 | `compute_work_hours` (line 41) | Uses Python's built-in `round(..., 2)` which implements banker's rounding (round to nearest even). | Value like `4.125` rounds to `4.12` instead of `4.13` (violating Rule R4 half-up requirement). | Implemented `round_half_up()` using `decimal.Decimal` with `ROUND_HALF_UP`. |
| 25 | `compute_overtime` (lines 45-47) | Does not enforce 30-minute minimum threshold (Rule R3 requires overtime < 30 min to be 0). | Punch-out 10 minutes after shift end returns `overtime_minutes = 10` instead of 0. | Added threshold check: return `diff_minutes if diff_minutes >= 30 else 0`. |
| 26 | `compute_overtime` (lines 45-47) | Assumes `shift_end` is always on the same date `date_str`, breaking overnight shifts where shift ends on next day. | Overnight shift (22:00-06:00) punched out at 06:40 IST compares against 06:00 yesterday (1440 min overtime). | Added overnight shift check: if `shift_end <= shift_start`, scheduled end date is `att_date + 1 day`. |
| 27 | `list_attendance` (line 144) | Loads entire collection into application memory with `list(db.attendance_logs.find(q))`. | On a ~100k-record dataset, exhausts process memory and causes severe latency / OOM crash. | Streamed records via MongoDB cursor using `.skip()` and `.limit()` on database engine. |
| 28 | `list_attendance` (line 145) | Performs sorting in Python memory via `docs.sort(...)` rather than database index. | COLLSCAN on database and heavy CPU load on Python server for 100k records. | Added database-level index scan `.sort([("date", -1), ("emp_code", 1)])`. |
| 29 | `list_attendance` (lines 127-142) | Does not validate `date_from <= date_to`, does not cap `page_size`, and injects unexpected `"id"` field. | Passing `date_from > date_to` succeeds with empty list instead of 422; response contains `"id"`. | Added date range validation (422 if `date_from > date_to`), `page_size <= 100` cap, and removed `"id"`. |

## Items Investigated and Decided NOT to be Defects

1. **Legacy documents missing `history` and `half_day` in `attendance_logs`:**
   - Sample document #9 (`EMP0002` on 2026-07-14) omits `history` and `half_day`.
   - I checked `DATA_MODEL.md` (Section 5, Document #9) and confirmed this is intentional schema evolution representing older historical records.
   - It is not a database defect; my application handles missing fields gracefully via `$ifNull` defaults (`history: []`, `half_day: False`, `late_minutes: 0`, `overtime_minutes: 0`).

2. **Exclusion of `_id` in API responses:**
   - The starter code explicitly removed `_id` before returning documents.
   - I confirmed against `openapi.yaml` and `DATA_MODEL.md` that MongoDB's internal `_id` must never be exposed; records are keyed naturally by `emp_code` and `date`.

3. **Absence of User Authentication / Authorization:**
   - The API accepts plain text `regularized_by` in `history` entries without authentication tokens or session validation.
   - As stated in the problem statement, authentication is explicitly out of scope for this assignment.
