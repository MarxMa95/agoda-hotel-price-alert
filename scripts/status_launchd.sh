#!/bin/zsh
set -euo pipefail
LABEL="com.zhema.hotel-price-alert-agoda"
launchctl print "gui/$(id -u)/${LABEL}" 2>/dev/null || echo "Service not loaded: ${LABEL}"
