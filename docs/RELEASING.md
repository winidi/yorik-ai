# Releasing Yorik

For whoever cuts the next version, person or agent. A release is a git
tag; GitHub does the building. What families get from it: new images
and install files, offered in Settings → System → Updates.

**Only the maintainer decides when a release goes out.** Prepare
everything, then ask; tag and push only on their word.

## What a tag starts

Pushing `vX.Y.Z` runs `.github/workflows/release.yml`:

1. **release**: a GitHub release titled `vX.Y.Z`, the notes taken from
   the `## [X.Y.Z]` section of `CHANGELOG.md`. No section, no release
   (the job stops before anything is published).
2. **images**: `ghcr.io/winidi/yorik-ai` and `…/yorik-ai-whatsapp-bridge`
   for amd64 and arm64, tagged `X.Y.Z` and `stable`.
3. **bundles**, attached to the release:
   - `yorik-deploy.zip`: the `deploy/` folder, the install files.
   - `Yorik-Setup-Windows.zip`: `Yorik-Setup.cmd` and the PowerShell script.
   - `Yorik-Setup-Mac.zip`: `Yorik Setup.command` and its script.

Every push to main also builds `…:edge` (amd64 only,
`images-edge.yml`) for testing. Installs follow `stable` unless their
`.env` says otherwise.

How an install updates (`deploy/updater.sh`, started by "Update now"):

1. It downloads `yorik-deploy.zip` of the version in `.env` (`stable` =
   the latest release) and puts it over the install folder, except
   `.env` and `backups/`.
2. It adds settings that are new in `env.template` to `.env`, and gives
   new secrets random values.
3. It pulls the images, recreates what changed, then restarts itself.

## Version numbers

`0.MINOR.PATCH` while Yorik is alpha: MINOR for new features, PATCH for
fixes only. The next after `0.2.0` is `0.2.1` (fixes) or `0.3.0`.

Tags for families are plain: `v0.3.0`. A tag with `-rc1`, `-beta` or
`-alpha` becomes a **prerelease**:
- no `stable` images;
- GitHub doesn't count it as "latest", so installs never offer it and
  the Windows and Mac downloads keep pointing at the last real release.

Use one only to let a tester try it (`YORIK_VERSION=0.3.0-rc1` in their
`.env`).

## The three rules that keep updates safe

1. **Database migrations only add.** New tables and new nullable
   columns, yes; renaming, dropping or changing the type of a column,
   no. There's no way to undo a migration on a family's box.
   Downgrading (below) only works because the old code still runs on
   the newer database. If something really has to go, stop using it in
   one release and remove it several releases later.
2. **Settings in `deploy/env.template` are never renamed or removed.**
   The updater only adds.
   - A new setting gets a safe default.
   - A new secret goes in empty, named `…PASSWORD…`, `…SECRET…`,
     `…TOKEN…` or `…_KEY`, and gets a random value.
   - Nothing else may use those names; that's why `TS_AUTHKEY` stays
     empty.
3. **Everything an install needs is in `deploy/`**, with paths relative
   to that folder. `yorik-deploy.zip` is all a Windows or Mac install
   ever gets. Files dropped from `deploy/` stay on old installs, so they
   must be harmless there.

## Before tagging

Main must be green (CI). Then, on this machine:

```bash
venv/bin/python -m pytest -q                 # backend
bash e2e/run.sh                              # the app, in a browser
bash scripts/test-docker-stack.sh            # the whole stack from this checkout
bash scripts/test-updater.sh                 # the updater, on a stand-in install
```

If the change touches installing: `bash scripts/test-fresh-install.sh`
(a new Ubuntu VM), and for the stick `bash scripts/build-appliance.sh --test`.

Then `CHANGELOG.md`:

1. Rename `## [Unreleased]` to `## [X.Y.Z] — <date>`, and put a fresh
   empty `## [Unreleased]` above it.
2. The section starts with `### Highlights`: 3–6 bullets, one line each,
   in plain words for a family. **The app shows exactly these** under "A
   new version is ready". The detailed entries follow below.

Commit it (`release X.Y.Z`) on main.

## Tagging (only on the maintainer's word)

```bash
git tag vX.Y.Z && git push origin main vX.Y.Z
gh run list --limit 3                 # arm64 is emulated: allow up to an hour
```

Afterwards:

```bash
gh release view vX.Y.Z               # three zip files attached?
docker manifest inspect ghcr.io/winidi/yorik-ai:X.Y.Z | grep architecture   # amd64 + arm64
IMAGE_VERSION=X.Y.Z bash scripts/test-docker-stack.sh    # the published images work
```

The last check matters because the images are built on GitHub, not
here. If an install of the previous version is at hand, press "Update
now" there too.

## When a release is broken

**Fix forward if you can:** a `vX.Y.(Z+1)` with the fix. Installs that
already updated are offered it; those that didn't skip the broken one.

**Pull it back** when a fix takes longer:

```bash
# once: a token that may write packages
gh auth refresh -s write:packages && gh auth token | docker login ghcr.io -u winidi --password-stdin
PREV=X.Y.W   # the last good version
docker buildx imagetools create -t ghcr.io/winidi/yorik-ai:stable ghcr.io/winidi/yorik-ai:$PREV
docker buildx imagetools create -t ghcr.io/winidi/yorik-ai-whatsapp-bridge:stable ghcr.io/winidi/yorik-ai-whatsapp-bridge:$PREV
gh release edit vX.Y.Z --prerelease   # "latest" is the good one again
```

- New installs and "Update now" now get the good version again.
- Installs already on the broken one see "A new version is ready" and go
  back with one click. That works because of rule 1.
- For the same reason the updater never deletes files, so an install
  folder can always go back.

**One household** goes back by itself: `YORIK_VERSION=X.Y.W` in `.env`,
then "Update now" or `docker compose up -d` in the install folder.

## What's not covered

- **Classic installs** (`install.sh --classic`, the maintainer's own
  box) don't use releases. They follow main with `./scripts/yorik
  upgrade`, or the Updates button through `yorik-update.service`.
- **Local edits to install files** (`compose.yaml` etc.) are overwritten
  by the next update. Settings belong in `.env`.
- **Linux Docker installs from a git checkout:** `deploy/` is inside the
  checkout, so after an update git shows those files as modified. That's
  expected; those installs update through the app, not `git pull`.
- **Edge installs** (`YORIK_VERSION=edge`) only get new images; the
  install files stay as they were.
