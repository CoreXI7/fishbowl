#!/usr/bin/env bash
# Picks the strictest systemd sandbox the collector still works under on THIS machine, and writes it as a drop-in:
#   /etc/systemd/system/homelab-dash-collector.service.d/10-sandbox.conf
# It runs `homelab-dash-collector --selftest` (every check once; it writes only a throwaway folder in out_dir) twice
# under the plain unit's settings, then under each stricter level, strictest first, and keeps the first level that
# gives the same answers. A level that blocks something the collector needs (SMART through an HBA, the firewall
# status, ...) changes an answer, so it's skipped instead of leaving a tile quietly empty. An answer may equal either
# plain run's (a check that comes and goes can't fail a level). If the collector is running, it's restarted with the
# chosen level and checked; if it doesn't keep updating, the previous drop-in (or none) is put back.
# Run by install.sh; on its own: sudo bash collector/sandbox.sh
# Undo: sudo rm /etc/systemd/system/homelab-dash-collector.service.d/10-sandbox.conf && sudo systemctl daemon-reload
#       && sudo systemctl restart homelab-dash-collector
# Needs systemd 245 or newer for the stricter levels (older ones fail every level, and nothing changes).
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
BIN=${BIN:-/usr/local/sbin/homelab-dash-collector}
UNIT=homelab-dash-collector
DROPIN_DIR=${DROPIN_DIR:-/etc/systemd/system/$UNIT.service.d}
DROPIN=$DROPIN_DIR/10-sandbox.conf
CONF=${HOMELAB_DASH_COLLECTOR_CONFIG:-/etc/homelab-dash/collector.toml}
# the collector writes only to its output folder (out_dir in collector.toml, default /var/lib/homelab-dash); how often
# it writes there decides how long the check after the restart waits (the page can set up to 60 s)
read -r INTERVAL OUT < <(python3 -I -c 'import sys, tomllib
try:
    c = tomllib.load(open(sys.argv[1], "rb"))
except FileNotFoundError:
    c = {}
i = c.get("interval", 5)
print(int(i) if isinstance(i, (int, float)) and 0 < i < 86400 else 5, c.get("out_dir", "/var/lib/homelab-dash"))' "$CONF") \
  || { echo "Can't read $CONF (is it valid TOML?): nothing changed."; exit 1; }
[[ "$OUT" =~ ^/[A-Za-z0-9._/-]+$ ]] || { echo "out_dir must be an absolute path of letters, digits and . _ - / only: $OUT"; exit 1; }
install -d -m 0755 "$OUT"   # ReadWritePaths= needs it to exist (on a fresh install the service hasn't made it yet)

BASE=(ProtectHome=read-only ProtectSystem=full PrivateTmp=yes NoNewPrivileges=yes)   # what the plain unit has
CAPS_MIN="CAP_DAC_READ_SEARCH CAP_CHOWN CAP_FOWNER CAP_SYS_RAWIO CAP_NET_ADMIN CAP_NET_RAW"
CAPS_MORE="$CAPS_MIN CAP_SYS_ADMIN CAP_SYS_PTRACE CAP_DAC_OVERRIDE"
LEVELS=(strict strict-more-powers no-network-read-only)   # strictest first

level() {   # $1 = level name: sets PROPS (each item one whole systemd property)
  local net=(IPAddressDeny=any "RestrictAddressFamilies=AF_UNIX AF_NETLINK AF_INET AF_INET6")
  local ro=(ProtectSystem=strict "ReadWritePaths=$OUT" ProtectKernelTunables=yes ProtectKernelModules=yes ProtectKernelLogs=yes
            ProtectControlGroups=yes ProtectClock=yes ProtectHostname=yes)
  local misc=(RestrictNamespaces=yes RestrictRealtime=yes RestrictSUIDSGID=yes LockPersonality=yes SystemCallArchitectures=native)
  case $1 in
    # an unexpected system call fails with "not permitted" (a check errors) instead of killing the collector
    strict) PROPS=("${net[@]}" "${ro[@]}" "${misc[@]}" MemoryDenyWriteExecute=yes SystemCallFilter=@system-service
                   SystemCallErrorNumber=EPERM "CapabilityBoundingSet=$CAPS_MIN") ;;
    strict-more-powers) PROPS=("${net[@]}" "${ro[@]}" "${misc[@]}" "CapabilityBoundingSet=$CAPS_MORE") ;;
    no-network-read-only) PROPS=("${net[@]}" "${ro[@]}") ;;
  esac
}

ERR=$(mktemp); OLD=$(mktemp); STOPPED=no
# whatever happens (Ctrl-C, a closed terminal, an error), the collector isn't left stopped
trap 'rm -f "$ERR" "$OLD"; [ "$STOPPED" = no ] || systemctl start "$UNIT" 2>/dev/null || true' EXIT
trap 'exit 130' INT TERM HUP
selftest() {   # $@ = extra properties: prints the self-test's line of JSON, or "failed" (the reason is in $ERR)
  local args=() p out
  for p in "${BASE[@]}" "$@"; do args+=(-p "$p"); done
  out=$(timeout 330 systemd-run --quiet --wait --pipe --collect -p MemoryMax=400M -p Nice=10 -p RuntimeMaxSec=300 \
          --setenv=HOMELAB_DASH_COLLECTOR_CONFIG="$CONF" "${args[@]}" /usr/bin/python3 -I "$BIN" --selftest 2>"$ERR") || out=failed
  printf '%s\n' "$out" | tail -n 1
}

