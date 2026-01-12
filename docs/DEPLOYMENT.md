# OTP Minneapolis - Production Deployment Guide

Complete guide for deploying OTP Minneapolis to a production server.

## Table of Contents

1. [Server Requirements](#server-requirements)
2. [Initial Setup](#initial-setup)
3. [SSL/HTTPS Configuration](#sslhttps-configuration)
4. [Systemd Service](#systemd-service)
5. [Monitoring & Logging](#monitoring--logging)
6. [Maintenance & Updates](#maintenance--updates)
7. [Backup & Recovery](#backup--recovery)
8. [Performance Tuning](#performance-tuning)

## Server Requirements

### Minimum Specifications

- **CPU**: 2 cores
- **RAM**: 4GB (2GB for OTP, 2GB for system)
- **Storage**: 10GB
- **OS**: Ubuntu 22.04 LTS or later
- **Network**: Public IP with ports 80/443 open

### Recommended Specifications

- **CPU**: 4+ cores
- **RAM**: 8GB (4GB for OTP)
- **Storage**: 20GB SSD
- **OS**: Ubuntu 22.04 LTS
- **Network**: Static IP or domain with DNS

## Initial Setup

### 1. System Preparation

```bash
# Update system
sudo apt update && sudo apt upgrade -y

# Install prerequisites
sudo apt install -y openjdk-21-jdk maven wget git curl

# Create OTP user (optional but recommended)
sudo useradd -r -m -s /bin/bash otp
sudo su - otp
```

### 2. Clone Repository

```bash
cd /opt
sudo git clone --recursive https://github.com/rightwaytrey/otp-minneapolis.git
sudo chown -R otp:otp otp-minneapolis
cd otp-minneapolis
```

### 3. Download Data & Build

```bash
./scripts/setup.sh
```

This will:
- Download GTFS from Metro Transit
- Download OSM data for Minnesota
- Build OTP from source
- Build the graph

**Time estimate**: 15-30 minutes

### 4. Test Run

```bash
./scripts/run.sh
```

Access at `http://your-server-ip:8090/otp`

Press Ctrl+C to stop the test run.

## SSL/HTTPS Configuration

### Option A: Using the Setup Script (Recommended)

```bash
./scripts/setup-ssl.sh
```

Follow the prompts to choose between Caddy (automatic HTTPS) or Nginx (manual).

### Option B: Manual Caddy Setup

```bash
# Install Caddy
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update
sudo apt install -y caddy

# Configure Caddyfile
sudo tee /etc/caddy/Caddyfile > /dev/null <<'EOF'
otp.yourdomain.com {
    reverse_proxy localhost:8090
    log {
        output file /var/log/caddy/otp.log
    }
}
EOF

# Restart Caddy
sudo systemctl restart caddy
sudo systemctl enable caddy
```

### Option C: Manual Nginx Setup

```bash
# Install nginx and certbot
sudo apt install -y nginx certbot python3-certbot-nginx

# Get SSL certificate
sudo certbot --nginx -d otp.yourdomain.com

# Create nginx config
sudo tee /etc/nginx/sites-available/otp > /dev/null <<'EOF'
server {
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name otp.yourdomain.com;

    ssl_certificate /etc/letsencrypt/live/otp.yourdomain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/otp.yourdomain.com/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!MD5;

    location / {
        proxy_pass http://localhost:8090;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection 'upgrade';
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_cache_bypass $http_upgrade;
    }
}

server {
    listen 80;
    listen [::]:80;
    server_name otp.yourdomain.com;
    return 301 https://$server_name$request_uri;
}
EOF

# Enable site
sudo ln -s /etc/nginx/sites-available/otp /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

## Systemd Service

Create a systemd service for automatic startup and process management.

```bash
# Create service file
sudo tee /etc/systemd/system/otp.service > /dev/null <<'EOF'
[Unit]
Description=OpenTripPlanner Minneapolis
After=network.target

[Service]
Type=simple
User=otp
Group=otp
WorkingDirectory=/opt/otp-minneapolis
Environment="JAVA_OPTS=-Xmx4G"
ExecStart=/opt/otp-minneapolis/scripts/run.sh
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=otp

[Install]
WantedBy=multi-user.target
EOF

# Reload systemd
sudo systemctl daemon-reload

# Start OTP
sudo systemctl start otp

# Enable on boot
sudo systemctl enable otp

# Check status
sudo systemctl status otp
```

### Service Management

```bash
# Start/stop/restart
sudo systemctl start otp
sudo systemctl stop otp
sudo systemctl restart otp

# View logs
sudo journalctl -u otp -f

# View recent logs
sudo journalctl -u otp --since "1 hour ago"
```

## Monitoring & Logging

### Application Logs

OTP logs are available through journald:

```bash
# Follow logs
sudo journalctl -u otp -f

# Search logs
sudo journalctl -u otp | grep ERROR
```

### Log Rotation

Systemd handles log rotation automatically. Configure retention:

```bash
# Edit journald config
sudo nano /etc/systemd/journald.conf

# Set retention (e.g., 2 weeks)
SystemMaxUse=1G
MaxRetentionSec=2week
```

### Health Monitoring

Create a simple health check script:

```bash
sudo tee /usr/local/bin/otp-health-check.sh > /dev/null <<'EOF'
#!/bin/bash
RESPONSE=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:8090/otp/routers/default)
if [ "$RESPONSE" != "200" ]; then
    echo "OTP health check failed with status: $RESPONSE"
    systemctl restart otp
fi
EOF

sudo chmod +x /usr/local/bin/otp-health-check.sh

# Add to crontab (every 5 minutes)
echo "*/5 * * * * /usr/local/bin/otp-health-check.sh" | sudo crontab -
```

### Monitoring with Prometheus (Optional)

OTP exposes metrics at `/actuator/prometheus`. Set up Prometheus + Grafana for advanced monitoring.

## Maintenance & Updates

### Weekly: Update GTFS Data

```bash
# Update GTFS and rebuild graph
cd /opt/otp-minneapolis
./scripts/update-gtfs.sh

# Restart OTP
sudo systemctl restart otp
```

**Automate** with cron (every Sunday at 2 AM):

```bash
sudo crontab -e
# Add:
0 2 * * 0 cd /opt/otp-minneapolis && ./scripts/update-gtfs.sh && systemctl restart otp
```

### Monthly: Update OSM Data

```bash
cd /opt/otp-minneapolis/data

# Backup current OSM
mv minneapolis-saint-paul_minnesota.osm.pbf minneapolis-saint-paul_minnesota.osm.pbf.backup

# Download new OSM
wget https://download.geofabrik.de/north-america/us/minnesota-latest.osm.pbf

# Extract metro area (if osmium installed)
osmium extract --bbox=-93.8,44.7,-92.8,45.1 minnesota-latest.osm.pbf -o minneapolis-saint-paul_minnesota.osm.pbf
rm minnesota-latest.osm.pbf

# Rebuild graph
cd ..
./scripts/build-graph.sh

# Restart OTP
sudo systemctl restart otp
```

### Updating OTP Code

```bash
cd /opt/otp-minneapolis/OpentripPlanner
git fetch origin
git merge origin/dev-2.x

# Rebuild OTP
cd ..
./scripts/build.sh

# Rebuild graph
./scripts/build-graph.sh

# Restart
sudo systemctl restart otp
```

## Backup & Recovery

### What to Backup

1. **Configuration files** (config/)
2. **Data files** (data/ - except graph.obj which can be rebuilt)
3. **Custom modifications** (OpentripPlanner fork)

### Backup Script

```bash
#!/bin/bash
BACKUP_DIR="/backup/otp"
DATE=$(date +%Y%m%d)

mkdir -p "$BACKUP_DIR"

# Backup configs
tar -czf "$BACKUP_DIR/otp-config-$DATE.tar.gz" /opt/otp-minneapolis/config/

# Backup data (exclude graph)
tar -czf "$BACKUP_DIR/otp-data-$DATE.tar.gz" \
    --exclude='*.obj' \
    /opt/otp-minneapolis/data/

# Keep only last 7 days
find "$BACKUP_DIR" -name "otp-*.tar.gz" -mtime +7 -delete
```

### Recovery

```bash
# Restore config
tar -xzf otp-config-20260112.tar.gz -C /

# Restore data
tar -xzf otp-data-20260112.tar.gz -C /

# Rebuild graph
cd /opt/otp-minneapolis
./scripts/build-graph.sh

# Restart
sudo systemctl restart otp
```

## Performance Tuning

### Java Memory Settings

Edit systemd service or JAVA_OPTS:

```bash
# For 8GB server
Environment="JAVA_OPTS=-Xmx4G -Xms2G"

# For 16GB server
Environment="JAVA_OPTS=-Xmx8G -Xms4G"
```

### Graph Building Performance

Use more memory for faster graph builds:

```bash
# Edit scripts/build-graph.sh
# Change:
java -Xmx4G ...
# To:
java -Xmx6G ...  # or higher
```

### Request Rate Limiting (Nginx)

Add to nginx config:

```nginx
limit_req_zone $binary_remote_addr zone=otp:10m rate=10r/s;

location / {
    limit_req zone=otp burst=20;
    # ... rest of config
}
```

## Troubleshooting

### OTP Won't Start

```bash
# Check logs
sudo journalctl -u otp -n 100

# Common issues:
# - Java heap too small: increase JAVA_OPTS
# - Graph corrupted: rebuild with ./scripts/build-graph.sh
# - Port in use: check for other processes on 8090
```

### High Memory Usage

```bash
# Check memory
free -h

# Reduce Java heap in systemd service
sudo systemctl edit otp
# Add:
[Service]
Environment="JAVA_OPTS=-Xmx2G"
```

### Real-time Updates Not Working

```bash
# Test GTFS-RT URLs manually
curl https://svc.metrotransit.org/mtgtfs/tripupdates.pb -o /tmp/test.pb
# Should return data

# Check OTP logs for updater errors
sudo journalctl -u otp | grep -i updater
```

## Security Hardening

### Firewall Setup

```bash
# Install UFW
sudo apt install ufw

# Allow SSH, HTTP, HTTPS
sudo ufw allow 22/tcp
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp

# Block OTP port from external access (only allow localhost)
sudo ufw deny 8090/tcp

# Enable firewall
sudo ufw enable
```

### Fail2ban (Optional)

```bash
sudo apt install fail2ban
sudo systemctl enable fail2ban
```

## Conclusion

Your OTP Minneapolis instance should now be running in production with:

- ✅ Automatic startup on boot
- ✅ HTTPS encryption
- ✅ Automated GTFS updates
- ✅ Health monitoring
- ✅ Regular backups

For questions or issues, refer to:
- OTP Documentation: https://docs.opentripplanner.org/
- This deployment's bicycle+transit fix: docs/bicycle-transit-fix/
