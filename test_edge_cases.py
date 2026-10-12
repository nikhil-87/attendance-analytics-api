import concurrent.futures
from datetime import datetime, timezone
import pytest
from fastapi.testclient import TestClient

from app.main import app, db, init_indexes

test_client = TestClient(app)

def setup_module():
    init_indexes()

def test_truncation_to_whole_seconds():
    emp_code = "EMP0001"
    # Punch in with sub-second ms: 1783312500900 -> should truncate to 1783312500000
    ms_raw = 1783312500900
    ms_expected = 1783312500000

    db.attendance_logs.delete_one({"emp_code": emp_code, "date": "2026-07-06"})
    # But wait, EMP0001 has records in sample data. Let's use a temporary date
    test_date_ms = 1784000000900  # some date in 2026
    dt = datetime.fromtimestamp(1784000000900 / 1000, tz=timezone.utc)
    # Clear test log
    db.attendance_logs.delete_many({"emp_code": "EMP0001", "date": dt.date().isoformat()})

    res = test_client.post(
        "/attendance/punch-in",
        json={"emp_code": emp_code, "punched_at": test_date_ms, "status": "PRESENT"},
    )
    assert res.status_code == 201
    data = res.json()
    assert data["punch_in"] == (test_date_ms // 1000) * 1000

    # Clean up
    db.attendance_logs.delete_many({"emp_code": "EMP0001", "date": data["date"]})

def test_epoch_validation_bounds():
    # Value below 100000000000 (looks like seconds) -> 422
    res = test_client.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP0001", "punched_at": 1783312500, "status": "PRESENT"},
    )
    assert res.status_code == 422

    # Value above 4102444800000 -> 422
    res_high = test_client.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP0001", "punched_at": 5000000000000, "status": "PRESENT"},
    )
    assert res_high.status_code == 422

def test_concurrent_punch_ins():
    emp_code = "EMP0001"
    test_date = "2026-07-25"
    db.attendance_logs.delete_one({"emp_code": emp_code, "date": test_date})

    # Punch in instant for 2026-07-25 09:30:00 IST = 04:00:00 UTC
    dt = datetime(2026, 7, 25, 4, 0, 0, tzinfo=timezone.utc)
    ms = int(dt.timestamp() * 1000)

    results = []
    def make_punch():
        client = TestClient(app)
        return client.post("/attendance/punch-in", json={"emp_code": emp_code, "punched_at": ms, "status": "PRESENT"})

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(make_punch) for _ in range(5)]
        for f in concurrent.futures.as_completed(futures):
            results.append(f.result().status_code)

    assert results.count(201) == 1
    assert results.count(409) == 4

    # Clean up
    db.attendance_logs.delete_one({"emp_code": emp_code, "date": test_date})

def test_concurrent_punch_outs():
    emp_code = "EMP0001"
    test_date = "2026-07-26"
    db.attendance_logs.delete_one({"emp_code": emp_code, "date": test_date})

    # Create punch in
    dt_in = datetime(2026, 7, 26, 4, 0, 0, tzinfo=timezone.utc)
    ms_in = int(dt_in.timestamp() * 1000)
    in_res = test_client.post("/attendance/punch-in", json={"emp_code": emp_code, "punched_at": ms_in, "status": "PRESENT"})
    assert in_res.status_code == 201

    # Concurrent punch out
    dt_out = datetime(2026, 7, 26, 13, 0, 0, tzinfo=timezone.utc)
    ms_out = int(dt_out.timestamp() * 1000)

    results = []
    def make_punch_out():
        client = TestClient(app)
        return client.post("/attendance/punch-out", json={"emp_code": emp_code, "punched_at": ms_out})

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(make_punch_out) for _ in range(5)]
        for f in concurrent.futures.as_completed(futures):
            results.append(f.result().status_code)

    assert results.count(200) == 1
    assert results.count(409) == 4

    # Clean up
    db.attendance_logs.delete_one({"emp_code": emp_code, "date": test_date})

