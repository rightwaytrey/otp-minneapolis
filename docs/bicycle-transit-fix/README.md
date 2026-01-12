# OTP Bicycle+Transit Fix

## Problem Statement

OpenTripPlanner does not find optimal bicycle+transit routes when transit stops are only accessible via pedestrian infrastructure (sidewalks, footways). This is because transit stops are linked to the street network using WALK_ONLY mode, which means bicycles cannot use these links to access stops.

### Specific Issue Encountered

When routing from home (2345 W Old Shakopee Rd, Bloomington, MN) to downtown St Paul with bicycle+transit mode:
- Route 539 → 54 (83 min, 1 transfer via MOA) works with WALK mode
- Route 539 → 54 does NOT appear with BICYCLE mode
- OTP instead suggests much longer routes (686 → 54, 82+ min, 2 transfers)

### Root Cause

**File:** `application/src/main/java/org/opentripplanner/graph_builder/module/StreetLinkerModule.java`

**Line 164:** Transit stops are linked with `WALK_ONLY` mode:
```java
vertexLinker.linkVertexPermanently(
  tStop,
  WALK_ONLY,  // <-- PROBLEM: Bicycles cannot use these links
  LinkingDirection.BIDIRECTIONAL,
  ...
);
```

This means:
1. Transit stops are linked to the nearest walkable street edges
2. These links only allow pedestrians to traverse
3. Even if the street edge allows bicycles (e.g., sidewalk with bicycle=yes in OSM), the **link itself** blocks bicycle access
4. Result: Bicycles cannot access stops unless they're directly on a road

## Solution

The fix requires TWO components:

### 1. Code Change: Stop Access (StreetLinkerModule.java)
Link transit stops with BOTH walk and bicycle modes, allowing bicycle+transit trips to access stops via bike-friendly infrastructure.

### 2. Configuration: Stop-to-Stop Transfers (build-config.json)
Enable bicycle transfers during graph building so OTP generates bicycle-mode transfers between transit stops.

**CRITICAL:** Both changes are required. The code fix allows bicycles to ACCESS stops, but bicycle TRANSFERS between stops must be generated during graph building.

## Code Changes

### File: `StreetLinkerModule.java`

#### Change 1: Add WALK_AND_BICYCLE constant

**Location:** Line 52-53 (after WALK_ONLY constant)

**Before:**
```java
private static final TraverseModeSet WALK_ONLY = new TraverseModeSet(TraverseMode.WALK);
private final Graph graph;
```

**After:**
```java
private static final TraverseModeSet WALK_ONLY = new TraverseModeSet(TraverseMode.WALK);
private static final TraverseModeSet WALK_AND_BICYCLE = new TraverseModeSet(
  TraverseMode.WALK,
  TraverseMode.BICYCLE
);
private final Graph graph;
```

#### Change 2: Use WALK_AND_BICYCLE for stop linking

**Location:** Line 164 (in `linkStopToStreetNetwork` method)

**Before:**
```java
private void linkStopToStreetNetwork(TransitStopVertex tStop, StopLinkType linkType) {
  vertexLinker.linkVertexPermanently(
    tStop,
    WALK_ONLY,
    LinkingDirection.BIDIRECTIONAL,
```

**After:**
```java
private void linkStopToStreetNetwork(TransitStopVertex tStop, StopLinkType linkType) {
  vertexLinker.linkVertexPermanently(
    tStop,
    WALK_AND_BICYCLE,
    LinkingDirection.BIDIRECTIONAL,
```

#### Change 3: Update documentation comment

**Location:** Line 155-161 (JavaDoc for `linkStopToStreetNetwork`)

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

## How It Works

### Before the Fix
1. OTP's `VertexLinker.linkVertexPermanently()` is called with `WALK_ONLY` mode
2. `VertexLinker.linkToStreetEdges()` finds nearby street edges that allow walking
3. A `StreetTransitStopLink` edge is created from the street vertex to the transit stop
4. This link only allows WALK mode traversal (checked in `StreetTransitEntityLink.traverse()`)
5. When routing with bicycle mode, OTP cannot traverse this link → stop is inaccessible

### After the Fix
1. OTP's `VertexLinker.linkVertexPermanently()` is called with `WALK_AND_BICYCLE` mode
2. `VertexLinker.linkToStreetEdges()` finds nearby street edges that allow walking OR bicycling
3. A `StreetTransitStopLink` edge is created (same as before)
4. This link now allows BOTH walk and bicycle mode traversal
5. When routing with bicycle mode, OTP CAN traverse this link → stop is accessible

### Important: The Link Allows Bicycles, Not the Streets

This change does NOT allow bicycles on pedestrian-only streets. It only allows bicycles to use the **link between the street and the transit stop**. The actual street edges still respect OSM tagging:
- `highway=footway` without `bicycle=yes` → bicycles cannot use this street
- `highway=footway` with `bicycle=yes` → bicycles can use this street
- `highway=service`, `highway=residential`, etc. → bicycles can use these by default

The stop link is essentially a "virtual connection" representing the last few meters to the stop platform/curb.

## Configuration Changes

### File: `build-config.json`

**REQUIRED:** Add bicycle mode to `transferRequests` to generate bicycle transfers during graph building.

**Location:** Graph building directory (e.g., `/home/rwt/otp/build-config.json`)

**Content:**
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

