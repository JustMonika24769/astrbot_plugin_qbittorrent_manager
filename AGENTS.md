# Repository Guidelines

## Project Structure & Module Organization

- `main.py` contains the AstrBot plugin implementation, including command handlers, PT search parsing, and qBittorrent/uTorrent integration.
- `_conf_schema.json` defines AstrBot plugin configuration fields shown in the dashboard.
- `templates/torrent_results.html` is the HTML card template rendered by AstrBot t2i.
- `assets/` stores README and UI preview images, such as `assets/preview.png`.
- `metadata.yaml`, `requirements.txt`, `README.md`, and `LICENSE` provide plugin metadata, dependencies, usage docs, and licensing.
- `package.ps1` is a local packaging helper and is intentionally ignored by Git.

## Build, Test, and Development Commands

- `python3.13 -m compileall .` checks Python syntax across the plugin.
- `python3.13 -m ruff check .` runs lint checks.
- `ruff format .` formats Python code.
- `.\package.ps1` creates `dist/astrbot_plugin_qbittorrent_manager.zip` using 7-Zip and `.gitignore` filtering.

Run formatting and linting before submitting changes. If `python3.13` is unavailable, use the project’s AstrBot-compatible Python interpreter.

## Coding Style & Naming Conventions

- Follow Ruff formatting for Python; use 4-space indentation.
- Keep command handlers small and delegate logic to private helper methods.
- Use descriptive snake_case names for functions, variables, and config keys.
- Keep user-facing error messages concise and in Chinese.
- Avoid broad behavior changes in parsing logic without a focused regression sample.

## Testing Guidelines

This repository does not currently include a formal test suite. Validate changes with:

- Syntax check: `python3.13 -m compileall .`
- Lint check: `python3.13 -m ruff check .`
- Manual parser checks using representative NexusPHP HTML snippets when editing search parsing.

For new tests, prefer small focused samples around parsing, URL handling, and client API request construction.

## Commit & Pull Request Guidelines

Recent history uses short messages, often Conventional Commit style, for example:

- `feat: 支持 uTorrent 下载客户端`
- `feat: 支持直接添加磁链和种子链接`
- `update readme`

Prefer `feat:`, `fix:`, `docs:`, `refactor:`, or `chore:` prefixes. Pull requests should include a short summary, changed commands/configs, validation results, and screenshots when README images or card templates change.

## Security & Configuration Tips

- Never commit real PT cookies, `access_token`, qBittorrent/uTorrent passwords, or generated config files.
- Treat `.torrent` links and magnet URIs as untrusted input; keep size limits and validation paths intact.
- Do not commit generated archives from `dist/` or root zip files.
