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

.PHONY: test test-go test-py test-js vuln vuln-go vuln-py build deploy provision fmt site-config site site-local

test: vuln test-go test-py test-js

# Fails on any known vulnerability. govulncheck checks reachability, not just
# version numbers, so it only fires on something this code can actually
# reach. pip-audit has no equivalent notion and reports on installed versions.
vuln: vuln-go vuln-py

# govulncheck is a pinned tool dependency (see cloud/go.mod's `tool` line
# and cloud/go.sum), not @latest, so this and CI run the identical version
# and Dependabot's gomod updates cover it like any other dependency.
vuln-go:
	cd cloud && $(GO) tool govulncheck ./...

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

# The site has no dependencies and no build step, so its test runner is Node's
# own: nothing to install and nothing to audit. Node is found on PATH; under
# nvm that means running make from a shell that has loaded it.
test-js:
	@command -v node >/dev/null || { echo "node not found on PATH; the site's tests need Node 22 or later"; exit 1; }
	cd site && node --test tests/*.test.js

fmt:
	cd cloud && gofmt -l . && test -z "$$(gofmt -l .)"

# The admin site's runtime settings, taken from the stack itself so the page
# can never point at an API or a Cognito domain the stack does not have.
site-config:
	cd terraform && printf '{\n  "apiBase": "%s",\n  "cognitoDomain": "%s",\n  "clientId": "%s"\n}\n' \
	  "$$(terraform output -raw api_endpoint)" \
	  "$$(terraform output -raw cognito_domain)" \
	  "$$(terraform output -raw user_pool_client_id)" > ../site/config.json

# Upload the admin site. Tests first, so a broken page cannot ship.
#
# Cache lifetimes differ from HockeyTrack's on purpose. Its assets are cached
# for a day; these are five minutes, because this site's JavaScript carries the
# sign-in flow and has no hashed filenames, so a fix to it has to reach browsers
# promptly. config.json is never cached. Fonts never change and are immutable.
# tests/, package.json and config.json are excluded from the main sync --
# excluded files are also exempt from --delete, so the separate config.json
# upload is not removed by it.
site: test-js site-config
	aws s3 sync site/ s3://$$(cd terraform && terraform output -raw site_bucket)/ --exclude 'assets/*' --exclude 'tests/*' --exclude 'package.json' --exclude 'config.json' --delete --cache-control 'public, max-age=300' --region $(REGION)
	aws s3 cp site/config.json s3://$$(cd terraform && terraform output -raw site_bucket)/config.json --cache-control 'no-store' --content-type 'application/json' --region $(REGION)
	aws s3 sync site/assets/ s3://$$(cd terraform && terraform output -raw site_bucket)/assets/ --exclude 'fonts/*' --delete --cache-control 'public, max-age=300' --region $(REGION)
	aws s3 sync site/assets/fonts/ s3://$$(cd terraform && terraform output -raw site_bucket)/assets/fonts/ --delete --cache-control 'public, max-age=31536000, immutable' --content-type 'font/woff2' --region $(REGION)
	aws cloudfront create-invalidation --distribution-id $$(cd terraform && terraform output -raw site_distribution_id) --paths '/*' --query 'Invalidation.Id' --output text

# Serve the site locally on the one non-production callback URL Cognito
# accepts. Sign-in works here; API calls do not, because the API's CORS admits
# only the production origin, and widening production CORS for a development
# convenience is the wrong trade.
site-local: site-config
	cd site && python3 -m http.server 8000

# Lambda zips: static arm64 binaries named `bootstrap` for provided.al2023,
# zipped with python3's zipfile module (no `zip` binary on this machine).
build:
	mkdir -p build/reducer build/today build/api build/enroll
	cd cloud && CGO_ENABLED=0 GOOS=linux GOARCH=arm64 $(GO) build -ldflags="-s -w" -o ../build/reducer/bootstrap ./cmd/reducer
	cd cloud && CGO_ENABLED=0 GOOS=linux GOARCH=arm64 $(GO) build -ldflags="-s -w" -o ../build/today/bootstrap ./cmd/today
	cd build/reducer && python3 -m zipfile -c ../reducer.zip bootstrap
	cd build/today && python3 -m zipfile -c ../today.zip bootstrap
	cd cloud && CGO_ENABLED=0 GOOS=linux GOARCH=arm64 $(GO) build -ldflags="-s -w" -o ../build/api/bootstrap ./cmd/api
	cd build/api && python3 -m zipfile -c ../api.zip bootstrap
	cd cloud && CGO_ENABLED=0 GOOS=linux GOARCH=arm64 $(GO) build -ldflags="-s -w" -o ../build/enroll/bootstrap ./cmd/enroll
	cd build/enroll && python3 -m zipfile -c ../enroll.zip bootstrap

deploy: test build
	mkdir -p $(XDG_RUNTIME_DIR)
	cd terraform && terraform apply -auto-approve

provision:
	@test -n "$(DEVICE)" || (echo "usage: make provision DEVICE=<thing-name>"; exit 2)
	./tools/provision.sh $(DEVICE)