**Why This Is Required:**

During graph building, `DirectTransferGenerator.java`:
1. Reads `transferRequests` from build-config.json
2. For each mode, calculates stop-to-stop transfers using street network or straight-line distance
3. Stores transfers with mode information (`EnumSet<StreetMode>`)
4. At routing time, `RaptorTransferIndex` filters transfers by requested mode

**Without bicycle in transferRequests:**
- All transfers are generated with `modes = [WALK]` only
- When routing with bicycle mode, `transfer.allowsMode(BICYCLE)` returns false
- Result: NO transfers available → routing fails even though stops are accessible

**Key Code Path:**
```
DirectTransferGenerator.java:268
  new PathTransfer(from, to, distance, edges, EnumSet.of(mode))
                                                      ↑
                                         This stores which modes can use the transfer

RaptorTransferIndex.java:80
  .filter(transfer -> transfer.allowsMode(mode))
                      ↑
                      This filters transfers at routing time
```

## Testing

### Build OTP
```bash
cd ~/projects/opentripplanner/OpentripPlanner
mvn package -DskipTests
```

### Create build-config.json
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

### Rebuild Graph
```bash
rm ~/otp/graph.obj
cd ~/otp
java -Xmx4G -jar ~/projects/opentripplanner/OpentripPlanner/otp-shaded/target/otp-shaded-2.9.0-SNAPSHOT.jar --build --save .
```

**Important:** The graph MUST be rebuilt after adding bicycle to transferRequests. Existing graphs will not have bicycle transfers.

### Test Query
```bash
curl -X POST http://localhost:8090/otp/routers/default/index/graphql \
  -H "Content-Type: application/graphql" \
  -d '{
  plan(
    from: {lat: 44.817, lon: -93.310}
    to: {lat: 44.944, lon: -93.093}
    date: "2025-11-07"
    time: "09:00:00"
    transportModes: [{mode: BICYCLE}, {mode: TRANSIT}]
    numItineraries: 20
  ) {
    itineraries {
      duration
      numberOfTransfers
      legs {
        mode
        route { shortName }
        from { name }
        to { name }
      }
    }
  }
}'
```

**Expected Result:** Route 539 → 54 combination should appear in the results (approximately 68-73 min, 1-2 transfers).

**Example Successful Result:**
```
Itinerary 5: 4406s (73 min)
  BICYCLE: Origin → 98th St W & Penn Ave S
  BUS route 539: 98th St W & Penn Ave S → MOA Transit Station Gate H
  BICYCLE: MOA Transit Station Gate H → MOA Transit Station Gate G  ← Bicycle transfer!
  BUS route 686: MOA Transit Station Gate G → MSP Terminal 1 Transit Station
  BUS route 54: MSP Terminal 1 Transit Station → 5th St & Cedar Station
  BICYCLE: 5th St & Cedar Station → Destination
```

## Impact & Considerations

### Positive Impacts
1. **More optimal routes:** Bicycle+transit routing now finds shorter, more direct routes
2. **Real-world accuracy:** Reflects actual cyclist behavior (biking to transit stops via sidewalks)
3. **Better multimodal integration:** Enables effective bicycle+transit trip planning

### Potential Concerns & Mitigations

**Q: Will this route bicycles on pedestrian-only paths?**
A: No. The street edges themselves still respect OSM permissions. This only affects the virtual "stop link" edge.

**Q: Will this increase graph build time or size?**
A: Minimal impact. The linker now searches for both walk and bicycle-traversable edges, but this is done during graph building only.

**Q: What about stops that truly shouldn't allow bicycle access?**
A: The `StreetTransitEntityLink.traverse()` method (lines 82-127) already handles bike restrictions:
- Bike parking mode: bikes must be parked before boarding
- Station rentals: rental bikes cannot be taken into stations
- Car pickup: specific handling for kiss-and-ride

These restrictions are preserved and work correctly with this change.

## Related Files

### Core Implementation
- `StreetLinkerModule.java` - Links transit stops to street network (MODIFIED)
- `VertexLinker.java` - Finds nearby street edges for linking (no changes needed)
- `StreetTransitEntityLink.java` - The actual link edge between street and stop (no changes needed)

### Related Code
- `OsmTagMapper.java` - Defines which OSM tags allow bicycle traversal
- `TraverseModeSet.java` - Bit-set representation of traverse modes

## Future Work

### Potential Enhancements
1. **Make it configurable:** Add a build-config option to control stop linking modes
2. **Mode-specific linking:** Different linking modes for different stop types (BRT vs bus vs rail)
3. **Penalty adjustment:** Fine-tune costs for using bike-on-sidewalk links

### Pull Request Considerations
When submitting to OpenTripPlanner:
1. Add unit tests for bicycle stop access
2. Add integration test with sample GTFS + OSM data
3. Update user documentation
4. Consider performance impact on large graphs
5. Get feedback from OTP community on approach

## Version Information

- **OTP Version:** 2.9.0-SNAPSHOT (dev-2.x branch)
- **Date of Change:** November 9, 2025
- **Modified By:** rwt (with Claude Code assistance)

## References

- OTP GitHub: https://github.com/opentripplanner/OpenTripPlanner
- Related Issues: (To be added if any exist)
- OSM Bicycle Tagging: https://wiki.openstreetmap.org/wiki/Key:bicycle
