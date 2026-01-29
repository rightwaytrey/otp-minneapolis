#!/bin/bash
# Fix nginx to route /pelias/ to Nominatim proxy on port 8001

echo "Updating nginx config to use Nominatim proxy (port 8001)..."

# Backup the current config
sudo cp /etc/nginx/sites-enabled/otp /etc/nginx/sites-enabled/otp.backup-$(date +%Y%m%d-%H%M%S)

# Update the proxy_pass line for /pelias/ location
sudo sed -i '/location \/pelias\//,/proxy_pass/ s|proxy_pass http://localhost:4000;|proxy_pass http://localhost:8001;|' /etc/nginx/sites-enabled/otp

echo ""
echo "Testing nginx configuration..."
sudo nginx -t

if [ $? -eq 0 ]; then
    echo ""
    echo "Configuration valid. Reloading nginx..."
    sudo nginx -s reload
    echo ""
    echo "✓ Nginx updated successfully!"
    echo ""
    echo "Verifying the change:"
    grep -A 2 "location /pelias/" /etc/nginx/sites-enabled/otp | grep proxy_pass
else
    echo ""
    echo "✗ Configuration error! Restoring backup..."
    BACKUP=$(ls -t /etc/nginx/sites-enabled/otp.backup-* | head -1)
    sudo cp "$BACKUP" /etc/nginx/sites-enabled/otp
    echo "Backup restored."
fi
