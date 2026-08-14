# Refactor Forge

[English](README.md) | **한국어**

반복 가능한 저장소 마이그레이션을 안전하게 계획하고, 검증하고, 적용하며, 지속적으로 모니터링하기 위한 확장 가능한 코드 변환 실행기입니다.

> **상태:** 초기 MVP입니다. 로컬 CLI, 격리된 계획 실행, 검증, 저장소 감시 기능은 실제로 동작합니다. MCP, Agent Skill, GitHub Pull Request, Claude Code/Codex 전용 어댑터는 아직 구현되지 않은 향후 계획입니다.

## 왜 Refactor Forge인가요?

대규모 마이그레이션에서 LLM이 모든 파일을 제각각 다시 작성하게 해서는 안 됩니다. Refactor Forge는 역할을 다음과 같이 분리합니다.

- 결정론적 엔진이 반복 가능한 변경을 수행합니다.
- 격리된 계획기가 원본 저장소를 건드리지 않고 diff를 생성합니다.
- 빌드 및 테스트 명령으로 결과를 검증합니다.
- 정책 게이트가 패치를 리포트만 할지 실제 적용할지 통제합니다.
- 향후 LLM CLI는 공통 MCP 인터페이스를 통해 동일한 엔진을 사용할 수 있습니다.

장기적으로 지향하는 아키텍처는 다음과 같습니다.

```text
Claude Code / Codex CLI / 기타 에이전트
                 |
          Agent Skill + MCP
                 |
          Refactor Forge Core
          /       |        \
 OpenRewrite   ast-grep   커스텀 엔진
                 |
        빌드 / 테스트 / 리포트 / PR
```

Refactor Forge는 LLM 없이도 사용할 수 있습니다. 현재 MVP는 특정 모델에 종속되지 않으며 Python 런타임 의존성이 없습니다.

## 현재 기능

- JSON 기반 변환 명세
- 결정론적 정규식 변환
- OpenRewrite, ast-grep, codemod 또는 사내 도구를 위한 command adapter
- Git 저장소의 커밋된 `HEAD`를 독립적인 임시 Git clone에서 실행하는 격리형 `plan` (비-Git, unborn 또는 디렉터리가 아직 `HEAD`에 없는 대상은 격리 복사본으로 대체)
- unified diff 생성
- Git 저장소에서만 동작하는 안전한 `apply`
- 기본적으로 깨끗한 작업 트리 요구
- 변환 후 빌드 및 테스트 명령 실행
- 저장소 변경 지속 모니터링
- 원본을 변경하지 않는 검증된 패치 리포트
- 명시적으로 허용해야만 동작하는 자동 적용
- Python `Transformation` 확장 인터페이스

## 안전 모델

- `plan`은 대상 저장소를 변경하지 않습니다. 커밋된 `HEAD`가 있는 Git 대상에서는 object database, refs, local config가 독립되고 remote가 제거된 임시 clone을 만들며, 모든 종료 경로에서 clone을 삭제합니다. 원본 worktree 등록을 만들지 않습니다.
- 기본 Git `plan`은 커밋된 tracked `HEAD` 스냅샷을 평가합니다. dirty, staged, untracked, ignored 및 submodule 내용은 제외되며 리포트에 명시됩니다. 대상이 `HEAD`에 아직 없으면 현재 대상을 격리 복사본으로 처리하고 Git 컨텍스트가 없음을 리포트합니다. 비-Git 및 unborn 저장소도 같은 격리 복사본을 사용합니다.
- Git 스냅샷은 checkout 대신 raw blob에서 materialize하므로 저장소 checkout hook과 smudge filter를 실행하지 않습니다. 변환과 스냅샷은 파일/디렉터리 symlink를 따라가지 않아 sandbox 밖 쓰기를 차단합니다.
- 외부 command step은 `--allow-command`가 있어야 실행되며, command와 검증은 임시 clone의 Git 컨텍스트와 정리된 Git 환경에서 실행됩니다. 따라서 그 컨텍스트에서 원본 refs, config, remote를 변경할 수 없습니다.
- 명령은 인자 배열로 실행되며 셸을 거치지 않습니다.
- Watch는 현재 working tree를 fingerprint하고 동일한 working-tree 스냅샷을 계획합니다. dirty, untracked, ignored 및 내용이 존재하는 submodule도 (기본 제외 디렉터리 규칙(Watch report 포함)과 symlink 안전 규칙 안에서) 평가합니다. `--auto-apply`를 명시하지 않으면 리포트만 생성합니다.
- `apply`는 저장소 루트와 nested target 모두 허용하고 포함 저장소를 검사하며, 기본적으로 dirty working tree를 거부합니다. 추적 중인 변경이 있으면 자동 적용을 차단합니다.
- 검증 실패는 실행 실패로 처리하며, 정리만 실패한 경우 완료된 리포트에 경고를 붙이고 sandbox 누수는 치명적 오류로 처리합니다.
- 현재 코어는 커밋, 브랜치 push 또는 Pull Request 생성을 수행하지 않습니다.