# exit 0 if $3 answers like the plain runs $1/$2 (each answer equal to one of theirs: a check that comes and goes
# can't fail a level); otherwise print what changed
compare() {
  python3 -I -c 'import json, sys
a, b = json.loads(sys.argv[1]), json.loads(sys.argv[2])
try:
    c = json.loads(sys.argv[3])
except ValueError:
    c = None
if c is None:
    print("    (the self-test failed under it)")
    sys.exit(1)
diff = [k for k in sorted(set(a) | set(b)) if c.get(k) not in (a.get(k), b.get(k))]   # one of the plain answers
for k in diff:
    print("    %s: %s -> %s" % (k, a.get(k), c.get(k)))
sys.exit(1 if diff else 0)' "$1" "$2" "$3"
}

echo "Checking which sandbox the collector works under here (a few seconds per try)..."
base=$(selftest)
case "$base" in "{"*) ;; *) echo "The collector's self-test failed even without the extra sandbox; nothing changed:"; head -n 5 "$ERR"; exit 1 ;; esac
base2=$(selftest)
case "$base2" in "{"*) ;; *) echo "The collector's self-test failed on its second run; nothing changed:"; head -n 5 "$ERR"; exit 1 ;; esac
chosen=""
for name in "${LEVELS[@]}"; do
  level "$name"
  got=$(selftest "${PROPS[@]}")
  if compare "$base" "$base2" "$got" > /dev/null; then chosen=$name; break; fi
  echo "  $name: changes what the collector sees, skipped:"
  compare "$base" "$base2" "$got" || true
  if [ "$got" = failed ]; then head -n 3 "$ERR" | sed 's/^/    /'; fi
done
mkdir -p "$DROPIN_DIR"
touch "$DROPIN_DIR/.sandbox-checked"
had_dropin=no; if [ -f "$DROPIN" ]; then had_dropin=yes; cp "$DROPIN" "$OLD"; fi
if [ -z "$chosen" ]; then
  rm -f "$DROPIN"
  echo "No stricter sandbox works here: the collector keeps the plain unit's settings."
else
  tmp=$(mktemp "$DROPIN_DIR/.sandbox.XXXXXX")
  {
    echo "# Written by collector/sandbox.sh: the strictest level the collector's self-test passed on this machine ($chosen)."
    echo "# Undo: delete this file, then systemctl daemon-reload && systemctl restart $UNIT"
    echo "[Service]"
    printf '%s\n' "${PROPS[@]}"
  } > "$tmp"
  chmod 0644 "$tmp"
  mv -f "$tmp" "$DROPIN"
  echo "Sandbox: $chosen ($DROPIN)."
fi
systemctl daemon-reload || true

# the collector is running: restart it with the new settings and make sure it keeps updating hw.json (two writes after
# the restart, by the same process: a crash loop that writes once doesn't pass)
if systemctl is-active --quiet "$UNIT" && { [ -n "$chosen" ] || [ "$had_dropin" = yes ]; }; then
  hw="$OUT/hw.json"
  mtime() { stat -c %Y "$hw" 2>/dev/null || echo 0; }
  wait_newer() {   # $1 = seconds since the epoch: wait until hw.json is newer than that
    for _ in $(seq 1 $(( (INTERVAL > 60 ? INTERVAL : 60) * 2 + 30 ))); do
      sleep 1
      systemctl is-active --quiet "$UNIT" || return 1
      [ "$(mtime)" -gt "$1" ] && return 0
    done
    return 1
  }
  STOPPED=yes
  systemctl stop "$UNIT" || true
  sleep 1
  start=$(date +%s)
  systemctl start "$UNIT" || true
  STOPPED=no
  ok=no
  if wait_newer "$start"; then
    pid=$(systemctl show -p MainPID --value "$UNIT")
    first=$(mtime)
    if wait_newer "$first" && [ "$(systemctl show -p MainPID --value "$UNIT")" = "$pid" ]; then ok=yes; fi
  fi
  if [ "$ok" = yes ]; then
    echo "Collector restarted and updating."
  elif [ -n "$chosen" ]; then
    echo "The collector didn't keep updating under '$chosen': putting the previous sandbox settings back."
    if [ "$had_dropin" = yes ]; then cp "$OLD" "$DROPIN"; else rm -f "$DROPIN"; fi
    systemctl daemon-reload || true
    systemctl restart "$UNIT" || true
    echo "See why: journalctl -u $UNIT -n 30"
    exit 1
  else
    echo "PROBLEM: the collector isn't updating: journalctl -u $UNIT -n 30"
    exit 1
  fi
fi
