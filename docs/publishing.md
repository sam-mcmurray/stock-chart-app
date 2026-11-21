# Publishing this project without private data

The README and its screenshots are designed for public sharing. A public README does not certify the rest of a repository or its history. Review the exact material being pushed.

## Use a separate public review copy

Your working environment can remain exactly where it is. The public-export helper reads eligible source files and creates a new directory; it never replaces an existing destination, commits, pushes, or copies the original `.git` history.

```bash
python3 scripts/publication_guard.py --export /tmp/stockapp-public-review
```

It excludes progress/goals, planning and personal notes, local validation checklists, dated operational reports, private configuration, runtime data, credentials, certificates, dependencies/build output, and the original `docker-compose.yml`. It includes the portable `compose.starter.yaml` instead. Public README/testing/frontend guides come from the dedicated `docs/public/` templates. Other historical documentation and authenticated portal snapshots remain local; only explicitly selected public documentation is copied. A date-stamped operational news regression report is excluded as well.

The export takes a byte snapshot before scanning, so concurrent development cannot substitute unchecked content during copying. `PUBLICATION_MANIFEST.json` records SHA-256 hashes of all exported source/documentation files. It also makes small public-copy fixes without changing the working app: renames an ordinary sentiment event handler so ESLint does not mistake it for a hook, adds configurable frontend CSRF-cookie lookup so the starter does not share cookies with the original app, and adds a test package marker to avoid collision with Python's standard-library `test` package. Symlinks are not copied. Eligible source/doc files still require human review for private information that cannot be inferred from their filenames.

A local pre-push hook in the working repository runs `scripts/publication_guard.py --history`. It blocks known private paths or detected credential patterns in tracked content and reachable history. It deliberately blocks a repository containing private parent `config.yaml` history. This is not fixed by `.gitignore`; use the separate reviewed export instead of rewriting your working history just to publish.

The hook is local to this checkout and is not automatically installed in clones. It can be bypassed with Git's `--no-verify`, so do not rely on it as the only boundary. The export does not include `.git` or install a remote. Nothing is sent to GitHub by either script.

## Choose the repository boundary

Run `git rev-parse --show-toplevel` locally. If Git's root is a containing directory, a push can include sibling files and commits outside `stock-chart-app/`. Review that root, not only the application directory. Do not publish the containing personal workspace.

For a clean public repository, copy only reviewed source/configuration templates/documentation into a new directory, excluding the existing `.git`, real `.env`, `certs`, and runtime `data`. Initialize Git there only after review. This avoids carrying old private history into the public copy; it does not remove secrets embedded in source files.

For an existing repository, inspect current files and every commit reachable from branches and tags you plan to push. If a real credential was committed, remove it from the intended publication history and rotate/revoke it with its provider. Deleting the current file or adding an ignore rule is insufficient. Coordinate any history rewrite with collaborators.

## Never publish

- Backend/root/frontend environment files, except reviewed `.env.example` templates.
- Schwab app secrets, authorization codes, refresh/access tokens, account numbers or account hashes.
- Session cookies, browser storage, HAR recordings, callback URLs with query strings, or raw account responses.
- TLS private keys, SSH/model-service identity keys, encryption keys, or credentials in connection URLs.
- PostgreSQL dumps, Redis persistence, Neo4j databases, Qdrant snapshots, model directories, or training exports.
- Real balances, positions, orders, transaction history, personal calendar reviews, and unreviewed logs/screenshots.
- Private hostnames, personal filesystem paths, private email addresses, or account-specific operational reports.

Local model servers can generate private identity keys inside their data directories. Those folders are excluded alongside databases and caches; they are not safe just because they are model-related.

## Review before staging

From the repository root, inspect these commands locally:

```bash
git status --short
git ls-files
git ls-files -ci --exclude-standard
git diff --stat
```

`git ls-files -ci --exclude-standard` finds tracked files that now match ignore rules. They remain tracked until explicitly removed from the index. Do not remove local credentials or databases just to prepare a public repository; keep them private and exclude them from publication.

The application `.gitignore` excludes runtime data while permitting hand-written `data/**/README.md` files. Review those documents too. The rules do not cover sibling files outside the application directory.

Use a maintained secret scanner on both the candidate working tree and Git history, with redacted output. Supplement it with a private comparison against your known credentials. Automated detectors can miss account identifiers, personal notes, novel token formats, images, and private metadata.

## Review the staged result

Stage only files you have reviewed. Do not use a blanket `git add .` on the working installation. Then inspect:

```bash
git diff --cached --name-status
git diff --cached --check
git diff --cached
```

Do not paste unredacted diff output into an issue or chat. Verify all README links and screenshot files are included. Open every screenshot and confirm its synthetic-data label, fictional values, and lack of personal browser/account information.

Review commit author name/email and remote URLs before publishing. If desired, configure an appropriate public identity or GitHub-provided no-reply address in the public repository before committing. Changing local identity does not rewrite older commits.

## Publication checklist

- [ ] Repository root contains only the intended project.
- [ ] Source files and public templates have been inspected for hardcoded credentials and private data.
- [ ] No real environment files, keys, database/model state, or unreviewed reports are staged.
- [ ] Secret scanning covers both the staged content and history to be pushed.
- [ ] Screenshots are synthetic and visually reviewed.
- [ ] Personal paths, account identifiers, notes, and commit metadata have been reviewed.
- [ ] Provider data/model redistribution terms have been checked for any included assets.
- [ ] A deliberate project license has been selected, if one is to be granted.
- [ ] README local links resolve in the published directory structure.
- [ ] The exact staged diff has been reviewed before commit and push.

Push only after these checks. Uploading a directory ZIP can bypass Git ignore rules and accidentally include private runtime files.
