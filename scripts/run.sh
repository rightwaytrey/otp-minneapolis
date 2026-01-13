#!/bin/bash

# OpenTripPlanner Run Script
# Runs the pre-built OTP server and optionally the frontend dev server

# Get the repository root directory
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

# Find the shaded JAR (exclude sources JAR)
JAR=$(find OpentripPlanner/otp-shaded/target -name "otp-shaded-*.jar" -type f ! -name "*-sources.jar" 2>/dev/null | head -n 1)

if [ -z "$JAR" ]; then
    echo "Error: OTP JAR not found. Please run ./scripts/build.sh first."
    exit 1
fi

# Check for flags and filter arguments
START_FRONTEND=false
OTP_PORT="${OTP_PORT:-8090}"  # Default to 8090, override with env var
FILTERED_ARGS=()

while [ $# -gt 0 ]; do
    case "$1" in
        --with-frontend)
            START_FRONTEND=true
            shift
            ;;
        --port)
            shift
            OTP_PORT="$1"
            shift
            ;;
        *)
            FILTERED_ARGS+=("$1")
            shift
            ;;
    esac
done

# Set data directory (default: ./data, override with first non-flag argument)
DATA_DIR="${FILTERED_ARGS[0]:-$REPO_ROOT/data}"

# Sync config files to data directory
CONFIG_DIR="$REPO_ROOT/config"
if [ -d "$CONFIG_DIR" ]; then
    cp "$CONFIG_DIR/"*.json "$DATA_DIR/" 2>/dev/null || true
fi

echo "======================================"
echo "Starting OpenTripPlanner..."
echo "Using JAR: $JAR"
echo "Data directory: $DATA_DIR"
echo "OTP Port: $OTP_PORT"

# Get network IPs for display
NETWORK_IPS=$(ip addr show 2>/dev/null | grep "inet " | grep -v "127.0.0.1" | awk '{print $2}' | cut -d'/' -f1 | grep -v "^172\." | head -2)

echo "======================================"
echo ""
echo "Access OTP API at:"
echo "  Local:   http://localhost:$OTP_PORT"
for IP in $NETWORK_IPS; do
    echo "  Network: http://$IP:$OTP_PORT"
done
echo ""

# Start frontend dev server if requested
if [ "$START_FRONTEND" = true ]; then
    OTPRR_DIR="${OTPRR_DIR:-$REPO_ROOT/../otprr/otp-react-redux}"

    if [ ! -d "$OTPRR_DIR" ]; then
        echo "Warning: Frontend directory not found at $OTPRR_DIR"
        echo "Set OTPRR_DIR environment variable to specify location."
    else
        echo "Starting otp-react-redux frontend on port 9967..."

        cd "$OTPRR_DIR"
        YAML_CONFIG="$OTPRR_DIR/port-config.yml" yarn start &
        FRONTEND_PID=$!
        cd "$REPO_ROOT"

        echo ""
        echo "Frontend dev server started (PID: $FRONTEND_PID)"
        echo "Access Frontend at:"
        echo "  Local:   http://localhost:9967"
        for IP in $NETWORK_IPS; do
            echo "  Network: http://$IP:9967"
        done
        echo ""
        echo "Note: Frontend may take a minute to compile..."
        echo ""

        # Cleanup function to kill frontend on exit
        trap "kill $FRONTEND_PID 2>/dev/null" EXIT
    fi
fi

# Run OTP with 2GB heap memory (override with JAVA_OPTS env var)
# Usage: ./scripts/run.sh [data-directory] [--with-frontend] [--port PORT]
java ${JAVA_OPTS:--Xmx2G} -jar "$JAR" --load "$DATA_DIR" --port "$OTP_PORT"
