#!/bin/bash
# Restart proxy: kill old process (may need sudo) and start new one

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PROJECT_ROOT="$( cd "$SCRIPT_DIR/.." && pwd )"
BACKEND_DIR="$PROJECT_ROOT/backend"

echo "Stopping old proxy process (may prompt for sudo password)..."
sudo pkill -9 -f nominatim_proxy.py
sleep 2

echo "Starting new proxy as current user..."
cd "$BACKEND_DIR"

# Check if venv exists
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
fi

# Activate and install deps
source venv/bin/activate
pip install -q -r requirements.txt

# Start proxy
echo "Starting proxy on port 8001..."
python nominatim_proxy.py > /tmp/nominatim-proxy.log 2>&1 &
PROXY_PID=$!

sleep 3

echo ""
echo "Proxy started with PID: $PROXY_PID"
echo "Logs: /tmp/nominatim-proxy.log"
echo ""

# Test the endpoint
echo "Testing /autocomplete endpoint..."
RESPONSE=$(curl -s "http://localhost:8001/autocomplete?text=100+main+minneapolis&size=1")

if echo "$RESPONSE" | grep -q '"type":"FeatureCollection"'; then
    echo "✓ SUCCESS! /autocomplete endpoint is working"
    FEATURE_COUNT=$(echo "$RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d.get('features', [])))" 2>/dev/null)
    echo "  Found $FEATURE_COUNT result(s)"
else
    echo "✗ FAILED - Response:"
    echo "$RESPONSE"
fi

echo ""
echo "You can now test in the OTP UI at https://tre.hopto.org:9966/"
