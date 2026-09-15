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
#
# Two things gate a deploy here, and neither alone is the whole story. No
# test imports app.js -- it touches the DOM on load, and the test runner has
# none -- so `node --check` parses it (and every other file under assets/)
# for a syntax error first, failing on the first one found. `--check` does
# not resolve imports, though, so tests/imports.test.js separately confirms
# every name app.js imports from another module is actually exported by it.
# Together this catches a broken or drifted app.js before it ships; it is
# not a substitute for the module-level tests the other test files run.
test-js:
	@command -v node >/dev/null || { echo "node not found on PATH; the site's tests need Node 22 or later"; exit 1; }
	cd site && for f in assets/*.js; do node --check "$$f" || exit 1; done
	cd site && node --test tests/*.test.js

fmt:
	cd cloud && gofmt -l . && test -z "$$(gofmt -l .)"

# The admin site's runtime settings, taken from the stack itself. Each output
# is captured on its own, so a failed `terraform output` fails this recipe
# instead of being swallowed by printf's exit status; each captured value is
# then checked non-empty, so a renamed or not-yet-applied output is refused
# by name instead of shipping a page that silently can't reach anything. The
# JSON is built by python3 (already assumed by site-local) with real
# escaping, and written to a temp file that is only moved into place once
# it is complete, so a failed run never leaves a truncated or empty
# config.json behind.
site-config:
	cd terraform && api=$$(terraform output -raw api_endpoint) \
	  && domain=$$(terraform output -raw cognito_domain) \
	  && client=$$(terraform output -raw user_pool_client_id) \
	  && { [ -n "$$api" ] || { echo "site-config: terraform output api_endpoint is empty" >&2; exit 1; }; } \
	  && { [ -n "$$domain" ] || { echo "site-config: terraform output cognito_domain is empty" >&2; exit 1; }; } \
	  && { [ -n "$$client" ] || { echo "site-config: terraform output user_pool_client_id is empty" >&2; exit 1; }; } \
	  && API_ENDPOINT="$$api" COGNITO_DOMAIN="$$domain" CLIENT_ID="$$client" python3 -c \
	    'import json, os; print(json.dumps({"apiBase": os.environ["API_ENDPOINT"], "cognitoDomain": os.environ["COGNITO_DOMAIN"], "clientId": os.environ["CLIENT_ID"]}, indent=2))' \
	    > ../site/config.json.tmp \
	  && mv ../site/config.json.tmp ../site/config.json

# Upload the admin site. test-js gates the JavaScript (see its own comment
# for exactly what that covers); site-config gates the runtime settings.
#
# The bucket and distribution are resolved once, up front, and refused if
# either comes back empty -- the whole sequence below is one &&-chain, so a
# failure at any step (including resolving those two values) stops
# everything after it rather than running the remaining aws calls against
# an empty bucket name.
#
# Upload order matters: fonts, then the other assets, then config.json,
# then the root sync -- which carries index.html -- last, and the
# invalidation after that. index.html references the other three, so it
# must never be live before they are; publishing it first would let
# CloudFront serve or even cache a 403 for a visitor who lands between the
# two syncs.
#
# Cache lifetimes differ from HockeyTrack's on purpose. Its assets are cached
# for a day; these are five minutes, because this site's JavaScript carries the
# sign-in flow and has no hashed filenames, so a fix to it has to reach browsers
# promptly. config.json is never cached. Fonts never change and are immutable.
# tests/, package.json and config.json are excluded from the main sync --
# excluded files are also exempt from --delete, so the separate config.json
# upload is not removed by it. Every sync also excludes dotfiles and editor
# backups, so a stray .DS_Store, ~-file, .bak or .swp under site/ never
# becomes public.
DOTFILE_EXCLUDES := --exclude '.*' --exclude '*/.*' --exclude '*~' --exclude '*.bak' --exclude '*.swp'
site: test-js site-config
	bucket=$$(cd terraform && terraform output -raw site_bucket) \
	  && dist=$$(cd terraform && terraform output -raw site_distribution_id) \
	  && { [ -n "$$bucket" ] || { echo "site: terraform output site_bucket is empty" >&2; exit 1; }; } \
	  && { [ -n "$$dist" ] || { echo "site: terraform output site_distribution_id is empty" >&2; exit 1; }; } \
	  && aws s3 sync site/assets/fonts/ s3://$$bucket/assets/fonts/ $(DOTFILE_EXCLUDES) --delete --cache-control 'public, max-age=31536000, immutable' --content-type 'font/woff2' --region $(REGION) \
	  && aws s3 sync site/assets/ s3://$$bucket/assets/ --exclude 'fonts/*' $(DOTFILE_EXCLUDES) --delete --cache-control 'public, max-age=300' --region $(REGION) \
	  && aws s3 cp site/config.json s3://$$bucket/config.json --cache-control 'no-store' --content-type 'application/json' --region $(REGION) \
	  && aws s3 sync site/ s3://$$bucket/ --exclude 'assets/*' --exclude 'tests/*' --exclude 'package.json' --exclude 'config.json' $(DOTFILE_EXCLUDES) --delete --cache-control 'public, max-age=300' --region $(REGION) \
	  && aws cloudfront create-invalidation --distribution-id $$dist --paths '/*' --query 'Invalidation.Id' --output text

