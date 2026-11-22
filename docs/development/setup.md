# Local Development and Infrastructure Setup

## 1. Environment Configuration

Copy the template environment file and generate secure keys before starting services:

```bash
cp .env.example .env
```
Generate a fresh Fernet key and operator bcrypt password hash inside the backend virtual environment:
```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
python -c "from app.core.security import hash_password; print(hash_password('your-strong-password'))"
```
Update CREDENTIAL_ENCRYPTION_KEYS, OPERATOR_PASSWORD_HASH, and APP_SECRET_KEY in .env.

## 2. Running the Stack with Docker Compose
Start PostgreSQL, Redis, Qdrant, FastAPI, and the Celery worker:

```bash
docker compose up -d --build
```
Apply database migrations inside the API container:

```bash
docker compose exec api alembic upgrade head
```

If host port 5432 or 6379 is already occupied on your machine, override POSTGRES_HOST_PORT or REDIS_HOST_PORT in .env.

## 3. Verifying Service Health
Check public liveness:

```bash
curl -i http://localhost:8000/api/v1/health/live
```
Authenticate and check protected readiness:

```bash
TOKEN=$(curl -s -X POST http://localhost:8000/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"your-strong-password"}' | python -c "import sys, json; print(json.load(sys.stdin)['access_token'])")
curl -i -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/v1/health/ready
```
Verify the Celery worker responds to a ping:

```bash
docker compose exec worker celery -A app.workflows.celery_app:celery_app inspect ping
```