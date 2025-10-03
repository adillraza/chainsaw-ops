# Chainsaw Operations Dashboard

A Flask-based operations dashboard for managing purchase orders, tracking inventory, and comparing prices between NETO and REX systems.

## Features

- 📊 **Dashboard** - Overview of operations with quick actions
- 🛒 **REX PO Orders** - View and search purchase orders with comparison data
- 💰 **Cost Price Check** - Compare prices across systems with disparity detection
- 📝 **Notes System** - Add and track item-level and PO-level notes
- 🔄 **BigQuery Integration** - Real-time data sync with caching for performance
- 👥 **User Management** - Admin-controlled user access

## Recent Updates

- ✅ New "Cost Price Check" tab with filtering by PO ID and SKU
- ✅ Refined Notes UI - cleaner, more compact display
- ✅ Fixed "All Item Notes" display for PO-level notes
- ✅ Removed unnecessary columns from comparison tables
- ✅ Hidden Quick Info panel in REX PO Orders view
- ✅ Improved search functionality (PO ID and Order ID)

## Tech Stack

- **Backend**: Python 3.11, Flask
- **Database**: SQLite (local), Google BigQuery (data warehouse)
- **Frontend**: Bootstrap 5, JavaScript
- **Authentication**: Flask-Login
- **ORM**: SQLAlchemy

## Installation

### Prerequisites
- Python 3.11+
- Google Cloud credentials with BigQuery access

### Local Setup

```bash
# Clone the repository
git clone <repository-url>
cd chainsaw-ops

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Set up environment variables
cp env_template.txt .env
# Edit .env with your credentials

# Run the application
bash run.sh
```

The app will be available at `http://localhost:5001`

## Deployment

### Server Deployment (Ubuntu/Debian)

1. **Clone the repository on the server:**
   ```bash
   cd /root
   git clone <repository-url> chainsaw-ops
   cd chainsaw-ops
   ```

2. **Set up the environment:**
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```

3. **Create systemd service** (`/etc/systemd/system/chainsaw-ops.service`):
   ```ini
   [Unit]
   Description=Chainsaw Operations Dashboard
   After=network.target

   [Service]
   User=root
   WorkingDirectory=/root/chainsaw-ops
   Environment="PATH=/root/chainsaw-ops/venv/bin"
   ExecStart=/root/chainsaw-ops/venv/bin/python app.py
   Restart=always

   [Install]
   WantedBy=multi-user.target
   ```

4. **Enable and start the service:**
   ```bash
   systemctl daemon-reload
   systemctl enable chainsaw-ops
   systemctl start chainsaw-ops
   ```

### Deploying Updates

To deploy updates from GitHub:

```bash
cd /root/chainsaw-ops
bash deploy.sh
```

Or manually:
```bash
cd /root/chainsaw-ops
systemctl stop chainsaw-ops
git pull origin main
source venv/bin/activate
pip install -r requirements.txt
systemctl start chainsaw-ops
```

## Configuration

### Environment Variables

Create a `.env` file with:

```bash
FLASK_SECRET_KEY=your-secret-key-here
GOOGLE_APPLICATION_CREDENTIALS=/path/to/bigquery-credentials.json
# Add other configuration as needed
```

### BigQuery Credentials

Place your BigQuery service account credentials JSON file in a secure location and reference it in your environment variables.

## Usage

### Default Admin Account

After first run, create an admin user through the Flask shell or modify the database directly.

### Adding Users

Admins can add users through the User Management interface at `/admin`.

## Production Notes

- **Domain**: ops.jonoandjohno.com.au
- **Server**: 170.64.179.76
- **SSL**: Let's Encrypt (auto-renewal configured)
- **Reverse Proxy**: Nginx
- **Port**: 5001 (internal), 443 (HTTPS external)

## Troubleshooting

### View logs
```bash
journalctl -u chainsaw-ops -f
```

### Check service status
```bash
systemctl status chainsaw-ops
```

### Clear cache
Access the dashboard and use the "Refresh Data" button, or restart the service.

## License

Proprietary - Chainsaw Spares Operations

## Support

For issues or questions, contact the development team.
