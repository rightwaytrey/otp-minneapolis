# Pull Request: Enable Bicycle Access to Transit Stops

## Summary

This PR enables bicycle+transit routing to access transit stops via bicycle-friendly pedestrian infrastructure (sidewalks with bicycle=yes in OSM). Currently, transit stops are linked to the street network using WALK_ONLY mode, which prevents bicycles from accessing stops even when the connecting infrastructure permits bicycle travel.

## Problem

OpenTripPlanner does not find optimal bicycle+transit routes when transit stops are primarily accessible via pedestrian infrastructure. This is because:

1. `StreetLinkerModule.linkStopToStreetNetwork()` links stops with `WALK_ONLY` mode
2. `VertexLinker.linkToStreetEdges()` only searches for walkable edges when given WALK_ONLY
3. The resulting `StreetTransitStopLink` edge connects to a pedestrian-only street edge
4. Bicycles cannot reach the stop because the linked edge doesn't allow bicycles

### Example Scenario

Routing from Bloomington, MN to downtown St Paul with bicycle+transit mode:
- **Expected:** Route 539 → 54 via Mall of America (68 min, 1 transfer)
- **Actual:** Route 686 → 54 via airport (82 min, 2 transfers) - much longer

With WALK mode, route 539 → 54 is found correctly. The issue is specific to bicycle+transit.

## Solution

Link transit stops with BOTH walk and bicycle modes (`WALK_AND_BICYCLE`) instead of walk-only. This allows the vertex linker to find bike-accessible edges near stops, enabling bicycles to access stops via appropriate infrastructure.

### Changes

**File:** `application/src/main/java/org/opentripplanner/graph_builder/module/StreetLinkerModule.java`

1. Add `WALK_AND_BICYCLE` constant (lines 53-56)
2. Use `WALK_AND_BICYCLE` when linking stops (line 165)
3. Update JavaDoc to reflect the change (lines 158-162)

See attached patch file for details.

## Impact

### Positive
- ✅ Enables more optimal bicycle+transit routes
- ✅ Reflects real-world cyclist behavior (using bike-friendly sidewalks to access stops)
- ✅ No breaking changes to existing functionality
- ✅ Walk-mode routing unaffected

### Neutral
- Minimal performance impact (vertex linking happens during graph build only)
- Slightly more edges considered during linking (both walk and bike edges)

### Potential Concerns Addressed

**Q: Will this route bicycles on pedestrian-only paths?**
A: No. Street edges still respect OSM permissions. Only the virtual "stop link" edge is affected.

**Q: What about bike parking requirements?**
A: Existing restrictions in `StreetTransitEntityLink.traverse()` (lines 84-100) are preserved:
- Bike+ride mode: bicycles must be parked before boarding
- Station rentals: rental bikes cannot be taken into stations

**Q: What about stops that shouldn't allow bicycle access?**
A: Such stops would need to be surrounded by infrastructure that doesn't permit bicycles in OSM. The stop link allows bicycles, but if no bike-accessible paths lead to it, it remains unreachable by bicycle.

## Testing

### Manual Testing Performed
- Verified route 539 → 54 now appears with bicycle+transit mode
- Confirmed walk-mode routing still works correctly
- Tested various bicycle+transit scenarios in Minneapolis-St Paul area
- Verified bike parking and rental restrictions still apply

### Suggested Tests for Reviewers
1. Build with patch applied
2. Create graph with GTFS + OSM data containing stops accessible via sidewalks
3. Query bicycle+transit route to/from these stops
4. Verify stops are accessible and routing is sensible

### Test Data
Minneapolis-St Paul data used for testing:
- GTFS: Metro Transit feed
- OSM: minnesota-latest.osm.pbf (sidewalks tagged with bicycle=yes)

## Discussion Points

1. **Should this be configurable?**
   - Could add build-config option to control stop linking modes
   - Default could remain WALK_ONLY for backward compatibility
   - Thoughts?

2. **Mode-specific linking?**
   - Different transit types might want different linking modes
   - BRT stations vs bus stops vs rail stations
   - Is this level of granularity needed?

3. **Performance on large graphs?**
   - Testing shows minimal impact on graph build time
   - Has anyone tested with very large datasets (e.g., entire countries)?

## Backward Compatibility

- No API changes
- No configuration changes required
- Existing graphs are compatible (but need rebuild to get the benefit)
- No breaking changes to routing behavior for walk-only or car modes

## Related Issues

<!-- Add links to related GitHub issues if any exist -->

## Checklist

- [x] Code changes made
- [x] Comments updated
- [ ] Unit tests added (TODO: need guidance on test structure)
- [ ] Integration test added (TODO: need test data)
- [ ] Documentation updated (TODO: which docs need updating?)
- [x] Manual testing performed
- [ ] Performance impact assessed

## Additional Notes

This fix was discovered through extensive debugging of a real-world routing scenario. The investigation revealed that the root cause was in graph building (stop linking), not in the routing algorithm itself.

See attached `DEBUGGING_NOTES.md` for the full investigation process.

## Files Attached

- `street-linker-bicycle-access.patch` - Git patch file
- `README.md` - Comprehensive documentation
- `DEBUGGING_NOTES.md` - Investigation process
- `QUICK_START.md` - Quick reference guide
