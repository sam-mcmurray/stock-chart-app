##
# Dockerfile for the FastAPI backend (scanner service).  This image
# installs the required Python dependencies and starts the Uvicorn
# server to expose the API on port 8000.  You should replace this
# stub with your actual scanner implementation if you already have
# one, but the structure will remain largely the same.
##

FROM python:3.12-slim

# Install runtime dependencies
WORKDIR /app
ARG REQUIREMENTS_FILE=requirements.txt
COPY requirements*.txt ./
RUN pip install --upgrade pip setuptools wheel \
 && pip install --no-cache-dir -r "$REQUIREMENTS_FILE"

# Copy the application code
COPY . ./

# Expose the port FastAPI listens on
EXPOSE 8000

# Start the Uvicorn server
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
