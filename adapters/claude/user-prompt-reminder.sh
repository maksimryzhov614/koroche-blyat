#!/bin/sh

PATH=/usr/bin:/bin
export PATH
unset -f cat dirname sed pwd printf test 2>/dev/null || :
cat >/dev/null || :

script_dir=$(CDPATH= cd -P -- "$(dirname -- "$0")" 2>/dev/null && pwd)
if test -z "$script_dir"; then
    printf '%s\n' 'reminder unavailable or invalid' >&2
    exit 0
fi

reminder_file=
if test -f "$script_dir/reminder.txt"; then
    reminder_file=$script_dir/reminder.txt
elif test -f "$script_dir/../generated/reminder.txt"; then
    reminder_file=$script_dir/../generated/reminder.txt
fi

if test -z "$reminder_file"; then
    printf '%s\n' 'reminder unavailable or invalid' >&2
    exit 0
fi

header=$(sed -n '1p' "$reminder_file" 2>/dev/null)
case "$header" in
    'canonical-sha256: '*) hash=${header#'canonical-sha256: '} ;;
    *) hash= ;;
esac
case "$hash" in
    ''|*[!0123456789abcdef]*) hash= ;;
esac
if test "${#hash}" -ne 64; then
    printf '%s\n' 'reminder unavailable or invalid' >&2
    exit 0
fi

encoded=$({ sed '1,/^$/d' "$reminder_file" | sed 's/^/x/'; } 2>/dev/null)
newline='
'
case "$encoded" in
    x*) payload=${encoded#x} ;;
    *) payload= ;;
esac
case "$payload" in
    ''|[[:space:]]*|*[[:space:]]|*"$newline"*) payload= ;;
esac
if test -z "$payload"; then
    printf '%s\n' 'reminder unavailable or invalid' >&2
    exit 0
fi

printf '%s\n' "$payload"
exit 0
