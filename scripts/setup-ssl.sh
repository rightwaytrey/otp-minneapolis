#!/bin/bash

# OTP SSL Setup Script
# Configures nginx or Caddy with SSL for OTP
# This is a template - adjust domain and ports as needed

set -e

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo "======================================"
echo "OTP SSL/Reverse Proxy Setup"
echo "======================================"
echo ""
echo "This script helps you set up HTTPS access to OTP."
echo "Choose your preferred web server:"
echo ""
echo "1) Caddy (automatic HTTPS, simpler)"
echo "2) Nginx (more control, requires certbot)"
echo "3) Exit"
echo ""
read -p "Select option [1-3]: " CHOICE

case $CHOICE in
    1)
        echo ""
        echo "Setting up Caddy..."
        echo ""

        # Install Caddy if not present
        if ! command -v caddy &> /dev/null; then
            echo "Installing Caddy..."
            sudo apt update
            sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https
            curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
            curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
            sudo apt update
            sudo apt install -y caddy
        fi

        # Create Caddyfile
        read -p "Enter your domain (e.g., otp.example.com): " DOMAIN
        read -p "Enter OTP port [8090]: " OTP_PORT
        OTP_PORT=${OTP_PORT:-8090}

        echo "Creating Caddyfile..."
        sudo tee /etc/caddy/Caddyfile > /dev/null <<EOF
$DOMAIN {
    reverse_proxy localhost:$OTP_PORT
    log {
        output file /var/log/caddy/otp.log
    }
}
EOF

        echo "Restarting Caddy..."
        sudo systemctl restart caddy
        sudo systemctl enable caddy

        echo ""
        echo "✓ Caddy setup complete!"
        echo "  Access OTP at: https://$DOMAIN"
        ;;

    2)
        echo ""
        echo "Setting up Nginx with Let's Encrypt..."
        echo ""

        # Install nginx and certbot if not present
        if ! command -v nginx &> /dev/null; then
            echo "Installing nginx..."
            sudo apt update
            sudo apt install -y nginx
        fi

        if ! command -v certbot &> /dev/null; then
            echo "Installing certbot..."
            sudo apt install -y certbot python3-certbot-nginx
        fi

        read -p "Enter your domain (e.g., otp.example.com): " DOMAIN
        read -p "Enter OTP port [8090]: " OTP_PORT
        OTP_PORT=${OTP_PORT:-8090}
        read -p "Enter SSL port [443]: " SSL_PORT
        SSL_PORT=${SSL_PORT:-443}

        # Get SSL certificate
        echo ""
        echo "Obtaining SSL certificate..."
        read -p "Enter email for Let's Encrypt notifications: " EMAIL
        sudo certbot certonly --nginx -d "$DOMAIN" --non-interactive --agree-tos --email "$EMAIL"

        # Create nginx config
        echo "Creating nginx configuration..."
        sudo tee /etc/nginx/sites-available/otp > /dev/null <<EOF
server {
    listen $SSL_PORT ssl http2;
    listen [::]:$SSL_PORT ssl http2;
    server_name $DOMAIN;

    ssl_certificate /etc/letsencrypt/live/$DOMAIN/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/$DOMAIN/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!MD5;

    location / {
        proxy_pass http://localhost:$OTP_PORT;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection 'upgrade';
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_cache_bypass \$http_upgrade;
    }
}

# Redirect HTTP to HTTPS
server {
    listen 80;
    listen [::]:80;
    server_name $DOMAIN;
    return 301 https://\$server_name\$request_uri;
}
EOF

        # Enable site
        sudo ln -sf /etc/nginx/sites-available/otp /etc/nginx/sites-enabled/otp

        # Test and reload
        echo "Testing nginx configuration..."
        sudo nginx -t

        echo "Reloading nginx..."
        sudo systemctl reload nginx
        sudo systemctl enable nginx

        # Setup auto-renewal
        echo "Setting up automatic certificate renewal..."
        sudo systemctl enable certbot.timer

        echo ""
        echo "✓ Nginx setup complete!"
        echo "  Access OTP at: https://$DOMAIN"
        ;;

    3)
        echo "Exiting..."
        exit 0
        ;;

    *)
        echo "Invalid option"
        exit 1
        ;;
esac

echo ""
echo "======================================"
echo "Important Notes:"
echo "======================================"
echo ""
echo "1. Make sure OTP is running on port $OTP_PORT"
echo "2. Ensure your domain DNS points to this server"
echo "3. Firewall must allow HTTPS traffic (port 443/80)"
echo ""
echo "To start OTP:"
echo "  cd $REPO_ROOT && ./scripts/run.sh"
