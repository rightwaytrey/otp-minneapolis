#!/bin/bash

# OTP Initial Setup Script
# Downloads required data files (GTFS and OSM) for Minneapolis-St. Paul

set -e  # Exit on error

# Get the repository root directory
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DATA_DIR="$REPO_ROOT/data"

echo "======================================"
echo "OTP Minneapolis Initial Setup"
echo "======================================"
echo ""

# Check if data directory exists
if [ ! -d "$DATA_DIR" ]; then
    echo "Error: Data directory not found at $DATA_DIR"
    exit 1
fi

# Metro Transit GTFS
GTFS_URL="https://svc.metrotransit.org/mtgtfs/gtfs.zip"
GTFS_FILE="$DATA_DIR/gtfs.zip"

# Minnesota OSM data
OSM_URL="https://download.geofabrik.de/north-america/us/minnesota-latest.osm.pbf"
OSM_FILE="$DATA_DIR/minneapolis-saint-paul_minnesota.osm.pbf"

echo "This script will download:"
echo "  1. Metro Transit GTFS data (~19 MB)"
echo "  2. Minnesota OSM data (~100 MB)"
echo ""
read -p "Continue? (y/n) " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "Setup cancelled."
    exit 1
fi

echo ""
echo "======================================"
echo "Step 1/3: Downloading GTFS data"
echo "======================================"

if [ -f "$GTFS_FILE" ]; then
    echo "GTFS file already exists. Backing up..."
    mv "$GTFS_FILE" "$GTFS_FILE.backup-$(date +%Y%m%d-%H%M%S)"
fi

echo "Downloading from $GTFS_URL"
wget -q --show-progress "$GTFS_URL" -O "$GTFS_FILE"
echo "✓ GTFS download complete"
echo ""

echo "======================================"
echo "Step 2/3: Downloading OSM data"
echo "======================================"

if [ -f "$OSM_FILE" ]; then
    echo "OSM file already exists. Skipping download."
    echo "To re-download, delete: $OSM_FILE"
else
    echo "Downloading Minnesota OSM data from Geofabrik..."
    echo "This will download the full state (~100 MB)"

    TEMP_OSM="/tmp/minnesota-latest.osm.pbf"
    wget -q --show-progress "$OSM_URL" -O "$TEMP_OSM"

    # Check if osmium is installed for extracting metro area
    if command -v osmium &> /dev/null; then
        echo "Extracting Minneapolis-St. Paul metro area..."
        osmium extract --bbox=-93.8,44.7,-92.8,45.1 "$TEMP_OSM" -o "$OSM_FILE"
        rm "$TEMP_OSM"
        echo "✓ OSM extract complete"
    else
        echo "Warning: osmium-tool not found. Using full Minnesota file."
        echo "Install osmium-tool for smaller extract: sudo apt install osmium-tool"
        mv "$TEMP_OSM" "$OSM_FILE"
        echo "✓ OSM download complete (full state)"
    fi
fi

echo ""
echo "======================================"
echo "Step 3/3: Building OTP graph"
echo "======================================"

# Check if JAR exists
JAR=$(find "$REPO_ROOT/OpentripPlanner/otp-shaded/target" -name "otp-shaded-*.jar" -type f ! -name "*-sources.jar" 2>/dev/null | head -n 1)

if [ -z "$JAR" ]; then
    echo "OTP JAR not found. Building OTP first..."
    "$REPO_ROOT/scripts/build.sh"
    JAR=$(find "$REPO_ROOT/OpentripPlanner/otp-shaded/target" -name "otp-shaded-*.jar" -type f ! -name "*-sources.jar" 2>/dev/null | head -n 1)
fi

echo "Building graph (this may take 5-10 minutes)..."
java -Xmx4G -jar "$JAR" --build --save "$DATA_DIR"
echo "✓ Graph build complete"

echo ""
echo "======================================"
echo "Setup Complete!"
echo "======================================"
echo ""
echo "Data files downloaded to: $DATA_DIR"
echo "  - gtfs.zip (Metro Transit)"
echo "  - minneapolis-saint-paul_minnesota.osm.pbf"
echo "  - graph.obj (built graph)"
echo ""
echo "To start OTP server:"
echo "  ./scripts/run.sh"
echo ""
echo "To update GTFS data in the future:"
echo "  ./scripts/update-gtfs.sh"
