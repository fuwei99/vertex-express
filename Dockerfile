FROM python:3.11-slim

WORKDIR /app

# Install dependencies
# Using the root requirements.txt
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the entire project
# .dockerignore will handle excluding unnecessary files
COPY . .

# Ensure credentials directory exists
RUN mkdir -p /app/credentials

# Switch to the app directory to run the application
# This ensures that 'main:app' and sibling imports work correctly
WORKDIR /app/app

# Use the default Hugging Face port 7860
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "7860"]