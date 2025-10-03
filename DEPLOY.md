# Deployment Guide

## 🚀 Automated Deployment (GitHub Actions)

### Simple 2-Step Process

**1. Make changes locally and test:**
```bash
cd /Users/pmru/chainsaw-ops
source venv/bin/activate
python app.py
```

**2. Commit and push to GitHub:**
```bash
git add .
git commit -m "Your commit message"
git push origin main
```

**That's it!** GitHub Actions will automatically:
- Pull latest changes to the server
- Update dependencies if needed
- Restart the Flask service

You can watch the deployment progress in the **Actions** tab on GitHub:
https://github.com/adillraza/chainsaw-ops/actions

---

## Manual Deployment (Backup Method)

If GitHub Actions is not set up or you need to deploy manually:

### Option 1: One-Line Command
```bash
ssh -i ~/Downloads/id_rsa root@82.64.179.76 "cd /opt/chainsaw-ops && ./deploy.sh"
```

### Option 2: SSH and Deploy
```bash
ssh -i ~/Downloads/id_rsa root@82.64.179.76
cd /opt/chainsaw-ops
./deploy.sh
```

---

## Server Information

- **Server IP**: 82.64.179.76
- **App URL**: http://82.64.179.76
- **App Directory**: `/opt/chainsaw-ops`
- **Service Name**: `chainsaw-ops`

---

## Useful Server Commands

### View logs (real-time)
```bash
journalctl -u chainsaw-ops -f
```

### View recent logs
```bash
journalctl -u chainsaw-ops -n 100 --no-pager
```

### Check service status
```bash
systemctl status chainsaw-ops
```

### Restart service manually
```bash
systemctl restart chainsaw-ops
```

### Check Nginx status
```bash
systemctl status nginx
```

---

## Important Files on Server

- **App code**: `/opt/chainsaw-ops/`
- **BigQuery credentials**: `/opt/chainsaw-ops/bigquery-credentials.json`
- **Environment variables**: `/opt/chainsaw-ops/.env`
- **Service file**: `/etc/systemd/system/chainsaw-ops.service`
- **Nginx config**: `/etc/nginx/sites-available/chainsaw-ops`
- **Database**: `/opt/chainsaw-ops/instance/users.db`

---

## Troubleshooting

### If deployment fails:
1. Check if the service is running: `systemctl status chainsaw-ops`
2. Check logs for errors: `journalctl -u chainsaw-ops -n 50`
3. Verify file permissions: `ls -la /opt/chainsaw-ops/`
4. Test BigQuery connection: Check for "invalid_grant" errors in logs

### If changes don't appear:
1. Clear your browser cache (Cmd+Shift+R or Ctrl+Shift+R)
2. Check if deploy.sh actually ran successfully
3. Verify the correct branch: `git branch` on server

### If you get "502 Bad Gateway":
1. Check Flask service: `systemctl status chainsaw-ops`
2. Check Nginx: `systemctl status nginx`
3. Verify port in Nginx config matches Flask port (5001)

---

## Security Notes

- **Never commit** `bigquery-credentials.json` to GitHub
- **Never commit** `.env` file to GitHub  
- **Keep** your SSH key (`id_rsa`) secure and never share it
- The BigQuery credentials file must be manually placed on the server
- If credentials expire, generate new ones from Google Cloud Console and update on server

---

## Initial Server Setup (Already Done)

For reference, here's what was set up:

1. Cloned GitHub repo to `/opt/chainsaw-ops`
2. Created Python virtual environment
3. Installed dependencies from `requirements.txt`
4. Created systemd service file
5. Configured Nginx as reverse proxy
6. Added BigQuery credentials
7. Configured environment variables
8. Enabled and started services

If you need to set up a new server, follow these steps again.

