#!/bin/sh
# Retry loop shared by the periodic jobs. Sourced, not run.
#
# Run the job, sleep, repeat. On failure, retry soon (the app may still be
# migrating), doubling the delay up to RETRY_MAX so a permanent failure doesn't
# hit public servers every minute.

# Maximum retry delay.
RETRY_MAX="${RETRY_MAX:-3600}"


# Sleep in the background so SIGTERM is handled (sh is PID 1).
_nap() {
	sleep "$1" &
	wait $!
}


# run_periodically <interval> <first-retry> <label> <command...>
#
# $HEARTBEAT must be set; it's touched after each successful run.
run_periodically() {
	interval="$1"
	first_retry="$2"
	label="$3"
	shift 3

	# Never wait longer than the job's own interval.
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
