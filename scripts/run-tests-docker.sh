#!/usr/bin/env bash
set -euo pipefail

IMAGE_NAME="${IMAGE_NAME:-mqtty-test:latest}"
PYTHON_VERSION="${PYTHON_VERSION:-3.13}"
VERBOSE=0
ARGS=()

while [ "$#" -gt 0 ]; do
  case "$1" in
    -v|--verbose)
      VERBOSE=1
      shift
      ;;
    --)
      shift
      ARGS+=("$@")
      break
      ;;
    *)
      ARGS+=("$1")
      shift
      ;;
  esac
done

# Defaults to full suite.
if [ "${#ARGS[@]}" -eq 0 ]; then
  ARGS=(tests)
fi

HAS_VERBOSITY_FLAG=0
for arg in "${ARGS[@]}"; do
  case "$arg" in
    -q|--quiet|-v|-vv|-vvv|--verbose)
      HAS_VERBOSITY_FLAG=1
      ;;
  esac
done

if [ "$VERBOSE" -eq 1 ]; then
  if [ "$HAS_VERBOSITY_FLAG" -eq 0 ]; then
    ARGS+=(-vv)
  fi
else
  if [ "$HAS_VERBOSITY_FLAG" -eq 0 ]; then
    ARGS+=(-q)
  fi
fi

docker build \
  --build-arg PYTHON_VERSION="$PYTHON_VERSION" \
  -f Dockerfile.test \
  -t "$IMAGE_NAME" \
  .

docker run --rm "$IMAGE_NAME" pytest "${ARGS[@]}"
