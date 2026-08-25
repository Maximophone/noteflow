#!/bin/zsh
# record_memo.sh — toggle-record a voice memo into NoteFlow's Audio/Incoming.
#
# First invocation starts recording the mic; the second one stops it and drops
# the file into the pipeline's Incoming folder, named so the transcriber reads
# the date from the filename and the classifier is forced to <tag>:
#     YYYY-MM-DD-<Tag> HH-MM-SS #<tag>.m4a
#
# Usage: record_memo.sh [tag]        (default tag: todo — "idea" also works)
#
# Meant to be bound to a hotkey via a macOS Shortcut ("Run Shell Script"
# action). Sounds: Pop = recording started, Glass = memo delivered,
# Basso = something went wrong (see $LOG).
#
# Overrides (mostly for testing): FFMPEG, MEMO_MIC, MEMO_INCOMING.

set -u

TAG="${1:-todo}"
FFMPEG="${FFMPEG:-/opt/homebrew/bin/ffmpeg}"
MIC="${MEMO_MIC:-MacBook Pro Microphone}"

if [[ -z "${MEMO_INCOMING:-}" ]]; then
    local -a candidates
    candidates=( ~/Library/CloudStorage/GoogleDrive-*/"My Drive"/KnowledgeBot/Audio/Incoming(N/) )
    MEMO_INCOMING="${candidates[1]:-}"
fi

STATE_DIR="/tmp/noteflow-memo-$USER"
mkdir -p "$STATE_DIR"
PIDFILE="$STATE_DIR/rec.pid"
TMPOUT="$STATE_DIR/rec.m4a"
LOG="$STATE_DIR/rec.log"

fail() {
    echo "$(date '+%F %T') ERROR: $1" >> "$LOG"
    afplay /System/Library/Sounds/Basso.aiff &!
    exit 1
}

if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    # --- Recording in progress: stop it and deliver the file ---
    PID="$(cat "$PIDFILE")"
    rm -f "$PIDFILE"
    kill -INT "$PID" 2>/dev/null
    # Wait for ffmpeg to exit so the m4a container is finalized before moving.
    for _ in {1..100}; do
        kill -0 "$PID" 2>/dev/null || break
        sleep 0.1
    done
    kill -0 "$PID" 2>/dev/null && kill -KILL "$PID" 2>/dev/null

    [[ -s "$TMPOUT" ]] || fail "recording produced no output (see above for ffmpeg output)"
    [[ -d "$MEMO_INCOMING" ]] || fail "Incoming folder not found: '$MEMO_INCOMING'"

    DEST_NAME="$(date +%Y-%m-%d)-${(C)TAG} $(date +%H-%M-%S) #${TAG}.m4a"
    # Copy under a hidden name first: the transcriber skips dotfiles, and the
    # rename makes the file appear in Incoming only once fully written.
    HIDDEN="$MEMO_INCOMING/.incoming-memo.m4a.tmp"
    cp "$TMPOUT" "$HIDDEN" || fail "copy to Incoming failed"
    mv "$HIDDEN" "$MEMO_INCOMING/$DEST_NAME" || fail "rename in Incoming failed"
    rm -f "$TMPOUT"
    echo "$(date '+%F %T') delivered: $DEST_NAME" >> "$LOG"
    afplay /System/Library/Sounds/Glass.aiff &!
else
    # --- No recording in progress: start one ---
    rm -f "$PIDFILE" "$TMPOUT"
    [[ -x "$FFMPEG" ]] || fail "ffmpeg not found at $FFMPEG"
    "$FFMPEG" -hide_banner -nostdin -f avfoundation -i ":${MIC}" \
        -ac 1 -c:a aac -b:a 96k -y "$TMPOUT" >> "$LOG" 2>&1 &!
    PID=$!
    echo "$PID" > "$PIDFILE"
    # Give ffmpeg a moment; if it died (bad device, no mic permission), report.
    sleep 0.5
    if ! kill -0 "$PID" 2>/dev/null; then
        rm -f "$PIDFILE"
        fail "ffmpeg exited immediately — check mic permission/device name (mic: '$MIC')"
    fi
    afplay /System/Library/Sounds/Pop.aiff &!
fi
