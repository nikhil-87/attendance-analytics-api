import time
import pytest
from datetime import datetime, timezone
from fastapi.testclient import TestClient
from pymongo import MongoClient

import app.main
from app.main import app, client, db, init_indexes

test_client = TestClient(app)

def setup_module():
    # Initialize indexes
    init_indexes()

def test_health():
    res = test_client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}

def test_create_employee():
    # Clean up if exists
    db.employees.delete_one({"emp_code": "EMP9999"})
    payload = {
        "emp_code": "EMP9999",
        "name": "Test User",
        "email": "test@example.com",
        "department": "QA",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-01",
    }
    res = test_client.post("/employees", json=payload)
    assert res.status_code == 201
    data = res.json()
    assert data["emp_code"] == "EMP9999"
    assert "_id" not in data
    assert "id" not in data
    assert isinstance(data["created_at"], int)
    assert data["created_at"] >= 100000000000

    # Duplicate creation should return 409
    res_dup = test_client.post("/employees", json=payload)
    assert res_dup.status_code == 409

    # Equal shift_start and shift_end should return 422
    payload_invalid = dict(payload)
    payload_invalid["emp_code"] = "EMP9998"
    payload_invalid["shift_end"] = "09:30"
    res_inv = test_client.post("/employees", json=payload_invalid)
    assert res_inv.status_code == 422

def test_list_employees():
    res = test_client.get("/employees?page=1&page_size=5")
    assert res.status_code == 200
    data = res.json()
    assert "items" in data
    assert "total" in data
    assert data["page"] == 1
    assert data["page_size"] == 5
    assert len(data["items"]) <= 5
    # Verify sorting by emp_code asc
    codes = [e["emp_code"] for e in data["items"]]
    assert codes == sorted(codes)

    # Test department filter
    res_dept = test_client.get("/employees?department=Engineering")
    assert res_dept.status_code == 200
    dept_data = res_dept.json()
    for e in dept_data["items"]:
        assert e["department"] == "Engineering"
    assert dept_data["total"] >= len(dept_data["items"])

def test_punch_in_and_punch_out():
    emp_code = "EMP9999"
    # Punch in at 09:40:01 (10 min late) -> late_minutes = 10
    # 2026-07-20 09:40:01 IST = 2026-07-20 04:10:01 UTC
    dt_in = datetime(2026, 7, 20, 4, 10, 1, tzinfo=timezone.utc)
    ms_in = int(dt_in.timestamp() * 1000)

    # Clean up any log for this date
    db.attendance_logs.delete_one({"emp_code": emp_code, "date": "2026-07-20"})

    punch_in_res = test_client.post(
        "/attendance/punch-in",
        json={"emp_code": emp_code, "punched_at": ms_in, "status": "PRESENT"},
    )
    assert punch_in_res.status_code == 201
    in_data = punch_in_res.json()
    assert in_data["date"] == "2026-07-20"
    assert in_data["late_minutes"] == 10
    assert in_data["punch_out"] is None
    assert in_data["work_hours"] is None
    assert in_data["half_day"] is False
    assert in_data["history"] == []
    assert "_id" not in in_data
    assert "id" not in in_data

    # Duplicate punch-in for same date should return 409
    punch_in_dup = test_client.post(
        "/attendance/punch-in",
        json={"emp_code": emp_code, "punched_at": ms_in, "status": "PRESENT"},
    )
    assert punch_in_dup.status_code == 409

    # Punch out at 19:15:00 IST = 13:45:00 UTC (45 min overtime, 9h 35m = 9.58h)
    dt_out = datetime(2026, 7, 20, 13, 45, 0, tzinfo=timezone.utc)
    ms_out = int(dt_out.timestamp() * 1000)

    punch_out_res = test_client.post(
        "/attendance/punch-out",
        json={"emp_code": emp_code, "punched_at": ms_out},
    )
    assert punch_out_res.status_code == 200
    out_data = punch_out_res.json()
    assert out_data["work_hours"] == 9.58
    assert out_data["overtime_minutes"] == 45
    assert out_data["half_day"] is False
    assert out_data["punch_out"] == ms_out

    # Second punch out should return 409
    punch_out_dup = test_client.post(
        "/attendance/punch-out",
        json={"emp_code": emp_code, "punched_at": ms_out},
    )
    assert punch_out_dup.status_code == 409

