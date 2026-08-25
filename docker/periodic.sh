#!/bin/sh
# The retry loop the three containerised "cron" jobs share. Sourced, not run.
#
# Each job is the same shape: do the thing, sleep a long time, repeat; and on
# failure, try again soon because the usual failure is "the app is still applying
# migrations". The three had a copy of that loop each, and all three copies had
# the same bug: a *permanent* failure — the Assemblée nationale export URL
# carries the legislature number and changes once per legislature, which is a
# documented, expected event — retried every 30 or 60 seconds for ever. That is
# 1 440 requests a day against a public government service that is not going to
# answer differently, from a platform whose whole standing rests on being a good
# citizen of the open-data ecosystem.
#
# So the retry delay doubles after each failure, up to RETRY_MAX. A hiccup at
# startup still recovers in a minute; a source that has genuinely moved settles
# to one attempt an hour and stays there until somebody fixes the setting. The
# compose healthcheck watches the heartbeat's age, so the silence is still loud.

# Longest the backoff may grow to, whatever the job's own cadence. An hour is the
# balance: rare enough not to hammer anyone, frequent enough that the job picks
# itself up within the hour once the source is reachable again.
RETRY_MAX="${RETRY_MAX:-3600}"


# Sleep, but wake up for SIGTERM. As PID 1 of its container, `sh` blocked inside
# `sleep` never sees the signal `docker compose down` sends, so every one of
# these containers spent the full stop-timeout being killed on each redeploy.
_nap() {
	sleep "$1" &
	wait $!
}


# run_periodically <interval> <first-retry> <label> <command...>
#
# $HEARTBEAT must already be set: it is touched only after a *successful* run, so
# its age is what the healthcheck reads to tell a working job from a stopped one.
run_periodically() {
	interval="$1"
	first_retry="$2"
	label="$3"
	shift 3

	# Cap the backoff at the job's own cadence too: retrying more often than the
	# job would have run anyway is pointless even when RETRY_MAX allows it.
	ceiling="$RETRY_MAX"
	[ "$interval" -lt "$ceiling" ] && ceiling="$interval"

	delay="$first_retry"
	trap 'exit 0' TERM INT

	while true; do
		if "$@"; then
			: > "$HEARTBEAT"
			delay="$first_retry"
			_nap "$interval"
		else
			echo "→ ${label} : échec ; nouvel essai dans ${delay}s"
			_nap "$delay"
			delay=$((delay * 2))
			[ "$delay" -gt "$ceiling" ] && delay="$ceiling"
		fi
	done
}
