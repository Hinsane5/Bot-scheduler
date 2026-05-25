# Deploy To Oracle ARM VM

Phase 9 target: run the Discord bot 24/7 on an Oracle Cloud Ampere A1 Ubuntu VM.

## VM Shape

- Oracle Cloud Free Tier
- Ampere A1 ARM
- Ubuntu 22.04
- 4 OCPU / 24 GB RAM / up to 200 GB disk

## 1. Prepare The VM

SSH into the VM, then install git and clone the private repo:

```bash
sudo apt-get update
sudo apt-get install -y git
sudo mkdir -p /opt/bot-timetable
sudo chown "$USER":"$USER" /opt/bot-timetable
git clone <PRIVATE_REPO_URL> /opt/bot-timetable
cd /opt/bot-timetable
```

Run the bootstrap script:

```bash
chmod +x deploy/bootstrap_ubuntu.sh
./deploy/bootstrap_ubuntu.sh
```

## 2. Copy Secrets

From your laptop:

```bash
scp .env auth_state_lms.json ubuntu@<VM_IP>:/opt/bot-timetable/
```

On the VM:

```bash
cd /opt/bot-timetable
chmod 600 .env auth_state_*.json
.venv/bin/python -m src.auth --portal=lms --check
```

Expected:

```text
valid
```

Do not paste `.env`, bearer tokens, cookies, or `auth_state_*.json` contents into chat, logs, GitHub, or docs.

## 3. Install systemd Unit

On the VM:

```bash
cd /opt/bot-timetable
sudo cp deploy/bot-timetable.service /etc/systemd/system/bot-timetable.service
sudo systemctl daemon-reload
sudo systemctl enable --now bot-timetable
```

Check it:

```bash
sudo systemctl status bot-timetable
journalctl -u bot-timetable -f
```

## 4. Smoke Tests

On the VM:

```bash
cd /opt/bot-timetable
.venv/bin/python -m src.auth --portal=lms --check
.venv/bin/python -m src.scrapers.lms --days 7
sqlite3 data/events.db "SELECT id, type, title, start FROM events ORDER BY start LIMIT 10;"
```

In Discord:

- Bot is online.
- `/status` works.
- `/today` and `/week` list LMS/manual events.
- Manual `/add` schedules reminders.

## 5. Reboot Acceptance

```bash
sudo reboot
```

After reconnecting:

```bash
sudo systemctl status bot-timetable
journalctl -u bot-timetable -n 100 --no-pager
```

Acceptance for Phase 9:

- Service starts after reboot.
- Bot stays online.
- LMS sync keeps running.
- Reminders fire on time.
- `data/backup/events_YYYY-MM-DD.db` is created daily.

## Common Operations

Restart after pulling code:

```bash
cd /opt/bot-timetable
git pull
.venv/bin/python -m pip install -r requirements.txt
sudo systemctl restart bot-timetable
journalctl -u bot-timetable -f
```

Refresh LMS manually:

```bash
cd /opt/bot-timetable
.venv/bin/python -m src.auth --portal=lms --refresh
.venv/bin/python -m src.auth --portal=lms --check
sudo systemctl restart bot-timetable
```

If LMS SSO cookies are dead, rerun interactive auth locally and copy the new `auth_state_lms.json` to the VM.