def test_overnight_shift():
    # EMP0005 has shift 22:00 - 06:00
    # Punch in at 2026-07-21 01:30:00 IST = 2026-07-20 20:00:00 UTC
    # Since 01:30 < 06:00, date belongs to previous day (2026-07-20)
    db.attendance_logs.delete_one({"emp_code": "EMP0005", "date": "2026-07-20"})
    dt_in = datetime(2026, 7, 20, 20, 0, 0, tzinfo=timezone.utc)
    ms_in = int(dt_in.timestamp() * 1000)

    res = test_client.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP0005", "punched_at": ms_in, "status": "PRESENT"},
    )
    assert res.status_code == 201
    data = res.json()
    assert data["date"] == "2026-07-20"
    # Scheduled start 22:00 IST on 2026-07-20, punched at 01:30 on 2026-07-21 -> late 210 min
    assert data["late_minutes"] == 210

    # Punch out at 2026-07-21 06:35:00 IST = 2026-07-21 01:05:00 UTC (35 min overtime)
    dt_out = datetime(2026, 7, 21, 1, 5, 0, tzinfo=timezone.utc)
    ms_out = int(dt_out.timestamp() * 1000)
    res_out = test_client.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP0005", "punched_at": ms_out},
    )
    assert res_out.status_code == 200
    data_out = res_out.json()
    assert data_out["overtime_minutes"] == 35
    assert data_out["work_hours"] == 5.08
    assert data_out["half_day"] is False

def test_regularize_attendance():
    # Test regularization of EMP9999 record
    res = test_client.patch(
        "/attendance/EMP9999/2026-07-20",
        json={
            "reason": "Corrected punch in time after glitch",
            "regularized_by": "hr_manager",
            "punch_in": int(datetime(2026, 7, 20, 4, 0, 0, tzinfo=timezone.utc).timestamp() * 1000),  # 09:30 IST (on time)
        },
    )
    assert res.status_code == 200
    data = res.json()
    assert data["late_minutes"] == 0
    assert len(data["history"]) == 1
    hist = data["history"][0]
    assert hist["by"] == "hr_manager"
    assert hist["reason"] == "Corrected punch in time after glitch"
    assert "late_minutes" in hist["changes"]
    assert hist["changes"]["late_minutes"] == {"from": 10, "to": 0}

    # Request that changes nothing should return 422
    res_noop = test_client.patch(
        "/attendance/EMP9999/2026-07-20",
        json={
            "reason": "Attempting identical update",
            "regularized_by": "hr_manager",
            "punch_in": int(datetime(2026, 7, 20, 4, 0, 0, tzinfo=timezone.utc).timestamp() * 1000),
        },
    )
    assert res_noop.status_code == 422

def test_employee_monthly():
    res = test_client.get("/analytics/employees/EMP0001/monthly?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["emp_code"] == "EMP0001"
    assert data["month"] == "2026-07"
    assert data["working_days"] == 23  # July 2026 has 23 weekdays
    assert data["present_days"] >= 2.0
    assert data["attendance_pct"] is not None

def test_department_summary():
    res = test_client.get("/analytics/departments/summary?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["month"] == "2026-07"
    assert len(data["items"]) >= 3
    # Check Engineering
    eng = next((d for d in data["items"] if d["department"] == "Engineering"), None)
    assert eng is not None
    assert eng["headcount"] == 2
    assert eng["present_days"] == 3.0
    assert eng["avg_work_hours"] == 9.04

def test_late_leaderboard():
    res = test_client.get("/analytics/leaderboard/late?month=2026-07&limit=5")
    assert res.status_code == 200
    data = res.json()
    assert "items" in data
    if data["items"]:
        # First item should have rank 1
        assert data["items"][0]["rank"] == 1
        assert data["items"][0]["total_late_minutes"] > 0

def test_department_trend():
    res = test_client.get("/analytics/departments/Engineering/trend?from=2026-07-06&to=2026-07-12")
    assert res.status_code == 200
    data = res.json()
    assert data["department"] == "Engineering"
    assert len(data["items"]) == 7
    # July 6 is Monday
    assert data["items"][0]["date"] == "2026-07-06"
    assert data["items"][0]["is_working_day"] is True
    assert data["items"][0]["headcount"] == 1
    assert data["items"][0]["attendance_rate"] == 1.0
    # July 11 is Saturday
    assert data["items"][5]["is_working_day"] is False
    assert data["items"][5]["attendance_rate"] is None

def test_admin_explain():
    # Test explain on all 5 endpoints
    for ep, params in [
        ("attendance_list", {"date_from": "2026-07-01", "date_to": "2026-07-31"}),
        ("employee_monthly", {"emp_code": "EMP0001", "month": "2026-07"}),
        ("department_summary", {"month": "2026-07"}),
        ("late_leaderboard", {"month": "2026-07"}),
        ("department_trend", {"department": "Engineering", "from": "2026-07-06", "to": "2026-07-12"}),
    ]:
        res = test_client.get(f"/admin/explain/{ep}", params=params)
        assert res.status_code == 200
        data = res.json()
        assert data["endpoint"] == ep
        assert "collection" in data
        assert "explain" in data
