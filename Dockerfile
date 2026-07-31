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

# Default command to run migrations, collect static files, and launch Gunicorn on dynamic PORT
CMD ["sh", "-c", "python manage.py migrate --noinput && python manage.py collectstatic --noinput && gunicorn rapper.wsgi:application --bind 0.0.0.0:${PORT:-8000}"]
