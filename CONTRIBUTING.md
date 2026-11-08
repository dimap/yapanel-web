# Как работать с репозиторием

Базовое правило: **в `main` не пушим напрямую**. Любое изменение — через короткоживущую ветку и pull request. Это нужно не для бюрократии, а чтобы CI успевал поймать поломку до того, как она попадёт в `main` и в релиз.

## 1. Ветка под задачу

```bash
git switch main
git pull fork main
git switch -c feat/korotkoe-opisanie
```

Имена веток:

- `feat/...` — новая функциональность;
- `fix/...` — исправление;
- `docs/...` — документация;
- `ci/...` — workflows и инфраструктура;
- `release/vX.Y.Z` — подготовка релиза.

Одна ветка — одна задача. Не складывайте в неё несколько независимых изменений.

## 2. Коммиты

Стиль — Conventional Commits:

```text
feat(dsp): humanize preset labels
fix(assistant): parse onAliceStateChanged events
docs: rewrite README
ci: retry setup-python on transient failures
```

В теле коммита можно объяснить «почему», если из диффа это неочевидно.

## 3. Pull request

```bash
git push -u fork feat/korotkoe-opisanie
```

Откройте PR в `main` по ссылке, которую предложит GitHub:

```text
https://github.com/Timobzdecc/yapanel-web_max/compare/main...feat/korotkoe-opisanie?expand=1
```

В PR заполните шаблон: что изменено, зачем, как проверено. Если меняли UI — приложите скриншот. Если проверяли на живой станции — напишите, что именно проверяли.

PR должен быть небольшим. Большую задачу лучше разбить на несколько PR.

## 4. CI

В PR должны быть зелёными проверки:

- `tests / python (3.9)`, `python (3.11)`, `python (3.14)`;
- `tests / agent`, `tests / assistant`, `tests / viz`;
- сборка Docker, если менялись зависимости или Dockerfile.

Локально перед PR:

```bash
python -m pytest tests -q
python -m py_compile server.py config.py max_profile.py profiles.py assistant_max.py dsp_max.py
```

Если CI упал из-за инфраструктурного сбоя (например, `HTTP 504` при скачивании Python), перезапустите job. Не «чините» код, если код не виноват.

## 5. Merge

После зелёного CI и проверки:

- **Squash and merge** — для обычных PR;
- удалите ветку после merge;
- обновите локальный `main`:

```bash
git switch main
git pull fork main
git branch -d feat/korotkoe-opisanie
```

Прямой push в `main`, force-push и перемещение тегов запрещены. Ошибку исправляем новым коммитом или patch-релизом, а не переписыванием истории.

## 6. Релиз

Релиз — отдельный PR из ветки `release/vX.Y.Z`:

1. Поднять `VERSION`.
2. Обновить ссылки на ассеты в `README.md` и `README_WINDOWS.txt`.
3. Добавить `docs/release-notes/vX.Y.Z.md` — это описание будущего релиза.
4. Пересобрать установщик:

```bash
python tools/build_installer.py
```

5. Проверить `install_to_max.ps1` и `yapanel_max.tar.gz`.
6. Смержить релизный PR в `main`.
7. Поставить тег **после merge**, уже на `main`:

```bash
git switch main
git pull fork main
git tag -a vX.Y.Z -m "yapanel X.Y.Z"
git push fork vX.Y.Z
```

Тег запускает `release` workflow. Он собирает ZIP и публикует релиз, беря описание из `docs/release-notes/vX.Y.Z.md`. Если файла нет — использует запасной текст.

Не перемещайте существующие теги и не перезаписывайте ассеты релиза. Ошибку исправляем новым patch-релизом.

## 7. Секреты и данные

В репозиторий нельзя коммитить:

- API-ключи и токены;
- `.panel_settings.json`, `.yandex_auth.json`, `.panel_auth.json`, `.metrics.json`;
- IP-адреса, MAC-адреса и другие данные конкретной станции;
- логи с приватными данными.

Ключи ассистента и TTS хранятся только на колонке в `/data/quasar/webpanel/assistant/config.json` с правами `600`.

## 8. Защита `main`

В настройках GitHub: **Settings → Branches → Add branch protection rule** для `main`:

- Require a pull request before merging;
- Require status checks to pass before merging (все джобы `tests`);
- Require branches to be up to date before merging;
- Do not allow force pushes;
- Do not allow deletions.

Для соло-разработки достаточно 0 approvals: PR всё равно нужен, чтобы прогнать CI и сохранить точку отката.

Дополнительные правила по железу и тестированию — в [CONTRIBUTION.md](CONTRIBUTION.md).
