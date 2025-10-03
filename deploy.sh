#!/bin/bash
# Deployment script for chainsaw-ops
# Run this on the server after pulling from GitHub

set -e  # Exit on any error

echo "🚀 Starting deployment..."

# Navigate to app directory
cd /opt/chainsaw-ops

# Pull latest changes from GitHub
echo "📥 Pulling latest changes from GitHub..."
git pull origin main

# Install/update dependencies (if requirements.txt changed)
echo "📦 Updating dependencies..."
source venv/bin/activate
pip install -r requirements.txt --quiet

# Restart the service
echo "♻️  Restarting chainsaw-ops service..."
systemctl restart chainsaw-ops

# Wait a moment for service to start
sleep 2

# Check service status
echo "✅ Checking service status..."
systemctl status chainsaw-ops --no-pager -l

echo ""
echo "🎉 Deployment complete!"
echo "📊 App is running at: http://82.64.179.76"
echo ""
echo "💡 To view logs: journalctl -u chainsaw-ops -f"

