#!/usr/bin/with-contenv bashio
export HA_URL="http://supervisor/core"
bashio::log.info "Starting huddle renderer on :8099"
exec python3 -m app.main
