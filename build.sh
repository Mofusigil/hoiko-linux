#!/bin/sh
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j2
