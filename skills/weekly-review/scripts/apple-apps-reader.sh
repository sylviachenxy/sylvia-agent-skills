#!/bin/sh

# Stable launcher for apple-apps-reader.js. Child and utility diagnostics are
# discarded because Apple events can embed private object descriptions.

set -u

MAX_TRANSPORT_BYTES=1048577

emit_prelaunch_error() {
    printf '%s\n' '{"ok":false,"schema_version":1,"command":"launcher","app_contacted":false,"error":{"code":"LAUNCHER_ERROR","message":"The read-only Apple Apps adapter could not be launched.","retryable":false}}'
    printf '%s\n' 'apple-apps-reader: launcher failure; private diagnostics were suppressed' >&2
}

emit_postlaunch_error() {
    printf '%s\n' '{"ok":false,"schema_version":1,"command":"launcher","app_contacted":"unknown","app_contact_possible":true,"error":{"code":"LAUNCHER_ERROR","message":"The read-only Apple Apps adapter failed after execution may have begun.","retryable":false}}'
    printf '%s\n' 'apple-apps-reader: execution failed; private diagnostics were suppressed' >&2
}

emit_output_limit_error() {
    printf '%s\n' '{"ok":false,"schema_version":1,"command":"launcher","app_contacted":"unknown","app_contact_possible":true,"error":{"code":"OUTPUT_LIMIT_EXCEEDED","message":"The adapter response exceeded the fixed output limit.","retryable":false}}'
    printf '%s\n' 'apple-apps-reader: output limit exceeded; private output was suppressed' >&2
}

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0" 2>/dev/null)" 2>/dev/null && pwd -P 2>/dev/null) || {
    emit_prelaunch_error
    exit 70
}
JXA_SCRIPT="$SCRIPT_DIR/apple-apps-reader.js"

if [ ! -r "$JXA_SCRIPT" ]; then
    emit_prelaunch_error
    exit 70
fi

# The bounded helper holds at most MAX_TRANSPORT_BYTES + 1 bytes, applies the
# 60-second watchdog, suppresses child stderr, and emits nothing unless the
# complete child response is a single compact JSON object.
OUTPUT=$(
    /usr/bin/env \
        -u PERL5OPT -u PERL5LIB -u PERLLIB -u PERL5DB -u PERLDB_OPTS \
        -u PERLIO -u PERL_UNICODE -u PERL_USE_UNSAFE_INC \
        /usr/bin/perl - "$MAX_TRANSPORT_BYTES" /usr/bin/osascript -l JavaScript "$JXA_SCRIPT" -- "$@" 2>/dev/null <<'PERL'
use strict;
use warnings;
use JSON::PP ();

my $limit = shift @ARGV;
exit 75 unless defined($limit) && $limit =~ /\A[0-9]+\z/ && $limit > 0;

pipe(my $reader, my $writer) or exit 75;
my $pid = fork();
exit 75 unless defined $pid;

if ($pid == 0) {
    close $reader;
    open(STDOUT, ">&", $writer) or exit 126;
    close $writer;
    open(STDERR, ">", "/dev/null") or exit 126;
    exec @ARGV;
    exit 126;
}

close $writer;
binmode $reader;
binmode STDOUT;
my $buffer = "";
my $oversized = 0;
my $timed_out = 0;
my $read_failed = 0;

local $SIG{ALRM} = sub {
    $timed_out = 1;
    kill "TERM", $pid;
};
alarm 60;

while (!$timed_out) {
    my $remaining = ($limit + 1) - length($buffer);
    my $wanted = $remaining < 8192 ? $remaining : 8192;
    my $chunk = "";
    my $count = sysread($reader, $chunk, $wanted);
    if (!defined $count) {
        if ($timed_out) {
            last;
        }
        $read_failed = 1;
        last;
    }
    last if $count == 0;
    $buffer .= $chunk;
    if (length($buffer) > $limit) {
        $oversized = 1;
        kill "TERM", $pid;
        last;
    }
}

alarm 0;
if ($timed_out || $oversized || $read_failed) {
    kill "KILL", $pid;
}
close $reader;
waitpid($pid, 0);
my $child_status = $?;

exit 74 if $timed_out;
exit 73 if $oversized;
exit 75 if $read_failed;
exit 72 if $child_status != 0;

$buffer =~ s/\n\z//;
exit 76 if $buffer eq "" || $buffer =~ /[\x00\r\n]/;
my $decoded = eval { JSON::PP->new->utf8->decode($buffer) };
exit 76 if $@ || ref($decoded) ne "HASH";
exit 76 unless exists($decoded->{ok}) && exists($decoded->{schema_version});

print STDOUT $buffer or exit 75;
exit 0;
PERL
)
STATUS=$?

if [ "$STATUS" -ne 0 ]; then
    if [ "$STATUS" -eq 73 ]; then
        emit_output_limit_error
    else
        emit_postlaunch_error
    fi
    exit 70
fi

case "$OUTPUT" in
    '{"ok":true,'*'}')
        printf '%s\n' "$OUTPUT"
        exit 0
        ;;
    '{"ok":false,'*'}')
        printf '%s\n' "$OUTPUT"
        printf '%s\n' 'apple-apps-reader: request rejected or failed; see the JSON error code' >&2
        exit 2
        ;;
    *)
        emit_postlaunch_error
        exit 70
        ;;
esac
