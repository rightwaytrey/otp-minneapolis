# Debugging Notes - Bicycle+Transit Issue

## Investigation Journey

### Initial Symptom
Route 539 → 54 appeared with WALK mode (83 min) but not with BICYCLE mode when routing from home to downtown St Paul.

### Hypotheses Tested

#### ❌ Hypothesis 1: OSM sidewalks need bicycle=yes tags
- **Tested:** Added bicycle=yes to footways near route 539 stops (77 footways)
- **Result:** Did not fix the issue
- **Learning:** The street network allowed bicycles, but something else was blocking access

#### ❌ Hypothesis 2: Specific way has bicycle=no restriction
- **Tested:** Found way 905657288 with bicycle=no, changed to bicycle=yes
- **Result:** Did not fix the issue
- **Learning:** This wasn't the bottleneck

#### ❌ Hypothesis 3: MOA Transit Station Gate C inaccessible
- **Tested:** Added bicycle=yes to 21 footways near Gate C
- **Result:** Gate C became accessible, but route 539 → 54 still didn't appear
- **Learning:** Individual segments work (home → MOA via 539, MOA → StPaul via 54) but not the combination

#### ❌ Hypothesis 4: Transfer path between Gate H and Gate C blocked
- **Tested:** Verified bicycle path exists between gates (173m, bikeable)
- **Result:** Path exists and is bikeable
- **Learning:** Not a street network issue

#### ❌ Hypothesis 5: OTP filtering/cost limits removing the route
- **Tested:** Removed all itinerary filters, minimized costs, increased limits
- **Result:** Route 539 → 54 still not found
- **Learning:** Route was never generated, not just filtered out

#### ✅ Hypothesis 6: OTP fundamentally doesn't link stops for bicycle access
- **Investigated:** Read OTP source code, found `StreetLinkerModule.java:164`
- **Discovery:** Transit stops linked with WALK_ONLY mode
- **Confirmed:** This prevents bicycles from using the stop links
- **Solution:** Change to WALK_AND_BICYCLE mode

### Key Insights

1. **Stop links ≠ Street edges**
   - Stop links are virtual "last meter" connections between street and transit platform
   - Even if streets allow bicycles, the stop link itself was blocking them

2. **Walk mode worked because:**
   - Stops linked with WALK_ONLY
   - Walk access uses these links
   - Works fine for pedestrians

3. **Bicycle mode failed because:**
   - Stops linked with WALK_ONLY
   - Bicycles cannot traverse WALK_ONLY links (checked in StreetTransitEntityLink.traverse())
   - OTP routing algorithm can't find a path to the stop

4. **Why individual segments worked:**
   - Home → MOA via 539: Works because route 539 stop at Penn Ave is accessible (likely on a road)
   - MOA → StPaul via 54: After Gate C fix, this worked fine
   - But the COMBINATION didn't appear because OTP never explored route 539 in the full search

### Code Deep Dive

```
StreetLinkerModule.linkStopToStreetNetwork()
  ↓
  vertexLinker.linkVertexPermanently(tStop, WALK_ONLY, ...)
    ↓
    VertexLinker.linkToStreetEdges()
      ↓ Finds nearby walkable street edges
      ↓ Creates StreetTransitStopLink edge
      ↓
      StreetTransitStopLink (extends StreetTransitEntityLink)
        ↓
        traverse(State s0) checks s0.currentMode()
          ↓ If mode == BICYCLE:
            → Checks various bike restrictions
            → Allows traversal (lines 83-102)
          ↓ If mode == WALK:
            → Allows traversal (line 124)
```

The issue: Even though `StreetTransitEntityLink.traverse()` allows bicycles (lines 83-102), the `VertexLinker.linkToStreetEdges()` method (line 284) filters edges based on the TraverseModeSet:

```java
.filter(e -> e.canTraverse(traverseModes) && e.isReachableFromGraph())
```

If `traverseModes = WALK_ONLY`, then only edges that allow walking are considered. The stop link is then created to the nearest walkable edge. But when a bicycle tries to route to that stop, the algorithm searches for edges that bicycles can traverse. If the linked street edge doesn't allow bicycles, the stop is unreachable.

### Testing Methodology

1. **Comparative testing:** WALK mode vs BICYCLE mode
2. **Segment testing:** Individual route segments (home → MOA, MOA → StPaul)
3. **Direct testing:** Starting right next to stops
4. **OSM inspection:** Extracting and analyzing specific areas
5. **Source code reading:** Understanding OTP internals
6. **Iterative hypothesis testing:** Systematically eliminating possibilities

### Tools Used

- `osmium`: OSM file manipulation and extraction
- `grep`/`sed`: Text processing and OSM XML editing
- `curl`: GraphQL API testing
- Python: Data analysis and scripting
- Git: Version control and patch creation

## Files Modified (OSM)

All OSM modifications were attempts to fix the street network, which turned out to be unnecessary:

1. **98th St area (route 539 stops):** 77 footways + bicycle=yes tags
2. **Way 905657288:** Changed bicycle=no to bicycle=yes
3. **MOA Gate C area:** 21 footways + bicycle=yes tags

These changes don't hurt (they make the network more bike-friendly), but they weren't the root cause.

## Lesson Learned

When debugging routing issues:
1. Test individual segments first
2. Compare different modes (walk vs bike)
3. Look at the graph building process, not just the routing algorithm
4. Read the source code when behavior seems fundamentally wrong
5. Understand the distinction between street edges and virtual connection edges

The fix was ultimately a 3-line code change, but finding it required deep investigation.
