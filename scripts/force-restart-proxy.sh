#!/bin/bash
# Force restart the Nominatim proxy with new code

echo "Stopping old proxy process..."
sudo pkill -9 -f nominatim_proxy.py
sleep 2

echo "Starting new proxy..."
cd /home/rwt/projects/otp-minneapolis/backend

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
echo "Testing new /autocomplete endpoint..."
curl -s "http://localhost:8001/autocomplete?text=100+main+minneapolis&size=1" | python3 -c "import sys,json; d=json.load(sys.stdin); print('✓ SUCCESS! Found', len(d.get('features', [])), 'results'); print('First result:', d.get('features', [{}])[0].get('properties', {}).get('name', 'N/A'))" 2>&1 || echo "✗ FAILED - endpoint not responding"

echo ""
echo "Proxy PID: $PROXY_PID"
echo "Logs: /tmp/nominatim-proxy.log"
