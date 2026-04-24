# systemd units

Copy into `/etc/systemd/system/` on the VPS, reload, enable, start:

```bash
cp deploy/systemd/chainsaw-ops-refresh.service /etc/systemd/system/
cp deploy/systemd/chainsaw-ops-refresh.timer   /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now chainsaw-ops-refresh.timer
```

Run once manually (same thing the timer does, useful for debugging):

```bash
systemctl start chainsaw-ops-refresh.service
journalctl -u chainsaw-ops-refresh.service -n 50 --no-pager
```

Schedule: `*:05,35` — 5 min after each Dataform workflow run (which fires at
`:00` and `:30` and takes ~1 min).

The refresh is a no-op if a manual refresh is already in flight (guarded by
`SyncStateService`), so the timer is safe to leave enabled even if a user
clicks the UI Refresh Data button.