# Serve the site locally on 127.0.0.1 -- nothing it serves is secret, but a
# dev server has no reason to listen on the LAN. Browse http://localhost:8000,
# not 127.0.0.1:8000: Cognito's hosted UI has localhost, not 127.0.0.1,
# registered as a callback URL, so the redirect_uri would not match.
site-local: site-config
	cd site && python3 -m http.server --bind 127.0.0.1 8000

# Lambda zips: static arm64 binaries named `bootstrap` for provided.al2023,
# zipped with python3's zipfile module (no `zip` binary on this machine).
#
# -buildvcs=false and -trimpath make the binaries reproducible: Go otherwise
# stamps each one with the commit and the checkout's absolute path, so every
# commit changes every function's hash, and Terraform redeploys -- and
# HockeyTrack's security rules page on -- code that did not change.
# site/tests/build-config.test.js keeps both flags on every build line.
#
# The zips the python3 lines below write are neither reproducible nor what
# Terraform deploys: each archive_file data source (lambda.tf, admin.tf,
# enroll.tf, signin.tf) rewrites its zip from the bootstrap whenever Terraform
# plans (an apply plans first), with a pinned file mode, and that regenerated
# zip is the one uploaded.
build:
	mkdir -p build/reducer build/today build/api build/enroll build/authgate
	cd cloud && CGO_ENABLED=0 GOOS=linux GOARCH=arm64 $(GO) build -buildvcs=false -trimpath -ldflags="-s -w" -o ../build/reducer/bootstrap ./cmd/reducer
	cd cloud && CGO_ENABLED=0 GOOS=linux GOARCH=arm64 $(GO) build -buildvcs=false -trimpath -ldflags="-s -w" -o ../build/today/bootstrap ./cmd/today
	cd build/reducer && python3 -m zipfile -c ../reducer.zip bootstrap
	cd build/today && python3 -m zipfile -c ../today.zip bootstrap
	cd cloud && CGO_ENABLED=0 GOOS=linux GOARCH=arm64 $(GO) build -buildvcs=false -trimpath -ldflags="-s -w" -o ../build/api/bootstrap ./cmd/api
	cd build/api && python3 -m zipfile -c ../api.zip bootstrap
	cd cloud && CGO_ENABLED=0 GOOS=linux GOARCH=arm64 $(GO) build -buildvcs=false -trimpath -ldflags="-s -w" -o ../build/enroll/bootstrap ./cmd/enroll
	cd build/enroll && python3 -m zipfile -c ../enroll.zip bootstrap
	cd cloud && CGO_ENABLED=0 GOOS=linux GOARCH=arm64 $(GO) build -buildvcs=false -trimpath -ldflags="-s -w" -o ../build/authgate/bootstrap ./cmd/authgate
	cd build/authgate && python3 -m zipfile -c ../authgate.zip bootstrap

deploy: test build
	mkdir -p $(XDG_RUNTIME_DIR)
	cd terraform && terraform apply -auto-approve

provision:
	@test -n "$(DEVICE)" || (echo "usage: make provision DEVICE=<thing-name>"; exit 2)
	./tools/provision.sh $(DEVICE)
