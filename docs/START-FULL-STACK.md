# Start OTP Full Stack (Nginx + Frontend)

## Overview
This guide covers starting all components needed for the OTP Minneapolis deployment.

## Components

| Component | Port | Service |
|-----------|------|---------|
| OTP Backend | 8090 | OpenTripPlanner Java server |
| Frontend | 9967 | otp-react-redux Vite dev server |
| Nginx | 9966 | Reverse proxy with SSL |

## Startup Steps

### 1. Start OTP Backend
```bash
cd /home/rwt/projects/otp-minneapolis
./scripts/run.sh
```

### 2. Start Nginx
```bash
sudo systemctl start nginx
sudo systemctl status nginx
```

### 3. Start Frontend Dev Server
```bash
cd /home/rwt/projects/otprr/otp-react-redux
YAML_CONFIG=/home/rwt/projects/otprr/otp-react-redux/port-config.yml yarn start
```

## Quick Start (All Components)

```bash
# Terminal 1 - OTP Backend
cd /home/rwt/projects/otp-minneapolis && ./scripts/run.sh

# Terminal 2 - Nginx (if not already running)
sudo systemctl start nginx

# Terminal 3 - Frontend
cd /home/rwt/projects/otprr/otp-react-redux && YAML_CONFIG=port-config.yml yarn start
```

## Verification
Access the full stack at: **https://tre.hopto.org:9966**

Nginx routes:
- `/otp/*` → OTP backend (localhost:8090)
- `/*` → Frontend (localhost:9967)

## Stopping Services

```bash
# Stop OTP
cd /home/rwt/projects/otp-minneapolis && ./scripts/stop.sh

# Stop Nginx
sudo systemctl stop nginx

# Stop Frontend
# Ctrl+C in the terminal running yarn start
```

## Troubleshooting

Check if services are running:
```bash
# OTP - check for java process
pgrep -f "otp.*8090"

# Nginx
sudo systemctl status nginx

# Frontend - check port 9967
lsof -i :9967
```
