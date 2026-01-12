# OTP Data Directory

This directory contains the transit and street network data used by OpenTripPlanner.

**Note**: Data files are NOT stored in git due to size. They are downloaded during setup.

## Data Files

### GTFS (Transit Schedules)
- **File**: `gtfs.zip`
- **Source**: Metro Transit (Minneapolis-St. Paul)
- **URL**: https://svc.metrotransit.org/mtgtfs/gtfs.zip
- **Size**: ~19 MB
- **Update Frequency**: Weekly (recommended)
- **Content**: Routes, stops, schedules, fares, shapes

### OSM (Street Network)
- **File**: `minneapolis-saint-paul_minnesota.osm.pbf`
- **Source**: Geofabrik (OpenStreetMap)
- **URL**: https://download.geofabrik.de/north-america/us/minnesota-latest.osm.pbf
- **Size**: ~99 MB (metro extract)
- **Update Frequency**: Monthly (recommended)
- **Content**: Streets, bike paths, pedestrian infrastructure

### Graph (Built Index)
- **File**: `graph.obj`
- **Generated**: By OTP during graph building
- **Size**: ~420 MB
- **Built From**: GTFS + OSM data + configuration
- **Rebuild**: After GTFS/OSM updates or config changes

## Downloading Data

### Automatic Download (Recommended)

Run the setup script:
```bash
cd ..
./scripts/setup.sh
```

This will:
1. Download GTFS from Metro Transit
2. Download OSM data
3. Build the graph automatically

### Manual Download

#### GTFS Data
```bash
wget https://svc.metrotransit.org/mtgtfs/gtfs.zip -O gtfs.zip
```

#### OSM Data

**Option A**: Download full Minnesota state and extract metro area
```bash
# Download full state
wget https://download.geofabrik.de/north-america/us/minnesota-latest.osm.pbf

# Extract metro area (requires osmium-tool)
sudo apt install osmium-tool
osmium extract --bbox=-93.8,44.7,-92.8,45.1 \
    minnesota-latest.osm.pbf \
    -o minneapolis-saint-paul_minnesota.osm.pbf

# Clean up
rm minnesota-latest.osm.pbf
```

**Option B**: Use full Minnesota file (no extraction)
```bash
wget https://download.geofabrik.de/north-america/us/minnesota-latest.osm.pbf \
    -O minneapolis-saint-paul_minnesota.osm.pbf
```

## Building the Graph

After downloading data, build the graph:

```bash
cd ..
./scripts/build-graph.sh
```

**Time**: 5-10 minutes
**Memory**: 4GB recommended (use `-Xmx4G`)

## Real-time Data

In addition to static GTFS, OTP fetches real-time updates:

| Feed | URL | Frequency |
|------|-----|-----------|
| Trip Updates | https://svc.metrotransit.org/mtgtfs/tripupdates.pb | 10s |
| Vehicle Positions | https://svc.metrotransit.org/mtgtfs/vehiclepositions.pb | 10s |
| Service Alerts | https://svc.metrotransit.org/mtgtfs/alerts.pb | 30s |

These are configured in `../config/router-config.json` and fetched automatically at runtime.

## Update Schedule

### Weekly: GTFS Update
Transit schedules change occasionally. Update weekly:
```bash
cd ..
./scripts/update-gtfs.sh
```

### Monthly: OSM Update
Street network changes are less frequent:
```bash
# Re-download OSM
cd data
rm minneapolis-saint-paul_minnesota.osm.pbf
wget https://download.geofabrik.de/north-america/us/minnesota-latest.osm.pbf

# Extract metro area
osmium extract --bbox=-93.8,44.7,-92.8,45.1 \
    minnesota-latest.osm.pbf \
    -o minneapolis-saint-paul_minnesota.osm.pbf
rm minnesota-latest.osm.pbf

# Rebuild graph
cd ..
./scripts/build-graph.sh
```

## Bounding Box

The metro area extract uses this bounding box:

```
West:  -93.8° (west of Minnetonka)
East:  -92.8° (east of St. Paul)
South:  44.7° (south of Burnsville)
North:  45.2° (north of Blaine)
```

To adjust, modify the `--bbox` parameter in scripts.

## Data Licensing

- **GTFS**: Metro Transit open data (public domain)
- **OSM**: OpenStreetMap ODbL license
- See: https://wiki.openstreetmap.org/wiki/Open_Database_License

## Troubleshooting

### GTFS Download Fails

Metro Transit URL may change. Check current URL:
- https://www.metrotransit.org/developers

### OSM Extract Fails

If osmium is not available, use the full Minnesota file (larger but works).

### Graph Build Fails

- **Out of memory**: Increase Java heap (`-Xmx4G` → `-Xmx6G`)
- **Invalid GTFS**: Check GTFS validity at https://gtfs-validator.netlify.app/
- **Missing files**: Ensure gtfs.zip and .osm.pbf exist

## File Sizes

| File | Size | Compressed | Notes |
|------|------|-----------|-------|
| gtfs.zip | 19 MB | - | Already compressed |
| *.osm.pbf | 99 MB | - | Already compressed (PBF format) |
| graph.obj | 420 MB | 150 MB (gzip) | Can be compressed for backup |

Total: ~538 MB uncompressed
