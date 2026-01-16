#!/bin/bash

# Start OpenTripPlanner backend and frontend for local development
# Ctrl+C stops both services cleanly

set -e

# Get the repository root directory
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

# Configuration
OTP_PORT="${OTP_PORT:-8090}"
FRONTEND_PORT=9967
DATA_DIR="${DATA_DIR:-$REPO_ROOT/data}"
# Frontend can be in several locations - check in order:
# 1. OTPRR_DIR environment variable
# 2. ../otprr/otp-react-redux (user's fork)
# 3. ../otp-react-redux (sibling directory)
# 4. frontend/otp-react-redux (if cloned as submodule)
if [ -n "$OTPRR_DIR" ] && [ -d "$OTPRR_DIR" ]; then
    FRONTEND_DIR="$OTPRR_DIR"
elif [ -d "$REPO_ROOT/../otprr/otp-react-redux" ]; then
    FRONTEND_DIR="$REPO_ROOT/../otprr/otp-react-redux"
elif [ -d "$REPO_ROOT/../otp-react-redux" ]; then
    FRONTEND_DIR="$REPO_ROOT/../otp-react-redux"
elif [ -d "$REPO_ROOT/frontend/otp-react-redux" ]; then
    FRONTEND_DIR="$REPO_ROOT/frontend/otp-react-redux"
else
    FRONTEND_DIR=""
fi
FRONTEND_CONFIG="$REPO_ROOT/frontend/port-config.yml"

# Colors for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

# Find the shaded JAR (exclude sources JAR)
JAR=$(find OpentripPlanner/otp-shaded/target -name "otp-shaded-*.jar" -type f ! -name "*-sources.jar" 2>/dev/null | head -n 1)

if [ -z "$JAR" ]; then
    echo -e "${RED}Error: OTP JAR not found. Please run ./scripts/build.sh first.${NC}"
    exit 1
fi

if [ -z "$FRONTEND_DIR" ]; then
    echo -e "${RED}Error: otp-react-redux frontend not found.${NC}"
    echo "Please clone it with:"
    echo "  git clone https://github.com/opentripplanner/otp-react-redux.git ../otp-react-redux"
    echo ""
    echo "Or set OTPRR_DIR environment variable to its location."
    exit 1
fi

if [ ! -f "$FRONTEND_CONFIG" ]; then
    echo -e "${RED}Error: Frontend config not found at $FRONTEND_CONFIG${NC}"
    exit 1
fi

# Sync config files to data directory
CONFIG_DIR="$REPO_ROOT/config"
if [ -d "$CONFIG_DIR" ]; then
    cp "$CONFIG_DIR/"*.json "$DATA_DIR/" 2>/dev/null || true
fi

echo "======================================"
echo "Starting OTP Minneapolis"
echo "======================================"
echo "Backend JAR:  $JAR"
echo "Data dir:     $DATA_DIR"
echo "Frontend dir: $FRONTEND_DIR"
echo "Config file:  $FRONTEND_CONFIG"
echo "======================================"
echo ""

# Cleanup function to stop both services
cleanup() {
    echo ""
    echo -e "${YELLOW}Stopping services...${NC}"
    if [ ! -z "$OTP_PID" ] && kill -0 $OTP_PID 2>/dev/null; then
        echo "Stopping OTP backend (PID: $OTP_PID)..."
        kill $OTP_PID 2>/dev/null
        wait $OTP_PID 2>/dev/null
    fi
    if [ ! -z "$FRONTEND_PID" ] && kill -0 $FRONTEND_PID 2>/dev/null; then
        echo "Stopping frontend (PID: $FRONTEND_PID)..."
        kill $FRONTEND_PID 2>/dev/null
        wait $FRONTEND_PID 2>/dev/null
    fi
    echo -e "${GREEN}All services stopped.${NC}"
    exit 0
}

trap cleanup SIGINT SIGTERM

# Start OTP backend in background
echo -e "${YELLOW}[1/3] Starting OTP backend...${NC}"
java ${JAVA_OPTS:--Xmx2G} -jar "$JAR" --load "$DATA_DIR" --port "$OTP_PORT" > /tmp/otp-backend.log 2>&1 &
OTP_PID=$!

echo "OTP backend started (PID: $OTP_PID)"
echo "Logs: /tmp/otp-backend.log"
echo ""

# Wait for OTP to be ready (health check)
echo -e "${YELLOW}[2/3] Waiting for OTP to load graph (~30 seconds)...${NC}"
MAX_ATTEMPTS=60
ATTEMPT=0
HEALTH_URL="http://localhost:$OTP_PORT/otp"

while [ $ATTEMPT -lt $MAX_ATTEMPTS ]; do
    if curl -sf "$HEALTH_URL" > /dev/null 2>&1; then
        echo -e "${GREEN}OTP backend is ready!${NC}"
        break
    fi
    ATTEMPT=$((ATTEMPT + 1))
    if [ $((ATTEMPT % 5)) -eq 0 ]; then
        echo "  Still waiting... (${ATTEMPT}s elapsed)"
    fi
    sleep 1
done

if [ $ATTEMPT -eq $MAX_ATTEMPTS ]; then
    echo -e "${RED}Error: OTP backend failed to start within ${MAX_ATTEMPTS} seconds${NC}"
    echo "Check logs at /tmp/otp-backend.log"
    kill $OTP_PID 2>/dev/null
    exit 1
fi

echo ""

# Start frontend
echo -e "${YELLOW}[3/3] Starting frontend...${NC}"

# Copy the latest Minneapolis config to frontend directory
echo "Using config from: $FRONTEND_CONFIG"
cp "$FRONTEND_CONFIG" "$FRONTEND_DIR/port-config.yml"

cd "$FRONTEND_DIR"
YAML_CONFIG="$FRONTEND_DIR/port-config.yml" yarn start > /tmp/otp-frontend.log 2>&1 &
FRONTEND_PID=$!
cd "$REPO_ROOT"

echo "Frontend started (PID: $FRONTEND_PID)"
echo "Logs: /tmp/otp-frontend.log"
echo ""

# Display access information
echo "======================================"
echo -e "${GREEN}All services running!${NC}"
echo "======================================"
echo ""
echo "Backend API:"
echo "  http://localhost:$OTP_PORT"
echo ""
echo "Frontend:"
echo "  http://localhost:$FRONTEND_PORT"
echo ""
echo "Note: Frontend may take a minute to compile on first start"
echo ""
echo -e "${YELLOW}Press Ctrl+C to stop all services${NC}"
echo ""

# Wait indefinitely (services run in background)
# The trap will handle cleanup on Ctrl+C
wait
