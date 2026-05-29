#!/bin/bash

# Stop all OTP Minneapolis Docker Services

set -e

# Get the repository root directory
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# Colors for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo "======================================"
echo -e "${BLUE}Stopping OTP Minneapolis Docker Services${NC}"
echo "======================================"
echo ""

echo -e "${YELLOW}Stopping main OTP services...${NC}"
cd "$REPO_ROOT/docker"
docker compose down

echo ""
echo -e "${YELLOW}Stopping Pelias services...${NC}"
cd "$REPO_ROOT/pelias"
docker compose down

echo ""
echo -e "${GREEN}✓ All Docker services stopped${NC}"
echo ""
