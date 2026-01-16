#!/bin/bash

# OpenTripPlanner Stop Script
# Gracefully stops the running OTP server

# Get the repository root directory
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

echo "Looking for OpenTripPlanner process..."

# Find OTP process by JAR name
PID=$(pgrep -f "otp-shaded.*jar")

if [ -z "$PID" ]; then
    echo "No OpenTripPlanner process found."
    exit 0
fi

echo "Found OTP process (PID: $PID)"
echo "Stopping gracefully..."

# Send SIGTERM for graceful shutdown
kill "$PID" 2>/dev/null

# Wait up to 30 seconds for graceful shutdown
for i in {1..30}; do
    if ! kill -0 "$PID" 2>/dev/null; then
        echo "OpenTripPlanner stopped successfully."
        exit 0
    fi
    sleep 1
done

# If still running after 30 seconds, force kill
echo "Process did not stop gracefully, forcing shutdown..."
kill -9 "$PID" 2>/dev/null

if ! kill -0 "$PID" 2>/dev/null; then
    echo "OpenTripPlanner stopped (forced)."
    exit 0
else
    echo "Error: Could not stop OpenTripPlanner."
    exit 1
fi
