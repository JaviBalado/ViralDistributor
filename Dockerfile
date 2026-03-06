FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY src/ ./src/

# Create necessary directories
RUN mkdir -p auth/tokens videos/uploads logs

# Expose port
EXPOSE 8000

# Start the web dashboard
CMD ["uvicorn", "src.web.app:app", "--host", "0.0.0.0", "--port", "8000"]
