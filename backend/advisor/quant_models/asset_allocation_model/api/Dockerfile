FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better caching
COPY api/requirements.txt /app/requirements.txt

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the entire project (needed for SAA Model)
COPY . /app/

# Copy gcs.json credentials file if it exists (for local development)
# The file should be in api/gcs.json in the build context
RUN if [ -f api/gcs.json ]; then cp api/gcs.json /app/api/gcs.json && cp api/gcs.json /app/gcs.json; fi || true

# Set Python path to include SAA Model
ENV PYTHONPATH=/app:/app/SAA\ Model:$PYTHONPATH

# Expose port
EXPOSE 8080

# Set environment variable for port
ENV PORT=8080

# Run the Flask app
# Use gunicorn for production
CMD ["python", "-m", "gunicorn", "--bind", "0.0.0.0:8080", "--workers", "2", "--timeout", "300", "api.app:app"]

