#!/bin/bash
# Restart Nominatim Proxy Service

echo "Stopping Nominatim proxy..."
sudo pkill -f nominatim_proxy.py
sleep 2

echo "Starting Nominatim proxy..."
cd /home/rwt/projects/otp-minneapolis
./scripts/start-nominatim-proxy.sh &

sleep 3
echo ""
echo "Checking status..."
curl -s http://localhost:8001/health || echo "Proxy not responding yet"
