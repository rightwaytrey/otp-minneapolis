#!/bin/bash

# OTP GTFS Update Script
# Downloads fresh Metro Transit GTFS data and rebuilds the OTP graph

set -e  # Exit on error

# Get the repository root directory
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo "======================================"
echo "OTP GTFS Data Update"
echo "======================================"
echo ""

# Configuration
DATA_DIR="$REPO_ROOT/data"
GTFS_URL="https://svc.metrotransit.org/mtgtfs/gtfs.zip"

# Find the JAR
JAR=$(find "$REPO_ROOT/OpentripPlanner/otp-shaded/target" -name "otp-shaded-*.jar" -type f ! -name "*-sources.jar" 2>/dev/null | head -n 1)

if [ -z "$JAR" ]; then
    echo "Error: OTP JAR not found. Please run ./scripts/build.sh first."
    exit 1
fi

# Step 1: Backup current GTFS
echo "1. Backing up current GTFS file..."
if [ -f "$DATA_DIR/gtfs.zip" ]; then
    BACKUP_NAME="gtfs.zip.backup-$(date +%Y%m%d-%H%M%S)"
    cp "$DATA_DIR/gtfs.zip" "$DATA_DIR/$BACKUP_NAME"
    echo "   ✓ Backed up to: $BACKUP_NAME"
else
    echo "   No existing GTFS file to backup."
fi
echo ""

# Step 2: Download fresh GTFS
echo "2. Downloading fresh Metro Transit GTFS data..."
wget -q --show-progress "$GTFS_URL" -O "$DATA_DIR/gtfs.zip"
echo "   ✓ Downloaded successfully"
echo ""

# Step 3: Rebuild graph
echo "3. Rebuilding OTP graph (this may take a few minutes)..."
java -Xmx2G -jar "$JAR" --build --save "$DATA_DIR"
echo "   ✓ Graph rebuilt successfully"
echo ""

# Step 4: Check if OTP is running and offer to restart
echo "4. Checking if OTP server is running..."
OTP_PID=$(pgrep -f "otp-shaded.*--load" || echo "")

if [ -n "$OTP_PID" ]; then
    echo "   OTP server is running (PID: $OTP_PID)"
    echo ""
    echo "To apply the changes, you need to restart the OTP server:"
    echo "   1. Stop current server: kill $OTP_PID"
    echo "   2. Start new server: cd $REPO_ROOT && ./scripts/run.sh"
    echo ""
    echo "Or run this command to restart automatically:"
    echo "   kill $OTP_PID && sleep 2 && cd $REPO_ROOT && ./scripts/run.sh"
else
    echo "   OTP server is not running"
    echo "   Start it with: cd $REPO_ROOT && ./scripts/run.sh"
fi

echo ""
echo "======================================"
echo "GTFS Update Complete!"
echo "======================================"
