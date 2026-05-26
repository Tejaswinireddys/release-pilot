# Pushing Release Pilot to GitHub

Follow these steps exactly. Replace `<USERNAME>` with your GitHub username and
`<REPO>` with the name you want for the repository.

---

## Step 1 — Create an empty repository on GitHub

1. Go to https://github.com/new
2. Set the repository name to `<REPO>` (e.g., `release-pilot`)
3. Choose Public or Private
4. **Do NOT** check "Add a README file"
5. **Do NOT** check "Add .gitignore"
6. **Do NOT** choose a license
   (This repo ships its own README, .gitignore, and LICENSE — GitHub's defaults would create a merge conflict.)
7. Click **Create repository**

---

## Step 2 — Initialize and push from your machine

Run these commands from the root of the `release-pilot` directory:

```bash
git init
git add .
git status
```

> Carefully read the `git status` output. Confirm that `.env` is **NOT** listed
> under "Changes to be committed". It must appear under "Untracked files" or not
> appear at all (if you have not created it yet). If `.env` appears staged, run
> `git rm --cached .env` before continuing.

```bash
git commit -m "Initial commit: Release Pilot multi-agent deployment system"
git branch -M main
git remote add origin https://github.com/<USERNAME>/<REPO>.git
git push -u origin main
```

---

## Step 3 — Verify the push succeeded

```bash
git log --oneline -5
git remote -v
```

Then open your browser and navigate to:

```
https://github.com/<USERNAME>/<REPO>
```

You should see the README rendered on the repository landing page.

---

## Important: .env must never be committed

`.env` is listed in `.gitignore`. As a double-check, run:

```bash
git ls-files | grep '\.env$'
```

This command should return **no output**. If it prints `.env`, your credentials
are tracked. Remove them immediately:

```bash
git rm --cached .env
git commit -m "Remove accidentally tracked .env"
git push
```

Rotate any credentials that were exposed before removing them.

---

## Optional: Add GitHub topics and a description

After the push, on the GitHub repository page:

- Click the gear icon next to "About"
- Add a description: `5-agent AI system for safe, compliant software deployments`
- Add topics: `ai-agents`, `mcp`, `opentelemetry`, `pci-dss`, `canary-deployment`, `opa`, `python`

---

## CI is already configured

The repository ships `.github/workflows/ci.yml`, which runs the full test suite on every
push and pull request. It installs OPA automatically so all 71 tests (including OPA policy
tests) run in CI without any additional setup.

To see CI status after your push, visit:

```
https://github.com/<USERNAME>/<REPO>/actions
```

No changes to the workflow are needed — the test suite runs clean with the committed
configuration.
