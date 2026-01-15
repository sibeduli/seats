FROM python:3.11-slim

# Set timezone to GMT+7 (Asia/Jakarta)
ENV TZ=Asia/Jakarta
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code (will be overridden by volume mount in dev)
COPY . .

# Create directory for SQLite database
RUN mkdir -p /app/instance

# Expose port
EXPOSE 6666

# Environment variables
ENV FLASK_APP=app.py
ENV FLASK_ENV=production

# Run with gunicorn for production
# Use --reload for development with volume mounts
CMD ["gunicorn", "--bind", "0.0.0.0:6666", "--workers", "2", "--threads", "4", "--reload", "wsgi:app"]
