#!/usr/bin/env bash
# Install only the ETF reminder. No notification is sent by this installer.
set -euo pipefail
cd /home/ubuntu/investment-dashboard
sudo install -d -o ubuntu -g ubuntu -m 700 /srv/investment-dashboard/reminder-state
if [[ ! -e /srv/investment-dashboard/reminder.env ]]; then
  sudo install -o ubuntu -g ubuntu -m 600 deploy/reminder.env.example /srv/investment-dashboard/reminder.env
fi
sudo install -m 644 deploy/systemd/position-etf-reminder.service /etc/systemd/system/position-etf-reminder.service
sudo install -m 644 deploy/systemd/position-etf-reminder.timer /etc/systemd/system/position-etf-reminder.timer
sudo systemd-analyze verify /etc/systemd/system/position-etf-reminder.service /etc/systemd/system/position-etf-reminder.timer
sudo systemctl daemon-reload
sudo systemctl enable --now position-etf-reminder.timer
systemctl list-timers position-etf-reminder.timer --no-pager
