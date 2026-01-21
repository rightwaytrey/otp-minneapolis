# Geocoder Debug Report

**Date:** 2026-01-20
**Status:** Root cause identified ✓

## Executive Summary

**Root Cause: Patches are not being applied in the running Docker container.**

The geocoder patch to display house numbers was created but never applied because the Dockerfile didn't include the patch-package step until recent uncommitted changes.

## Findings

### 1. Raw Photon API Testing ✓

Tested both public Photon and nginx proxy with query: `100 Washington Ave Minneapolis`

**Result:** Both APIs working correctly and returning identical data.

**Key Data Points:**
- House numbers ARE present in raw response: `"housenumber": "100"`
- Full address data available: street, city, state, postcode
- Response includes multiple results with proper geocoding
- Sample response structure:
  ```json
  {
    "type": "Feature",
    "properties": {
      "housenumber": "100",
      "street": "Washington Avenue South",
      "city": "Minneapolis",
      "state": "MN",
      "postcode": "55401",
      "name": "100 Washington Square"
    }
  }
  ```

**Conclusion:** This is NOT a data issue - Photon has all the necessary data including house numbers.

### 2. Patch Application Status ✗

**Current State:**
- Patch file exists: `frontend/patches/@opentripplanner+geocoder+3.0.5.patch`
- Patch content verified: Correctly combines `housenumber` + `street` fields
- Dockerfile has uncommitted changes that add patch application

**Problem:**
The Dockerfile in the last commit (`b5c1db8`) did NOT include these lines:
```dockerfile
# Copy patches and apply them
COPY patches /app/patches
RUN npx patch-package
```

These lines were added AFTER the container was built, meaning:
- The running container was built WITHOUT applying patches
- The geocoder is using the unpatched code
- House numbers are not being extracted from Photon's response

**Dockerfile Comparison:**

Last commit (b5c1db8):
```dockerfile
# Install dependencies
RUN yarn install

# Copy the custom configuration  <-- No patch steps
COPY port-config.yml /app/port-config.yml
```

Current uncommitted:
```dockerfile
# Install dependencies
RUN yarn install

# Copy patches and apply them  <-- NEW: Patch application added
COPY patches /app/patches
RUN npx patch-package

# Copy the custom configuration
COPY port-config.yml /app/port-config.yml
```

### 3. What the Patch Does

The patch modifies `@opentripplanner/geocoder/lib/geocoders/photon.js` to:

1. Extract `housenumber` and `street` from Photon properties
2. Combine them: `"100" + " " + "Washington Avenue South"` = `"100 Washington Avenue South"`
3. Replace the `street` field with this combined value
4. Use the modified properties when generating the label

**Expected behavior with patch:**
- Suggestion: "100 Washington Avenue South, Minneapolis, MN 55401"

**Current behavior without patch:**
- Suggestion: "Washington Avenue South, Minneapolis, MN 55401" (missing house number)

## Root Cause Summary

| Issue | Diagnosis |
|-------|-----------|
| House numbers not displaying | ✓ Confirmed - patch not applied |
| Photon data quality | ✓ Data is good, includes house numbers |
| Proxy configuration | ✓ Working correctly |
| Patch content | ✓ Correct implementation |
| **Problem** | **Dockerfile missing patch-package step** |

## Solution

**Rebuild the Docker container with the updated Dockerfile:**

```bash
# Commit the Dockerfile changes first
git add frontend/Dockerfile
git commit -m "Add patch-package to Dockerfile to apply geocoder fixes"

# Rebuild the frontend container
docker-compose build frontend
docker-compose up -d frontend
```

The updated Dockerfile will:
1. Copy the patches directory into the container
2. Run `npx patch-package` to apply all patches
3. The geocoder will then display house numbers correctly

## Additional Testing Needed

After rebuilding:
1. Test geocoder in browser with "100 Washington Ave Minneapolis"
2. Verify house numbers appear in suggestions
3. Test other specific addresses that were previously failing
4. Check browser network tab to confirm patched code is running

## Files Involved

- `frontend/Dockerfile` - Modified (uncommitted)
- `frontend/patches/@opentripplanner+geocoder+3.0.5.patch` - Patch file (committed)
- `frontend/port-config.yml` - Configuration (modified, uncommitted)

## Next Steps

1. Commit Dockerfile changes
2. Rebuild frontend container
3. Verify house numbers display correctly
4. Document any remaining issues (transit stops, POIs, etc.)
