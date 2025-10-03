# Deployment Guide

## 🚀 Automated Deployment via GitHub Webhook

### How It Works

Your server runs a webhook service that listens for GitHub push events. When you push code to GitHub:

1. GitHub sends a webhook notification to your server
2. The webhook service verifies the request signature
3. Automatically runs `deploy.sh` which:
   - Pulls latest code from GitHub
   - Updates Python dependencies
   - Restarts the Flask application

**No manual deployment needed!**

---

## 📝 Daily Workflow

### 1. Develop and Test Locally

```bash
cd /Users/pmru/chainsaw-ops
source venv/bin/activate
python app.py
```

Visit http://localhost:5000 to test your changes.

### 2. Deploy to Production

```bash
git add .
git commit -m "Your change description"
git push origin main
```

**That's it!** Your changes will be live in 10-20 seconds.

---

## 🔧 Webhook Configuration (Already Set Up)

**GitHub Webhook Settings:**
- Payload URL: `http://82.64.179.76/webhook`
- Content type: `application/json`
- Secret: (stored in server's `.env` file)
- Events: Just the push event

**View webhook deliveries:** https://github.com/adillraza/chainsaw-ops/settings/hooks

---

## 🛠️ Manual Deployment (Emergency Backup)

If you need to deploy manually (webhook issues, emergency fix, etc.):

```bash
ssh -i ~/Downloads/id_rsa root@82.64.179.76 "cd /opt/chainsaw-ops && ./deploy.sh"
```

---

## 📊 Server Information

- **Production URL**: http://82.64.179.76
- **Server Directory**: `/opt/chainsaw-ops`
- **Main App Service**: `chainsaw-ops` (port 5001)
- **Webhook Service**: `webhook` (port 5002)

---

## 🔍 Monitoring & Debugging

### View Application Logs
```bash
# Main app logs (real-time)
ssh root@82.64.179.76 "journalctl -u chainsaw-ops -f"

# Webhook deployment logs (real-time)
ssh root@82.64.179.76 "journalctl -u webhook -f"

# Recent logs (last 50 lines)
ssh root@82.64.179.76 "journalctl -u chainsaw-ops -n 50"
```

### Check Service Status
```bash
ssh root@82.64.179.76 "systemctl status chainsaw-ops"
ssh root@82.64.179.76 "systemctl status webhook"
ssh root@82.64.179.76 "systemctl status nginx"
```

### Restart Services
```bash
# Restart main app
ssh root@82.64.179.76 "systemctl restart chainsaw-ops"

# Restart webhook server
ssh root@82.64.179.76 "systemctl restart webhook"

# Restart Nginx
ssh root@82.64.179.76 "systemctl restart nginx"
```

---

## 📁 Important Server Files

| File/Directory | Purpose |
|---------------|---------|
| `/opt/chainsaw-ops/` | Application code |
| `/opt/chainsaw-ops/.env` | Environment variables (secrets) |
| `/opt/chainsaw-ops/bigquery-credentials.json` | BigQuery service account key |
| `/opt/chainsaw-ops/instance/users.db` | SQLite database |
| `/opt/chainsaw-ops/deploy.sh` | Deployment script |
| `/opt/chainsaw-ops/webhook.py` | Webhook server code |
| `/etc/systemd/system/chainsaw-ops.service` | Main app service |
| `/etc/systemd/system/webhook.service` | Webhook service |
| `/etc/nginx/sites-available/chainsaw-ops` | Nginx configuration |

---

## 🚨 Troubleshooting

### Webhook Not Triggering

1. **Check webhook deliveries** in GitHub:
   - Go to: https://github.com/adillraza/chainsaw-ops/settings/hooks
   - Click on the webhook → "Recent Deliveries"
   - Should show green checkmark (200 response)

2. **Check webhook service logs:**
   ```bash
   ssh root@82.64.179.76 "journalctl -u webhook -n 50"
   ```

3. **Verify webhook secret** matches in both GitHub and server's `.env`

### App Not Updating After Deployment

1. **Clear browser cache**: `Cmd+Shift+R` or `Ctrl+Shift+R`

2. **Check if deployment completed:**
   ```bash
   ssh root@82.64.179.76 "journalctl -u webhook -n 20"
   ```

3. **Manually trigger deployment:**
   ```bash
   ssh root@82.64.179.76 "cd /opt/chainsaw-ops && ./deploy.sh"
   ```

### "502 Bad Gateway" Error

1. **Check main app is running:**
   ```bash
   ssh root@82.64.179.76 "systemctl status chainsaw-ops"
   ```

2. **Check Nginx:**
   ```bash
   ssh root@82.64.179.76 "systemctl status nginx"
   ```

3. **Restart services:**
   ```bash
   ssh root@82.64.179.76 "systemctl restart chainsaw-ops && systemctl restart nginx"
   ```

### BigQuery Connection Errors

1. **Check logs for "invalid_grant" errors:**
   ```bash
   ssh root@82.64.179.76 "journalctl -u chainsaw-ops | grep -i bigquery"
   ```

2. **Verify credentials file exists:**
   ```bash
   ssh root@82.64.179.76 "ls -lh /opt/chainsaw-ops/bigquery-credentials.json"
   ```

3. **If expired**, generate new key from Google Cloud Console and update on server

---

## 🔒 Security Best Practices

- ✅ **Never commit** credentials (`.json`, `.env`) to GitHub
- ✅ **`.gitignore`** properly configured to block sensitive files
- ✅ **Webhook secret** protects against unauthorized deployments
- ✅ **SSH keys** stored locally, never in repository
- ✅ **BigQuery credentials** manually placed on server only
- ✅ **Public repository** safe - all secrets excluded

### Rotating Credentials

**BigQuery Credentials:**
1. Generate new key in Google Cloud Console
2. Test locally first
3. Update on server via SSH copy-paste
4. Delete old key from Google Cloud

**Webhook Secret:**
1. Generate new secret: `openssl rand -hex 32`
2. Update in server's `.env` file
3. Update in GitHub webhook settings
4. Restart webhook service: `systemctl restart webhook`

---

## 📚 Additional Resources

- **GitHub Repository**: https://github.com/adillraza/chainsaw-ops
- **Webhook Settings**: https://github.com/adillraza/chainsaw-ops/settings/hooks
- **Flask Documentation**: https://flask.palletsprojects.com/
- **BigQuery API**: https://cloud.google.com/bigquery/docs

