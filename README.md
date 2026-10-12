# Employee Attendance & Analytics API

FastAPI and MongoDB application providing employee attendance tracking, manual corrections with an immutable audit trail, and analytics aggregation pipelines conforming to the OpenAPI 3.0.3 specification (`openapi.yaml`).

## Tech Stack
- **Python**: 3.11+ (tested on 3.13)
- **Framework**: FastAPI, Uvicorn
- **Database**: MongoDB 6.0+ (tested on MongoDB 7.0)
- **Driver & Validation**: PyMongo 4.7+, Pydantic v2

## Project Structure
- `app/main.py`: Complete single-file application containing all models, helpers, index initialization, and 13 endpoints.
- `REVIEW.md`: Comprehensive review documenting all 29 defects identified in the starter code and how they were resolved.
- `DECISIONS.md`: Design rationale covering index strategy, concurrency handling, tie-breaking, headcount aggregation, and 100x scaling.
- `sample_seed.py`: Script to seed initial sample documents into MongoDB.
- `requirements.txt`: Python package dependencies.
- `test_api.py`, `test_edge_cases.py`: Automated test suites covering all contract endpoints, business rules (R1–R10), and race conditions.

## Local Setup & Running

1. **Create and activate virtual environment:**
   ```bash
   python -m venv .venv
   # Windows:
   .venv\Scripts\activate
   # Linux / macOS:
   source .venv/bin/activate
   ```

2. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

3. **Configure Environment:**
   Set `MONGO_URI` and `MONGO_DB` environment variables (or create a local `.env` file):
   ```env
   MONGO_URI=mongodb://localhost:27017
   MONGO_DB=attendance_db
   ```

4. **Seed Database:**
   ```bash
   python sample_seed.py
   ```

5. **Start Application:**
   ```bash
   uvicorn app.main:app --port 8000 --reload
   ```

6. **Interactive API Documentation:**
   Once running, access Swagger UI at `http://localhost:8000/docs`.

## Running Automated Tests

Run the test suite with pytest:
```bash
pytest test_api.py test_edge_cases.py -v
```

All indexes are created idempotently at application startup, ensuring all list queries and analytics aggregations execute with `IXSCAN` and zero `COLLSCAN`.
