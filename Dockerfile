# Use a lightweight base image with Python and build tools
FROM python:3.11-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Install system dependencies
RUN apt-get update && \
    apt-get install -y tesseract-ocr tesseract-ocr-eng libglib2.0-0 libsm6 libxext6 libxrender-dev gcc && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy project files
COPY . /app/

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Command to run the app
CMD ["gunicorn", "app:app", "--bind", "0.0.0.0:$PORT"]
