REGION      ?= us-east-1
export XDG_RUNTIME_DIR ?= $(HOME)/.cache/xdg-runtime
GO          := go
PY          := .venv/bin/python
PYTEST      := .venv/bin/pytest

.PHONY: test test-go test-py build deploy provision fmt

test: test-go test-py

test-go:
	cd cloud && $(GO) vet ./... && $(GO) test ./...

test-py:
	cd device && SDL_VIDEODRIVER=dummy SDL_AUDIODRIVER=dummy ../$(PYTEST) -q; \
	status=$$?; \
	if [ $$status -ne 0 ] && [ $$status -ne 5 ]; then exit $$status; fi

fmt:
	cd cloud && gofmt -l . && test -z "$$(gofmt -l .)"

# Lambda zips: static arm64 binaries named `bootstrap` for provided.al2023,
# zipped with python3's zipfile module (no `zip` binary on this machine).
build:
	mkdir -p build/reducer build/today
	cd cloud && CGO_ENABLED=0 GOOS=linux GOARCH=arm64 $(GO) build -ldflags="-s -w" -o ../build/reducer/bootstrap ./cmd/reducer
	cd cloud && CGO_ENABLED=0 GOOS=linux GOARCH=arm64 $(GO) build -ldflags="-s -w" -o ../build/today/bootstrap ./cmd/today
	cd build/reducer && python3 -m zipfile -c ../reducer.zip bootstrap
	cd build/today && python3 -m zipfile -c ../today.zip bootstrap

deploy: test build
	mkdir -p $(XDG_RUNTIME_DIR)
	cd terraform && terraform apply -auto-approve

provision:
	@test -n "$(DEVICE)" || (echo "usage: make provision DEVICE=<thing-name>"; exit 2)
	./tools/provision.sh $(DEVICE)
