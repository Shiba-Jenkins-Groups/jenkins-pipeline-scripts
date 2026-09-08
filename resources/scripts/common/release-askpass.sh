#!/usr/bin/env sh
# This file contains no secret; values are scoped Jenkins credential bindings.
case "$1" in
    *Username*) printf '%s' "$RELEASE_GIT_USER" ;;
    *Password*) printf '%s' "$RELEASE_GIT_PASSWORD" ;;
    *) exit 1 ;;
esac