def test_competition_ranking_ties_and_limit():
    # Insert two employees with identical late minutes in a test month (2026-08)
    db.employees.delete_many({"emp_code": {"$in": ["EMP8001", "EMP8002", "EMP8003"]}})
    db.attendance_logs.delete_many({"emp_code": {"$in": ["EMP8001", "EMP8002", "EMP8003"]}})

    for code in ["EMP8001", "EMP8002", "EMP8003"]:
        test_client.post("/employees", json={
            "emp_code": code,
            "name": f"User {code}",
            "email": f"{code.lower()}@example.com",
            "department": "TieTestDept",
            "joined_on": "2026-08-01",
        })

    # EMP8001: 50 late minutes
    # EMP8002: 50 late minutes (tied for rank 1!)
    # EMP8003: 30 late minutes (should be rank 3!)
    for code, late in [("EMP8001", 50), ("EMP8002", 50), ("EMP8003", 30)]:
        db.attendance_logs.insert_one({
            "emp_code": code,
            "date": "2026-08-05",
            "status": "PRESENT",
            "punch_in": datetime(2026, 8, 5, 4, 50, 0, tzinfo=timezone.utc),
            "punch_out": datetime(2026, 8, 5, 13, 0, 0, tzinfo=timezone.utc),
            "late_minutes": late,
            "overtime_minutes": 0,
            "work_hours": 8.17,
            "half_day": False,
            "history": [],
        })

    # Query leaderboard with limit=1: both tied rank 1 employees must be returned!
    res_lim1 = test_client.get("/analytics/leaderboard/late?month=2026-08&limit=1&department=TieTestDept")
    assert res_lim1.status_code == 200
    items1 = res_lim1.json()["items"]
    assert len(items1) == 2  # Both rank 1!
    assert items1[0]["rank"] == 1
    assert items1[1]["rank"] == 1
    assert items1[0]["emp_code"] == "EMP8001"
    assert items1[1]["emp_code"] == "EMP8002"

    # Query with limit=3: rank 3 must be EMP8003 (rank 2 was skipped because of tie at 1!)
    res_lim3 = test_client.get("/analytics/leaderboard/late?month=2026-08&limit=3&department=TieTestDept")
    assert res_lim3.status_code == 200
    items3 = res_lim3.json()["items"]
    assert len(items3) == 3
    assert items3[0]["rank"] == 1
    assert items3[1]["rank"] == 1
    assert items3[2]["rank"] == 3  # Standard competition ranking: 1, 1, 3!
    assert items3[2]["emp_code"] == "EMP8003"

    # Clean up
    db.employees.delete_many({"emp_code": {"$in": ["EMP8001", "EMP8002", "EMP8003"]}})
    db.attendance_logs.delete_many({"emp_code": {"$in": ["EMP8001", "EMP8002", "EMP8003"]}})

def test_weekend_present_days_exclusion():
    # R7: Weekend records contribute to late/overtime totals, but NOT to present_days
    db.employees.delete_one({"emp_code": "EMP8004"})
    db.attendance_logs.delete_many({"emp_code": "EMP8004"})

    test_client.post("/employees", json={
        "emp_code": "EMP8004",
        "name": "Weekend Worker",
        "email": "weekend@example.com",
        "department": "Operations",
        "joined_on": "2026-08-01",
    })

    # Saturday 2026-08-01
    db.attendance_logs.insert_one({
        "emp_code": "EMP8004",
        "date": "2026-08-01",
        "status": "PRESENT",
        "punch_in": datetime(2026, 8, 1, 4, 0, 0, tzinfo=timezone.utc),
        "punch_out": datetime(2026, 8, 1, 13, 0, 0, tzinfo=timezone.utc),
        "late_minutes": 25,
        "overtime_minutes": 45,
        "work_hours": 9.0,
        "half_day": False,
        "history": [],
    })

    res = test_client.get("/analytics/employees/EMP8004/monthly?month=2026-08")
    assert res.status_code == 200
    data = res.json()
    # present_days must be 0.0 because August 1, 2026 was a Saturday!
    assert data["present_days"] == 0.0
    # But late and overtime must be counted
    assert data["total_late_minutes"] == 25
    assert data["total_overtime_minutes"] == 45
    assert data["late_count"] == 1

    # Clean up
    db.employees.delete_one({"emp_code": "EMP8004"})
    db.attendance_logs.delete_many({"emp_code": "EMP8004"})
