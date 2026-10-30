#!/usr/bin/env bash
# Inicia o Music Player, encerrando antes outras instâncias em execução.
DIR="$(dirname "$(readlink -f "$0")")"
cd "$DIR" || exit 1

for pid in $(pgrep -f "python3? .*musicplayer\.py"); do
    [ "$pid" = "$$" ] && continue
    kill "$pid" 2>/dev/null
    for _ in $(seq 1 30); do
        kill -0 "$pid" 2>/dev/null || break
        sleep 0.1
    done
    kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null
done

exec python3 musicplayer.py "$@"
