# OTP Minneapolis - Metro Transit Deployment

OpenTripPlanner deployment configured for Minneapolis-St. Paul Metro Transit with bicycle+transit routing enhancements.

## Project Structure

This is the **primary authoritative project** for OTP Minneapolis deployment. The project structure is:

```
/home/rwt/projects/
├── otp-minneapolis/           # PRIMARY - Full deployment project (this repo)
│   ├── OpentripPlanner/       # OTP backend source (git submodule)
│   ├── config/                # OTP configs (build-config.json, router-config.json)
│   ├── data/                  # GTFS, OSM, graph.obj
│   ├── scripts/               # Build, run, update scripts
│   └── frontend/              # Frontend config (port-config.yml)
│
├── otprr/                     # FRONTEND - otp-react-redux UI
│   └── otp-react-redux/       # React frontend app
│
└── _archived/                 # ARCHIVED - Old redundant copies
```

**Note**: The old `opentripplanner/` directory has been archived. All OTP source code, scripts, and configurations should now be managed through this `otp-minneapolis/` repository.

## Features

- **Bicycle+Transit Fix**: Enhanced routing that allows bicycles to access transit stops via pedestrian infrastructure
- **Real-time Updates**: GTFS-RT integration for trip updates, vehicle positions, and service alerts
- **Complete Deployment Scripts**: One-command setup for new servers
- **Docker Support**: Containerized deployment with automatic HTTPS
- **Frontend Configuration**: Pre-configured otp-react-redux for Minneapolis

## Quick Start

### Prerequisites

- **System**: Linux/Ubuntu (tested on Ubuntu 22.04+)
- **Java**: OpenJDK 21
- **Maven**: 3.8+
- **Git**: 2.30+
- **Disk Space**: ~2GB for OTP build, ~500MB for data

### Installation

```bash
# 1. Clone repository with submodules
git clone --recursive https://github.com/rightwaytrey/otp-minneapolis.git
cd otp-minneapolis

# 2. Install prerequisites (Ubuntu/Debian)
sudo apt update
sudo apt install -y openjdk-21-jdk maven wget

# 3. Download data and build graph
./scripts/setup.sh

# 4. Start OTP server
./scripts/run.sh
```

Access OTP at `http://localhost:8090`

## What's Included

This deployment repository contains:

- **OpentripPlanner/** (git submodule): Forked OTP source with bicycle+transit fix
- **config/**: OTP configuration files
  - `build-config.json`: Graph building settings
  - `router-config.json`: Runtime routing and real-time updater config
  - `logback-include-extensions.xml`: Debug logging for bicycle routing
- **scripts/**: Deployment and management scripts
- **docker/**: Docker and docker-compose files
- **frontend/**: otp-react-redux configuration
- **docs/**: Documentation including bicycle+transit fix details

## Custom Modifications

### Bicycle+Transit Routing Enhancement

This deployment includes a critical fix that enables bicycle+transit routing to work correctly when transit stops are only accessible via pedestrian infrastructure.

**Problem**: Standard OTP doesn't find optimal bicycle+transit routes because stops are linked with WALK_ONLY mode.

**Solution**: Links transit stops with both WALK and BICYCLE modes, and generates bicycle transfers during graph building.

**Details**: See [docs/bicycle-transit-fix/README.md](docs/bicycle-transit-fix/README.md)

**Requirements**:
1. Code change in `StreetLinkerModule.java` (already in OTP fork)
2. Configuration with `"modes": "BICYCLE"` in `transferRequests` (in build-config.json)

## Scripts Reference

| Script | Description |
|--------|-------------|
| `./scripts/setup.sh` | Initial setup: downloads data, builds OTP, creates graph |
| `./scripts/build.sh` | Build OTP from source |
| `./scripts/run.sh` | Start OTP server |
| `./scripts/build-graph.sh` | Rebuild graph from existing data |
| `./scripts/update-gtfs.sh` | Update GTFS data and rebuild graph |
| `./scripts/setup-ssl.sh` | Configure HTTPS with nginx or Caddy |

## Docker Deployment

### Quick Start

```bash
# Build and run with Docker Compose
cd docker
docker-compose up -d

# Check status
docker-compose ps

# View logs
docker-compose logs -f

# With SSL proxy (Caddy)
OTP_DOMAIN=otp.example.com docker-compose --profile with-ssl up -d
```

### Services

The Docker deployment includes:
- **OTP Backend**: Port 8090 (mapped to host)
- **Frontend**: Port 9967 (mapped to host)
- **Caddy** (optional): HTTPS reverse proxy with automatic SSL

### Nginx Reverse Proxy (Recommended for Production)

For production deployments, use nginx as a reverse proxy with SSL:

1. Copy nginx configuration:
```bash
sudo cp config/nginx/otp.conf /etc/nginx/sites-available/otp
sudo ln -s /etc/nginx/sites-available/otp /etc/nginx/sites-enabled/
```

2. Update server name and SSL certificate paths in the config

3. Test and reload:
```bash
sudo nginx -t
sudo systemctl reload nginx
```

The nginx config includes:
- SSL/TLS termination with HTTP/2
- Proxy to OTP backend (port 8090) at `/otp`
- Proxy to frontend (port 9967) at `/`
- **Geocoder CORS proxy** at `/photon/api` - proxies to photon.komoot.io with CORS headers to avoid browser blocking
- WebSocket support for frontend hot reload

See `config/nginx/otp.conf` for the complete configuration.

## Data Sources

- **GTFS**: Metro Transit (Minneapolis-St. Paul)
  - URL: https://svc.metrotransit.org/mtgtfs/gtfs.zip
  - Update frequency: Weekly
- **OSM**: Minnesota extract from Geofabrik
  - URL: https://download.geofabrik.de/north-america/us/minnesota-latest.osm.pbf
  - Update frequency: Daily
- **Real-time**: Metro Transit GTFS-RT feeds
  - Trip updates, vehicle positions, service alerts

## Frontend Setup

### Integrated Startup (Recommended)

The easiest way to run both backend and frontend together:

```bash
# Start both OTP backend and frontend in one command
./scripts/run.sh --with-frontend

# The script will:
# - Start OTP on port 8090
# - Start frontend on port 9967 (from ../otprr/otp-react-redux)
# - Kill frontend when OTP is stopped
```

Access points:
- **OTP Backend**: http://localhost:8090
- **Frontend**: http://localhost:9967
- **Nginx Proxy** (if configured): https://tre.hopto.org:9966

### Frontend Configuration (`frontend/port-config.yml`)

The frontend is configured to work with the nginx proxy for production deployments:

- **OTP API**: Routes through nginx at `/otp` (port 9966)
- **Geocoder**: Uses PHOTON via nginx CORS proxy at `/photon/api`
  - Configured with `baseUrl: https://tre.hopto.org:9966/photon`
  - PhotonGeocoder automatically appends `/api`, making requests to `/photon/api`
  - Nginx proxies to photon.komoot.io and adds CORS headers
- **Search bounds**: Minneapolis-St. Paul metro area (44.85-45.05 lat, -93.35 to -93.0 lon)

This configuration avoids CORS blocking issues when accessing the geocoder from mobile browsers.

### Manual Frontend Setup

If you prefer to run the frontend separately:

1. Clone otp-react-redux (if not already in ../otprr/):
```bash
cd ..
mkdir -p otprr
cd otprr
git clone https://github.com/opentripplanner/otp-react-redux.git
cd otp-react-redux
```

2. Copy config and start:
```bash
cp ../../otp-minneapolis/frontend/port-config.yml ./
YAML_CONFIG=port-config.yml yarn start
```

Frontend will be at `http://localhost:9967`

### Custom Frontend Location

If your frontend is at a different location, set the `OTPRR_DIR` environment variable:

```bash
OTPRR_DIR=/path/to/otp-react-redux ./scripts/run.sh --with-frontend
```

## Configuration

### Build Configuration (`config/build-config.json`)

```json
{
  "transitFeeds": [{"type": "gtfs", "source": "gtfs.zip"}],
  "osm": [{"source": "minneapolis-saint-paul_minnesota.osm.pbf"}],
  "transferRequests": [
    { "modes": "WALK" },
    { "modes": "BICYCLE" }  // Required for bicycle+transit routing
  ]
}
```

### Router Configuration (`config/router-config.json`)

- Bicycle routing: 5.0 m/s speed, 90 min max access/egress
- Transfer settings: 600s slack, max 5 transfers
- Real-time updaters: Metro Transit GTFS-RT (10s/30s intervals)

## Deployment to Production

See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) for detailed production deployment guide including:
- SSL/HTTPS setup
- Systemd service configuration
- Monitoring and logging
- Performance tuning
- Backup strategies

## Troubleshooting

### Graph Build Issues

**Problem**: Graph build fails or runs out of memory

**Solution**: Increase Java heap:
```bash
# Edit scripts/build-graph.sh or scripts/setup.sh
# Change -Xmx4G to -Xmx6G or higher
```

### Bicycle+Transit Routes Not Found

**Problem**: Bicycle+transit routing returns no results or suboptimal routes

**Check**:
1. Graph was built with bicycle in transferRequests
2. OTP fork includes bicycle+transit fix
3. Verify with: `grep "WALK_AND_BICYCLE" OpentripPlanner/application/src/main/java/org/opentripplanner/graph_builder/module/StreetLinkerModule.java`

### Real-time Updates Not Working

**Problem**: Real-time data not showing in results

**Check**:
1. OTP server logs for updater errors
2. Metro Transit GTFS-RT URLs are accessible
3. `router-config.json` has correct feed URLs

## Development

### Updating OTP Codebase

```bash
cd OpentripPlanner
git fetch upstream
git merge upstream/dev-2.x
# Resolve conflicts if any
cd ..
./scripts/build.sh
```

### Running Tests

```bash
cd OpentripPlanner
mvn test
```

## Version Information

- **OTP Version**: 2.9.0-SNAPSHOT (dev-2.x branch)
- **Release Tag**: v2.9.0-minneapolis-1
- **Fork**: https://github.com/rightwaytrey/OpenTripPlanner
- **Upstream**: https://github.com/opentripplanner/OpenTripPlanner

## Support & Documentation

- **OTP Documentation**: https://docs.opentripplanner.org/
- **OTP Community**: https://github.com/opentripplanner/OpenTripPlanner/discussions
- **Bicycle+Transit Fix**: See `docs/bicycle-transit-fix/`

## License

OpenTripPlanner is licensed under the LGPL v3. See `OpentripPlanner/LICENSE` for details.

## Acknowledgments

- OpenTripPlanner community
- Metro Transit for open data access
- OpenStreetMap contributors
