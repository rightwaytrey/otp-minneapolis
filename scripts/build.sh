#!/bin/bash

# OpenTripPlanner Build Script
# Builds the project and creates the shaded JAR with all dependencies

set -e  # Exit on error

# Get the repository root directory
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

echo "======================================"
echo "Building OpenTripPlanner..."
echo "======================================"
echo ""
echo "Repository root: $REPO_ROOT"
echo ""

# Check if OpentripPlanner submodule exists
if [ ! -d "OpentripPlanner" ]; then
    echo "Error: OpentripPlanner submodule not found."
    echo "Run: git submodule update --init --recursive"
    exit 1
fi

cd OpentripPlanner

# Build the project (skip tests for faster build, use 'mvn package' to include tests)
echo "Running Maven build (skipping tests for speed)..."
mvn clean package -DskipTests

echo ""
echo "======================================"
echo "Build Complete!"
echo "======================================"
echo "JAR location: OpentripPlanner/otp-shaded/target/otp-shaded-*.jar"
echo ""
echo "To run OTP, use: ./scripts/run.sh"
