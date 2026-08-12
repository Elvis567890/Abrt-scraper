FROM python:3.11-slim

WORKDIR /app

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the app
COPY . .

# Start the server
CMD ["gunicorn", "--workers", "1", "--bind", "0.0.0.0:8000", "scraper:app"]
