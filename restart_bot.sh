#!/bin/sh
# Restart the dynasty fantasy football Discord bot (fantasybot.service).
set -e
sudo systemctl restart fantasybot
sudo systemctl status fantasybot --no-pager
