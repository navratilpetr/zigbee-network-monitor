FROM python:3.12-alpine

# Install build dependencies, tzdata for proper timezone support
RUN apk add --no-cache tzdata curl jq bash

WORKDIR /app

# Install Python requirements
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY . /app/

# Make run script executable
RUN chmod +x /app/run.sh

# Persistent data directory
VOLUME ["/data"]

EXPOSE 8765

CMD ["/app/run.sh"]
