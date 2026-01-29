#!/bin/bash
# Start Nominatim Proxy Service

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PROJECT_ROOT="$( cd "$SCRIPT_DIR/.." && pwd )"
BACKEND_DIR="$PROJECT_ROOT/backend"

echo "Starting Nominatim Proxy..."

# Check if virtual environment exists
if [ ! -d "$BACKEND_DIR/venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv "$BACKEND_DIR/venv"
fi

# Activate virtual environment
source "$BACKEND_DIR/venv/bin/activate"

# Install/upgrade dependencies
echo "Installing dependencies..."
pip install -q --upgrade pip
pip install -q -r "$BACKEND_DIR/requirements.txt"

# Start the proxy
echo "Starting Nominatim proxy on port 8001..."
cd "$BACKEND_DIR"
python nominatim_proxy.py
