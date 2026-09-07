REGION      ?= us-east-1

# make runs recipes with /bin/sh, which does not read a login profile, so a
# toolchain that is only on PATH interactively is invisible here. Append the
# known Go location; if go is already on PATH the earlier entry wins, so this
# is a no-op where it is installed system-wide.
export PATH := $(PATH):$(HOME)/.local/share/go/bin

# Terraform ships as a snap and refuses to run without a *writable*
# XDG_RUNTIME_DIR. A login shell usually points it at /run/user/$(shell id -u),
# which systemd-logind may never have created and the user often cannot create
# either -- so `?=` is not enough: an already-set but unusable value has to be
# replaced, not deferred to.
export XDG_RUNTIME_DIR := $(shell test -w "$${XDG_RUNTIME_DIR}" 2>/dev/null && echo "$${XDG_RUNTIME_DIR}" || (mkdir -p "$(HOME)/.cache/xdg-runtime" && chmod 700 "$(HOME)/.cache/xdg-runtime" && echo "$(HOME)/.cache/xdg-runtime"))

GO          := go
PY          := .venv/bin/python
PYTEST      := .venv/bin/pytest

.PHONY: test test-go test-py vuln vuln-go vuln-py build deploy provision fmt

test: vuln test-go test-py

# Fails on any known vulnerability. govulncheck checks reachability, not just
# version numbers, so it only fires on something this code can actually
# reach. pip-audit has no equivalent notion and reports on installed versions.
vuln: vuln-go vuln-py

vuln-go:
	cd cloud && go run golang.org/x/vuln/cmd/govulncheck@latest ./...

# Audits what is installed in .venv rather than resolving requirements.txt,
# because resolving a requirements file makes pip-audit build its own
# throwaway virtualenv, which fails on this machine (python3-venv is not
# installed and needs root). Auditing the real environment is closer to the
# truth anyway -- it is what actually runs.
vuln-py:
	@test -x .venv/bin/pip-audit || { echo "pip-audit missing: .venv/bin/python -m pip install -r device/requirements-dev.txt"; exit 1; }
	.venv/bin/pip-audit --progress-spinner off

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
