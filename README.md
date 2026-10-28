# Download Organizer

Windows 10/11용 다운로드 폴더 **읽기 전용 분석기**입니다. 파일을 날짜별로 정리했을 때의 경로를 미리 비교합니다. 파일 이동·삭제·폴더 생성 기능은 없습니다.

## 실행 방법

Python 3.12와 Tcl/Tk가 포함된 Windows Python 배포판을 사용합니다. PC 로컬 개발환경 없이 GitHub Actions에서 테스트 및 EXE 빌드를 수행할 수 있습니다.

```powershell
python -m pip install -e ".[dev]"
python -m download_organizer
```

Actions의 `Windows tests and EXE` 실행이 성공하면 `DownloadOrganizer-Windows` 아티팩트를 내려받아 압축을 풀고 `DownloadOrganizer.exe`를 실행하세요. GitHub 로그인과 아티팩트 접근 권한이 필요합니다. EXE는 아직 코드 서명되지 않아 Windows에서 경고할 수 있습니다.

1. Windows Known Folder API로 다운로드 폴더를 기본 선택합니다. 조회 실패 시 사용자 홈의 `Downloads`를 사용합니다.
2. 폴더 선택 버튼 또는 입력란에서 분석할 폴더를 지정합니다.
3. 날짜 기준을 수정일(기본) 또는 생성일로 선택하고 **읽기 전용 분석**을 누릅니다.
4. 파일명·용량·생성일·수정일·원래 경로·예상 경로·충돌 경고를 확인합니다. 가로 스크롤로 긴 경로를 확인할 수 있습니다.
5. 취소하면 이미 분석한 부분 결과를 표시합니다. 오류 및 제외 이유는 창 하단에 표시합니다.

예: `Downloads/문서.pdf`의 수정일이 2026년 10월 28일이면 예상 경로는 `Downloads/26.10.28/문서.pdf`입니다. 날짜는 실행 PC의 로컬 시간 기준입니다. GUI의 전체 파일 개수 및 용량은 **제외·오류 항목을 뺀 분석 성공 파일**의 합계이며, 진행률은 선택 폴더의 전체 직계 항목 처리 비율입니다.

## 안전 정책 및 범위

- 선택 폴더의 직계 일반 파일만 분석하며 하위 폴더는 순회하지 않습니다.
- 심볼릭 링크, Windows junction/reparse point, 특수 파일을 제외합니다. 선택 경로의 상위 구성요소에 링크가 있어도 거부합니다.
- `.crdownload`, `.part`, `.partial`, `.download`, `.tmp`, `.temp` 확장자(대소문자 무관)와 `~$` 임시 파일은 제외합니다.
- `YY.MM.DD` 형태 폴더 및 그 하위 경로는 재분석 위험 때문에 거부합니다. 날짜 모양의 일반 폴더도 보수적으로 거부합니다.
- 기존 대상 파일이나 안전하지 않은 대상 폴더는 경고만 표시합니다. 덮어쓰기나 대상 폴더 생성은 하지 않습니다.
- 한글·공백·특수문자는 `pathlib`와 Unicode 문자열로 처리합니다.
- 확장자만으로 다운로드 상태를 완전히 판별할 수 없습니다. 일반 확장자로 기록 중인 파일과 분석 중 변경된 파일은 정확한 상태를 보장하지 않습니다. 결과는 해당 시점의 예상 정보입니다.
- Windows 생성일을 수집합니다. 생성일을 제공하지 않는 Unix 파일시스템에서는 생성일을 ‘제공되지 않음’으로 표시하고, 생성일 기준 분석은 항목별 오류로 기록합니다. Unix ctime을 생성일로 잘못 사용하지 않습니다.
- 백그라운드 스레드는 파일 메타데이터를 읽고 큐로 전달합니다. Tk 갱신은 메인 스레드에서만 수행하며 결과 행은 나누어 표시합니다.

## 프로젝트 구조

```text
src/download_organizer/
  analyzer.py     # 메타데이터 수집, 제외 정책, 날짜별 예상 경로
  paths.py        # Windows 기본 다운로드 경로 조회
  gui.py          # Tkinter/ttk GUI, 스레드·큐·진행률·취소
  __main__.py     # python -m 실행 진입점
  launcher.py     # PyInstaller 실행 진입점
  __init__.py     # 패키지 선언
tests/
  test_analyzer.py # 임시 디렉터리 기반 읽기 전용 분석 테스트
  test_gui.py      # GUI·백그라운드 결과 전달·오류 후 재분석 통합 테스트
.github/workflows/windows.yml # Python 3.12 테스트·EXE 빌드·아티팩트 배포
pyproject.toml    # 패키징 및 개발 의존성
```

## 테스트 및 빌드

```powershell
python -m pytest -v
python -m PyInstaller --noconfirm --clean --onefile --windowed --name DownloadOrganizer --paths src src/download_organizer/launcher.py
```

Windows EXE는 Windows 러너에서 빌드합니다. Linux에서 만든 실행 파일은 Windows EXE가 아닙니다. CI는 push, PR, 수동 실행을 지원하며 Windows에서 테스트 후 EXE를 아티팩트로 제공합니다. GitHub Release 자동 게시와 코드 서명은 아직 구현하지 않았습니다.

테스트는 pytest의 임시 디렉터리만 사용합니다. 예상 경로·한글 파일명·메타데이터·읽기 전용 유지·임시 파일 제외·폴더 및 링크 제외·재분석 차단·충돌·취소·개별 파일 오류 후 계속 분석·생성일 정책을 확인합니다. 링크 생성 권한이 없는 환경에서는 링크 테스트가 skip으로 표시됩니다.

## 다음 단계

Windows 10/11에서 GUI 및 EXE 수동 검증, 대규모 폴더에서 메모리와 표시 성능 개선, 분석 결과 내보내기를 권장합니다. 실제 이동은 별도 개발 단계로 진행하며 명시적 사용자 확인, 충돌 정책, 변경 재검증, 작업 로그 및 복구 기능을 먼저 설계해야 합니다.
