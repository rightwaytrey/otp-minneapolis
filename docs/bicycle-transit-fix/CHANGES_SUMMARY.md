# Complete Changes Summary - OTP Bicycle+Transit Fix

## Overview

This fix enables bicycle+transit routing with transfers in OpenTripPlanner by addressing two separate issues:
1. **Stop Access:** Allowing bicycles to access transit stops
2. **Stop Transfers:** Generating bicycle-mode transfers between stops

Both components are **required** for full functionality.

---

## 1. Code Changes (OTP Source)

### File: `application/src/main/java/org/opentripplanner/graph_builder/module/StreetLinkerModule.java`

#### Change 1.1: Add WALK_AND_BICYCLE constant

**Location:** After line 52 (after `WALK_ONLY` constant)

**Add:**
```java
private static final TraverseModeSet WALK_AND_BICYCLE = new TraverseModeSet(
  TraverseMode.WALK,
  TraverseMode.BICYCLE
);
```

**Purpose:** Define a mode set that allows both walking and bicycling for stop access.

---

#### Change 1.2: Modify stop linking mode

**Location:** Line 165 (in `linkStopToStreetNetwork` method)

**Before:**
```java
vertexLinker.linkVertexPermanently(
  tStop,
  WALK_ONLY,  // ← OLD
  LinkingDirection.BIDIRECTIONAL,
  ...
);
```

**After:**
```java
vertexLinker.linkVertexPermanently(
  tStop,
  WALK_AND_BICYCLE,  // ← NEW
  LinkingDirection.BIDIRECTIONAL,
  ...
);
```

**Purpose:** Allow bicycles to traverse the virtual link between street and stop.

---

#### Change 1.3: Update JavaDoc

**Location:** Lines 158-164 (method documentation)

**Before:**
```java
/**
 * Link a stop to the nearest "relevant" edges.
 * <p>
 * These are mostly walk edges but if a stop is used by a flex pattern it also needs to be
 * car-accessible. Therefore, flex stops are ensured to be connected to the car-accessible
 * edge. This may lead to several links being created.
 */
```

**After:**
```java
/**
 * Link a stop to the nearest "relevant" edges.
 * <p>
 * Stops are linked with both walk and bicycle modes to allow bicycle+transit trips to access
 * stops. If a stop is used by a flex pattern it also needs to be car-accessible. Therefore,
 * flex stops are ensured to be connected to the car-accessible edge. This may lead to several
 * links being created.
 */
```

**Purpose:** Document the new behavior for future maintainers.

---

## 2. Configuration Changes (Graph Building)

### File: `build-config.json`

**Location:** Graph building directory (where GTFS/OSM data resides)

**Complete file:**
```json
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
```

**Key section:**
```json
"transferRequests": [
  { "modes": "WALK" },
  { "modes": "BICYCLE" }  // ← ADD THIS
]
```

**Purpose:** Instructs `DirectTransferGenerator` to generate bicycle-mode transfers during graph building.

**What happens:**
- During graph build, OTP processes each mode in `transferRequests`
- For each mode, it calculates stop-to-stop transfers using street network
- Transfers are stored with mode information (`EnumSet<StreetMode>`)
- At routing time, only transfers matching the requested mode are used

**Without this:**
- All transfers are generated as WALK-only
- Bicycle routing cannot find transfers between stops
- Routes fail even though stops are accessible

---

## 3. Build & Deployment Steps

### 3.1 Apply Code Changes

```bash
cd ~/projects/opentripplanner/OpentripPlanner
git apply ~/projects/opentripplanner/bicycle-transit-fix/street-linker-bicycle-access.patch
```

Or manually edit the file as described above.

### 3.2 Build OTP

```bash
cd ~/projects/opentripplanner/OpentripPlanner
mvn package -DskipTests
```

### 3.3 Create Configuration

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

### 3.4 Rebuild Graph

**CRITICAL:** The graph MUST be rebuilt after adding bicycle to transferRequests.

```bash
rm ~/otp/graph.obj
cd ~/otp
java -Xmx4G -jar ~/projects/opentripplanner/OpentripPlanner/otp-shaded/target/otp-shaded-2.9.0-SNAPSHOT.jar --build --save .
```

### 3.5 Start OTP Server

```bash
cd ~/otp
java -Xmx2G -jar ~/projects/opentripplanner/OpentripPlanner/otp-shaded/target/otp-shaded-2.9.0-SNAPSHOT.jar --load --port 8090 .
```

---

## 4. Testing & Verification

### Test Query

```bash
curl -s -X POST http://localhost:8090/otp/gtfs/v1 \
  -H "Content-Type: application/json" \
  -d '{"query": "{ plan(from: {lat: 44.817, lon: -93.310}, to: {lat: 44.944, lon: -93.093}, date: \"2025-11-12\", time: \"09:00:00\", transportModes: [{mode: BICYCLE}, {mode: TRANSIT}], numItineraries: 30) { itineraries { duration numberOfTransfers legs { mode from { name } to { name } ... on Leg { route { shortName } } } } } }"}'
```