## 요구 사항

- Python 3.9 이상
- Git 대상 `plan`, `apply` 및 Watch 자동 적용을 위한 Git
- 변환 명세에서 사용하는 빌드 도구

OpenRewrite와 ast-grep은 선택적 외부 도구이며 Refactor Forge에 포함되어 있지 않습니다.

## 설치

### 권장: 격리된 CLI 도구로 설치

[uv](https://docs.astral.sh/uv/)를 사용해 GitHub의 최신 버전을 직접 설치합니다.

```bash
uv tool install "refactor-forge @ git+https://github.com/namekun/refactor-forge.git"
```

`uv tool`을 처음 사용한다면 실행 파일 디렉터리를 `PATH`에 추가합니다.

```bash
uv tool update-shell
```

나중에 업그레이드하려면 다음 명령을 사용합니다.

```bash
uv tool upgrade refactor-forge
```

PyPI 릴리스 이후에는 다음 명령이 권장 설치 방식이 됩니다.

```bash
uv tool install refactor-forge
```

### 개발용 설치

Refactor Forge 자체 개발에 참여할 때만 editable 환경을 사용하세요.

```bash
git clone https://github.com/namekun/refactor-forge.git
cd refactor-forge
uv venv
. .venv/bin/activate
uv pip install -e .
```

표준 `venv` 및 `pip` 방식도 사용할 수 있지만, 일반 사용자를 위한 기본 설치 경로는 아닙니다.

## 빠른 시작

저장소를 clone하지 않고 CLI만 설치했다면 예제 변환 명세를 내려받습니다.

```bash
curl -fsSLO https://raw.githubusercontent.com/namekun/refactor-forge/main/examples/javax-to-jakarta.json
```

커밋된 `HEAD`의 임시 Git clone(비-Git, unborn 및 `HEAD`에 없는 대상은 격리 복사본)에서 변환 결과를 미리 확인합니다.

```bash
refactor-forge plan \
  --spec javax-to-jakarta.json \
  --target /path/to/repository
```

깨끗한 Git 작업 트리에 실제로 적용합니다.

```bash
refactor-forge apply \
  --spec javax-to-jakarta.json \
  --target /path/to/repository
```

성공한 계획은 다음과 같은 unified diff를 출력합니다.

```diff
-import javax.annotation.PostConstruct;
+import jakarta.annotation.PostConstruct;
```

포함된 예제는 실행 모델을 보여주기 위한 작은 결정론적 치환입니다. 타입 정보를 활용하는 Java 마이그레이션에는 command adapter를 통해 OpenRewrite recipe를 사용하는 것이 적절합니다.

## 변환 명세

```json
{
  "schema_version": 1,
  "name": "javax-annotation-to-jakarta",
  "description": "Migrate javax.annotation imports to jakarta.annotation",
  "steps": [
    {
      "type": "regex",
      "name": "replace-annotation-imports",
      "includes": ["**/*.java"],
      "pattern": "\\bjavax\\.annotation\\.",
      "replacement": "jakarta.annotation."
    }
  ],
  "verify": [["./mvnw", "test"]]
}
```

### 외부 변환 엔진

Command step으로 OpenRewrite, ast-grep 또는 다른 결정론적 엔진을 실행할 수 있습니다.

```json
{
  "schema_version": 1,
  "name": "openrewrite-external-recipe",
  "steps": [
    {
      "type": "command",
      "name": "run-openrewrite",
      "command": [
        "./mvnw",
        "rewrite:run",
        "-Drewrite.activeRecipes=com.example.MyRecipe"
      ]
    }
  ],
  "verify": [["./mvnw", "test"]]
}
```

외부 명령은 명시적으로 허용해야 실행됩니다.

```bash
refactor-forge plan \
  --spec examples/openrewrite-command.json \
  --target /path/to/repository \
  --allow-command
```

커밋된 Git 대상에서는 command step과 검증 명령이 독립된 임시 clone을 현재 디렉터리로 사용하므로 Git-aware 도구도 원본과 분리된 refs, config, object database 컨텍스트에서 동작하며 source remote는 제거되어 있습니다. 기본 `plan`은 커밋된 `HEAD`만 사용하며 원본 상태를 변경하지 않습니다. Watch는 이에 대응하는 현재 working-tree 스냅샷을 명시적으로 사용합니다.

## 지속 모니터링

원본을 변경하지 않고 리포트만 생성하는 Watch를 시작합니다.

```bash
refactor-forge watch \
  --spec examples/javax-to-jakarta.json \
  --target /path/to/repository \
  --interval 30
```

CI, cron, `launchd` 또는 `systemd`에서 한 번만 검사할 수도 있습니다.

```bash
refactor-forge watch \
  --spec examples/javax-to-jakarta.json \
  --target /path/to/repository \
  --once
```

상태와 리포트는 기본적으로 `.refactor-forge/` 아래에 저장됩니다. Watch 이벤트는 다음과 같습니다.

- `baseline` — 최초 저장소 상태를 기록했습니다.
- `unchanged` — 저장소 변경이 감지되지 않았습니다.
- `no_patch` — 저장소는 변경됐지만 해당 변환이 필요하지 않습니다.
- `patch_available` — 검증을 통과한 패치 리포트를 생성했습니다.
- `blocked_dirty` — 추적 중인 변경 때문에 자동 적용을 차단했습니다.
- `applied` — 패치를 적용하고 검증했습니다.

자동 적용은 명시적으로 활성화해야 합니다.

```bash
refactor-forge watch \
  --spec examples/javax-to-jakarta.json \
  --target /path/to/repository \
  --auto-apply
```

## 테스트

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

테스트 스위트는 격리 clone 계획과 정리, symlink containment, 원본 refs/config/remote 보존, hook/filter 안전성, excluded 이름을 포함한 nested target, untracked/ignored/submodule 처리, Git 컨텍스트 검증, 비-Git 및 unborn fallback, nested/dirty tree 적용, working-tree drift 감시, dirty tree 차단 및 명시적 자동 적용을 검증합니다.

## 로드맵

- OpenRewrite 전용 어댑터 및 recipe catalog
- Claude Code, Codex CLI 및 기타 MCP 클라이언트가 공유하는 MCP 서버
- 안전한 마이그레이션 절차를 설명하는 이식 가능한 Agent Skill
- 의존성 및 보안 advisory 모니터
- 위험도 기반 승인 정책
- 브랜치 및 Pull Request 생성
- 제한된 범위의 LLM 기반 실패 분석
- 여러 저장소를 아우르는 오케스트레이션

## 라이선스 및 서드파티 도구

Refactor Forge는 [MIT License](LICENSE)로 제공됩니다.

현재 Python 패키지는 서드파티 변환 엔진을 번들하지 않으며 Python 런타임 의존성도 없습니다. 선택적 외부 도구에는 각각의 라이선스가 적용됩니다.

- [OpenRewrite](https://github.com/openrewrite/rewrite) — Apache License 2.0
- [ast-grep](https://github.com/ast-grep/ast-grep) — MIT License

Claude Code, Codex CLI, Maven 및 Gradle은 선택적 외부 프로그램이며 이 저장소에 포함되어 배포되지 않습니다. 사용자는 각 프로그램의 약관과 라이선스를 준수할 책임이 있습니다.

## 기여

이슈와 범위가 명확한 Pull Request를 환영합니다. 동작 변경에는 테스트를 포함하고, 가능한 한 변환을 결정론적으로 유지해 주세요.
