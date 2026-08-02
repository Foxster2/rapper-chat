# Use official lightweight Python image
FROM python:3.13-slim

# Prevent Python from writing .pyc files & buffer stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Set working directory inside container
WORKDIR /app

# Install system dependencies (needed for compiling python dependencies if required)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies first (for docker layer caching)
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install --no-cache-dir gunicorn

# Copy project files
COPY . /app/

# Collect static files (optional for production setups)
# RUN python manage.py collectstatic --noinput

# Expose port
EXPOSE 8000

# Default command to run migrations, collect static files, and launch Gunicorn on dynamic PORT.
#
# --threads 8 is what makes SSE usable: it promotes the worker class from `sync`
# (one request per process, start to finish) to `gthread`, so a long-running
# /stream/ response no longer blocks every other request. It also keeps the
# arbiter heartbeat alive during a stream, which the sync worker does not do --
# under `sync` any reply exceeding --timeout is killed mid-generation.
#
# Sizing: 2 x 8 = 16 concurrent requests. Each worker is a full Django+LangChain
# process (~150-250MB), so if the container hits its memory limit, prefer
# --workers 1 --threads 12 over adding processes; this workload is I/O-bound.
CMD ["sh", "-c", "python manage.py migrate --noinput && python manage.py collectstatic --noinput && gunicorn rapper.wsgi:application --bind 0.0.0.0:${PORT:-8000} --workers 2 --threads 8 --timeout 120 --access-logfile - --error-logfile -"]
