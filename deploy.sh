#!/bin/bash
# Deployment script for chainsaw-ops
# Run this on the server after pulling from GitHub

set -e  # Exit on any error

echo "🚀 Starting deployment..."

# Navigate to app directory
cd /root/chainsaw-ops

# Stop the service
echo "⏸️  Stopping chainsaw-ops service..."
systemctl stop chainsaw-ops

# Pull latest changes from GitHub
echo "📥 Pulling latest changes from GitHub..."
git pull origin main

# Install/update dependencies (if requirements.txt changed)
echo "📦 Checking dependencies..."
source venv/bin/activate
pip install -r requirements.txt --quiet

# Restart the service
echo "▶️  Starting chainsaw-ops service..."
systemctl start chainsaw-ops

# Check service status
echo "✅ Checking service status..."
systemctl status chainsaw-ops --no-pager

echo "🎉 Deployment complete!"
echo "📊 App is running at: https://ops.jonoandjohno.com.au"