### Expected Results

**Success indicators:**
1. Routes using route 539 appear (home → MOA)
2. Routes using route 54 appear (MOA → St Paul)
3. **Bicycle transfers between stops** appear (e.g., between MOA gates)
4. Multi-route combinations work (539 → 686 → 54)

**Example working itinerary:**
```
Itinerary: 4406s (73 min)
  BICYCLE: Origin → 98th St W & Penn Ave S
  BUS route 539: 98th St W & Penn Ave S → MOA Transit Station Gate H
  BICYCLE: MOA Transit Station Gate H → MOA Transit Station Gate G  ← Bicycle transfer!
  BUS route 686: MOA Transit Station Gate G → MSP Terminal 1 Transit Station
  BUS route 54: MSP Terminal 1 Transit Station → 5th St & Cedar Station
  BICYCLE: 5th St & Cedar Station → Destination
```

---

## 5. Technical Details

### Code Path: Stop Access

1. **Graph Building:**
   - `StreetLinkerModule.linkStopToStreetNetwork()` called for each stop
   - `VertexLinker.linkVertexPermanently(tStop, WALK_AND_BICYCLE, ...)` creates link
   - `StreetTransitStopLink` edge created with modes `[WALK, BICYCLE]`

2. **Routing:**
   - Bicycle routing reaches street vertex
   - `StreetTransitEntityLink.traverse()` checks if bicycle mode allowed
   - Line 101: `yield buildState(s0, s1, pref);` - Allows bicycle traversal
   - Bicycle can now access the transit stop

### Code Path: Transfers

1. **Graph Building:**
   - `DirectTransferGenerator.buildGraph()` reads `transferRequests`
   - Line 380-396: `calculateDefaultTransfers()` called for each mode
   - Line 268: `new PathTransfer(from, to, distance, edges, EnumSet.of(mode))`
   - Transfers stored in `TimetableRepository` with mode information

2. **Routing:**
   - `RaptorRoutingRequestTransitData` constructor (line 100)
   - `transferIndex = raptorTransitData.getRaptorTransfersForRequest(request)`
   - `RaptorTransferIndex.create()` filters transfers by mode (line 80)
   - `.filter(transfer -> transfer.allowsMode(mode))`
   - Only bicycle-mode transfers available for bicycle routing

---

## 6. Files Affected

### Modified Files
- `OpentripPlanner/application/src/main/java/org/opentripplanner/graph_builder/module/StreetLinkerModule.java`

### Created Files
- `~/otp/build-config.json` (or wherever graph is built)

### Rebuilt Files
- `~/otp/graph.obj` (graph must be rebuilt)

---

## 7. Verification Checklist

- [ ] Code changes applied to `StreetLinkerModule.java`
- [ ] OTP rebuilt successfully (`mvn package`)
- [ ] `build-config.json` created with bicycle in `transferRequests`
- [ ] Graph rebuilt with new configuration
- [ ] OTP server started successfully
- [ ] Test query shows bicycle+transit routes
- [ ] Bicycle transfers between stops appear in results
- [ ] Route combinations work (e.g., 539 → 54 or 539 → 686 → 54)

---

## 8. Version Information

- **OTP Version:** 2.9.0-SNAPSHOT (dev-2.x branch)
- **Commit:** bd00bc57c9853272eaa255a04e31ebbb46aeb1a8
- **Date:** November 9, 2025
- **Author:** rwt (with Claude Code)

---

## 9. References

### Documentation Files
- `README.md` - Complete technical documentation
- `QUICK_START.md` - Quick setup guide
- `DEBUGGING_NOTES.md` - Investigation process
- `PR_TEMPLATE.md` - Upstream submission template
- `INDEX.md` - Documentation index

### Patch File
- `street-linker-bicycle-access.patch` - Git patch for code changes

### OTP Code References
- `StreetLinkerModule.java:165` - Stop linking (modified)
- `DirectTransferGenerator.java:268` - Transfer generation
- `RaptorTransferIndex.java:80` - Transfer filtering
- `StreetTransitEntityLink.java:101` - Bicycle traversal

---

## 10. Important Notes

### Both Changes Required

**The fix requires BOTH components:**
1. Code change enables stop **access**
2. Config change enables stop **transfers**

Without code change: Bicycles cannot access stops at all.
Without config change: Bicycles can access stops but cannot transfer between routes.

### Graph Rebuild Required

The graph MUST be rebuilt after configuration changes. Simply restarting OTP with a modified config file will NOT work - the transfers are baked into the graph during building.

### Date Validation

Ensure test queries use valid service dates in your GTFS data. Check `calendar.txt` and `calendar_dates.txt` for service exceptions.

---

## Questions or Issues?

See documentation files in `~/projects/opentripplanner/bicycle-transit-fix/` or create an issue on the OTP GitHub repository.
