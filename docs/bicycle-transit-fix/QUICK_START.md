# Quick Start Guide - OTP Bicycle+Transit Fix

**IMPORTANT:** This fix requires BOTH code changes AND configuration changes!

## Step 1: Apply Code Changes

### Option 1: Using Git Patch
```bash
cd ~/projects/opentripplanner/OpentripPlanner
git apply ~/projects/opentripplanner/bicycle-transit-fix/street-linker-bicycle-access.patch
```

### Option 2: Manual Changes
Edit `application/src/main/java/org/opentripplanner/graph_builder/module/StreetLinkerModule.java`:

1. **Line 53** - Add constant after WALK_ONLY:
   ```java
   private static final TraverseModeSet WALK_AND_BICYCLE = new TraverseModeSet(
     TraverseMode.WALK,
     TraverseMode.BICYCLE
   );
   ```

2. **Line 164** - Change WALK_ONLY to WALK_AND_BICYCLE:
   ```java
   WALK_AND_BICYCLE,  // Changed from WALK_ONLY
   ```

3. **Line 158-162** - Update JavaDoc comment (optional but recommended)

## Step 2: Configure Bicycle Transfers

**CRITICAL:** Create or update `build-config.json` to generate bicycle transfers:

```bash
cat > ~/otp/build-config.json << 'EOF'
{
  "transitFeeds": [
    {
      "type": "gtfs",
      "source": "gtfs.zip"
    }
  ],
  "osm": [
    {
      "source": "minneapolis-saint-paul_minnesota.osm.pbf"
    }
  ],
  "transferRequests": [
    { "modes": "WALK" },
    { "modes": "BICYCLE" }
  ]
}
EOF
```

**Why Required:** Without bicycle in `transferRequests`, the graph will have no bicycle-mode transfers between stops, even though stops are accessible.

## Step 3: Build & Test

```bash
# 1. Build OTP
cd ~/projects/opentripplanner/OpentripPlanner
mvn package -DskipTests

# 2. Remove old graph
rm ~/otp/graph.obj

# 3. Rebuild graph with bicycle transfers (takes 5-10 minutes)
cd ~/otp
java -Xmx4G -jar ~/projects/opentripplanner/OpentripPlanner/otp-shaded/target/otp-shaded-2.9.0-SNAPSHOT.jar --build --save .

# 4. Start server
java -Xmx2G -jar otp-shaded-2.9.0-SNAPSHOT.jar --load --port 8090 /home/rwt/otp
```

## Verify Fix Works

Test query that should now show route 539 → 54:
```bash
curl -X POST http://localhost:8090/otp/routers/default/index/graphql \
  -H "Content-Type: application/graphql" \
  -d '{ plan(from: {lat: 44.817, lon: -93.310}, to: {lat: 44.944, lon: -93.093}, date: "2025-11-07", time: "09:00:00", transportModes: [{mode: BICYCLE}, {mode: TRANSIT}], numItineraries: 20) { itineraries { duration numberOfTransfers legs { mode route { shortName } from { name } to { name } } } } }'
```

Look for route 539 → 54 (or 539 → 686 → 54) with bicycle transfers between stops!

**Expected:** Bicycle legs should appear between transit stops (e.g., between MOA gates).

## Revert the Fix

```bash
cd ~/projects/opentripplanner/OpentripPlanner
git checkout application/src/main/java/org/opentripplanner/graph_builder/module/StreetLinkerModule.java
```

Then rebuild as above.
