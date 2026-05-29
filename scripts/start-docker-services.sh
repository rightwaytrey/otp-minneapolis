#!/bin/bash

# Safe Docker Services Startup Script
# Only starts runtime containers, not data import jobs
# Starts sequentially to avoid memory spikes that can freeze the system

set -e

# Get the repository root directory
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# Colors for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo "======================================"
echo -e "${BLUE}Starting OTP Minneapolis Docker Services${NC}"
echo "======================================"
echo ""

# Check if Docker is running
if ! docker info > /dev/null 2>&1; then
    echo -e "${RED}Error: Docker is not running${NC}"
    exit 1
fi

echo -e "${YELLOW}Step 1: Starting Pelias geocoding services${NC}"
echo "Starting only runtime containers (libpostal, elasticsearch, api, placeholder)"
echo "NOT starting data import containers (whosonfirst, openstreetmap, openaddresses)"
echo ""

cd "$REPO_ROOT/pelias"

# Start Pelias services one at a time to avoid memory spike
echo "  → Starting libpostal..."
docker compose up -d libpostal
sleep 5

echo "  → Starting elasticsearch..."
docker compose up -d elasticsearch
sleep 10

echo "  → Starting pelias-api..."
docker compose up -d api
sleep 3

echo "  → Starting placeholder..."
docker compose up -d placeholder
sleep 3

echo -e "${GREEN}✓ Pelias services started${NC}"
echo ""

echo -e "${YELLOW}Step 2: Starting main OTP services${NC}"
cd "$REPO_ROOT/docker"

echo "  → Starting OTP backend..."
docker compose up -d otp
sleep 5

echo "  → Starting Nominatim proxy..."
docker compose up -d nominatim-proxy
sleep 3

echo "  → Starting frontend..."
# Start frontend (production) - for dev mode, manually run: docker compose up -d frontend-dev
docker compose up -d frontend
sleep 3

echo -e "${GREEN}✓ Main OTP services started${NC}"
echo ""

# Display status
echo "======================================"
echo -e "${GREEN}All Docker services started!${NC}"
echo "======================================"
echo ""

echo "Container status:"
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}" | grep -E "NAME|otp|pelias|nominatim"

echo ""
echo "Memory usage:"
docker stats --no-stream --format "table {{.Name}}\t{{.MemUsage}}\t{{.MemPerc}}" | grep -E "NAME|otp|pelias|nominatim"

echo ""
echo -e "${YELLOW}Access points:${NC}"
echo "  OTP API:           http://localhost:8090"
echo "  Nominatim Proxy:   http://localhost:8001"
echo "  Pelias API:        http://localhost:4000"
echo "  Frontend:          http://localhost:9967"
echo ""
echo -e "${YELLOW}Note:${NC} Frontend may take a minute to compile on first start"
echo ""
echo "To stop all services:"
echo "  $0 stop"
echo ""
