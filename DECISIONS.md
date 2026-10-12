# DECISIONS.md

1. **Indexes.**
I created unique indexes on `employees.emp_code` and `attendance_logs.(emp_code, date)` to enforce business uniqueness and prevent duplicate punch-ins. For fast pagination and queries, I added compound indexes on `attendance_logs.(date, emp_code)`, `(date, late_minutes)` for the leaderboard, and `employees.(department, joined_on)` for department analytics. I considered indexing `attendance_logs.date` alone but rejected it since `(date, emp_code)` already covers date prefix queries without the index overhead.

2. **Punch-in race.**
If two identical punch-in requests arrive at the exact same instant, both compute the attendance date and attempt `insert_one()`. Because of the unique index on `(emp_code, date)`, MongoDB's WiredTiger engine accepts only the first write. The second write immediately fails with PyMongo's `DuplicateKeyError`, which I catch and return as HTTP 409 (`"already punched in for this date"`).

3. **Ties.**
I used MongoDB's `$rank` window operator for standard competition ranking (1, 2, 2, 4). The `limit` filter is applied after ranking (`rank <= limit`), so if two employees tie at the cutoff rank, both are returned. This prevents arbitrary tie-breaking.

4. **Headcount.**
My pipeline starts from the `employees` collection (filtering `joined_on <= end_of_month`) and performs a `$lookup` for attendance logs. Employees with zero logs still contribute 1 to `headcount` during the department `$group` stage.

5. **One thing I would change for 100x data.**
I would pre-aggregate monthly attendance summaries into a dedicated rollup collection via MongoDB Change Streams on punch events, avoiding expensive multi-collection scans across millions of raw logs during read requests.
