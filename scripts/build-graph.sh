#!/bin/bash

# OTP Graph Build Script
# Rebuilds the OTP graph from existing GTFS and OSM data

set -e  # Exit on error

# Get the repository root directory
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DATA_DIR="$REPO_ROOT/data"

echo "======================================"
echo "OTP Graph Build"
echo "======================================"
echo ""

# Check if data files exist
if [ ! -f "$DATA_DIR/gtfs.zip" ]; then
    echo "Error: GTFS data not found at $DATA_DIR/gtfs.zip"
    echo "Run: ./scripts/setup.sh"
    exit 1
fi

if [ ! -f "$DATA_DIR/minneapolis-saint-paul_minnesota.osm.pbf" ]; then
    echo "Error: OSM data not found at $DATA_DIR/minneapolis-saint-paul_minnesota.osm.pbf"
    echo "Run: ./scripts/setup.sh"
    exit 1
fi

# Find the JAR
JAR=$(find "$REPO_ROOT/OpentripPlanner/otp-shaded/target" -name "otp-shaded-*.jar" -type f ! -name "*-sources.jar" 2>/dev/null | head -n 1)

if [ -z "$JAR" ]; then
    echo "Error: OTP JAR not found. Please run ./scripts/build.sh first."
    exit 1
fi

# Backup existing graph if it exists
if [ -f "$DATA_DIR/graph.obj" ]; then
    echo "Backing up existing graph..."
    BACKUP_NAME="graph.obj.backup-$(date +%Y%m%d-%H%M%S)"
    mv "$DATA_DIR/graph.obj" "$DATA_DIR/$BACKUP_NAME"
    echo "✓ Backed up to: $BACKUP_NAME"
    echo ""
fi

echo "Building graph from:"
echo "  GTFS: $(du -h "$DATA_DIR/gtfs.zip" | cut -f1)"
echo "  OSM:  $(du -h "$DATA_DIR/minneapolis-saint-paul_minnesota.osm.pbf" | cut -f1)"
echo ""
echo "This may take 5-10 minutes..."
echo ""

# Build graph with 4GB heap
java -Xmx4G -jar "$JAR" --build --save "$DATA_DIR"

echo ""
echo "======================================"
echo "Graph Build Complete!"
echo "======================================"
echo "Graph size: $(du -h "$DATA_DIR/graph.obj" | cut -f1)"
echo ""
echo "To start OTP with the new graph:"
echo "  ./scripts/run.sh"
