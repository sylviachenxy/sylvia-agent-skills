#!/bin/zsh
LC_ALL=C /usr/bin/head -c 4194305 /dev/zero | /usr/bin/tr '\000' x
exit 0
